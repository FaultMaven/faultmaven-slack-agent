"""HTTP/OAuth transport — the FastAPI server hosting the Slack request handler.

This is the production transport (``docs/design.md`` §10, §16 P5). It exposes:

* ``POST /slack/events``        — event + interactivity callbacks (Bolt verifies
                                  the signing secret and replay timestamp).
* ``GET  /slack/install``       — begins multi-workspace OAuth (consent screen).
* ``GET  /slack/oauth_redirect``— exchanges the code, persists the per-team bot
                                  token in the InstallationStore.
* ``GET  /health``              — cheap liveness probe for k8s (never touches
                                  Slack or the FM backend, so a slow backend
                                  can't trip the liveness SIGKILL).

The Slack routes drive the **synchronous** Bolt app, whose dispatch does blocking
work (signature verify, a per-team InstallationStore DB read, and — on the OAuth
callback — a token-exchange HTTP call). That work is pushed onto the threadpool
(``run_in_threadpool``) so it never blocks the event loop, which must stay free to
serve ``/health`` and flush other in-flight responses. Turn work itself runs on
background daemon threads (``listeners._turn.offload_turn``), so a long
investigation is already off the loop.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from slack_bolt.adapter.starlette.handler import (
    to_bolt_request,
    to_starlette_response,
)
from starlette.concurrency import run_in_threadpool

import install_pages
from app import build_app, shutdown_runtime
from binding import bind_failure_message, complete_bind
from config import get_settings
from pending_binds import BIND_COOKIE_NAME

logger = logging.getLogger("faultmaven.slack.web")

# config validates LOG_LEVEL against Python's logging names, a superset of the
# levels uvicorn accepts; map the extras onto uvicorn's set so an otherwise-valid
# LOG_LEVEL (e.g. WARN, FATAL) doesn't crash uvicorn at boot.
_UVICORN_LOG_LEVELS = {"critical", "debug", "error", "info", "trace", "warning"}
_LOG_LEVEL_ALIASES = {
    "warn": "warning",
    "fatal": "critical",
    "notset": "trace",
}


def _uvicorn_log_level(level: str) -> str:
    lower = level.lower()
    lower = _LOG_LEVEL_ALIASES.get(lower, lower)
    return lower if lower in _UVICORN_LOG_LEVELS else "info"


def _html(body: str, *, status: int = 200, clear_cookie: bool = False) -> Response:
    """An HTML response that never leaks the flow's parameters onward.

    ``Referrer-Policy: no-referrer`` matters specifically here: this page is
    reached with ``?code=`` in the URL, and any outbound link or subresource
    would otherwise carry that query string in the ``Referer``. The code is
    already spent and PKCE-bound, so this is defence in depth rather than the
    only guard — but it costs one header.
    """

    headers = {
        "content-type": "text/html; charset=utf-8",
        "referrer-policy": "no-referrer",
        "cache-control": "no-store",
    }
    response = Response(content=body, status_code=status, headers=headers)
    if clear_cookie:
        # The record is spent either way; leaving the cookie behind would only
        # let a later request present a stale half of the pair.
        response.delete_cookie(
            BIND_COOKIE_NAME, path="/", httponly=True, secure=True, samesite="lax"
        )
    return response


def create_fastapi_app() -> FastAPI:
    """Build the ASGI app: Bolt handler on /slack/*, plus /health.

    Returned (rather than module-global) so tests can construct it against a
    patched environment. Object construction does no network I/O — the FM token
    bootstrap runs in the lifespan startup below, off the critical path.
    """

    bolt_app, store, fm, settings, stores = build_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Bootstrap the FM token on a background thread, NOT inline: uvicorn does
        # not begin serving (bind the listening socket's accept loop) until this
        # lifespan startup returns, so a slow/hanging backend here would block
        # /health up to the 120s client timeout and trip the k8s liveness
        # SIGKILL — the very failure /health is designed to avoid. The bootstrap
        # is best-effort (the first turn re-acquires lazily), so firing it and
        # returning immediately loses nothing.
        threading.Thread(
            target=fm.startup, name="fm-token-bootstrap", daemon=True
        ).start()
        logger.info("FaultMaven Slack Agent started (HTTP/OAuth)")
        try:
            yield
        finally:
            # Drain in-flight turns before releasing shared resources, mirroring
            # the Socket Mode finally. drain_turns joins turn threads for up to
            # ~130s, so run it OFF the event loop or /health and in-flight
            # responses freeze for the whole drain.
            await run_in_threadpool(shutdown_runtime, store, fm)

    api = FastAPI(lifespan=lifespan)
    binding_enabled = stores is not None and settings.install_binding_enabled
    if binding_enabled:
        logger.info("Install-time workspace binding enabled")
        # Spent records hold a live PKCE verifier and a state secret. Nothing
        # else deletes them, so a long-running process would accumulate expired
        # OAuth secrets in the shared database indefinitely.
        purged = stores.pending_binds.purge_expired()
        if purged:
            logger.info("Purged %d expired pending bind(s) at startup", purged)

    async def _dispatch_event(req: Request) -> Response:
        """POST callbacks → Bolt dispatch, off the event loop."""

        body = await req.body()  # async read on the loop (Starlette caches it)
        return await run_in_threadpool(
            lambda: to_starlette_response(
                bolt_app.dispatch(to_bolt_request(req, body))
            )
        )

    async def _oauth(req: Request, *, callback: bool) -> Response:
        """GET install / redirect → OAuthFlow, off the event loop.

        The redirect handler exchanges the code for tokens (a blocking HTTP call
        to Slack) and writes the InstallationStore, so both run in the threadpool.
        """

        body = await req.body()
        flow = bolt_app.oauth_flow
        handle = flow.handle_callback if callback else flow.handle_installation
        return await run_in_threadpool(
            lambda: to_starlette_response(handle(to_bolt_request(req, body)))
        )

    @api.post("/slack/events")
    async def slack_events(req: Request):  # noqa: ANN202 — FastAPI route
        return await _dispatch_event(req)

    @api.get("/slack/install")
    async def slack_install(req: Request):  # noqa: ANN202
        return await _oauth(req, callback=False)

    @api.get("/slack/oauth_redirect")
    async def slack_oauth_redirect(req: Request):  # noqa: ANN202
        return await _oauth(req, callback=True)

    @api.get("/faultmaven/callback")
    async def faultmaven_callback(req: Request):  # noqa: ANN202
        """Finish a workspace bind: the admin is back from FaultMaven's consent.

        The two halves are checked together and the record is claimed before the
        code is exchanged. A forwarded authorize URL carries the ``state`` but
        not the cookie, which is what stops an attacker's workspace being bound
        into the organization of whoever they forwarded it to.
        """

        if not binding_enabled:
            return _html(install_pages.unavailable_page(), status=404)

        code = req.query_params.get("code", "")
        state = req.query_params.get("state", "")
        bind_id = req.cookies.get(BIND_COOKIE_NAME, "")

        record = await run_in_threadpool(
            lambda: stores.pending_binds.consume(state=state, bind_id=bind_id)
        )
        if record is None:
            # One message for every failure — unknown state, wrong cookie,
            # expired, already used. Which one it was is information only
            # somebody probing the flow would want.
            logger.warning("Rejected a FaultMaven bind callback with no live record")
            return _html(
                install_pages.error_page(
                    "This connection link is no longer valid. Re-install "
                    "FaultMaven in Slack to start again."
                ),
                status=400,
                clear_cookie=True,
            )
        if not code:
            # The admin declined on the consent screen, or FaultMaven refused.
            # The record is already spent, which is correct — the link is done.
            return _html(
                install_pages.error_page(
                    "The workspace was not connected, because access was not "
                    "granted in FaultMaven."
                ),
                status=200,
                clear_cookie=True,
            )

        try:
            organization_id = await run_in_threadpool(
                lambda: complete_bind(
                    fm=fm,
                    workspace_credentials=stores.workspace_credentials,
                    record=record,
                    code=code,
                    redirect_uri=settings.faultmaven_oauth_redirect_uri,
                )
            )
        except Exception as exc:  # noqa: BLE001 — every failure is a page, not a 500
            logger.warning("Workspace bind failed for %s: %s", record.team_id, exc)
            return _html(
                install_pages.error_page(bind_failure_message(exc)),
                status=200,
                clear_cookie=True,
            )

        return _html(
            install_pages.bound_page(
                workspace_name=record.team_name, organization_id=organization_id
            ),
            clear_cookie=True,
        )

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
        log_level=_uvicorn_log_level(settings.log_level),
    )
