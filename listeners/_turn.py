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
from dataclasses import dataclass, field

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


class _ThreadSerializer:
    """One lock per Slack thread, so turns on the same thread run one at a time.

    A turn is stateful — it advances the case version — and the backend guards
    that with optimistic concurrency: two turns for the same case that overlap
    race, and the loser is rejected with a 409 (``expected vN, db vN+…``). Turns
    can take 20s+, so a second @mention (or an impatient re-mention) while the
    first is still running is easy to trigger. Serializing per thread makes the
    second turn *wait* for the first instead of colliding, and also closes the
    first-message race where two concurrent messages would each open a case for
    the same thread.

    Locks are created on demand and evicted LRU once idle (never while held), so
    the registry stays bounded over a long-running process.
    """

    def __init__(self, maxsize: int = 4096) -> None:
        self._locks: "OrderedDict[str, threading.Lock]" = OrderedDict()
        self._guard = threading.Lock()
        self._max = maxsize

    def lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            self._locks.move_to_end(key)
            # Evict the coldest locks, but only ones nobody holds — removing a
            # held lock would let a new acquirer make a *second* lock for the
            # same key and defeat the serialization.
            while len(self._locks) > self._max:
                old_key, old_lock = next(iter(self._locks.items()))
                if old_lock.locked():
                    break
                self._locks.popitem(last=False)
            return lock


# Serializes turns per Slack thread (see _ThreadSerializer). Threads are keyed by
# team+channel+thread, so different threads still run concurrently.
_serializer = _ThreadSerializer()

FOLDED_NOTE = ":link: Folded into FaultMaven's reply above."


@dataclass
class _TurnRequest:
    """One surface's request to advance a thread, plus how to reply to it.

    Carries its own placeholder ``ts`` so that when several requests for the same
    thread are coalesced into one turn, each still gets an answer: the batch's
    result lands on the first request's placeholder, and the rest are marked
    ``FOLDED_NOTE`` (their input was merged in, not dropped).
    """

    client: WebClient
    channel: str
    thread_ts: str
    team_id: str
    placeholder_ts: str
    text: str
    pasted_content: str | None = None
    source_url: str | None = None
    prior_context: str | None = None
    files: list[tuple[str, bytes, str]] | None = field(default=None)


def _merge_requests(reqs: list[_TurnRequest]) -> _TurnRequest:
    """Combine coalesced requests into one turn — losing no text or files."""

    base = reqs[0]
    if len(reqs) == 1:
        return base
    texts = [r.text for r in reqs if r.text and r.text.strip()]
    pasted = [r.pasted_content for r in reqs if r.pasted_content]
    files: list[tuple[str, bytes, str]] = []
    for r in reqs:
        files.extend(r.files or [])
    return _TurnRequest(
        client=base.client,
        channel=base.channel,
        thread_ts=base.thread_ts,
        team_id=base.team_id,
        placeholder_ts=base.placeholder_ts,
        text="\n\n".join(texts),
        pasted_content="\n\n".join(pasted) or None,
        # prior_context/source_url only matter on case creation (the first turn),
        # so the first non-empty wins.
        source_url=next((r.source_url for r in reqs if r.source_url), None),
        prior_context=next((r.prior_context for r in reqs if r.prior_context), None),
        files=files or None,
    )


class _CoalescingRunner:
    """Per-thread single-flight executor that coalesces bursts into one turn.

    The first request for an idle thread runs immediately (on a daemon worker, so
    the Bolt listener thread returns at once). Requests that arrive while that
    turn is in flight are *collected*; when it finishes they're merged into ONE
    combined next turn — so a burst of N replies (several people answering the
    same question at once) costs one extra turn, not N, and nobody's text or
    files are dropped. Different threads drain on independent workers.
    """

    def __init__(self) -> None:
        # key present ⇒ a worker is draining it; value = requests queued for the
        # *next* turn on that thread.
        self._pending: dict[str, list[_TurnRequest]] = {}
        self._guard = threading.Lock()

    def submit(self, key: str, req: _TurnRequest, run_batch) -> None:
        with self._guard:
            if key in self._pending:
                self._pending[key].append(req)
                return
            self._pending[key] = []
        threading.Thread(
            target=self._drain, args=(key, [req], run_batch), daemon=True
        ).start()

    def _drain(self, key: str, batch: list[_TurnRequest], run_batch) -> None:
        while batch:
            try:
                run_batch(batch)
            except Exception as exc:  # noqa: BLE001 — a bad turn must not kill the loop
                logger.exception("coalesced batch failed on %s: %s", key, exc)
            with self._guard:
                queued = self._pending.get(key)
                if queued:
                    self._pending[key] = []
                    batch = queued
                else:
                    del self._pending[key]
                    batch = None  # type: ignore[assignment]


_runner = _CoalescingRunner()


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

    # Serialize the whole find-or-create + submit for this thread: two turns on
    # the same thread must not overlap (backend OCC 409), and two first-messages
    # must not both open a case. Different threads keep running concurrently.
    key = "\x00".join((team_id, channel_id, thread_ts))
    with _serializer.lock_for(key):
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


def _process_channel_batch(
    fm: FaultMavenClient, store: CaseStore, batch: list[_TurnRequest]
) -> None:
    """Run one (possibly coalesced) turn and post it — the runner's unit of work.

    The batch's merged input becomes a single turn; its result lands on the first
    request's placeholder and the rest are marked ``FOLDED_NOTE`` so every
    replier gets closure. A failure marks them all with the error text.
    """

    req = _merge_requests(batch)
    client = req.client
    try:
        result = run_turn(
            fm,
            store,
            team_id=req.team_id,
            channel_id=req.channel,
            thread_ts=req.thread_ts,
            text=req.text,
            pasted_content=req.pasted_content,
            source_url=req.source_url,
            prior_context=req.prior_context,
            files=req.files,
        )
        client.chat_update(
            channel=req.channel,
            ts=req.placeholder_ts,
            text=result.agent_response[:300],
            blocks=build_turn_blocks(result),
        )
        for extra in batch[1:]:
            extra.client.chat_update(
                channel=extra.channel, ts=extra.placeholder_ts, text=FOLDED_NOTE
            )
    except Exception as exc:  # noqa: BLE001 — last line of defense for a bg turn
        logger.exception("turn failed in %s: %s", req.channel, exc)
        for r in batch:
            r.client.chat_update(
                channel=r.channel, ts=r.placeholder_ts, text=TURN_ERROR_TEXT
            )


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
    """Acknowledge a reply and hand it to the per-thread coalescing runner.

    Posts the "investigating…" placeholder now (instant feedback), then enqueues
    the turn and returns — the runner drains it on a background worker, so the
    Bolt listener thread is freed immediately. If a turn is already in flight for
    this thread, this reply is *coalesced* into the next one (see
    :class:`_CoalescingRunner`) rather than racing it. ``placeholder_ts`` reuses
    a placeholder a caller already posted (e.g. before a slow file download).
    """

    if placeholder_ts is None:
        placeholder_ts = post_placeholder(client, channel, thread_ts)
        if placeholder_ts is None:
            return

    req = _TurnRequest(
        client=client,
        channel=channel,
        thread_ts=thread_ts,
        team_id=team_id,
        placeholder_ts=placeholder_ts,
        text=text,
        pasted_content=pasted_content,
        source_url=source_url,
        prior_context=prior_context,
        files=files,
    )
    key = "\x00".join((team_id, channel, thread_ts))
    _runner.submit(key, req, lambda b: _process_channel_batch(fm, store, b))
