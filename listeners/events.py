"""Channel surfaces — the collaborative war-room.

Two entry points, both landing replies in the summoned thread so the parent
channel stays quiet:

- ``app_mention`` — the **summon**: ``@FaultMaven`` starts (or re-engages) an
  investigation in a thread. On the first summons the prior thread discussion is
  replayed as catch-up so the engine isn't blind to what preceded the mention.
- ``message`` — **active-thread continuity**: once a thread is an investigation,
  plain replies in *that* thread continue it without re-@mentioning. Every other
  channel message is ignored (the bot only acts on threads it already owns), so
  there's no ambient/firehose behavior.
"""

from __future__ import annotations

import logging
import re
from logging import Logger

from slack_bolt import App, BoltContext
from slack_sdk import WebClient

from faultmaven import FaultMavenClient
from rendering import clean_mention
from slack_files import download_message_content
from store import CaseStore

from ._turn import (
    case_gone_text,
    Dedup,
    RESTARTED_AFTER_MISSING_CASE,
    is_thread_busy,
    mark_skipped,
    post_placeholder,
    resolve_query,
    run_gated,
    run_turn_and_post,
    skipped_files_note,
    SUMMONS_TEXT,
    UNREADABLE_FILES_TEXT,
)


#: For the module-level helpers; Bolt injects its own logger into handlers.
logger = logging.getLogger(__name__)


def _post_note(
    client: WebClient, channel: str, thread_ts: str, text: str
) -> bool:
    """Best-effort threaded info note (e.g. skipped-attachments); never raises.

    Reports whether it landed, so a caller recording "this thread has been
    told" only records it when the thread actually was.
    """

    try:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        return True
    except Exception:  # noqa: BLE001 — a notice must never cost the turn
        return False

# Cap the replayed-context size (the backend size-guards turn fields too).
_THREAD_CONTEXT_LIMIT = 8000

# One-time etiquette note for a plain-DM investigation: in a DM the natural
# reply box is the main composer, but a top-level composer message is a NEW
# summons (new case) — without this pointer a user answering FaultMaven's
# question in the composer forks the investigation.
_DM_INTRO = (
    ":bulb: Reply *in this thread* to continue this investigation — a new "
    "message in the box below starts a separate one."
)


def _fetch_thread_context(
    client: WebClient, channel: str, thread_ts: str, *, exclude_ts: str | None
) -> str | None:
    """Return prior human thread messages as a context string, or None.

    Excludes the triggering mention and any bot-authored messages. Degrades to
    None (no context) on *any* failure — a Slack API error (missing history
    scope, ``not_in_channel``) or a transport error (timeout, reset) — rather
    than propagating: the caller has already posted the placeholder, so an
    unhandled raise here would strand it with no reply.
    """

    try:
        resp = client.conversations_replies(
            channel=channel, ts=thread_ts, limit=50
        )
    except Exception:  # noqa: BLE001 — catch-up is best-effort; never fail the turn
        return None

    lines: list[str] = []
    for message in resp.get("messages", []):
        if message.get("ts") == exclude_ts or message.get("bot_id"):
            continue
        text = clean_mention(message.get("text", ""))
        if text:
            lines.append(text)
    if not lines:
        return None
    return "\n".join(lines)[:_THREAD_CONTEXT_LIMIT]


# How far back the recovery probe reads. FaultMaven's case pointer rides on the
# opening reply of an investigation, so it sits near the top of the replies.
_RECOVERY_PROBE_LIMIT = 200

# The case pointer as ``rendering.build_turn_blocks`` writes it on a case's
# opening reply. It is the agent's own output, posted by the agent's own user,
# so it is proof of something no other signal here can establish: that a case
# was created for this thread, and which one.
_CASE_POINTER_RE = re.compile(r":card_index_dividers: `([A-Za-z0-9][\w.-]{0,63})`")


