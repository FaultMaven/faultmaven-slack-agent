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
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_cases (
                team_id    TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                thread_ts  TEXT NOT NULL,
                case_id    TEXT NOT NULL,
                PRIMARY KEY (team_id, channel_id, thread_ts)
            )
            """
        )
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
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO thread_cases "
                "(team_id, channel_id, thread_ts, case_id) VALUES (?, ?, ?, ?)",
                (team_id, channel_id, thread_ts, case_id),
            )
            self._conn.commit()

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
