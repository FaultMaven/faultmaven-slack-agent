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
import time
from collections import OrderedDict

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from faultmaven import (
    CaseNotFoundError,
    FaultMavenAPIError,
    FaultMavenClient,
    FaultMavenTimeoutError,
    TurnResult,
)
from rendering import build_turn_blocks
from slack_mrkdwn import escape_mrkdwn, to_mrkdwn
from store import CaseStore

logger = logging.getLogger(__name__)

# Set by app.py at the start of shutdown: in-flight turns that fail from the
# teardown itself (closed store/client) must not blame the turn or advise a
# retry the user can't evaluate.
_shutting_down = threading.Event()


def begin_shutdown() -> None:
    """Mark the process as shutting down (affects in-flight error messages)."""

    _shutting_down.set()

INVESTIGATING_PLACEHOLDER = ":mag: Investigating…"
TURN_ERROR_TEXT = (
    ":warning: FaultMaven hit an error on that turn. Please try again or "
    "@mention me."
)
# The turn may have completed backend-side — do NOT advise a retry (a resent
# message would run a duplicate turn against state the user never saw). Worded
# to hold whether or not a case/turn had yet been recorded (a create-case
# timeout reaches here too), so it never asserts a commit that didn't happen.
TURN_TIMEOUT_TEXT = (
    ":hourglass: I gave up waiting on the backend — that turn may still be "
    "completing on the case. Give it a moment before re-sending the same "
    "message or evidence."
)
# Stale mapping evicted; unlike TURN_ERROR_TEXT, a retry WILL work (fresh case).
CASE_GONE_TEXT = (
    ":warning: This investigation's case no longer exists on the backend, so "
    "I've unlinked it — your next message here starts a fresh investigation."
)
# The failure came from our own teardown, not the turn: say so.
RESTARTING_TEXT = (
    ":arrows_counterclockwise: I'm restarting and couldn't finish that turn — "
    "please resend it in a minute."
)
# Shared decline for a message whose only content was undownloadable files.
UNREADABLE_FILES_TEXT = (
    ":information_source: I couldn't read the attached file(s) (too large, or "
    "I lack access). Paste the key text and I'll take it from there."
)


def skipped_files_note(skipped: list[str]) -> str:
    """User-facing note for attachments beyond the one-file-per-turn limit.

    The backend ingests one file per turn; extra attachments on a single
    Slack message are not downloaded (see ``download_message_content``).
    Say so instead of dropping them silently — the fix is in the user's
    hands (send each in its own message).
    """

    names = ", ".join(f"`{n}`" for n in skipped)
    return (
        f":information_source: I can take one file per message — I kept the "
        f"first and skipped {names}. Send each in its own message and I'll "
        "ingest them."
    )


def turn_error_text(exc: Exception) -> str:
    """The user-facing message for a failed turn, by failure class.

    One generic "try again" for everything actively misleads: a 4xx reproduces
    identically on retry, and a timeout's turn may have committed — only
    transient transport/5xx failures deserve the retry advice.
    """

    # "Indeterminate — the backend may have committed this turn" is checked
    # BEFORE the shutdown override: a commit-then-fail during drain must still
    # warn against a blind re-send, not tell the user to resend in a minute. This
    # is a single class — the client already maps a client-side read timeout AND
    # a gateway 502/504 to FaultMavenTimeoutError, so this layer never inspects a
    # status code to recognize it. Everything else (a genuine 404, a 4xx, a
    # teardown-induced error) prefers the shutdown message during drain, matching
    # the original ordering.
    if isinstance(exc, FaultMavenTimeoutError):
        return TURN_TIMEOUT_TEXT
    if _shutting_down.is_set():
        return RESTARTING_TEXT
    if isinstance(exc, CaseNotFoundError):
        return CASE_GONE_TEXT
    if isinstance(exc, FaultMavenAPIError) and 400 <= exc.status_code < 500:
        if exc.status_code == 429:
            return TURN_ERROR_TEXT  # backend backpressure IS transient
        detail = escape_mrkdwn(exc.detail[:200]) if exc.detail else ""
        suffix = f": _{detail}_" if detail else "."
        return (
            f":warning: FaultMaven rejected that turn "
            f"(HTTP {exc.status_code}){suffix} "
            "Re-sending the same input won't help."
        )
    return TURN_ERROR_TEXT


def notification_text(result: TurnResult) -> str:
    """The short ``text=`` fallback accompanying a blocks post, neutralized."""

    return escape_mrkdwn(str(result.agent_response))[:300]


def plain_fallback(agent_response: str) -> str:
    """Plain-text degradation of a committed turn's reply.

    Runs through :func:`to_mrkdwn` so the degraded path keeps BOTH properties
    of the blocks path: untrusted entities stay neutralized (a raw
    ``agent_response`` here would re-open the ``<!channel>`` injection exactly
    when blocks fail) and Markdown still renders. ``str()`` guards schema
    drift — this function must never raise.
    """

    text = to_mrkdwn(str(agent_response))
    if len(text) > 3500:
        text = text[:3500] + "…\n_(reply truncated — full analysis is on the case)_"
    return text


