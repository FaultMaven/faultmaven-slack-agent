"""Message shortcut — the universal "Investigate with FaultMaven" case-opener.

The flagship entry (design §4.3): from *any* message, Slack hands us the full
selected message in the payload. We extract its text (blocks included), open a
case seeded with it as evidence, and post FaultMaven's first reply threaded under
that message. No copy-paste, no thread read.
"""

from __future__ import annotations

from logging import Logger

from slack_bolt import Ack, App, BoltContext
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from faultmaven import FaultMavenClient
from slack_files import download_message_files
from slack_text import message_to_text
from store import CaseStore

from ._turn import Dedup, run_turn_and_post

# The shortcut seeds the message as evidence (pasted_content); this is the query.
_SEED_QUERY = "Please investigate this."
# Cap the seed size (the backend size-guards too); matches events.py.
_SEED_LIMIT = 8000


def register_shortcuts(app: App, fm: FaultMavenClient, store: CaseStore) -> None:
    # De-dupe on trigger_id: a re-delivery of the SAME invocation reuses it
    # (so it's dropped), while a deliberate re-invocation gets a fresh one.
    dedup = Dedup()

    @app.shortcut(
        {"type": "message_action", "callback_id": "fm_investigate_message"}
    )
    def on_investigate_message(
        ack: Ack,
        shortcut: dict,
        context: BoltContext,
        client: WebClient,
        logger: Logger,
    ) -> None:
        ack()

        if dedup.is_duplicate(shortcut.get("trigger_id", "")):
            return

        channel = shortcut["channel"]["id"]
        message = shortcut.get("message") or {}
        message_ts = message.get("ts")
        if not message_ts:
            logger.warning("Shortcut payload missing message.ts; ignoring")
            return
        # Thread under the selected message (or its parent, if the target is
        # itself a reply — Slack only threads one level deep).
        thread_ts = message.get("thread_ts") or message_ts
        team_id = context.team_id or ""
        alert_text = message_to_text(message)[:_SEED_LIMIT]
        # Download any attached files (logs, screenshots) to forward as evidence.
        files = download_message_files(client.token, message)

        # Nothing usable at all — no text AND no ingestible file: don't open a
        # blank case; tell the user how to hand us the evidence.
        if not alert_text.strip() and not files:
            if message.get("files"):
                note = (
                    " — I couldn't read the attached file(s) (too large, or I "
                    "lack access). Paste the key text and @mention me."
                )
            else:
                note = " — describe the problem or @mention me."
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f":information_source: I couldn't read that message{note}",
                )
            except SlackApiError:
                pass
            return

        # Best-effort permalink back to the alert, for case provenance.
        source_url = None
        try:
            source_url = client.chat_getPermalink(
                channel=channel, message_ts=message_ts
            ).get("permalink")
        except SlackApiError:
            pass

        run_turn_and_post(
            client,
            fm,
            store,
            channel=channel,
            thread_ts=thread_ts,
            team_id=team_id,
            text=_SEED_QUERY,
            pasted_content=alert_text or None,
            source_url=source_url,
            files=files or None,
        )
