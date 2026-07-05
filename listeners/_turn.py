"""Shared turn pipeline: find-or-create case, submit a turn, post to Slack.

Every channel surface (mention, message shortcut, …) resolves to the same
operation, so it lives here once: ``run_turn`` advances the case, and
``run_turn_and_post`` wraps it with the placeholder→update→error-recovery posting
flow so the surfaces can't drift (they previously diverged on dedup, error
strings, and provenance). De-duplication is per-surface (different identities)
so it stays in each handler.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from faultmaven import FaultMavenClient, TurnResult
from rendering import build_turn_blocks
from store import CaseStore

logger = logging.getLogger(__name__)

INVESTIGATING_PLACEHOLDER = ":mag: FaultMaven is investigating…"
TURN_ERROR_TEXT = (
    ":warning: FaultMaven hit an error on that turn. Please try again or "
    "@mention me."
)


class Dedup:
    """Thread-safe, bounded guard against duplicate event delivery.

    Bolt dispatches listeners across a thread pool, so the check-and-record must
    be atomic; eviction drops the oldest key (LRU) rather than flushing the whole
    set, so a recently-seen retry can't slip through right after an overflow.
    """

    def __init__(self, maxsize: int = 5000) -> None:
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._max = maxsize
        self._lock = threading.Lock()

    def is_duplicate(self, key: str) -> bool:
        with self._lock:
            if not key:
                return False
            if key in self._seen:
                self._seen.move_to_end(key)
                return True
            self._seen[key] = None
            if len(self._seen) > self._max:
                self._seen.popitem(last=False)  # drop oldest
            return False


def run_turn(
    fm: FaultMavenClient,
    store: CaseStore,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    text: str,
    pasted_content: str | None = None,
    source_url: str | None = None,
    prior_context: str | None = None,
    files: list[tuple[str, bytes, str]] | None = None,
) -> TurnResult:
    """Find-or-create the case for this thread and advance it by one turn.

    - ``text`` is always the turn's ``query`` (never seeded as the case
      ``initial_message``, so it isn't recorded twice or bound by the 4000-char
      limit).
    - ``pasted_content`` is *this turn's* evidence (e.g. a shortcut's selected
      message) and is sent on **every** turn, new case or existing.
    - ``files`` are *this turn's* attachments (already-downloaded
      ``(name, bytes, content_type)`` tuples), forwarded as multipart evidence.
    - ``prior_context`` is the one-time catch-up (the prior thread discussion on
      an ``@mention``); it augments the evidence **only when the case is
      created**, never on later turns.
    - ``source_url`` (e.g. a permalink to the alert) is passed through for case
      provenance.
    """

    case_id = store.get(team_id, channel_id, thread_ts)
    if case_id is None:
        case_id = fm.create_case(title=None)
        store.put(team_id, channel_id, thread_ts, case_id)
        logger.info("Opened case %s for thread %s", case_id, thread_ts)
        if prior_context:
            pasted_content = (
                f"{prior_context}\n\n{pasted_content}"
                if pasted_content
                else prior_context
            )

    if pasted_content or source_url or files:
        return fm.submit_turn(
            case_id,
            query=text,
            pasted_content=pasted_content or None,
            source_url=source_url,
            files=files or None,
            input_type="paste" if pasted_content else None,
        )
    return fm.submit_turn(case_id, query=text)


def post_placeholder(
    client: WebClient, channel: str, thread_ts: str
) -> str | None:
    """Post the "investigating…" placeholder and return its ``ts``.

    Returns ``None`` (with actionable logging) if the bot can't post — e.g. it
    isn't in the channel — so callers stop rather than crash. Posting this
    *before* any slow pre-work (like downloading attachments) is what keeps the
    feedback instant.
    """

    try:
        resp = client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=INVESTIGATING_PLACEHOLDER
        )
    except SlackApiError as exc:
        logger.warning(
            "Cannot post in channel %s (%s) — is FaultMaven invited? "
            "Try /invite @FaultMaven",
            channel,
            exc.response.get("error"),
        )
        return None
    return resp["ts"]


def run_turn_and_post(
    client: WebClient,
    fm: FaultMavenClient,
    store: CaseStore,
    *,
    channel: str,
    thread_ts: str,
    team_id: str,
    text: str,
    pasted_content: str | None = None,
    source_url: str | None = None,
    prior_context: str | None = None,
    files: list[tuple[str, bytes, str]] | None = None,
    placeholder_ts: str | None = None,
) -> None:
    """Post a placeholder, run one turn, and update it in place — shared by the
    mention and shortcut surfaces so the post/error flow can't drift.

    ``placeholder_ts`` lets a caller that already posted the placeholder (e.g. to
    show feedback before a slow file download) reuse it instead of posting a
    second one.
    """

    if placeholder_ts is None:
        placeholder_ts = post_placeholder(client, channel, thread_ts)
        if placeholder_ts is None:
            return

    try:
        result = run_turn(
            fm,
            store,
            team_id=team_id,
            channel_id=channel,
            thread_ts=thread_ts,
            text=text,
            pasted_content=pasted_content,
            source_url=source_url,
            prior_context=prior_context,
            files=files,
        )
        client.chat_update(
            channel=channel,
            ts=placeholder_ts,
            text=result.agent_response[:300],
            blocks=build_turn_blocks(result),
        )
    except Exception as exc:  # noqa: BLE001 — last line of defense for a bg turn
        logger.exception("turn failed in %s: %s", channel, exc)
        client.chat_update(
            channel=channel, ts=placeholder_ts, text=TURN_ERROR_TEXT
        )
