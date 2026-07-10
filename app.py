"""FaultMaven Slack Agent — Bolt entrypoint (P0, Socket Mode).

Wires the Slack Bolt app (Assistant container + ``app_mention``) to the
FaultMaven core API and starts a Socket Mode connection — the fastest path to a
working end-to-end loop with no public URL. The HTTP + multi-workspace OAuth
transport lands in P5 (see ``docs/design.md`` §8, §14).
"""

from __future__ import annotations

import logging
import signal
import time

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import (
    ConnectionErrorRetryHandler,
    RateLimitErrorRetryHandler,
)

from config import Settings, get_settings
from faultmaven import FaultMavenClient
from listeners import register_listeners
from listeners._turn import begin_shutdown, drain_turns
from store import CaseStore

logger = logging.getLogger("faultmaven.slack")

# Watchdog: how often to check the Socket Mode connection, and how long a
# continuous disconnect is tolerated before the process exits so a supervisor
# can restart it (slack_sdk's session monitor retries failed reconnects —
# including invalid_auth on a revoked/rotated app token — forever, silently;
# without this the bot wedges "alive" while answering nothing).
_WATCH_POLL_SECONDS = 30.0
_MAX_DISCONNECTED_SECONDS = 600.0
# Headroom added to the turn timeout for the shutdown drain: a normal turn can
# legitimately run the full FAULTMAVEN_REQUEST_TIMEOUT, so the drain must
# outlast it or closing the store/API client yanks resources from live workers
# mid-turn. Deployment note: the supervisor's kill grace (e.g. Kubernetes
# terminationGracePeriodSeconds, systemd TimeoutStopSec) should exceed
# timeout + this headroom, or a SIGKILL lands mid-drain.
_SHUTDOWN_DRAIN_HEADROOM_SECONDS = 10.0


def make_fault_client(settings: Settings) -> FaultMavenClient:
    """Build the FaultMaven API client from settings.

    Shared by the runtime (:func:`build_app`) and the preflight doctor so the
    client wiring has one definition and can't drift between them.
    """

    return FaultMavenClient(
        settings.faultmaven_api_url,
        token=settings.faultmaven_api_token,
        dev_login_username=settings.faultmaven_dev_login_username,
        timeout=settings.faultmaven_request_timeout,
    )


def make_web_client(token: str) -> WebClient:
    """A WebClient that retries rate limits, not just connection errors.

    slack_sdk installs only ``ConnectionErrorRetryHandler`` by default: every
    429 raises immediately, so a busy incident channel (placeholder + echo +
    reply across threads exceeds chat.postMessage's ~1 msg/sec/channel) would
    silently drop replies. ``RateLimitErrorRetryHandler`` honors Retry-After.
    Bolt copies these handlers onto its per-request clients.
    """

    return WebClient(
        token=token,
        retry_handlers=[
            ConnectionErrorRetryHandler(),
            RateLimitErrorRetryHandler(max_retry_count=2),
        ],
    )


def build_app() -> tuple[App, CaseStore, FaultMavenClient, str]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    fm = make_fault_client(settings)
    fm.startup()

    store = CaseStore(settings.case_store_path)
    # The store is the source of truth for thread→case; make its resolved
    # location diagnosable (a forked/mislocated store silently orphans every
    # active investigation).
    logger.info("Case store: %s", settings.case_store_path)

    app = App(
        client=make_web_client(settings.slack_bot_token),
        signing_secret=settings.slack_signing_secret or None,
    )
    register_listeners(app, fm, store)

    return app, store, fm, settings.slack_app_token


def _watch_connection(handler: SocketModeHandler) -> None:
    """Block while the Socket Mode session is healthy; exit when it isn't.

    Exiting (rather than letting slack_sdk's monitor retry forever) hands
    recovery to the process supervisor, which restarts with fresh config —
    the only path that picks up a rotated ``SLACK_APP_TOKEN``.
    """

    disconnected_since: float | None = None
    while True:
        time.sleep(_WATCH_POLL_SECONDS)
        client = handler.client
        if client is not None and client.is_connected():
            disconnected_since = None
            continue
        now = time.monotonic()
        if disconnected_since is None:
            disconnected_since = now
            logger.warning("Socket Mode disconnected; watching for recovery")
        elif now - disconnected_since >= _MAX_DISCONNECTED_SECONDS:
            raise SystemExit(
                f"Socket Mode disconnected for over "
                f"{_MAX_DISCONNECTED_SECONDS:.0f}s — exiting so the supervisor "
                "can restart with fresh credentials"
            )


def main() -> None:
    app, store, fm, app_token = build_app()
    if not app_token:
        raise SystemExit(
            "SLACK_APP_TOKEN (xapp-...) is required for Socket Mode. "
            "Create one with the connections:write scope."
        )

    # Python's default SIGTERM action kills the process without unwinding the
    # stack: `docker stop`/systemd would skip the finally below, abandoning
    # in-flight turns and their placeholders. Raise SystemExit instead so
    # shutdown is one code path for SIGTERM and Ctrl-C alike.
    def _sigterm(signum: int, frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm)

    logger.info("FaultMaven Slack Agent starting")
    handler = SocketModeHandler(app, app_token)
    try:
        handler.connect()
        _watch_connection(handler)
    finally:
        # In-flight turns that fail from the teardown itself must say
        # "restarting", not blame the turn or advise a retry.
        begin_shutdown()
        try:
            handler.close()
        except Exception as exc:  # noqa: BLE001 — shutdown must keep going
            logger.warning("Socket Mode close failed: %s", exc)
        # Let running turns finish BEFORE closing the store and API client
        # they're using: the drain must outlast the turn timeout, or a live
        # worker gets its resources yanked mid-turn and the thread's
        # ":mag: Investigating…" placeholder strands forever.
        drain_turns(
            get_settings().faultmaven_request_timeout
            + _SHUTDOWN_DRAIN_HEADROOM_SECONDS
        )
        store.close()
        fm.close()


if __name__ == "__main__":
    main()
