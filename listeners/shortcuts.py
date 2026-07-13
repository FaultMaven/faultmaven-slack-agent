"""Message shortcut — the universal "Ask FaultMaven" case-opener.

The flagship entry (design §4.3): from *any* message, Slack hands us the full
selected message in the payload. We extract its text (blocks included), open a
case seeded with it as evidence, and post FaultMaven's first reply threaded under
that message. No copy-paste, no thread read.
"""

from __future__ import annotations

import logging
from logging import Logger

import httpx
from slack_bolt import Ack, App, BoltContext
from slack_sdk import WebClient

from faultmaven import FaultMavenClient
from slack_files import download_message_content
from slack_text import message_to_text
from store import CaseStore

from ._turn import Dedup, post_placeholder, run_gated, run_turn_and_post

logger = logging.getLogger(__name__)

# The shortcut seeds the message as evidence (pasted_content); this is the query.
_SEED_QUERY = "Please investigate this."
# Cap the seed size (the backend size-guards too); matches events.py.
_SEED_LIMIT = 8000

_CANNOT_POST_TEXT = (
    ":information_source: I can't post in this conversation — `/invite "
    "@FaultMaven` to the channel, or use *Ask FaultMaven* somewhere I'm a "
    "member."
)


def _respond_ephemeral(response_url: str | None, text: str) -> None:
    """Reply to the invoker via the shortcut's ``response_url``.

    A message shortcut is offered in every conversation the USER can see —
    including private channels and human-to-human DMs the bot can never post
    in. There, every ``chat_postMessage`` fails and the shortcut would appear
    to do nothing at all; the ``response_url`` posts an ephemeral note to the
    invoker without needing channel membership. Best-effort: it expires after
    30 minutes and must never raise.
    """

    if not response_url:
        return
    try:
        httpx.post(
            response_url,
            json={"response_type": "ephemeral", "text": text},
            timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("response_url post failed: %s", exc)


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
        has_files = bool(message.get("files"))

        response_url = shortcut.get("response_url")

        # Cheap decline before any work: nothing to read and nothing attached.
        if not alert_text.strip() and not has_files:
            _decline(
                client, channel, thread_ts,
                " — describe the problem or @mention me.",
                response_url=response_url,
            )
            return

        def work() -> None:
            # Placeholder BEFORE the (potentially slow) file download for instant
            # feedback; reuse it for the reply.
            placeholder_ts = post_placeholder(client, channel, thread_ts)
            if placeholder_ts is None:
                # Can't post here (not a member / private conversation) — tell
                # the invoker ephemerally instead of failing in total silence.
                _respond_ephemeral(response_url, _CANNOT_POST_TEXT)
                return

            # Download attached files (logs, screenshots) to forward as
            # evidence; pasted snippets come back as text and merge into the
            # pasted evidence, so the backend sees paste provenance instead
            # of a fake "Untitled" file upload.
            files: list = []
            snippet_text: str | None = None
            if has_files:
                files, snippet_text = download_message_content(
                    client.token, message
                )

            # Files attached but none ingestible, and no text: don't open a blank
            # case — turn the placeholder into a how-to instead.
            if not alert_text.strip() and not files and not snippet_text:
                try:
                    client.chat_update(
                        channel=channel,
                        ts=placeholder_ts,
                        text=(
                            ":information_source: I couldn't read the attached "
                            "file(s) (too large, or I lack access). Paste the key "
                            "text and @mention me."
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 — decline must never strand the placeholder
                    logger.warning("decline update failed in %s: %s", channel, exc)
                return

            # Best-effort permalink back to the alert, for case provenance.
            # Broad guard: a TRANSPORT error here (reset, timeout) is not a
            # SlackApiError, and letting it escape after the placeholder was
            # posted would strand ":mag: Investigating…" with no turn run.
            source_url = None
            try:
                source_url = client.chat_getPermalink(
                    channel=channel, message_ts=message_ts
                ).get("permalink")
            except Exception as exc:  # noqa: BLE001 — provenance is best-effort
                logger.debug("permalink fetch failed: %s", exc)

            pasted = "\n\n".join(p for p in (alert_text, snippet_text) if p)
            run_turn_and_post(
                client,
                fm,
                store,
                channel=channel,
                thread_ts=thread_ts,
                team_id=team_id,
                text=_SEED_QUERY,
                pasted_content=pasted or None,
                source_url=source_url,
                files=files or None,
                placeholder_ts=placeholder_ts,
                mention_user=context.user_id,
            )

        # Reserve the thread and run in the background. If a turn is already
        # running here, this deliberate action gets a note rather than a no-op.
        if not run_gated(
            client, team_id=team_id, channel=channel, thread_ts=thread_ts,
            skip_ts=None, work=work,
        ):
            _decline(
                client, channel, thread_ts,
                " — I'm still working on this thread; try again once I've replied.",
                response_url=response_url,
            )


def _decline(
    client: WebClient,
    channel: str,
    thread_ts: str,
    note: str,
    *,
    response_url: str | None = None,
) -> None:
    """Post a short 'couldn't read that' note without opening a case.

    Falls back to the shortcut's ``response_url`` (ephemeral, no membership
    needed) when the in-channel post fails — otherwise a shortcut run where the
    bot can't post would decline in total silence.
    """

    text = f":information_source: I couldn't read that message{note}"
    try:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
    except Exception as exc:  # noqa: BLE001 — a decline must never raise
        logger.debug("decline post failed in %s: %s", channel, exc)
        _respond_ephemeral(response_url, text)
