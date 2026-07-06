"""Per-thread turn serialization: turns on the same thread must not overlap
(they'd race the backend's optimistic-concurrency guard → 409), while turns on
different threads still run concurrently."""

from __future__ import annotations

import importlib.util
import threading
import time


def _load_turn():
    import sys

    spec = importlib.util.spec_from_file_location("_turn_s", "listeners/_turn.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # let @dataclass resolve annotations
    spec.loader.exec_module(mod)
    return mod


class _ConcurrencyFM:
    """submit_turn records the peak simultaneous in-flight turns per case."""

    def __init__(self) -> None:
        self.inflight = 0
        self.peak = 0
        self._lock = threading.Lock()

    def create_case(self, *, title=None, initial_message=None):
        return "case_1"

    def submit_turn(self, case_id, **kwargs):
        with self._lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        time.sleep(0.05)  # hold the "turn" open so overlap would be observable
        with self._lock:
            self.inflight -= 1
        from faultmaven.client import TurnResult

        return TurnResult(agent_response="ok")


class _Store:
    def __init__(self) -> None:
        self.m: dict = {}
        self._lock = threading.Lock()

    def get(self, t, c, th):
        with self._lock:
            return self.m.get((t, c, th))

    def put(self, t, c, th, cid):
        with self._lock:
            self.m[(t, c, th)] = cid


def _fire(_turn, fm, store, thread_ts, n=6):
    errors = []

    def one():
        try:
            _turn.run_turn(
                fm, store, team_id="T", channel_id="C", thread_ts=thread_ts,
                text="q",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=one) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_same_thread_turns_never_overlap():
    _turn = _load_turn()
    fm, store = _ConcurrencyFM(), _Store()
    errors = _fire(_turn, fm, store, "thread-A", n=6)
    assert errors == []
    assert fm.peak == 1  # strictly serialized — never two at once


def test_first_message_race_opens_exactly_one_case():
    # Six concurrent first-messages on a fresh thread must open ONE case.
    _turn = _load_turn()
    creates = {"n": 0}
    lock = threading.Lock()

    class CountingFM(_ConcurrencyFM):
        def create_case(self, *, title=None, initial_message=None):
            with lock:
                creates["n"] += 1
            return f"case_{creates['n']}"

    fm, store = CountingFM(), _Store()
    _fire(_turn, fm, store, "thread-B", n=6)
    assert creates["n"] == 1  # no double-open


def test_different_threads_run_concurrently():
    _turn = _load_turn()
    fm, store = _ConcurrencyFM(), _Store()
    threads = [
        threading.Thread(
            target=_turn.run_turn,
            args=(fm, store),
            kwargs=dict(team_id="T", channel_id="C", thread_ts=f"thr-{i}", text="q"),
        )
        for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fm.peak > 1  # distinct threads overlapped (not globally serialized)
