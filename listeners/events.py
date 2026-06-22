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
from slack_sdk.errors import SlackApiError

from faultmaven import FaultMavenClient
from rendering import build_turn_blocks, clean_mention
from store import CaseStore

from ._turn import Dedup, run_turn

# Cap the replayed-context size (the backend size-guards turn fields too).
_THREAD_CONTEXT_LIMIT = 8000


def _fetch_thread_context(
    client: WebClient, channel: str, thread_ts: str, *, exclude_ts: str | None
) -> str | None:
    """Return prior human thread messages as a context string, or None.

    Excludes the triggering mention and any bot-authored messages. Degrades to
    None (no context) on any Slack API error — e.g. missing history scope or
    ``not_in_channel`` — rather than failing the turn.
    """

    try:
        resp = client.conversations_replies(
            channel=channel, ts=thread_ts, limit=50
        )
    except SlackApiError:
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

        # Guard the placeholder post: if the bot isn't in the channel we can't
        # post at all — log actionable guidance rather than crashing silently.
        try:
            placeholder = client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=":mag: FaultMaven is investigating…",
            )
        except SlackApiError as exc:
            logger.warning(
                "Cannot post in channel %s (%s) — is FaultMaven invited? "
                "Try /invite @FaultMaven",
                channel,
                exc.response.get("error"),
            )
            return

        try:
            # First summons into a thread → replay the prior discussion.
            prior_context = None
            if store.get(team_id, channel, thread_ts) is None:
                prior_context = _fetch_thread_context(
                    client, channel, thread_ts, exclude_ts=event.get("ts")
                )

            result = run_turn(
                fm,
                store,
                team_id=team_id,
                channel_id=channel,
                thread_ts=thread_ts,
                text=text,
                prior_context=prior_context,
            )
            client.chat_update(
                channel=channel,
                ts=placeholder["ts"],
                text=result.agent_response[:300],
                blocks=build_turn_blocks(result),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("app_mention turn failed: %s", exc)
            client.chat_update(
                channel=channel,
                ts=placeholder["ts"],
                text=":warning: FaultMaven hit an error while investigating. "
                "Please mention me again.",
            )
