"""run_turn_and_post + the per-thread coalescing runner.

run_turn_and_post posts a placeholder, then hands the turn to a background runner
that coalesces bursts on the same thread into one combined turn (so several
people answering at once cost one extra turn, not N, and nobody's input is
dropped). These tests drive that async path and assert on the observable posts.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time

from faultmaven.client import TurnResult
from slack_sdk.errors import SlackApiError

_load_seq = 0


def _load_turn():
    # Fresh module per test so the module-level coalescing runner starts empty.
    # Register in sys.modules (unique name) so @dataclass can resolve annotations.
    global _load_seq
    _load_seq += 1
    name = f"_turn_p{_load_seq}"
    spec = importlib.util.spec_from_file_location(name, "listeners/_turn.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _wait_until(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


class FakeClient:
    def __init__(self, *, fail_post: bool = False) -> None:
        self.fail_post = fail_post
        self.posts: list = []
        self.updates: list = []
        self._lock = threading.Lock()
        self._ph = 0

    def chat_postMessage(self, **kw):
        if self.fail_post:
            raise SlackApiError("cannot_post", {"error": "not_in_channel"})
        with self._lock:
            self._ph += 1
            ts = f"PH{self._ph}"
            self.posts.append(kw)
        return {"ts": ts}

    def chat_update(self, **kw):
        with self._lock:
            self.updates.append(kw)
        return {"ok": True}


class FakeFM:
    def __init__(self, *, block_first: threading.Event | None = None) -> None:
        self.turns: list = []
        self._lock = threading.Lock()
        self._block_first = block_first
        self._calls = 0

    def create_case(self, *, title=None, initial_message=None):
        return "case_1"

    def submit_turn(self, case_id, **kwargs):
        with self._lock:
            self._calls += 1
            first = self._calls == 1
            self.turns.append((case_id, kwargs))
        if first and self._block_first is not None:
            self._block_first.wait(3.0)  # hold turn 1 open so a burst can queue
        return TurnResult(agent_response="on it")


class FakeStore:
    def __init__(self) -> None:
        self.m: dict = {}
        self._lock = threading.Lock()

    def get(self, t, c, th):
        with self._lock:
            return self.m.get((t, c, th))

    def put(self, t, c, th, cid):
        with self._lock:
            self.m[(t, c, th)] = cid


_COMMON = dict(channel="C", thread_ts="TS", team_id="T")


def test_posts_a_placeholder_then_updates_it():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    _turn.run_turn_and_post(client, fm, FakeStore(), text="hi", **_COMMON)
    assert len(client.posts) == 1
    assert _wait_until(lambda: client.updates)
    assert client.updates[0]["ts"] == "PH1"


def test_reuses_an_existing_placeholder_and_posts_no_second_one():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    _turn.run_turn_and_post(
        client, fm, FakeStore(), text="hi", placeholder_ts="PH_PRE", **_COMMON
    )
    assert _wait_until(lambda: client.updates)
    assert client.posts == []
    assert client.updates[0]["ts"] == "PH_PRE"


def test_bails_without_running_when_it_cannot_post():
    _turn = _load_turn()
    client, fm = FakeClient(fail_post=True), FakeFM()
    _turn.run_turn_and_post(client, fm, FakeStore(), text="hi", **_COMMON)
    time.sleep(0.05)
    assert fm.turns == []
    assert client.updates == []


def test_forwards_files_to_submit_turn():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    files = [("app.log", b"boom", "text/plain")]
    _turn.run_turn_and_post(
        client, fm, FakeStore(), text="hi", placeholder_ts="PH", files=files, **_COMMON
    )
    assert _wait_until(lambda: fm.turns)
    _, kw = fm.turns[0]
    assert kw["files"] == files


def test_burst_on_one_thread_coalesces_into_one_extra_turn():
    _turn = _load_turn()
    release = threading.Event()
    client, fm, store = FakeClient(), FakeFM(block_first=release), FakeStore()

    # First reply starts turn 1 (blocks inside submit_turn until released).
    _turn.run_turn_and_post(client, fm, store, text="Alice: OOMKilled", **_COMMON)
    assert _wait_until(lambda: fm.turns)  # turn 1 is now in flight

    # Two more replies arrive during turn 1 — they must queue, not race.
    _turn.run_turn_and_post(client, fm, store, text="Bob: heap bumped", **_COMMON)
    _turn.run_turn_and_post(client, fm, store, text="Carol: p99 spiked", **_COMMON)
    time.sleep(0.05)
    assert len(fm.turns) == 1  # still just turn 1 — the other two are queued

    release.set()  # let turn 1 finish; the runner drains Bob+Carol as ONE turn
    assert _wait_until(lambda: len(fm.turns) == 2)
    time.sleep(0.05)
    assert len(fm.turns) == 2  # coalesced: Bob+Carol → one turn, not two

    combined_query = fm.turns[1][1]["query"]
    assert "Bob: heap bumped" in combined_query
    assert "Carol: p99 spiked" in combined_query


def test_folded_replies_get_a_note_not_silent_drop():
    _turn = _load_turn()
    release = threading.Event()
    client, fm, store = FakeClient(), FakeFM(block_first=release), FakeStore()

    _turn.run_turn_and_post(client, fm, store, text="first", **_COMMON)
    assert _wait_until(lambda: fm.turns)
    _turn.run_turn_and_post(client, fm, store, text="second", **_COMMON)
    _turn.run_turn_and_post(client, fm, store, text="third", **_COMMON)
    time.sleep(0.05)
    release.set()

    # 4 placeholders posted (one per reply)… wait, 3 replies → 3 placeholders.
    assert _wait_until(lambda: len(fm.turns) == 2)
    # The second batch (second+third) updates one placeholder with the result and
    # marks the other FOLDED_NOTE — nobody is silently dropped.
    assert _wait_until(
        lambda: any(u.get("text") == _turn.FOLDED_NOTE for u in client.updates)
    )
