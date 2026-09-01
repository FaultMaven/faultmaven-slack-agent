"""Thread→case store — round-trip, tenant isolation, concurrency."""

from __future__ import annotations

import logging
import threading

from store import CaseStore


def test_roundtrip_and_overwrite(tmp_path):
    store = CaseStore(str(tmp_path / "cases.db"))
    try:
        assert store.get("T", "C", "ts") is None
        store.put("T", "C", "ts", "case1")
        assert store.get("T", "C", "ts") == "case1"
        store.put("T", "C", "ts", "case2")  # re-map
        assert store.get("T", "C", "ts") == "case2"
    finally:
        store.close()


def test_keyed_by_team_channel_thread(tmp_path):
    """Same channel+thread in a different workspace must not collide."""

    store = CaseStore(str(tmp_path / "cases.db"))
    try:
        store.put("T1", "C", "ts", "case_a")
        assert store.get("T2", "C", "ts") is None
        assert store.get("T1", "C2", "ts") is None
        assert store.get("T1", "C", "ts2") is None
    finally:
        store.close()


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "cases.db")
    s1 = CaseStore(path)
    s1.put("T", "C", "ts", "case1")
    s1.close()
    s2 = CaseStore(path)
    try:
        assert s2.get("T", "C", "ts") == "case1"
    finally:
        s2.close()


def test_concurrent_writes_are_safe(tmp_path):
    store = CaseStore(str(tmp_path / "cases.db"))
    try:

        def writer(i: int) -> None:
            store.put("T", "C", f"ts{i}", f"case{i}")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(store.get("T", "C", f"ts{i}") == f"case{i}" for i in range(50))
    finally:
        store.close()


def test_turn_and_action_tracking(tmp_path):
    store = CaseStore(str(tmp_path / "cases.db"))
    try:
        store.put("T", "C", "ts", "case1")
        assert store.get_last_turn_ts("T", "C", "ts") is None
        assert store.get_last_action_ts("T", "C", "ts") is None

        store.record_turn("T", "C", "ts", turn_ts="123.456", action_ts="123.456")
        assert store.get_last_turn_ts("T", "C", "ts") == "123.456"
        assert store.get_last_action_ts("T", "C", "ts") == "123.456"

        # A buttonless turn still moves the turn marker: that marker is what
        # the stale-click guard reads, so erasing it would disarm the guard.
        store.record_turn("T", "C", "ts", turn_ts="789.000", action_ts=None)
        assert store.get_last_turn_ts("T", "C", "ts") == "789.000"
        assert store.get_last_action_ts("T", "C", "ts") is None
    finally:
        store.close()


def test_record_turn_keeps_the_known_turn_when_the_reply_never_landed(tmp_path):
    store = CaseStore(str(tmp_path / "cases.db"))
    try:
        store.put("T", "C", "ts", "case1")
        store.record_turn("T", "C", "ts", turn_ts="123.456", action_ts="123.456")
        store.record_turn("T", "C", "ts", turn_ts=None, action_ts=None)
        assert store.get_last_turn_ts("T", "C", "ts") == "123.456"
        assert store.get_last_action_ts("T", "C", "ts") is None
    finally:
        store.close()


def test_clearing_the_buttons_keeps_the_turn_marker(tmp_path):
    store = CaseStore(str(tmp_path / "cases.db"))
    try:
        store.put("T", "C", "ts", "case1")
        store.record_turn("T", "C", "ts", turn_ts="123.456", action_ts="123.456")
        store.clear_last_action_ts("T", "C", "ts")
        assert store.get_last_action_ts("T", "C", "ts") is None
        assert store.get_last_turn_ts("T", "C", "ts") == "123.456"
    finally:
        store.close()


def test_recording_a_turn_for_an_evicted_thread_is_logged_not_silent(tmp_path, caplog):
    """A bare UPDATE writes nothing when the row is gone — say so, don't pretend."""

    store = CaseStore(str(tmp_path / "cases.db"))
    try:
        with caplog.at_level(logging.WARNING):
            store.record_turn("T", "C", "gone", turn_ts="1.1", action_ts="1.1")
        assert store.get_last_turn_ts("T", "C", "gone") is None
        assert any("not recorded" in r.getMessage() for r in caplog.records)
    finally:
        store.close()