def case_id_in_message(message: dict, *, bot_user_id, bot_id) -> str | None:
    """The case id this app announced in ``message``, if it announced one.

    Two things have to hold. The message must be **ours** — an incident thread
    is usually full of other bots, and one of them is generally what started it,
    so a bare ``bot_id`` would match the alert rather than the investigation.
    And it must carry the case pointer, not merely have come from us: the
    placeholder, the unreadable-file decline and the skipped-attachment note are
    all posted *before or instead of* a case existing, so "the agent spoke here"
    is not evidence that it ever opened one.
    """

    if message.get("user") != bot_user_id and (
        not bot_id or message.get("bot_id") != bot_id
    ):
        return None
    for block in message.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for element in block.get("elements") or []:
            if not isinstance(element, dict):
                continue
            found = _CASE_POINTER_RE.search(element.get("text") or "")
            if found:
                return found.group(1)
    return None


def recover_lost_case(
    client: WebClient,
    store: CaseStore,
    probed: "Dedup",
    *,
    team_id: str,
    channel: str,
    thread_ts: str,
    bot_user_id: str | None,
    bot_id: str | None,
) -> bool:
    """Re-link a thread whose mapping was lost, from the thread itself.

    A tombstone is only written when a turn 404s, so an *absent* row cannot mean
    that. It means either a thread the agent never touched, or one it did whose
    record is gone — an unpersisted ``CASE_STORE_PATH`` (``docs/HOSTING.md``),
    which loses every mapping on restart. The difference is not knowable from
    here, but it is written in the thread: the agent announced the case id when
    it opened the investigation, and that message is still there.

    So the case is not declared lost, it is looked up. The thread re-links to
    the case it always had and the reply is answered as the ordinary follow-up
    it is. If that case really is gone too, the turn 404s and the tombstone path
    takes over with the right message — this only removes the guessing.

    Seeded on re-link: the case demonstrably has turns behind it (we just read
    the reply announcing it), so the thread catch-up an unseen thread gets would
    re-send it its own history as fresh evidence.

    One read per thread per process, and the key is recorded only once the probe
    has actually answered — a read that fails leaves the thread to try again
    rather than spending its one chance on an error.
    """

    key = f"{team_id}:{channel}:{thread_ts}"
    if probed.seen(key):
        return False
    try:
        resp = client.conversations_replies(
            channel=channel, ts=thread_ts, limit=_RECOVERY_PROBE_LIMIT
        )
    except Exception:  # noqa: BLE001 — a failed probe leaves the thread unknown
        return False
    case_id = next(
        (
            found
            for message in resp.get("messages", [])
            if (
                found := case_id_in_message(
                    message, bot_user_id=bot_user_id, bot_id=bot_id
                )
            )
        ),
        None,
    )
    if case_id is None:
        probed.is_duplicate(key)  # answered: not ours, and cheap to skip after
        return False
    store.put(team_id, channel, thread_ts, case_id)
    store.mark_seeded(team_id, channel, thread_ts)
    logger.info(
        "Recovered case %s for thread %s from its own history", case_id, thread_ts
    )
    return True


def is_thread_followup_candidate(event: dict, *, bot_user_id: str | None) -> bool:
    """Cheap gate: is this a plain human reply *inside a thread* worth checking?

    Filters out everything that must NOT auto-continue an investigation, before
    the (slightly less cheap) store lookup the caller then does:

    - the bot's own posts (``bot_id``),
    - non-message subtypes (edits, deletes, joins…) — a normal message, a
      ``file_share``, or a ``thread_broadcast`` ("Also send to #channel", used
      constantly in incident threads) carries new user input; nothing else does,
    - DMs (``channel_type == "im"``) — the Assistant surface owns those,
    - top-level channel messages (no ``thread_ts``) — we never start an
      investigation from ambient channel chatter, only continue one in a thread,
    - messages that mention the bot — ``app_mention`` owns those (and does the
      first-summons catch-up read), so we don't double-process.
    """

    if event.get("bot_id"):
        return False
    if event.get("subtype") not in (None, "file_share", "thread_broadcast"):
        return False
    if event.get("channel_type") == "im":
        return False
    if not event.get("thread_ts"):
        return False
    text = event.get("text") or ""
    if bot_user_id and f"<@{bot_user_id}>" in text:
        return False
    return True


