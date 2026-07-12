"""Shared turn pipeline + dedup — routing, context replay, LRU, thread-safety."""

from __future__ import annotations

import threading

import listeners._turn as turn_mod
from faultmaven import FaultMavenAPIError, FaultMavenTimeoutError
from faultmaven.client import TurnResult
from listeners._turn import (
    RESTARTING_TEXT,
    TURN_ERROR_TEXT,
    TURN_TIMEOUT_TEXT,
    Dedup,
    run_turn,
    turn_error_text,
)

# _shutting_down is reset around every test by the autouse fixture in conftest.


class FakeFM:
    def __init__(self) -> None:
        self.creates: list[tuple] = []
        self.turns: list[tuple] = []

    def create_case(self, *, title=None, initial_message=None) -> str:
        self.creates.append((title, initial_message))
        return f"case{len(self.creates)}"

    def submit_turn(self, case_id, **kwargs) -> TurnResult:
        self.turns.append((case_id, kwargs))
        return TurnResult(agent_response="r")


class FakeStore:
    def __init__(self) -> None:
        self.m: dict = {}
        self.seeded: set = set()

    def get(self, t, c, th):
        return self.m.get((t, c, th))

    def put(self, t, c, th, cid):
        self.m[(t, c, th)] = cid

    def mark_seeded(self, t, c, th):
        self.seeded.add((t, c, th))

    def is_seeded(self, t, c, th):
        return (t, c, th) in self.seeded


# -- run_turn -----------------------------------------------------------------
def test_first_turn_creates_case_without_initial_message_and_routes_text():
    fm, store = FakeFM(), FakeStore()
    big = "x" * 9000
    run_turn(fm, store, team_id="T", channel_id="C", thread_ts="t1", text=big)

    assert fm.creates == [(None, None)]  # no initial_message seeded
    case_id, kwargs = fm.turns[0]
    assert case_id == "case1"
    assert kwargs["query"] == big  # full text via query (no 4000 cap)
    assert kwargs.get("pasted_content") is None


def test_prior_context_merged_whenever_the_caller_provides_it():
    """Callers gate prior_context on ``store.is_seeded`` (so a failed opening
    turn re-delivers it on retry); run_turn merges it whenever passed."""

    fm, store = FakeFM(), FakeStore()
    run_turn(fm, store, team_id="T", channel_id="C", thread_ts="t1",
             text="now", prior_context="earlier discussion")
    assert fm.turns[0][1]["pasted_content"] == "earlier discussion"
    assert store.is_seeded("T", "C", "t1")  # landed → callers stop passing it

    run_turn(fm, store, team_id="T", channel_id="C", thread_ts="t1",
             text="again")
    assert len(fm.creates) == 1  # reuses the existing case
    assert fm.turns[1][1].get("pasted_content") is None


def test_existing_thread_reuses_case():
    fm, store = FakeFM(), FakeStore()
    run_turn(fm, store, team_id="T", channel_id="C", thread_ts="t1", text="one")
    run_turn(fm, store, team_id="T", channel_id="C", thread_ts="t1", text="two")
    assert len(fm.creates) == 1
    assert fm.turns[0][0] == fm.turns[1][0] == "case1"


def test_committed_turn_survives_a_mark_seeded_failure():
    """A store-write failure AFTER submit_turn commits must not sink the reply
    and mislabel it a turn error — the committed result is still returned."""

    class BoomSeedStore(FakeStore):
        def mark_seeded(self, t, c, th):
            raise RuntimeError("db closed mid-shutdown")

    fm, store = FakeFM(), BoomSeedStore()
    result = run_turn(
        fm, store, team_id="T", channel_id="C", thread_ts="t1", text="hi"
    )
    assert result.agent_response == "r"  # committed reply returned regardless


# -- turn_error_text: committed-turn / retry-advice discipline ----------------
def test_timeout_class_is_indeterminate_not_retry():
    """The client maps BOTH a client-side read timeout AND a gateway 502/504 to
    FaultMavenTimeoutError, so this layer recognizes 'maybe committed' from that
    single type without inspecting any status code."""

    assert turn_error_text(FaultMavenTimeoutError("x")) == TURN_TIMEOUT_TEXT


def test_plain_5xx_advises_retry():
    # A raw 5xx APIError reaching here (e.g. from create_case, which is always
    # retry-safe — it commits no turn) gets the plain retry advice; the turn
    # path's gateway 502/504 is already converted to a timeout upstream.
    for code in (500, 502, 503, 504):
        assert (
            turn_error_text(FaultMavenAPIError("x", status_code=code))
            == TURN_ERROR_TEXT
        )


def test_indeterminate_failure_during_shutdown_still_warns_not_restart():
    """The shutdown override must NOT shadow a possibly-committed turn: a
    timeout (the single indeterminate class) during drain must still warn
    against a blind re-send rather than say 'restarting, resend in a minute'."""

    turn_mod.begin_shutdown()  # autouse fixture clears it again after
    assert turn_error_text(FaultMavenTimeoutError("x")) == TURN_TIMEOUT_TEXT
    # a genuine 404 or a generic teardown error during shutdown DOES say
    # restarting (shutdown wins over those, matching the original ordering)
    assert turn_error_text(RuntimeError("store closed")) == RESTARTING_TEXT
    from faultmaven import CaseNotFoundError

    assert (
        turn_error_text(CaseNotFoundError("gone", status_code=404))
        == RESTARTING_TEXT
    )


# -- Dedup --------------------------------------------------------------------
def test_dedup_detects_repeat():
    d = Dedup()
    assert d.is_duplicate("k") is False
    assert d.is_duplicate("k") is True


def test_dedup_lru_eviction_keeps_recent_keys():
    d = Dedup(maxsize=2)
    d.is_duplicate("a")
    d.is_duplicate("b")
    d.is_duplicate("c")  # evicts the oldest ("a"), not the whole set
    assert d.is_duplicate("a") is False  # "a" was evicted → treated as new
    assert d.is_duplicate("c") is True   # "c" still remembered


def test_dedup_is_thread_safe():
    d = Dedup()
    results: list[bool] = []

    def hit() -> None:
        results.append(d.is_duplicate("same"))

    threads = [threading.Thread(target=hit) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(False) == 1  # exactly one thread saw it as new