def deliver_turn_result(
    post,
    result: TurnResult,
    *,
    case_id: str | None = None,
    decorate=None,
) -> None:
    """Render and post a COMMITTED turn via ``post(text, blocks) -> bool``.

    ``post`` must be guarded (log-and-return-False, never raise). The backend
    already advanced the case, so every degradation step here — rendering
    failure, blocks rejected, plain retry — avoids "try again"; the last
    resort is a plain-text reply, and total posting failure is the caller's
    logs, never a retry prompt. ``decorate`` may mutate the built blocks
    (addressing, intro notes) and is guarded with the rendering.
    """

    blocks = None
    try:
        blocks = build_turn_blocks(result, case_id=case_id)
        if decorate is not None:
            decorate(blocks)
    except Exception as exc:  # noqa: BLE001 — rendering must not eat a committed turn
        logger.exception("rendering turn result failed: %s", exc)
    if blocks is not None and post(notification_text(result), blocks):
        return
    post(plain_fallback(result.agent_response), None)


def unlink_stale_case(
    store: CaseStore, team_id: str, channel: str, thread_ts: str, case_id: str
) -> None:
    """Evict a thread's mapping after the backend 404ed its case."""

    store.delete(team_id, channel, thread_ts)
    logger.warning(
        "Case %s vanished server-side; unlinked thread %s", case_id, thread_ts
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

    def is_busy(self, key: str) -> bool:
        with self._guard:
            return key in self._busy

    def release(self, key: str) -> None:
        with self._guard:
            self._busy.discard(key)


_gate = _ThreadGate()

# Turn worker threads currently in flight, so shutdown can drain them (a
# daemon thread killed mid-turn strands its placeholder at "Investigating…").
_active_turns: set[threading.Thread] = set()
_active_turns_lock = threading.Lock()


def _thread_key(team_id: str, channel: str, thread_ts: str) -> str:
    return "\x00".join((team_id, channel, thread_ts))


def is_thread_busy(team_id: str, channel: str, thread_ts: str) -> bool:
    """True while a turn is running for this thread (gate held)."""

    return _gate.is_busy(_thread_key(team_id, channel, thread_ts))


def mark_skipped(client: WebClient, channel: str, ts: str) -> None:
    """Mark a dropped message ⏭️ so its sender knows to resend. Never raises."""

    try:
        client.reactions_add(
            channel=channel, timestamp=ts, name=SKIPPED_REACTION
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
        mark_skipped(client, channel, skip_ts)
    return False


def end_turn(team_id: str, channel: str, thread_ts: str) -> None:
    """Release a thread reserved by :func:`try_begin_turn` (call in ``finally``)."""

    _gate.release(_thread_key(team_id, channel, thread_ts))


def offload_turn(work, *, team_id: str, channel: str, thread_ts: str) -> None:
    """Run ``work()`` on a tracked background daemon, releasing the gate after.

    The caller must already hold the thread gate (:func:`try_begin_turn`). The
    thread is registered so :func:`drain_turns` can wait for it at shutdown.
    """

    def runner() -> None:
        try:
            work()
        except Exception as exc:  # noqa: BLE001 — last line of defense for a bg turn
            logger.exception("gated turn failed in %s: %s", channel, exc)
        finally:
            end_turn(team_id, channel, thread_ts)
            with _active_turns_lock:
                _active_turns.discard(threading.current_thread())

    thread = threading.Thread(target=runner, daemon=True)
    with _active_turns_lock:
        _active_turns.add(thread)
    try:
        thread.start()
    except BaseException:
        # start() can fail under resource exhaustion ("can't start new
        # thread"); the runner's finally never runs then, so undo its work
        # here — otherwise the gate stays held forever (a permanently
        # ⏭️-wedged Slack thread) and drain_turns would later join() a
        # never-started Thread, which raises.
        with _active_turns_lock:
            _active_turns.discard(thread)
        end_turn(team_id, channel, thread_ts)
        raise


def drain_turns(timeout: float) -> None:
    """Give in-flight turns up to ``timeout`` seconds to finish (at shutdown)."""

    deadline = time.monotonic() + timeout
    with _active_turns_lock:
        pending = list(_active_turns)
    for thread in pending:
        try:
            thread.join(max(0.0, deadline - time.monotonic()))
        except Exception as exc:  # noqa: BLE001 — drain must never abort shutdown cleanup
            logger.warning("drain join failed for %s: %s", thread.name, exc)
    leftover = sum(1 for t in pending if t.is_alive())
    if leftover:
        logger.warning(
            "Shutting down with %d turn(s) still in flight after %.0fs grace",
            leftover,
            timeout,
        )


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
    offload_turn(work, team_id=team_id, channel=channel, thread_ts=thread_ts)
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
    - ``prior_context`` is the one-time catch-up (the prior thread discussion
      on an ``@mention``); it is merged into the evidence whenever provided.
      Callers pass it only while the thread is not yet **seeded**
      (``store.is_seeded``) — i.e. until a first turn actually lands — so a
      failed opening turn re-delivers it instead of silently losing it.
    - ``source_url`` (e.g. a permalink to the alert) is passed through for case
      provenance.
    """

    case_id = store.get(team_id, channel_id, thread_ts)
    if case_id is None:
        case_id = fm.create_case(title=None)
        # Map the thread immediately (unseeded) so the thread stays linked
        # even if this first submit fails: in-thread retries keep routing to
        # the case, and a timed-out-but-committed first turn stays reachable.
        # The seed isn't lost either — the row stays unseeded until a turn
        # lands, and callers re-fetch/re-send prior_context while unseeded.
        store.put(team_id, channel_id, thread_ts, case_id)
        logger.info("Opened case %s for thread %s", case_id, thread_ts)

    if prior_context:
        pasted_content = (
            f"{prior_context}\n\n{pasted_content}"
            if pasted_content
            else prior_context
        )

    try:
        if pasted_content or source_url or files:
            result = fm.submit_turn(
                case_id,
                query=text,
                pasted_content=pasted_content or None,
                source_url=source_url,
                files=files or None,
                input_type="paste" if pasted_content else None,
            )
        else:
            result = fm.submit_turn(case_id, query=text)
    except CaseNotFoundError:
        # Case deleted server-side (dashboard delete, DB reset) — evict the
        # stale mapping so the NEXT message starts fresh instead of routing
        # to the same dead case_id forever.
        unlink_stale_case(store, team_id, channel_id, thread_ts, case_id)
        raise

    # The turn is committed. A store-write failure here (a closed DB during a
    # shutdown/drain race, a lock, a full disk) must NOT sink the reply and
    # mislabel it a turn error — the only cost of a missed seed is that the
    # one-time catch-up context is re-sent next turn.
    try:
        store.mark_seeded(team_id, channel_id, thread_ts)
    except Exception as exc:  # noqa: BLE001 — never lose a committed turn to a store write
        logger.warning(
            "mark_seeded failed for thread %s (committed turn kept): %s",
            thread_ts, exc,
        )
    return result


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
        error = exc.response.get("error", "")
        if error in ("not_in_channel", "channel_not_found"):
            logger.warning(
                "Cannot post in channel %s (%s) — is FaultMaven invited? "
                "Try /invite @FaultMaven",
                channel,
                error,
            )
        else:
            # Rate limits, restrictions, transient API failures — do NOT
            # misdiagnose these as a missing invite.
            logger.warning(
                "Placeholder post failed in channel %s (%s)", channel, error
            )
        return None
    except Exception as exc:  # noqa: BLE001 — transport failure: Slack is unreachable
        logger.warning(
            "Placeholder post failed in channel %s (transport: %s)", channel, exc
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
    intro_note: str | None = None,
) -> None:
    """Post a placeholder, run one turn, and update it in place.

    The caller holds the per-thread gate (:func:`try_begin_turn`), so no two turns
    overlap here. ``mention_user`` addresses the reply to the person whose message
    it answers (channels), and the first reply in a thread carries a one-time
    etiquette note about the one-at-a-time behavior (``intro_note`` overrides that
    note for surfaces with different etiquette, e.g. the plain DM).
    ``placeholder_ts`` reuses a placeholder a caller already posted (e.g. before a
    slow file download).

    Failure discipline: "the turn failed" and "the turn succeeded but Slack
    wouldn't take the reply" are different failures. Only the former may advise
    a retry — after the backend committed the turn, a retry runs a duplicate
    turn against state the user never saw, so the post-side fallback degrades
    to plain text instead.
    """

    if placeholder_ts is None:
        placeholder_ts = post_placeholder(client, channel, thread_ts)
        if placeholder_ts is None:
            return

    def update(text_: str, blocks_: list[dict] | None = None) -> bool:
        """chat_update that reports failure instead of raising (never strands
        the placeholder by letting an exception skip later recovery)."""

        try:
            if blocks_ is None:
                client.chat_update(channel=channel, ts=placeholder_ts, text=text_)
            else:
                client.chat_update(
                    channel=channel, ts=placeholder_ts, text=text_, blocks=blocks_
                )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat_update failed in %s: %s", channel, exc)
            return False

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
    except Exception as exc:  # noqa: BLE001 — last line of defense for a bg turn
        logger.exception("turn failed in %s: %s", channel, exc)
        update(turn_error_text(exc))
        return

    # The turn is committed backend-side; deliver_turn_result owns the
    # never-say-try-again degradation ladder from here.
    # Stamp the case pointer only on the opening reply (thread = case).
    opening_case_id = store.get(team_id, channel, thread_ts) if first_turn else None

    def decorate(blocks: list[dict]) -> None:
        if mention_user:
            _address(blocks, mention_user)
        if first_turn and (intro_note or mention_user):
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": intro_note or _INTRO_WARNING}
                    ],
                }
            )

    deliver_turn_result(
        update, result, case_id=opening_case_id, decorate=decorate
    )
