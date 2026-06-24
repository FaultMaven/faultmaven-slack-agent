"""Thread→case store — round-trip, tenant isolation, concurrency."""

from __future__ import annotations

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
