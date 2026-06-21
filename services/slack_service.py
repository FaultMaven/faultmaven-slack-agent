"""Slack Web API integration: reading thread context and posting replies.

This module is the only place that talks to ``chat.*`` / ``conversations.*``
endpoints. The Slack SDK's ``WebClient`` is synchronous, so every network call
is dispatched onto a worker thread via :func:`asyncio.to_thread` to keep the
FastAPI event loop responsive while a background task runs.

Two responsibilities live here:

1. **Context assembly** — fetch a thread's message history so the FaultMaven
   engine sees the full collaborative conversation, not just the latest line.
2. **Rendering** — turn an :class:`InvestigationResult` into Block Kit blocks
   and post it back into the originating thread.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import get_settings
from services.faultmaven_api import ConversationTurn, InvestigationResult

logger = logging.getLogger(__name__)

# Matches a leading bot mention like "<@U12345>" so we can strip it from the
# user's actual prompt before handing text to the engine.
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


class SlackService:
    """Async-friendly facade over the Slack ``WebClient``."""

    def __init__(self) -> None:
        settings = get_settings()
        self._client = WebClient(token=settings.slack_bot_token)
        self._bot_user_id: str | None = None

    async def startup(self) -> None:
        """Resolve and cache the bot's own user id (used to label history)."""

        try:
            auth = await asyncio.to_thread(self._client.auth_test)
            self._bot_user_id = auth.get("user_id")
            logger.info("Slack agent authenticated as bot user %s", self._bot_user_id)
        except SlackApiError as exc:
            # Non-fatal: history labeling degrades gracefully without the id.
            logger.warning("auth.test failed; continuing without bot user id: %s", exc)

    # -- context assembly ---------------------------------------------------
    async def fetch_thread_history(
        self, channel: str, thread_ts: str, *, limit: int = 50
    ) -> list[ConversationTurn]:
        """Return the conversation history for a thread, oldest-first.

        Messages authored by the bot are tagged with role ``assistant``; all
        others are ``user``. Bot mentions are stripped from the text so the
        engine sees clean prompts.
        """

        try:
            response = await asyncio.to_thread(
                self._client.conversations_replies,
                channel=channel,
                ts=thread_ts,
                limit=limit,
            )
        except SlackApiError as exc:
            logger.warning("conversations.replies failed for %s: %s", thread_ts, exc)
            return []

        turns: list[ConversationTurn] = []
        for message in response.get("messages", []):
            text = self.clean_text(message.get("text", ""))
            if not text:
                continue
            author = message.get("user") or message.get("bot_id")
            is_bot = bool(message.get("bot_id")) or author == self._bot_user_id
            turns.append(
                ConversationTurn(
                    role="assistant" if is_bot else "user",
                    text=text,
                    user_id=author,
                )
            )
        return turns

    # -- posting ------------------------------------------------------------
    async def post_thinking_indicator(self, channel: str, thread_ts: str) -> str | None:
        """Post a transient "investigating…" message; return its ts.

        Because heavy work happens off the request path, this gives the user
        immediate feedback inside the 3-second ack window has already passed.
        The ts can be used to update-in-place once the result is ready.
        """

        return await self._post(
            channel=channel,
            thread_ts=thread_ts,
            text=":mag: FaultMaven is investigating…",
        )

    async def post_result(
        self,
        channel: str,
        thread_ts: str,
        result: InvestigationResult,
        *,
        replace_ts: str | None = None,
    ) -> None:
        """Render and post (or update) an investigation result in-thread."""

        blocks = self.build_result_blocks(result)
        fallback = result.summary

        if replace_ts:
            try:
                await asyncio.to_thread(
                    self._client.chat_update,
                    channel=channel,
                    ts=replace_ts,
                    text=fallback,
                    blocks=blocks,
                )
                return
            except SlackApiError as exc:
                logger.warning("chat.update failed; posting new message: %s", exc)

        await self._post(
            channel=channel, thread_ts=thread_ts, text=fallback, blocks=blocks
        )

    async def post_error(self, channel: str, thread_ts: str, message: str) -> None:
        """Post a user-facing error notice in-thread."""

        await self._post(
            channel=channel,
            thread_ts=thread_ts,
            text=f":x: {message}",
        )

    async def _post(
        self,
        *,
        channel: str,
        thread_ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str | None:
        try:
            response = await asyncio.to_thread(
                self._client.chat_postMessage,
                channel=channel,
                thread_ts=thread_ts,
                text=text,
                blocks=blocks,
            )
            return response.get("ts")
        except SlackApiError as exc:
            logger.error("chat.postMessage failed in %s: %s", channel, exc)
            return None

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def clean_text(text: str) -> str:
        """Strip bot mentions and surrounding whitespace from message text."""

        return _MENTION_RE.sub("", text or "").strip()

    @staticmethod
    def build_result_blocks(result: InvestigationResult) -> list[dict[str, Any]]:
        """Render an :class:`InvestigationResult` as Block Kit blocks."""

        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":robot_face: *FaultMaven*\n{result.summary}"},
            }
        ]

        if result.hypotheses:
            hypothesis_lines = "\n".join(f"• {h}" for h in result.hypotheses)
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Leading hypotheses*\n{hypothesis_lines}",
                    },
                }
            )

        if result.suggested_actions:
            action_lines = "\n".join(f"• {a}" for a in result.suggested_actions)
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Suggested next steps*\n{action_lines}",
                    },
                }
            )

        context_elements: list[dict[str, Any]] = []
        if result.confidence is not None:
            context_elements.append(
                {"type": "mrkdwn", "text": f"Confidence: {result.confidence:.0%}"}
            )
        if result.case_url:
            context_elements.append(
                {"type": "mrkdwn", "text": f"<{result.case_url}|Open case in FaultMaven>"}
            )
        if result.is_mock:
            context_elements.append({"type": "mrkdwn", "text": "_mock mode_"})

        if context_elements:
            blocks.append({"type": "context", "elements": context_elements})

        return blocks
