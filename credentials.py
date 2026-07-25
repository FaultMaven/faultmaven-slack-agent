"""Durable store for the FaultMaven refresh credential (SQLite).

The refresh grant rotates: every renewal revokes the token we presented and
returns a new one. The new token is therefore the *only* way back in, and it
must outlive the process — a pod restart with a stale token is a lockout that
needs an operator to re-run provisioning.

So this store is deliberately boring: one row, written and committed BEFORE the
rotated token is used, on the same PVC-backed volume as the case store.

It keeps its own SQLite file rather than sharing the case store's, so a write
here can never contend with a turn's thread→case lookup.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class CredentialStore:
    """Single-row store for the current FaultMaven refresh token."""

    _ROW_ID = 1

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the renewal can be driven from a turn worker
        # or the keepalive thread; we serialize with ``_lock``.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fm_credential (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                refresh_token TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def get(self) -> str | None:
        """The persisted refresh token, or None before the first bootstrap."""

        with self._lock:
            row = self._conn.execute(
                "SELECT refresh_token FROM fm_credential WHERE id=?",
                (self._ROW_ID,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def put(self, refresh_token: str) -> None:
        """Persist the refresh token, committing before returning.

        Callers rely on this being durable on return: a rotated token that is
        used before it is committed is lost if the process dies mid-cycle, and
        the token it replaced is already revoked server-side.
        """

        if not refresh_token:
            raise ValueError("refusing to persist an empty refresh token")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO fm_credential (id, refresh_token) "
                "VALUES (?, ?)",
                (self._ROW_ID, refresh_token),
            )
            self._conn.commit()

    def close(self) -> None:
        # Under the lock: shutdown must not close the connection out from under
        # a renewal still running on another thread.
        with self._lock:
            self._conn.close()
