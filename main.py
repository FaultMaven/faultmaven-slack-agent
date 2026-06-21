"""FastAPI entrypoint for the FaultMaven Slack Agent.

Design constraints (see project brief):

* **Option B — strict mention only.** The bot never reads background channel
  traffic. It acts only on an explicit ``app_mention`` event or a message
  shortcut (``message_action``). No ``message.channels`` subscription.
* **3-second ack.** Slack retries any webhook it doesn't see ``200`` for within
  ~3 seconds. Every handler verifies the signature, does the cheapest possible
  triage, schedules the real work on ``BackgroundTasks``, and returns ``200``
  immediately.
* **Threads are sessions.** ``thread_ts`` is the stable investigation id; the
  background worker reconstructs context from the thread before each turn.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from config import get_settings
from services.faultmaven_api import (
    ConversationTurn,
    FaultMavenClient,
    InvestigationRequest,
)
from services.slack_service import SlackService
from utils.security import SlackVerificationError, verify_slack_request

logging.basicConfig(level=get_settings().log_level)
logger = logging.getLogger("faultmaven.slack")

# Slack re-delivers events that aren't ack'd in time. We dedupe on event_id so
# a slow first attempt can't trigger duplicate investigations. Bounded so the
# process can't grow this set without limit in a long-lived deployment.
_PROCESSED_EVENTS: set[str] = set()
_PROCESSED_EVENTS_MAX = 10_000


def _remember_event(event_id: str) -> bool:
    """Record an event id; return True if it is new (should be processed)."""

    if event_id in _PROCESSED_EVENTS:
        return False
    if len(_PROCESSED_EVENTS) >= _PROCESSED_EVENTS_MAX:
        _PROCESSED_EVENTS.clear()
    _PROCESSED_EVENTS.add(event_id)
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own the lifecycle of the shared Slack and FaultMaven clients."""

    app.state.slack = SlackService()
    app.state.faultmaven = FaultMavenClient()
    await app.state.slack.startup()
    await app.state.faultmaven.startup()
    logger.info(
        "FaultMaven Slack Agent ready (faultmaven mock_mode=%s)",
        app.state.faultmaven.mock_mode,
    )
    try:
        yield
    finally:
        await app.state.faultmaven.shutdown()


app = FastAPI(title="FaultMaven Slack Agent", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "faultmaven-slack-agent"}


# ---------------------------------------------------------------------------
# Events API  (app_mention + url_verification)
# ---------------------------------------------------------------------------
@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks) -> Response:
    """Handle the Slack Events API webhook.

    Returns within the 3-second budget in every branch; real work is deferred.
    """

    try:
        raw_body = await verify_slack_request(request)
    except SlackVerificationError as exc:
        logger.warning("Rejected unverified Slack event: %s", exc)
        return PlainTextResponse("invalid signature", status_code=401)

    payload = json.loads(raw_body)
    event_type = payload.get("type")

    # 1. URL verification handshake performed when configuring the endpoint.
    if event_type == "url_verification":
        return PlainTextResponse(payload.get("challenge", ""))

    # 2. Real events. We only subscribe to app_mention (Option B).
    if event_type == "event_callback":
        event = payload.get("event", {})
        event_id = payload.get("event_id", "")

        # Ignore the bot's own messages to avoid self-trigger loops.
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return PlainTextResponse("ok")

        if event.get("type") == "app_mention" and _remember_event(event_id):
            background_tasks.add_task(_handle_mention, request.app, event)

    # Always ack — anything we don't explicitly handle is a no-op success.
    return PlainTextResponse("ok")