def is_dm_summons(event: dict) -> bool:
    """A first message in the plain DM composer that should open an investigation
    (channel_type "im", no ``thread_ts``).

    Messages in the assistant Chat carry a ``thread_ts`` (the container is
    thread-based) and are claimed by Bolt's Assistant middleware; a plainly-typed
    DM message has none, so it would reach no handler otherwise. We open the case
    here, rooted at the message; follow-up replies then flow through the Assistant
    handler (im + thread_ts). Ignores the bot's own posts and non-message subtypes
    (edits, joins…).
    """

    return (
        event.get("channel_type") == "im"
        and not event.get("thread_ts")
        and not event.get("bot_id")
        and event.get("subtype") in (None, "file_share")
    )


def mention_text(cleaned: str, *, seeded: bool) -> str:
    """The turn's ``query`` for an ``app_mention``.

    Real text passes through. A bare mention on a thread that is not yet a
    SEEDED investigation is a summons (:data:`SUMMONS_TEXT`) — "seeded", not
    merely mapped: a mapping whose opening turn failed still needs the summons
    and the catch-up read on the retry. A bare mention on a seeded thread is the
    user poking the assistant, not new data, and sends an EMPTY turn, which the
    backend answers with a state-aware orientation (``run_turn_and_post`` falls
    back to the summons on a backend that predates that).
    """
    if cleaned:
        return cleaned
    return "" if seeded else SUMMONS_TEXT


