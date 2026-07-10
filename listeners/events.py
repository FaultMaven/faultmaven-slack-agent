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

from ._turn import Dedup, post_placeholder, run_gated, run_turn_and_post

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


def is_dm_summons(event: dict) -> bool:
    """A top-level message in a DM / the assistant panel that should open an
    investigation (channel_type "im", no ``thread_ts``).

    Bolt's Assistant middleware claims an ``im`` message only once it carries a
    ``thread_ts`` (a "reply"/"ask"), so a plainly-typed first message reaches no
    handler otherwise. We open the case here, rooted at the message; follow-up
    replies then flow through the Assistant handler (im + thread_ts). Ignores the
    bot's own posts and non-message subtypes (edits, joins…).
    """

    return (
        event.get("channel_type") == "im"
        and not event.get("thread_ts")
        and not event.get("bot_id")
        and event.get("subtype") in (None, "file_share")
    )


def register_events(app: App, fm: FaultMavenClient, store: CaseStore) -> None:
    dedup = Dedup()
    followup_dedup = Dedup()

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
        # context.team_id (the app's install team) keys the thread's case and gate
        # uniformly across surfaces — event["team"] is the *sender's* team and can
        # differ in Slack Connect, which would fork the case/gate.
        team_id = context.team_id or ""
        # A mention may be top-level (use its ts) or already inside a thread.
        thread_ts = event.get("thread_ts") or event["ts"]

        def work() -> None:
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

        # Reserve the thread and run on a background worker; if a turn is already
        # running, this one is skipped (⏭️).
        run_gated(
            client, team_id=team_id, channel=channel, thread_ts=thread_ts,
            skip_ts=event.get("ts"), work=work,
        )

    @app.event("message")
    def on_thread_message(
        event: dict, context: BoltContext, client: WebClient, logger: Logger
    ) -> None:
        # A top-level message in a DM / the assistant panel (channel_type "im",
        # no thread_ts). Bolt's Assistant middleware only claims im messages that
        # already carry a thread_ts (i.e. a "reply"/"ask"), so a plainly-typed
        # first message would otherwise reach no handler at all. Treat it as a
        # summons: open the investigation in a thread rooted at this message —
        # every follow-up reply in that thread then flows through the Assistant
        # handler (im + thread_ts), keyed to the same case.
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
                files = (
                    download_message_files(client.token, event) if has_files else []
                )
                run_turn_and_post(
                    client,
                    fm,
                    store,
                    channel=channel,
                    thread_ts=thread_ts,
                    team_id=team_id,
                    text=text or "Please investigate this.",
                    files=files or None,
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
        # Only act on threads that are already an investigation.
        if store.get(team_id, channel, thread_ts) is None:
            return
        if followup_dedup.is_duplicate(f"{channel}:{event.get('ts')}"):
            return

        # Decide there's something to investigate BEFORE reserving the thread, so
        # a content-free reply (whitespace, or only another user's mention) can't
        # hold the gate and cause a concurrent real reply to be skipped.
        text = clean_mention(event.get("text") or "").strip()
        has_files = bool(event.get("files"))
        if not text and not has_files:
            return

        def work() -> None:
            files = download_message_files(client.token, event) if has_files else []
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

        # Reserve the thread and run in the background; if a turn is already
        # running, skip this reply (⏭️) — the sender waits, then resends.
        run_gated(
            client, team_id=team_id, channel=channel, thread_ts=thread_ts,
            skip_ts=event.get("ts"), work=work,
        )