# ---------------------------------------------------------------------------
# Interactivity  (message shortcuts)
# ---------------------------------------------------------------------------
@app.post("/slack/interactions")
async def slack_interactions(
    request: Request, background_tasks: BackgroundTasks
) -> Response:
    """Handle interactive payloads — specifically message shortcuts.

    Slack sends interactivity as ``application/x-www-form-urlencoded`` with a
    single ``payload`` field containing JSON.
    """

    try:
        raw_body = await verify_slack_request(request)
    except SlackVerificationError as exc:
        logger.warning("Rejected unverified Slack interaction: %s", exc)
        return PlainTextResponse("invalid signature", status_code=401)

    # Parse the form-encoded body from the already-read raw bytes.
    from urllib.parse import parse_qs

    form = parse_qs(raw_body.decode("utf-8"))
    payload_values = form.get("payload")
    if not payload_values:
        return PlainTextResponse("ok")

    payload = json.loads(payload_values[0])

    if payload.get("type") == "message_action":
        background_tasks.add_task(_handle_shortcut, request.app, payload)

    # Empty 200 closes the shortcut modal/loading state immediately.
    return JSONResponse({})


# ---------------------------------------------------------------------------
# Background workers — the heavy lifting runs here, off the ack path.
# ---------------------------------------------------------------------------
async def _handle_mention(app: FastAPI, event: dict[str, Any]) -> None:
    """Process an @mention: assemble thread context, investigate, reply."""

    slack: SlackService = app.state.slack
    faultmaven: FaultMavenClient = app.state.faultmaven

    channel = event["channel"]
    # A mention may be top-level (use its own ts as the thread root) or already
    # inside a thread (reuse the existing thread_ts as the session id).
    thread_ts = event.get("thread_ts") or event["ts"]
    user_id = event.get("user")
    prompt = slack.clean_text(event.get("text", ""))

    await _run_investigation(
        slack=slack,
        faultmaven=faultmaven,
        channel=channel,
        thread_ts=thread_ts,
        user_id=user_id,
        prompt=prompt or "Please investigate this thread.",
    )


async def _handle_shortcut(app: FastAPI, payload: dict[str, Any]) -> None:
    """Process a "Investigate with FaultMaven" message shortcut."""

    slack: SlackService = app.state.slack
    faultmaven: FaultMavenClient = app.state.faultmaven

    channel = payload["channel"]["id"]
    message = payload.get("message", {})
    # The shortcut targets a specific message; thread it under that message.
    thread_ts = message.get("thread_ts") or message.get("ts")
    user_id = payload.get("user", {}).get("id")
    prompt = slack.clean_text(message.get("text", ""))

    if not thread_ts:
        logger.warning("Shortcut payload missing message ts; ignoring")
        return

    await _run_investigation(
        slack=slack,
        faultmaven=faultmaven,
        channel=channel,
        thread_ts=thread_ts,
        user_id=user_id,
        prompt=prompt or "Please investigate this message.",
    )


async def _run_investigation(
    *,
    slack: SlackService,
    faultmaven: FaultMavenClient,
    channel: str,
    thread_ts: str,
    user_id: str | None,
    prompt: str,
) -> None:
    """Shared pipeline for mentions and shortcuts.

    Posts an immediate "investigating" placeholder, reconstructs thread
    context, runs one engine turn, and updates the placeholder in place.
    """

    placeholder_ts = await slack.post_thinking_indicator(channel, thread_ts)

    try:
        history = await slack.fetch_thread_history(channel, thread_ts)
        # Drop the latest user turn from history if it duplicates the prompt;
        # the engine receives it explicitly as `prompt`.
        history = _trim_current_turn(history, prompt)

        request = InvestigationRequest(
            session_id=thread_ts,
            prompt=prompt,
            history=history,
            channel_id=channel,
            user_id=user_id,
        )
        result = await faultmaven.investigate(request)
        await slack.post_result(
            channel, thread_ts, result, replace_ts=placeholder_ts
        )
    except Exception:  # noqa: BLE001 — last line of defense for a bg task
        logger.exception("Investigation failed for thread %s", thread_ts)
        await slack.post_error(
            channel,
            thread_ts,
            "FaultMaven hit an unexpected error while investigating. "
            "Please try mentioning me again.",
        )


def _trim_current_turn(
    history: list[ConversationTurn], prompt: str
) -> list[ConversationTurn]:
    """Remove a trailing user turn equal to the current prompt, if present."""

    if history and history[-1].role == "user" and history[-1].text == prompt:
        return history[:-1]
    return history


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
