"""Thread→case mapping store (SQLite).

A Slack thread maps one-to-one onto a FaultMaven case. Because we deliberately
do *not* hand the Slack ``thread_ts`` to the backend as a session id (it would
fail server-side session validation), this local map is the source of truth for
"which FaultMaven case is this thread." Keyed by ``(team_id, channel_id,
thread_ts)`` so it is already multi-workspace safe ahead of P5 OAuth.

A mapping whose case the backend can no longer find is **tombstoned**, not
deleted (``mark_unlinked``): the thread reads as unmapped everywhere, but the
agent still remembers that it once owned it. That memory is what lets a reply in
such a thread be answered ("I couldn't find the case") instead of being dropped
as the ambient channel chatter an unknown thread is.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class CaseStore:
    """Tiny synchronous SQLite-backed thread→case map."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: Bolt dispatches listeners across a thread
        # pool; we serialize access ourselves with ``_lock``.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        # A store that starts empty is either a first run or a lost volume, and
        # the two are indistinguishable from in here. The consequence is the
        # same either way and it is invisible at runtime — every thread already
        # open in Slack now looks like a thread we have never seen — so it is
        # said once, out loud, rather than left for someone to deduce from a
        # user reporting that the bot stopped answering.
        fresh = not self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_cases'"
        ).fetchone()
        # ``seeded`` = the case has landed at least one successful turn. Until
        # then the one-time context seed (thread catch-up) has NOT been
        # delivered, so callers re-fetch and re-send it on the next attempt.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_cases (
                team_id        TEXT NOT NULL,
                channel_id     TEXT NOT NULL,
                thread_ts      TEXT NOT NULL,
                case_id        TEXT NOT NULL,
                seeded         INTEGER NOT NULL DEFAULT 0,
                unlinked       INTEGER NOT NULL DEFAULT 0,
                unlink_notified INTEGER NOT NULL DEFAULT 0,
                last_turn_ts   TEXT,
                last_action_ts TEXT,
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
        for column in ("last_turn_ts", "last_action_ts"):
            try:
                self._conn.execute(
                    f"ALTER TABLE thread_cases ADD COLUMN {column} TEXT"
                )
            except sqlite3.OperationalError:
                pass
        # Pre-tombstone stores evicted a dead mapping with DELETE, so every row
        # that survives is a live one: default 0 (not unlinked) is right.
        for column in ("unlinked", "unlink_notified"):
            try:
                self._conn.execute(
                    f"ALTER TABLE thread_cases "
                    f"ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass
        self._conn.commit()
        if fresh:
            logger.warning(
                "thread→case map at %s is empty: no existing Slack thread will "
                "be recognised as an investigation. Expected on a first run; "
                "otherwise this volume did not persist (see docs/HOSTING.md).",
                path,
            )

    def get(self, team_id: str, channel_id: str, thread_ts: str) -> str | None:
        """The live case for this thread, or None.

        None for a tombstoned thread too (``unlinked=1``): its case is gone, so
        there is nothing to submit against. Callers that need to tell "never
        ours" from "ours, and its case went missing" ask ``is_unlinked``.
        """

        with self._lock:
            row = self._conn.execute(
                "SELECT case_id FROM thread_cases "
                "WHERE team_id=? AND channel_id=? AND thread_ts=? AND unlinked=0",
                (team_id, channel_id, thread_ts),
            ).fetchone()
        return row[0] if row else None

    def put(
        self, team_id: str, channel_id: str, thread_ts: str, case_id: str
    ) -> None:
        """Map a thread to its (new, not-yet-seeded) case."""

        # Explicitly live and un-notified: re-linking a tombstoned thread (an
        # @mention after its case went missing) must clear the tombstone, or the
        # thread would keep answering "I couldn't find the case" forever.
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO thread_cases "
                "(team_id, channel_id, thread_ts, case_id, seeded, "
                "unlinked, unlink_notified) "
                "VALUES (?, ?, ?, ?, 0, 0, 0)",
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
        means "the one-time context seed still needs to be (re)sent" — and for
        tombstoned ones, whose next case starts from nothing and so needs the
        thread catch-up exactly as a never-seen thread would.
        """

        with self._lock:
            row = self._conn.execute(
                "SELECT seeded FROM thread_cases "
                "WHERE team_id=? AND channel_id=? AND thread_ts=? AND unlinked=0",
                (team_id, channel_id, thread_ts),
            ).fetchone()
        return bool(row and row[0])

    def get_last_turn_ts(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> str | None:
        """The Slack ts of the newest bot turn message in this thread.

        This is what identifies the *current* turn: a button click carries no
        proof of which turn rendered it, so a click on any other message is a
        click from a turn the conversation has already moved past.
        """

        with self._lock:
            row = self._conn.execute(
                "SELECT last_turn_ts FROM thread_cases "
                "WHERE team_id=? AND channel_id=? AND thread_ts=? AND unlinked=0",
                (team_id, channel_id, thread_ts),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_last_action_ts(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> str | None:
        """Return the Slack ts of the last message in this thread with active buttons."""

        with self._lock:
            row = self._conn.execute(
                "SELECT last_action_ts FROM thread_cases "
                "WHERE team_id=? AND channel_id=? AND thread_ts=? AND unlinked=0",
                (team_id, channel_id, thread_ts),
            ).fetchone()
        return row[0] if row and row[0] else None

    def record_turn(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        *,
        turn_ts: str | None,
        action_ts: str | None,
    ) -> None:
        """Point the thread at its newest turn message, and at its live buttons.

        ``turn_ts=None`` keeps whatever turn is already on record — the reply
        never landed, so the previous turn is still the newest one we know of,
        and forgetting it would disarm the stale-click guard entirely.
        ``action_ts=None`` records that no buttons are live.

        This is an UPDATE, not an upsert (``case_id`` is NOT NULL, so there is
        nothing sensible to insert): a thread whose row was evicted mid-turn
        writes nothing, and that deserves a log line rather than a silent
        no-op that leaves the caller believing the turn was recorded.
        """

        with self._lock:
            cursor = self._conn.execute(
                "UPDATE thread_cases "
                "SET last_turn_ts=COALESCE(?, last_turn_ts), last_action_ts=? "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (turn_ts, action_ts, team_id, channel_id, thread_ts),
            )
            self._conn.commit()
            rowcount = cursor.rowcount
        if rowcount == 0:
            logger.warning(
                "no thread_cases row for %s/%s/%s: turn %s not recorded "
                "(this thread's stale-click guard is unarmed)",
                team_id, channel_id, thread_ts, turn_ts or "?",
            )

    def clear_last_action_ts(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> None:
        """Record that this thread has no live choice buttons left.

        Leaves the recorded turn alone: which turn is current is a separate
        fact, and it stays true after the buttons come down.
        """

        with self._lock:
            self._conn.execute(
                "UPDATE thread_cases SET last_action_ts=NULL "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (team_id, channel_id, thread_ts),
            )
            self._conn.commit()

    def mark_unlinked(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> None:
        """Tombstone a mapping whose case the backend can no longer find.

        Unlinking is what stops a missing case pinning its thread forever: every
        retry would otherwise route back to the same dead ``case_id``. It is a
        tombstone rather than a DELETE because forgetting the thread entirely
        costs the agent the one fact it needs to answer the next reply — that
        this thread was once an investigation of ours. ``unlink_notified`` is
        reset so the thread gets its one explanation.

        The dead ``case_id`` is kept for the log trail; no read returns it.
        Buttons are cleared: they carry decisions for a case that isn't there.
        """

        with self._lock:
            self._conn.execute(
                "UPDATE thread_cases "
                "SET unlinked=1, unlink_notified=0, last_action_ts=NULL "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (team_id, channel_id, thread_ts),
            )
            self._conn.commit()

    def is_unlinked(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> bool:
        """True if this thread was ours and its case has since gone missing.

        False for a thread we never owned — the two must not be conflated: one
        deserves an explanation, the other is someone else's conversation.
        """

        with self._lock:
            row = self._conn.execute(
                "SELECT unlinked FROM thread_cases "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (team_id, channel_id, thread_ts),
            ).fetchone()
        return bool(row and row[0])

    def unlink_notice_pending(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> bool:
        """True if this thread is tombstoned and hasn't been told yet.

        Gates the *unprompted* explanation only — the one posted into a thread
        whose plain reply the agent would otherwise ignore. Say it once: after
        that, repeating it on every reply would make the bot the loudest thing
        in a thread it no longer has any part in. Explicit invocations
        (@mention, a button) are answered every time regardless.
        """

        with self._lock:
            row = self._conn.execute(
                "SELECT unlinked, unlink_notified FROM thread_cases "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (team_id, channel_id, thread_ts),
            ).fetchone()
        return bool(row and row[0] and not row[1])

    def claim_unlink_notice(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> bool:
        """Take the right to post this thread's one notice; True if you got it.

        Claimed with a conditional UPDATE rather than a read followed by a
        write, because the read and the write would not be one step: replies
        arrive in bursts and Bolt dispatches them across a thread pool, so two
        of them can both see "not told yet" and both post. The row is the lock.

        The caller releases the claim if its post never lands, so a notice lost
        to a Slack failure is still owed to the thread.
        """

        with self._lock:
            cursor = self._conn.execute(
                "UPDATE thread_cases SET unlink_notified=1 "
                "WHERE team_id=? AND channel_id=? AND thread_ts=? "
                "AND unlinked=1 AND unlink_notified=0",
                (team_id, channel_id, thread_ts),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def release_unlink_notice(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> None:
        """Hand back a claim whose notice never reached Slack."""

        with self._lock:
            self._conn.execute(
                "UPDATE thread_cases SET unlink_notified=0 "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (team_id, channel_id, thread_ts),
            )
            self._conn.commit()

    def close(self) -> None:
        # Under the lock: shutdown must not close the connection out from
        # under a get/put still running on a turn worker thread.
        with self._lock:
            self._conn.close()
