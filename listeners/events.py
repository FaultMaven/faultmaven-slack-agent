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

from logging import Logger

from slack_bolt import App, BoltContext
from slack_sdk import WebClient

from faultmaven import FaultMavenClient
from rendering import clean_mention
from slack_files import download_message_files
from store import CaseStore

from ._turn import (
    Dedup,
    end_turn,
    post_placeholder,
    run_turn_and_post,
    try_begin_turn,
)

# Cap the replayed-context size (the backend size-guards turn fields too).
_THREAD_CONTEXT_LIMIT = 8000


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


def is_thread_followup_candidate(event: dict, *, bot_user_id: str | None) -> bool:
    """Cheap gate: is this a plain human reply *inside a thread* worth checking?

    Filters out everything that must NOT auto-continue an investigation, before
    the (slightly less cheap) store lookup the caller then does:

    - the bot's own posts (``bot_id``),
    - non-message subtypes (edits, deletes, joins…) — only a normal message or a
      ``file_share`` carries new user input,
    - DMs (``channel_type == "im"``) — the Assistant surface owns those,
    - top-level channel messages (no ``thread_ts``) — we never start an
      investigation from ambient channel chatter, only continue one in a thread,
    - messages that mention the bot — ``app_mention`` owns those (and does the
      first-summons catch-up read), so we don't double-process.
    """

    if event.get("bot_id"):
        return False
    if event.get("subtype") not in (None, "file_share"):
        return False
    if event.get("channel_type") == "im":
        return False
    if not event.get("thread_ts"):
        return False
    text = event.get("text") or ""
    if bot_user_id and f"<@{bot_user_id}>" in text:
        return False
    return True


def register_events(app: App, fm: FaultMavenClient, store: CaseStore) -> None:
    dedup = Dedup()
    followup_dedup = Dedup()

    @app.event("app_mention")
    def on_app_mention(event: dict, client: WebClient, logger: Logger) -> None:
        # Ignore the bot's own messages; de-dupe Slack retries.
        if event.get("bot_id"):
            return
        if dedup.is_duplicate(f"{event.get('channel')}:{event.get('ts')}"):
            return

        channel = event["channel"]
        team_id = event.get("team", "")
        # A mention may be top-level (use its ts) or already inside a thread.
        thread_ts = event.get("thread_ts") or event["ts"]

        # Reserve the thread; if a turn is already running, skip this one (⏭️).
        if not try_begin_turn(
            client, team_id=team_id, channel=channel, thread_ts=thread_ts,
            skip_ts=event.get("ts"),
        ):
            return
        try:
            text = clean_mention(event.get("text", "")) or (
                "Please investigate this thread."
            )
            # Placeholder up front, before the (possibly slow) catch-up read and
            # file download, so the summons is acknowledged immediately.
            placeholder_ts = post_placeholder(client, channel, thread_ts)
            if placeholder_ts is None:
                return  # can't post here — /invite @FaultMaven

            # First summons into a thread → replay the prior discussion.
            prior_context = None
            if store.get(team_id, channel, thread_ts) is None:
                prior_context = _fetch_thread_context(
                    client, channel, thread_ts, exclude_ts=event.get("ts")
                )

            # Files attached to the mention are forwarded as evidence (no-ops to
            # [] when there are none).
            files = download_message_files(client.token, event)

            run_turn_and_post(
                client,
                fm,
                store,
                channel=channel,
                thread_ts=thread_ts,
                team_id=team_id,
                text=text,
                prior_context=prior_context,
                files=files or None,
                placeholder_ts=placeholder_ts,
                mention_user=event.get("user"),
            )
        finally:
            end_turn(team_id, channel, thread_ts)

    @app.event("message")
    def on_thread_message(
        event: dict, context: BoltContext, client: WebClient, logger: Logger
    ) -> None:
        # Continue an *existing* investigation from a plain thread reply — no
        # re-@mention needed. Everything else is ignored (no firehose).
        if not is_thread_followup_candidate(
            event, bot_user_id=context.bot_user_id
        ):
            return

        channel = event["channel"]
        thread_ts = event["thread_ts"]
        team_id = event.get("team", "")
        # Only act on threads that are already an investigation.
        if store.get(team_id, channel, thread_ts) is None:
            return
        if followup_dedup.is_duplicate(f"{channel}:{event.get('ts')}"):
            return

        # Reserve the thread; if a turn is already running, skip this reply (⏭️)
        # — the sender should wait for FaultMaven's reply, then resend.
        if not try_begin_turn(
            client, team_id=team_id, channel=channel, thread_ts=thread_ts,
            skip_ts=event.get("ts"),
        ):
            return
        try:
            text = clean_mention(event.get("text") or "").strip()
            files = download_message_files(client.token, event)
            if not text and not files:
                return  # nothing new to add (e.g. an emoji-only reply)

            run_turn_and_post(
                client,
                fm,
                store,
                channel=channel,
                thread_ts=thread_ts,
                team_id=team_id,
                text=text or "Please continue the investigation with this evidence.",
                files=files or None,
                mention_user=event.get("user"),
            )
        finally:
            end_turn(team_id, channel, thread_ts)
