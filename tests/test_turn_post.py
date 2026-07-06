"""run_turn_and_post: post placeholder → run one turn → update it, addressing the
replier (@mention) and carrying a one-time etiquette note on a thread's first
reply. Runs synchronously (the caller holds the drop-if-busy gate)."""

from __future__ import annotations

import importlib.util
import sys

from faultmaven.client import TurnResult
from slack_sdk.errors import SlackApiError

_seq = 0


def _load_turn():
    global _seq
    _seq += 1
    name = f"_turn_rp{_seq}"
    spec = importlib.util.spec_from_file_location(name, "listeners/_turn.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeClient:
    def __init__(self, *, fail_post: bool = False) -> None:
        self.fail_post = fail_post
        self.posts: list = []
        self.updates: list = []

    def chat_postMessage(self, **kw):
        if self.fail_post:
            raise SlackApiError("cannot_post", {"error": "not_in_channel"})
        self.posts.append(kw)
        return {"ts": "PH1"}

    def chat_update(self, **kw):
        self.updates.append(kw)
        return {"ok": True}


class FakeFM:
    def __init__(self) -> None:
        self.turns: list = []

    def create_case(self, *, title=None, initial_message=None):
        return "case_1"

    def submit_turn(self, case_id, **kwargs):
        self.turns.append((case_id, kwargs))
        return TurnResult(agent_response="on it")


class FakeStore:
    def __init__(self) -> None:
        self.m: dict = {}

    def get(self, t, c, th):
        return self.m.get((t, c, th))

    def put(self, t, c, th, cid):
        self.m[(t, c, th)] = cid


_COMMON = dict(channel="C", thread_ts="TS", team_id="T")


def _first_section_text(update) -> str:
    for b in update["blocks"]:
        if b.get("type") == "section":
            return b["text"]["text"]
    return ""


def _context_texts(update) -> list[str]:
    return [
        e.get("text", "")
        for b in update["blocks"]
        if b.get("type") == "context"
        for e in b.get("elements", [])
    ]


def test_posts_a_placeholder_then_updates_it():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    _turn.run_turn_and_post(client, fm, FakeStore(), text="hi", **_COMMON)
    assert len(client.posts) == 1
    assert client.updates[0]["ts"] == "PH1"


def test_reuses_an_existing_placeholder():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    _turn.run_turn_and_post(
        client, fm, FakeStore(), text="hi", placeholder_ts="PH_PRE", **_COMMON
    )
    assert client.posts == []
    assert client.updates[0]["ts"] == "PH_PRE"


def test_bails_without_running_when_it_cannot_post():
    _turn = _load_turn()
    client, fm = FakeClient(fail_post=True), FakeFM()
    _turn.run_turn_and_post(client, fm, FakeStore(), text="hi", **_COMMON)
    assert fm.turns == []
    assert client.updates == []


def test_addresses_the_replier_and_warns_on_first_turn():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    _turn.run_turn_and_post(
        client, fm, FakeStore(), text="hi", mention_user="U42", **_COMMON
    )
    update = client.updates[0]
    assert _first_section_text(update).startswith("<@U42> ")  # addressed
    # First reply carries the one-time "one at a time" etiquette note.
    assert _turn._INTRO_WARNING in _context_texts(update)


def test_no_warning_on_later_turns():
    _turn = _load_turn()
    client, fm, store = FakeClient(), FakeFM(), FakeStore()
    store.put("T", "C", "TS", "case_1")  # case already exists → not the first turn
    _turn.run_turn_and_post(
        client, fm, store, text="again", mention_user="U42", **_COMMON
    )
    assert _turn._INTRO_WARNING not in _context_texts(client.updates[0])


def test_forwards_files_to_submit_turn():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    files = [("app.log", b"boom", "text/plain")]
    _turn.run_turn_and_post(
        client, fm, FakeStore(), text="hi", files=files, **_COMMON
    )
    _, kw = fm.turns[0]
    assert kw["files"] == files
