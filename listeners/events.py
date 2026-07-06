"""Channel ``app_mention`` — the collaborative war-room surface.

Strictly mention-driven (no ``message.channels`` subscription). Replies land in
the summoned thread so the parent channel stays quiet. On the first summons into
a thread, the prior thread discussion is replayed to the engine so it isn't
blind to what preceded the mention.
"""

from __future__ import annotations

from logging import Logger

from slack_bolt import App
from slack_sdk import WebClient

from faultmaven import FaultMavenClient
from rendering import clean_mention
from slack_files import download_message_files
from store import CaseStore

from ._turn import Dedup, post_placeholder, run_turn_and_post

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


def register_events(app: App, fm: FaultMavenClient, store: CaseStore) -> None:
    dedup = Dedup()

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
        text = clean_mention(event.get("text", "")) or (
            "Please investigate this thread."
        )

        # Post the placeholder up front, before the (possibly slow) catch-up read
        # and file download, so the summons is acknowledged immediately.
        placeholder_ts = post_placeholder(client, channel, thread_ts)
        if placeholder_ts is None:
            return  # can't post here — /invite @FaultMaven

        # First summons into a thread → replay the prior discussion (catch-up).
        prior_context = None
        if store.get(team_id, channel, thread_ts) is None:
            prior_context = _fetch_thread_context(
                client, channel, thread_ts, exclude_ts=event.get("ts")
            )

        # Files attached to the mention itself are forwarded as evidence
        # (download_message_files no-ops to [] when there are none).
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
        )
