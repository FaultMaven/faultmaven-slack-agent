"""Message shortcut — the universal "Investigate with FaultMaven" case-opener.

The flagship entry (design §4.3): from *any* message, Slack hands us the full
selected message in the payload. We extract its text (blocks included), open a
case seeded with it, and post FaultMaven's first reply threaded under that
message — starting the investigation in place. No copy-paste, no thread read.
"""

from __future__ import annotations

from logging import Logger

from slack_bolt import Ack, App, BoltContext
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from faultmaven import FaultMavenClient
from rendering import build_turn_blocks
from slack_text import message_to_text
from store import CaseStore

from ._turn import run_turn

# The shortcut seeds the alert as evidence (pasted_content); this is the query.
_SEED_QUERY = "Please investigate this."


def register_shortcuts(app: App, fm: FaultMavenClient, store: CaseStore) -> None:
    @app.shortcut("fm_investigate_message")
    def on_investigate_message(
        ack: Ack,
        shortcut: dict,
        context: BoltContext,
        client: WebClient,
        logger: Logger,
    ) -> None:
        ack()

        channel = shortcut["channel"]["id"]
        message = shortcut.get("message", {})
        # Thread the investigation under the selected message (or its parent, if
        # the target is itself a reply — Slack only threads one level deep).
        thread_ts = message.get("thread_ts") or message["ts"]
        team_id = context.team_id or ""
        alert_text = message_to_text(message)

        # Guard the placeholder post so an uninvited channel doesn't crash us.
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
            result = run_turn(
                fm,
                store,
                team_id=team_id,
                channel_id=channel,
                thread_ts=thread_ts,
                text=_SEED_QUERY,
                prior_context=alert_text or None,
            )
            client.chat_update(
                channel=channel,
                ts=placeholder["ts"],
                text=result.agent_response[:300],
                blocks=build_turn_blocks(result),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("shortcut investigation failed: %s", exc)
            client.chat_update(
                channel=channel,
                ts=placeholder["ts"],
                text=":warning: FaultMaven hit an error opening this "
                "investigation. Please try again.",
            )
