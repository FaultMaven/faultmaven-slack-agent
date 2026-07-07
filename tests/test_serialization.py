"""Drop-if-busy gate: only one turn runs per thread; a message arriving while a
thread is busy is skipped (marked ⏭️) rather than queued or raced."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time

_seq = 0


def _load_turn():
    global _seq
    _seq += 1
    name = f"_turn_g{_seq}"
    spec = importlib.util.spec_from_file_location(name, "listeners/_turn.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # let dataclass/annotations resolve
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


def test_reaction_failure_is_swallowed_and_logged():
    _turn = _load_turn()

    class Boom:
        def reactions_add(self, **kw):
            raise RuntimeError("transport reset")  # not even a SlackApiError

    client = Boom()
    assert _turn.try_begin_turn(client, **_KW) is True
    # A non-SlackApiError on the drop path must not propagate.
    assert _turn.try_begin_turn(client, skip_ts="m2", **_KW) is False


# -- run_gated: offload to a background worker, release the gate when done -----
def test_run_gated_runs_work_in_background_and_releases():
    _turn = _load_turn()
    client = FakeClient()
    done = threading.Event()

    assert _turn.run_gated(
        client, skip_ts="m1", work=lambda: done.set(), **_KW
    ) is True
    assert _wait_until(done.is_set)  # work ran on the daemon
    # Gate released after work → the thread can be entered again.
    assert _wait_until(lambda: _turn.try_begin_turn(client, **_KW) is True)


def test_run_gated_drops_when_busy():
    _turn = _load_turn()
    client = FakeClient()
    release = threading.Event()
    started = threading.Event()

    def slow():
        started.set()
        release.wait(3.0)

    assert _turn.run_gated(client, skip_ts="m1", work=slow, **_KW) is True
    assert _wait_until(started.is_set)  # first turn is in flight
    # A second dispatch while busy is dropped (⏭️) and does NOT run.
    ran_second = threading.Event()
    assert _turn.run_gated(
        client, skip_ts="m2", work=lambda: ran_second.set(), **_KW
    ) is False
    assert client.reactions[-1]["timestamp"] == "m2"
    assert not ran_second.is_set()
    release.set()


def test_run_gated_releases_even_if_work_raises():
    _turn = _load_turn()
    client = FakeClient()
    assert _turn.run_gated(
        client, skip_ts=None,
        work=lambda: (_ for _ in ()).throw(RuntimeError("boom")), **_KW
    ) is True
    # Despite the raise, the gate is released (finally) → re-enterable.
    assert _wait_until(lambda: _turn.try_begin_turn(client, **_KW) is True)
