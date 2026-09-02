"""FaultMaven Slack Agent — Bolt app construction and the Socket Mode runtime.

Two transports share one set of listeners (the turn pipeline is transport-blind:
every listener uses Bolt's per-request ``client`` and ``context.team_id``, never
a captured global token):

* **HTTP/OAuth** (``SLACK_TRANSPORT=http``) — the hosted, production transport.
  Multi-workspace OAuth (``/slack/install`` → ``/slack/oauth_redirect``) with a
  per-team ``InstallationStore``; served over HTTP by :mod:`web`. This is what
  makes the app installable into many workspaces and Marketplace-eligible
  (``docs/design.md`` §10, §16 P5).
* **Socket Mode** (``SLACK_TRANSPORT=socket``) — local development against a
  single dev app; no public URL, not multi-workspace. Runs from :func:`main`.

This module builds the Bolt ``App`` for either transport and owns the Socket
Mode process loop; :mod:`web` owns the HTTP process loop. Both funnel shutdown
through :func:`shutdown_runtime`.
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from typing import Any

from slack_bolt import App
from slack_bolt.oauth.callback_options import CallbackOptions, FailureArgs, SuccessArgs
from slack_bolt.oauth.oauth_settings import OAuthSettings
from slack_bolt.response import BoltResponse
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import (
    ConnectionErrorRetryHandler,
    RateLimitErrorRetryHandler,
)

from config import DEFAULT_BOT_SCOPES, Settings, get_settings
from credentials import CredentialStore
from faultmaven import FaultMavenClient
from listeners import register_listeners
from listeners._turn import begin_shutdown, drain_turns
import install_pages
from binding import InstallerAuthority, authorize_url, installer_authority
from oauth_store import OAuthStores, build_oauth_stores
from pending_binds import BIND_COOKIE_NAME, PENDING_BIND_TTL_SECONDS, PendingBindStore
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


def make_fault_client(
    settings: Settings, workspace_credentials: Any = None
) -> FaultMavenClient:
    """Build the FaultMaven API client from settings.

    Shared by the runtime (:func:`build_app`) and the preflight doctor so the
    client wiring has one definition and can't drift between them.

    ``workspace_credentials`` is the per-workspace binding store (ADR-013 D3).
    When it is passed, a turn authenticates as the credential bound to its Slack
    workspace and falls back to the credential below only while
    ``FAULTMAVEN_REQUIRE_WORKSPACE_BINDING`` is off — see
    :class:`faultmaven.client.FaultMavenWorkspaceUnlinkedError` for why that
    fallback is unsafe against a multi-tenant backend.

    The credential store is created when a refresh credential is configured, or
    when one has already been persisted and nothing more explicit is set — so a
    dev-login deployment writes no extra file, an operator who clears the
    one-time FAULTMAVEN_REFRESH_TOKEN seed after bootstrap keeps working off the
    store, and an explicitly configured FAULTMAVEN_API_TOKEN is never silently
    shadowed by a credentials.db left behind from an earlier deployment.
    """

    store_exists = Path(settings.credential_store_path).exists()
    if settings.faultmaven_refresh_token:
        use_refresh_grant = True
    elif store_exists and settings.faultmaven_api_token:
        # Explicit config wins over leftover state, but say so — otherwise the
        # unused store looks like it is in play.
        logger.warning(
            "Ignoring the credential store at %s: FAULTMAVEN_API_TOKEN is set "
            "explicitly. Unset it to use the stored refresh credential.",
            settings.credential_store_path,
        )
        use_refresh_grant = False
    else:
        use_refresh_grant = store_exists

    credential_store = (
        CredentialStore(settings.credential_store_path) if use_refresh_grant else None
    )
    return FaultMavenClient(
        settings.faultmaven_api_url,
        token=settings.faultmaven_api_token,
        dev_login_username=settings.faultmaven_dev_login_username,
        timeout=settings.faultmaven_request_timeout,
        refresh_token=settings.faultmaven_refresh_token,
        credential_store=credential_store,
        oauth_client_id=settings.faultmaven_oauth_client_id,
        workspace_credentials=workspace_credentials,
        require_workspace_binding=settings.faultmaven_require_workspace_binding,
    )


def make_web_client(token: str | None = None) -> WebClient:
    """A WebClient that retries rate limits, not just connection errors.

    slack_sdk installs only ``ConnectionErrorRetryHandler`` by default: every
    429 raises immediately, so a busy incident channel (placeholder + echo +
    reply across threads exceeds chat.postMessage's ~1 msg/sec/channel) would
    silently drop replies. ``RateLimitErrorRetryHandler`` honors Retry-After.
    Bolt copies these handlers onto its per-request clients.

    In OAuth mode the base client carries **no token** (per-team tokens come
    from the InstallationStore); it exists only so Bolt copies its retry
    handlers onto every per-team client — without this, the hosted transport
    would lose rate-limit retries entirely.
    """

    return WebClient(
        token=token,
        retry_handlers=[
            ConnectionErrorRetryHandler(),
            RateLimitErrorRetryHandler(max_retry_count=2),
        ],
    )


def _build_core(
    settings: Settings, workspace_credentials: Any = None
) -> tuple[CaseStore, FaultMavenClient]:
    """Build the transport-independent dependencies: FM client + case store.

    Does NOT call ``fm.startup()`` — the token bootstrap makes a (best-effort)
    network call, which each transport runs at *startup* rather than at object
    construction, so building the app never blocks on the backend (and, for
    HTTP, never blocks before uvicorn binds the port).
    """

    fm = make_fault_client(settings, workspace_credentials)

    store = CaseStore(settings.case_store_path)
    # The store is the source of truth for thread→case; make its resolved
    # location diagnosable (a forked/mislocated store silently orphans every
    # active investigation).
    logger.info("Case store: %s", settings.case_store_path)
    return store, fm


def _bind_cookie(bind_id: str) -> str:
    """The ``Set-Cookie`` header pinning a pending bind to this browser.

    Every attribute is doing a job:

    * ``__Host-`` forbids a ``Domain``, so a sibling host under ``faultmaven.ai``
      cannot set or overwrite this cookie. Cookie-tossing from a neighbouring
      subdomain is precisely how an attacker would supply the half of the pair
      they do not have.
    * ``HttpOnly`` keeps it away from script; ``Secure`` is required by the
      prefix anyway.
    * ``SameSite=Lax`` still arrives on the top-level GET that FaultMaven
      redirects back to us — a ``Strict`` cookie would not, and the callback
      would refuse every legitimate bind.
    * ``Max-Age`` matches the record's own TTL so a stale cookie cannot outlive
      the row it addresses.
    """

    return (
        f"{BIND_COOKIE_NAME}={bind_id}; Path=/; Max-Age={PENDING_BIND_TTL_SECONDS}; "
        "HttpOnly; Secure; SameSite=Lax"
    )


def _install_html_headers(args: SuccessArgs, *extra_cookies: str) -> dict:
    """Headers for an install page, keeping Bolt's own state-cookie deletion.

    Replacing Bolt's success response wholesale silently drops the
    ``Set-Cookie`` that expires the spent Slack OAuth ``state`` — leaving it in
    the installer's browser for the rest of its lifetime. Bolt emits it on every
    default success; so must we.

    ``no-store`` matters here specifically: this page is served on the OAuth
    redirect (its URL carries Slack's ``code``) and its body embeds the bind
    ``state``. Neither belongs in a disk or back/forward cache on a shared
    machine.
    """

    cookies = [args.settings.state_utils.build_set_cookie_for_deletion()]
    cookies.extend(extra_cookies)
    return {
        "content-type": ["text/html; charset=utf-8"],
        "cache-control": ["no-store"],
        "referrer-policy": ["no-referrer"],
        "set-cookie": cookies,
    }


def _install_callbacks(
    settings: Settings, pending_binds: PendingBindStore
) -> CallbackOptions:
    """What the browser sees when a Slack install finishes.

    Bolt's default is a bare "success" page. We replace it because the install is
    only half the story: the workspace still has no FaultMaven organization, and
    the installer is the one person positioned to say which one it belongs to.
    """

    def on_success(args: SuccessArgs) -> BoltResponse:
        installation = args.installation
        team_id = installation.team_id or ""
        workspace_name = installation.team_name or team_id

        if not settings.install_binding_enabled or not team_id:
            # Nothing to offer: either this deployment binds workspaces out of
            # band, or Slack gave us an org-wide install with no workspace to
            # name (Enterprise Grid — see docs/design.md §10.1). Report the
            # install honestly rather than starting a flow that cannot finish.
            return BoltResponse(
                status=200,
                headers=_install_html_headers(args),
                body=install_pages.unavailable_page(),
            )

        # Both sides of the join must consent. The FaultMaven leg is gated on
        # organization authority; this is the Slack leg. Without it a FaultMaven
        # org admin who is an ordinary member here could admit this workspace on
        # their own. Checked before the pending record is created, so a refusal
        # leaves no state behind.
        authority = installer_authority(
            make_web_client(installation.bot_token), installation.user_id or ""
        )
        if authority is not InstallerAuthority.ADMIN:
            logger.warning(
                "Not offering the FaultMaven bind for workspace %s: installer "
                "%s is %s",
                team_id,
                installation.user_id or "unknown",
                authority.value,
            )
            page = (
                install_pages.not_admin_page
                if authority is InstallerAuthority.NOT_ADMIN
                else install_pages.authority_unknown_page
            )
            return BoltResponse(
                status=200,
                headers=_install_html_headers(args),
                body=page(workspace_name=workspace_name),
            )

        record = pending_binds.create(
            team_id=team_id,
            enterprise_id=installation.enterprise_id or "",
            installer_user_id=installation.user_id or "",
            team_name=workspace_name,
        )
        url = authorize_url(
            dashboard_url=settings.faultmaven_dashboard_url,
            client_id=settings.faultmaven_oauth_client_id,
            redirect_uri=settings.faultmaven_oauth_redirect_uri,
            record=record,
        )
        logger.info(
            "Slack install complete for workspace %s by installer %s; offering "
            "FaultMaven bind",
            team_id,
            installation.user_id or "unknown",
        )
        return BoltResponse(
            status=200,
            headers=_install_html_headers(args, _bind_cookie(record.bind_id)),
            body=install_pages.confirm_page(
                workspace_name=workspace_name, team_id=team_id, authorize_url=url
            ),
        )

    def on_failure(args: FailureArgs) -> BoltResponse:
        # Bolt's own reason (denied consent, bad state) — do not dress it up.
        logger.warning("Slack install failed: %s", args.reason)
        return args.default.failure(args)

    return CallbackOptions(success=on_success, failure=on_failure)


def _oauth_settings(settings: Settings, stores: OAuthStores) -> OAuthSettings:
    """Bolt OAuth config: per-team InstallationStore + CSRF state store.

    The scopes here mirror ``manifest.json`` (see :data:`DEFAULT_BOT_SCOPES`) —
    they are what the authorize URL requests; the redirect URI is derived from
    the request unless pinned via ``SLACK_OAUTH_REDIRECT_URI``.

    Takes already-built ``stores`` rather than building them: the FM client needs
    the workspace-credential store from the same engine, and building twice would
    give the two halves of the process separate connection pools over one file.
    """

    return OAuthSettings(
        client_id=settings.slack_client_id,
        client_secret=settings.slack_client_secret,
        scopes=list(DEFAULT_BOT_SCOPES),
        installation_store=stores.installation_store,
        state_store=stores.state_store,
        redirect_uri=settings.slack_oauth_redirect_uri or None,
        callback_options=_install_callbacks(settings, stores.pending_binds),
    )


def build_app() -> tuple[App, CaseStore, FaultMavenClient, Settings, OAuthStores | None]:
    """Build the Bolt app and its dependencies for the configured transport.

    HTTP mode wires multi-workspace OAuth (no static bot token — per-team tokens
    are resolved from the InstallationStore per request). Socket mode uses the
    single static bot token. Listeners are identical across both.
    """

    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    stores: OAuthStores | None = None

    if settings.slack_transport == "http":
        # Built before the core: the FM client authenticates per workspace off
        # the credential store that rides this same engine (ADR-013 D3), so the
        # stores have to exist before the client does.
        stores = build_oauth_stores(
            database_url=settings.slack_database_url,
            client_id=settings.slack_client_id,
        )
        store, fm = _build_core(settings, stores.workspace_credentials)
        # Pass a tokenless base client so Bolt copies its retry handlers onto
        # the per-team clients it builds from InstallationStore tokens; the
        # oauth_settings drive per-request authorization, not this client.
        app = App(
            client=make_web_client(),
            signing_secret=settings.slack_signing_secret,
            oauth_settings=_oauth_settings(settings, stores),
        )

    else:
        store, fm = _build_core(settings)
        app = App(
            client=make_web_client(settings.slack_bot_token),
            signing_secret=settings.slack_signing_secret or None,
        )
    register_listeners(
        app, fm, store, stores.workspace_credentials if stores else None
    )

    # Returned rather than attached to the Bolt app: the callback route needs
    # these exact instances (one engine, one pending-bind table), and a private
    # attribute on a third-party object is a dependency nothing type-checks and
    # a library release could break silently.
    return app, store, fm, settings, stores


def shutdown_runtime(store: CaseStore, fm: FaultMavenClient) -> None:
    """Drain in-flight turns, then release shared resources. Idempotent-safe.

    Shared by both transports' shutdown paths. In-flight turns that fail from
    the teardown itself must say "restarting", not blame the turn or advise a
    retry — :func:`begin_shutdown` flips that message. The drain must outlast
    the turn timeout, or a live worker gets its resources yanked mid-turn and
    the thread's ":mag: Investigating…" placeholder strands forever.
    """

    begin_shutdown()
    drain_turns(
        get_settings().faultmaven_request_timeout
        + _SHUTDOWN_DRAIN_HEADROOM_SECONDS
    )
    store.close()
    fm.close()


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
    """Process entrypoint. Dispatches to the HTTP runtime or the Socket loop."""

    settings = get_settings()
    if settings.slack_transport == "http":
        # The HTTP transport is an ASGI server; hand off to :mod:`web`.
        from web import run_http

        run_http()
        return

    app, store, fm, settings = build_app()
    # SLACK_APP_TOKEN presence is already enforced for socket mode by
    # Settings._validate_transport_requirements, so no re-check here.

    # Python's default SIGTERM action kills the process without unwinding the
    # stack: `docker stop`/systemd would skip the finally below, abandoning
    # in-flight turns and their placeholders. Raise SystemExit instead so
    # shutdown is one code path for SIGTERM and Ctrl-C alike.
    def _sigterm(signum: int, frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm)

    logger.info("FaultMaven Slack Agent starting (Socket Mode)")
    fm.startup()  # best-effort token bootstrap, before the first event
    handler = SocketModeHandler(app, settings.slack_app_token)
    try:
        handler.connect()
        _watch_connection(handler)
    finally:
        try:
            handler.close()
        except Exception as exc:  # noqa: BLE001 — shutdown must keep going
            logger.warning("Socket Mode close failed: %s", exc)
        # Let running turns finish BEFORE closing the store and API client.
        shutdown_runtime(store, fm)


if __name__ == "__main__":
    main()