def register_events(app: App, fm: FaultMavenClient, store: CaseStore) -> None:
    dedup = Dedup()
    followup_dedup = Dedup()
    probed_threads = Dedup()

    @app.event("app_mention")
    def on_app_mention(
        event: dict, context: BoltContext, client: WebClient, logger: Logger
    ) -> None:
        # Ignore the bot's own messages; de-dupe Slack retries.
        if event.get("bot_id"):
            return
        if dedup.is_duplicate(f"{event.get('channel')}:{event.get('ts')}"):
            return

        channel = event["channel"]
        # context.team_id (the app's install team = the Slack workspace, which
        # binds to a FaultMaven Team — not itself a FaultMaven Team) keys the
        # thread's case and gate uniformly across surfaces — event["team"] is the
        # *sender's* team and can differ in Slack Connect, forking the case/gate.
        team_id = context.team_id or ""
        # A mention may be top-level (use its ts) or already inside a thread.
        thread_ts = event.get("thread_ts") or event["ts"]

        def work() -> None:
            cleaned = clean_mention(event.get("text", ""))
            # Placeholder up front, before the (possibly slow) catch-up read and
            # file download, so the summons is acknowledged immediately.
            placeholder_ts = post_placeholder(client, channel, thread_ts)
            if placeholder_ts is None:
                return  # can't post here — /invite @FaultMaven

            # One read of the thread's state, used for both decisions below.
            seeded = store.is_seeded(team_id, channel, thread_ts)
            # Read BEFORE the turn. The flag outlives the write that opens
            # the replacement case and is retired only when a turn lands, so a
            # restart whose first turn fails still explains itself on the retry
            # instead of continuing silently on a case nobody was told about.
            restarted = store.restart_pending(team_id, channel, thread_ts)

            # Replay the prior discussion until the case has actually landed a
            # turn (unseeded): a mapping whose first submit failed still needs
            # the catch-up on the retry, or the engine investigates blind.
            prior_context = None
            if not seeded:
                prior_context = _fetch_thread_context(
                    client, channel, thread_ts, exclude_ts=event.get("ts")
                )

            # Pasted snippets come back as text so the backend sees paste
            # provenance, not a fake "Untitled" file upload; attachments
            # beyond the one-file-per-turn limit come back as names.
            files, snippet_text, skipped = download_message_content(
                client.token, event
            )
            if skipped:
                _post_note(client, channel, thread_ts, skipped_files_note(skipped))
            # File(s) present but none readable, and no text: decline, as every
            # other surface does, instead of sending a turn with nothing on it.
            if not cleaned and event.get("files") and not files and not snippet_text:
                try:
                    client.chat_update(
                        channel=channel, ts=placeholder_ts, text=UNREADABLE_FILES_TEXT
                    )
                except Exception as exc:  # noqa: BLE001 — decline must never strand the placeholder
                    logger.warning("decline update failed in %s: %s", channel, exc)
                return
            run_turn_and_post(
                client,
                fm,
                store,
                channel=channel,
                thread_ts=thread_ts,
                team_id=team_id,
                text=mention_text(cleaned, seeded=seeded),
                pasted_content=snippet_text,
                prior_context=prior_context,
                files=files or None,
                placeholder_ts=placeholder_ts,
                mention_user=event.get("user"),
                intro_note=RESTARTED_AFTER_MISSING_CASE if restarted else None,
                empty_turn_fallback=SUMMONS_TEXT,
            )

        run_gated(
            client, team_id=team_id, channel=channel, thread_ts=thread_ts,
            skip_ts=event.get("ts"), work=work,
        )

    @app.event("message")
    def on_thread_message(
        event: dict, context: BoltContext, client: WebClient, logger: Logger
    ) -> None:
        # A first message in the plain DM composer (channel_type "im", no
        # thread_ts). Assistant-Chat messages carry a thread_ts and are claimed by
        # Bolt's Assistant middleware; a plainly-typed DM has none, so it would
        # otherwise reach no handler. Treat it as a summons: open the investigation
        # in a thread rooted at this message — every follow-up reply in that thread
        # then flows through the Assistant handler (im + thread_ts), same case.
        if is_dm_summons(event):
            channel = event["channel"]
            team_id = context.team_id or ""
            thread_ts = event["ts"]  # this message becomes the thread root
            if followup_dedup.is_duplicate(f"{channel}:{event.get('ts')}"):
                return
            text = clean_mention(event.get("text") or "").strip()
            has_files = bool(event.get("files"))
            if not text and not has_files:
                return

            def dm_work() -> None:
                # Acknowledge up front — the file download below can be slow, so
                # the user sees a placeholder rather than silence (mirrors
                # on_app_mention).
                placeholder_ts = post_placeholder(client, channel, thread_ts)
                if placeholder_ts is None:
                    return
                files: list = []
                snippet_text: str | None = None
                if has_files:
                    files, snippet_text, skipped = download_message_content(
                        client.token, event
                    )
                    if skipped:
                        _post_note(
                            client, channel, thread_ts, skipped_files_note(skipped)
                        )
                # File(s) present but unreadable and no text → decline instead of
                # opening a blank case (mirrors the Assistant surface).
                query = resolve_query(
                    text or None, downloaded_files=bool(files or snippet_text)
                )
                if query is None:
                    try:
                        client.chat_update(
                            channel=channel,
                            ts=placeholder_ts,
                            text=UNREADABLE_FILES_TEXT,
                        )
                    except Exception as exc:  # noqa: BLE001 — decline must never strand the placeholder
                        logger.warning("decline update failed in %s: %s", channel, exc)
                    return
                run_turn_and_post(
                    client,
                    fm,
                    store,
                    channel=channel,
                    thread_ts=thread_ts,
                    team_id=team_id,
                    text=query,
                    pasted_content=snippet_text,
                    files=files or None,
                    placeholder_ts=placeholder_ts,
                    intro_note=_DM_INTRO,
                )

            run_gated(
                client, team_id=team_id, channel=channel, thread_ts=thread_ts,
                skip_ts=event.get("ts"), work=dm_work,
            )
            return

        # Continue an *existing* investigation from a plain thread reply — no
        # re-@mention needed. Everything else is ignored (no firehose).
        if not is_thread_followup_candidate(
            event, bot_user_id=context.bot_user_id
        ):
            return

        channel = event["channel"]
        thread_ts = event["thread_ts"]
        team_id = context.team_id or ""  # install team — see on_app_mention

        # Decide there's something to investigate BEFORE reserving the thread, so
        # a content-free reply (whitespace, or only another user's mention) can't
        # hold the gate and cause a concurrent real reply to be skipped.
        text = clean_mention(event.get("text") or "").strip()
        has_files = bool(event.get("files"))

        def work() -> None:
            files: list = []
            snippet_text: str | None = None
            if has_files:
                files, snippet_text, skipped = download_message_content(
                    client.token, event
                )
                if skipped:
                    _post_note(
                        client, channel, thread_ts, skipped_files_note(skipped)
                    )
            # File(s) attached but none ingestible, and no text: say so instead
            # of submitting a phantom-evidence turn the engine can only be
            # confused by (mirrors the DM-summons and Assistant declines).
            if not text and not files and not snippet_text:
                try:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=UNREADABLE_FILES_TEXT,
                    )
                except Exception as exc:  # noqa: BLE001 — decline is best-effort
                    logger.warning("decline post failed in %s: %s", channel, exc)
                return
            # A mapped-but-unseeded thread means the opening turn never landed
            # — re-deliver the catch-up context that turn was carrying.
            prior_context = None
            if not store.is_seeded(team_id, channel, thread_ts):
                prior_context = _fetch_thread_context(
                    client, channel, thread_ts, exclude_ts=event.get("ts")
                )
            run_turn_and_post(
                client,
                fm,
                store,
                channel=channel,
                thread_ts=thread_ts,
                team_id=team_id,
                text=text or "Please continue the investigation with this data.",
                pasted_content=snippet_text,
                files=files or None,
                prior_context=prior_context,
                mention_user=event.get("user"),
            )


        # Only act on threads that are already an investigation. During the
        # case-OPENING turn the mapping doesn't exist yet (it's committed when
        # the first turn lands) but the gate is held — a reply in that window
        # is a real follow-up, so give it the ⏭️ skip signal instead of the
        # silent drop an unknown thread gets.
        if store.get(team_id, channel, thread_ts) is None:
            if is_thread_busy(team_id, channel, thread_ts) and event.get("ts"):
                if not followup_dedup.is_duplicate(f"{channel}:{event.get('ts')}"):
                    mark_skipped(client, channel, event["ts"])
                return
            if not text and not has_files:
                return  # nothing was said; nothing is owed
            # Ambient chatter reaches here on every reply in every thread the
            # agent can see, so the two cheapest reads decide it: a local flag,
            # and an in-memory note of threads already looked at and found to
            # belong to someone else. Neither touches Slack.
            if not store.is_unlinked(
                team_id, channel, thread_ts
            ) and probed_threads.seen(f"{team_id}:{channel}:{thread_ts}"):
                return
            if followup_dedup.is_duplicate(f"{channel}:{event.get('ts')}"):
                return

            def orphan_work() -> None:
                """Answer a reply in a thread with no mapping.

                Gated like a turn, because it can become one: recovery re-links
                the thread and the reply is then answered normally. The gate
                also serialises it against an @mention opening a case here, so
                neither can act on state the other has just replaced.
                """

                # Under the gate now — an @mention may have opened a case for
                # this thread while this was queued behind it.
                if store.get(team_id, channel, thread_ts) is not None:
                    work()
                    return
                if store.is_unlinked(team_id, channel, thread_ts):
                    # A thread that WAS ours, whose case is confirmed gone. The
                    # reply can't be answered — the investigation it belongs to
                    # is not there — but it was written to us, and dropping it
                    # the way an unknown thread is dropped is what left people
                    # typing into silence. Explain once, then leave the thread
                    # alone; an @mention is how it comes back.
                    if store.claim_unlink_notice(team_id, channel, thread_ts):
                        if not _post_note(
                            client, channel, thread_ts, case_gone_text(channel)
                        ):
                            # Slack refused it; the thread still hasn't been told.
                            store.release_unlink_notice(
                                team_id, channel, thread_ts
                            )
                    return
                # No row at all. Before treating this as a stranger's thread,
                # ask the thread whether it used to be ours.
                if recover_lost_case(
                    client, store, probed_threads,
                    team_id=team_id, channel=channel, thread_ts=thread_ts,
                    bot_user_id=context.bot_user_id, bot_id=context.bot_id,
                ):
                    work()  # live again — an ordinary follow-up

            run_gated(
                client, team_id=team_id, channel=channel, thread_ts=thread_ts,
                skip_ts=None, work=orphan_work,
            )
            return
        if followup_dedup.is_duplicate(f"{channel}:{event.get('ts')}"):
            return
        if not text and not has_files:
            return

        # Reserve the thread and run in the background; if a turn is already
        # running, skip this reply (⏭️) — the sender waits, then resends.
        run_gated(
            client, team_id=team_id, channel=channel, thread_ts=thread_ts,
            skip_ts=event.get("ts"), work=work,
        )
