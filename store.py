"""Thread→case mapping store (SQLite).

A Slack thread maps one-to-one onto a FaultMaven case. Because we deliberately
do *not* hand the Slack ``thread_ts`` to the backend as a session id (it would
fail server-side session validation), this local map is the source of truth for
"which FaultMaven case is this thread." Keyed by ``(team_id, channel_id,
thread_ts)`` so it is already multi-workspace safe ahead of P5 OAuth.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class CaseStore:
    """Tiny synchronous SQLite-backed thread→case map."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: Bolt dispatches listeners across a thread
        # pool; we serialize access ourselves with ``_lock``.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        # ``seeded`` = the case has landed at least one successful turn. Until
        # then the one-time context seed (thread catch-up) has NOT been
        # delivered, so callers re-fetch and re-send it on the next attempt.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_cases (
                team_id    TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                thread_ts  TEXT NOT NULL,
                case_id    TEXT NOT NULL,
                seeded     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (team_id, channel_id, thread_ts)
            )
            """
        )
        try:
            # Pre-``seeded`` stores: existing rows all had successful turns
            # (the old code only kept mappings that worked), so default 1.
            self._conn.execute(
                "ALTER TABLE thread_cases "
                "ADD COLUMN seeded INTEGER NOT NULL DEFAULT 1"
            )
        except sqlite3.OperationalError:
            pass  # column already exists (fresh create above, or already migrated)
        self._conn.commit()

    def get(self, team_id: str, channel_id: str, thread_ts: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT case_id FROM thread_cases "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (team_id, channel_id, thread_ts),
            ).fetchone()
        return row[0] if row else None

    def put(
        self, team_id: str, channel_id: str, thread_ts: str, case_id: str
    ) -> None:
        """Map a thread to its (new, not-yet-seeded) case."""

        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO thread_cases "
                "(team_id, channel_id, thread_ts, case_id, seeded) "
                "VALUES (?, ?, ?, ?, 0)",
                (team_id, channel_id, thread_ts, case_id),
            )
            self._conn.commit()

    def mark_seeded(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> None:
        """Record that the thread's case has landed a successful turn."""

        with self._lock:
            self._conn.execute(
                "UPDATE thread_cases SET seeded=1 "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (team_id, channel_id, thread_ts),
            )
            self._conn.commit()

    def is_seeded(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> bool:
        """True once the thread's case has had a successful turn.

        False for unknown threads too, so ``not is_seeded(...)`` uniformly
        means "the one-time context seed still needs to be (re)sent".
        """

        with self._lock:
            row = self._conn.execute(
                "SELECT seeded FROM thread_cases "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (team_id, channel_id, thread_ts),
            ).fetchone()
        return bool(row and row[0])

    def delete(self, team_id: str, channel_id: str, thread_ts: str) -> None:
        """Evict a mapping whose case no longer exists server-side.

        Without eviction a 404-ing case pins its thread forever: every retry
        routes back to the same dead case_id.
        """

        with self._lock:
            self._conn.execute(
                "DELETE FROM thread_cases "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (team_id, channel_id, thread_ts),
            )
            self._conn.commit()

    def close(self) -> None:
        # Under the lock: shutdown must not close the connection out from
        # under a get/put still running on a turn worker thread.
        with self._lock:
            self._conn.close()
