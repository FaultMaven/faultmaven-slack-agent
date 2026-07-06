"""Drop-if-busy gate: only one turn runs per thread; a message arriving while a
thread is busy is skipped (marked ⏭️) rather than queued or raced."""

from __future__ import annotations

import importlib.util
import sys
import threading


def _load_turn():
    spec = importlib.util.spec_from_file_location("_turn_g", "listeners/_turn.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # let dataclass/annotations resolve
    spec.loader.exec_module(mod)
    return mod


class FakeClient:
    def __init__(self) -> None:
        self.reactions: list = []

    def reactions_add(self, **kw):
        self.reactions.append(kw)
        return {"ok": True}


_KW = dict(team_id="T", channel="C", thread_ts="TS")


def test_first_enters_second_is_skipped_until_released():
    _turn = _load_turn()
    client = FakeClient()
    # First reply reserves the thread.
    assert _turn.try_begin_turn(client, skip_ts="m1", **_KW) is True
    # Second reply while busy is skipped and reacted ⏭️.
    assert _turn.try_begin_turn(client, skip_ts="m2", **_KW) is False
    assert client.reactions == [
        {"channel": "C", "timestamp": "m2", "name": _turn.SKIPPED_REACTION}
    ]
    # After the first turn finishes, the thread is free again.
    _turn.end_turn(**_KW)
    assert _turn.try_begin_turn(client, skip_ts="m3", **_KW) is True


def test_different_threads_do_not_block_each_other():
    _turn = _load_turn()
    client = FakeClient()
    assert _turn.try_begin_turn(client, team_id="T", channel="C", thread_ts="A") is True
    assert _turn.try_begin_turn(client, team_id="T", channel="C", thread_ts="B") is True
    assert client.reactions == []  # neither skipped


def test_concurrent_first_messages_exactly_one_wins():
    _turn = _load_turn()
    client = FakeClient()
    results: list[bool] = []
    lock = threading.Lock()

    def attempt():
        r = _turn.try_begin_turn(client, skip_ts="x", **_KW)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1  # exactly one entered (no double-open race)
    assert results.count(False) == 7


def test_skip_without_ts_does_not_react():
    _turn = _load_turn()
    client = FakeClient()
    assert _turn.try_begin_turn(client, **_KW) is True
    assert _turn.try_begin_turn(client, skip_ts=None, **_KW) is False
    assert client.reactions == []  # no message to react to → no reaction
