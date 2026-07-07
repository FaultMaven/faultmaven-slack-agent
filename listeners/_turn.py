"""Shared turn pipeline: find-or-create case, submit a turn, post to Slack.

Every channel surface (mention, message reply, shortcut, button) resolves to the
same operation, so it lives here once: ``run_turn`` advances the case and
``run_turn_and_post`` wraps it with the placeholder→update→error-recovery flow.

Concurrency model (a Slack thread is N:1 — many people, one case):
the backend is linear (a turn advances the case version by one, guarded by
optimistic concurrency), so two turns on the same thread must not overlap.
Instead of queuing overlaps — which would answer a message its sender wrote
*before* seeing FaultMaven's reply, against newer state — we **run the first
message and skip the rest**: while a thread is busy, a new message is dropped and
marked ⏭️ so its sender knows to resend after the reply lands. Callers reserve the
thread with :func:`try_begin_turn` and release it with :func:`end_turn`.
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

INVESTIGATING_PLACEHOLDER = ":mag: Investigating…"
TURN_ERROR_TEXT = (
    ":warning: FaultMaven hit an error on that turn. Please try again or "
    "@mention me."
)
# Reaction added to a message that was skipped because the thread was busy.
SKIPPED_REACTION = "track_next"  # ⏭️
# One-time etiquette note on the first reply in a channel thread.
_INTRO_WARNING = (
    ":bulb: I take these one at a time — wait for my reply before sending the "
    "next, or it'll be skipped (:track_next:) and you can resend it."
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


class _ThreadGate:
    """One 'busy' slot per Slack thread — the basis of drop-if-busy.

    ``try_enter`` marks a thread busy and returns True if it was idle, or False if
    a turn is already running for it. ``release`` frees it. The busy set only ever
    holds *currently-running* threads (released in the caller's ``finally``), so it
    stays small without any eviction bookkeeping.
    """

    def __init__(self) -> None:
        self._busy: set[str] = set()
        self._guard = threading.Lock()

    def try_enter(self, key: str) -> bool:
        with self._guard:
            if key in self._busy:
                return False
            self._busy.add(key)
            return True

    def release(self, key: str) -> None:
        with self._guard:
            self._busy.discard(key)


_gate = _ThreadGate()


def _thread_key(team_id: str, channel: str, thread_ts: str) -> str:
    return "\x00".join((team_id, channel, thread_ts))


def try_begin_turn(
    client: WebClient,
    *,
    team_id: str,
    channel: str,
    thread_ts: str,
    skip_ts: str | None = None,
) -> bool:
    """Reserve a thread for a turn (drop-if-busy).

    Returns True if the thread was idle — the caller now owns it and MUST call
    :func:`end_turn` when done. Returns False if a turn is already running; the
    caller should stop. When ``skip_ts`` is given, the skipped message is marked
    :data:`SKIPPED_REACTION` so its sender knows it was ignored and can resend.
    """

    if _gate.try_enter(_thread_key(team_id, channel, thread_ts)):
        return True
    if skip_ts:
        try:
            client.reactions_add(
                channel=channel, timestamp=skip_ts, name=SKIPPED_REACTION
            )
        except Exception as exc:  # noqa: BLE001 — reacting must never raise on the drop path
            # e.g. reactions:write not (re)consented → the skip has no visible
            # signal; log it loudly so the missing scope is diagnosable.
            logger.warning(
                "Could not mark a skipped message in %s (%s) — is reactions:write "
                "granted? The message was dropped with no ⏭️.",
                channel,
                exc,
            )
    return False


def end_turn(team_id: str, channel: str, thread_ts: str) -> None:
    """Release a thread reserved by :func:`try_begin_turn` (call in ``finally``)."""

    _gate.release(_thread_key(team_id, channel, thread_ts))


def run_gated(
    client: WebClient,
    *,
    team_id: str,
    channel: str,
    thread_ts: str,
    skip_ts: str | None,
    work,
) -> bool:
    """Reserve the thread and run ``work()`` on a background daemon.

    Returns True and offloads ``work`` (releasing the thread when it finishes) if
    the thread was idle; the Bolt listener thread returns immediately, so a long
    turn never pins a Socket Mode worker or delays the envelope ack. Returns False
    if a turn is already running — the caller decides how to signal that; when
    ``skip_ts`` is given the busy message is marked ⏭️ automatically.
    """

    if not try_begin_turn(
        client, team_id=team_id, channel=channel, thread_ts=thread_ts,
        skip_ts=skip_ts,
    ):
        return False

    def runner() -> None:
        try:
            work()
        except Exception as exc:  # noqa: BLE001 — last line of defense for a bg turn
            logger.exception("gated turn failed in %s: %s", channel, exc)
        finally:
            end_turn(team_id, channel, thread_ts)

    threading.Thread(target=runner, daemon=True).start()
    return True


def resolve_query(raw_text: str | None, *, downloaded_files: bool) -> str | None:
    """The query for a turn, or ``None`` if there's nothing to investigate.

    ``raw_text`` may be missing/``None`` (Slack sends ``text: null`` on some
    file-share messages) or whitespace. Returns ``None`` only when there is no
    text *and* no ingestible file, so a caller can decline (tell the user)
    instead of opening a blank case with an empty query the backend rejects.
    """

    text = (raw_text or "").strip()
    if text:
        return text
    if downloaded_files:
        return "Please investigate the attached file(s)."
    return None


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

    Concurrency is the caller's responsibility (:func:`try_begin_turn`): only one
    turn runs per thread at a time, so there is no case-version race here.

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


def _address(blocks: list[dict], user_id: str) -> None:
    """Prefix the reply's first section with an @mention of the addressed user."""

    for block in blocks:
        if block.get("type") == "section" and isinstance(block.get("text"), dict):
            block["text"]["text"] = f"<@{user_id}> {block['text']['text']}"
            return


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
    mention_user: str | None = None,
) -> None:
    """Post a placeholder, run one turn, and update it in place.

    The caller holds the per-thread gate (:func:`try_begin_turn`), so no two turns
    overlap here. ``mention_user`` addresses the reply to the person whose message
    it answers (channels), and the first reply in a thread carries a one-time
    etiquette note about the one-at-a-time behavior. ``placeholder_ts`` reuses a
    placeholder a caller already posted (e.g. before a slow file download).
    """

    if placeholder_ts is None:
        placeholder_ts = post_placeholder(client, channel, thread_ts)
        if placeholder_ts is None:
            return

    try:
        first_turn = store.get(team_id, channel, thread_ts) is None
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
        blocks = build_turn_blocks(result)
        if mention_user:
            _address(blocks, mention_user)
            if first_turn:
                blocks.append(
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": _INTRO_WARNING}],
                    }
                )
        client.chat_update(
            channel=channel,
            ts=placeholder_ts,
            text=result.agent_response[:300],
            blocks=blocks,
        )
    except Exception as exc:  # noqa: BLE001 — last line of defense for a bg turn
        logger.exception("turn failed in %s: %s", channel, exc)
        client.chat_update(
            channel=channel, ts=placeholder_ts, text=TURN_ERROR_TEXT
        )
