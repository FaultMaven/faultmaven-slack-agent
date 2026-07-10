"""HTTP/OAuth transport — the FastAPI server hosting the Slack request handler.

This is the submission transport (``docs/design.md`` §10, §16 P5). It exposes:

* ``POST /slack/events``        — event + interactivity callbacks (Bolt verifies
                                  the signing secret and replay timestamp).
* ``GET  /slack/install``       — begins multi-workspace OAuth (consent screen).
* ``GET  /slack/oauth_redirect``— exchanges the code, persists the per-team bot
                                  token in the InstallationStore.
* ``GET  /health``              — cheap liveness probe for k8s (never touches
                                  Slack or the FM backend, so a slow backend
                                  can't trip the liveness SIGKILL).

Bolt's ``SlackRequestHandler`` maps the three ``/slack/*`` paths onto the same
``App`` used by Socket Mode, so the listeners are shared verbatim. Turn work runs
on background daemon threads (``listeners._turn.offload_turn``), so the async
event loop here is never blocked by a long investigation.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slack_bolt.adapter.fastapi import SlackRequestHandler

from app import build_app, shutdown_runtime
from config import get_settings

logger = logging.getLogger("faultmaven.slack.web")


def create_fastapi_app() -> FastAPI:
    """Build the ASGI app: Bolt handler on /slack/*, plus /health.

    Returned (rather than module-global) so tests can construct it against a
    patched environment, and so uvicorn imports it via ``web:asgi`` below.
    """

    bolt_app, store, fm, _settings = build_app()
    handler = SlackRequestHandler(bolt_app)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Startup is done in build_app() (FM client + stores are already up).
        logger.info("FaultMaven Slack Agent started (HTTP/OAuth)")
        try:
            yield
        finally:
            # Drain in-flight turns before releasing shared resources, mirroring
            # the Socket Mode finally. uvicorn calls this on SIGTERM.
            shutdown_runtime(store, fm)

    api = FastAPI(lifespan=lifespan)

    @api.post("/slack/events")
    async def slack_events(req: Request):  # noqa: ANN202 — FastAPI route
        return await handler.handle(req)

    @api.get("/slack/install")
    async def slack_install(req: Request):  # noqa: ANN202
        return await handler.handle(req)

    @api.get("/slack/oauth_redirect")
    async def slack_oauth_redirect(req: Request):  # noqa: ANN202
        return await handler.handle(req)

    @api.get("/health")
    async def health():  # noqa: ANN202
        # Deliberately dependency-free: a liveness probe that called the FM
        # backend or Slack would fail the pod on THEIR outage and get it
        # SIGKILLed mid-turn (the event-loop-blocking → 502 failure mode).
        return JSONResponse({"status": "ok"})

    return api


def run_http() -> None:
    """Run the HTTP transport under uvicorn (the container entrypoint)."""

    import uvicorn

    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    uvicorn.run(
        create_fastapi_app(),
        host="0.0.0.0",  # noqa: S104 — bind all interfaces inside the container
        port=settings.http_port,
        log_level=settings.log_level.lower(),
    )
