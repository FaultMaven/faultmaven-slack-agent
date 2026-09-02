"""Synchronous client for FaultMaven's core investigation API.

The Slack agent is a thin translation layer: a Slack thread becomes a FaultMaven
*case*, each message becomes a *turn*, and the structured ``TurnResponse`` is
rendered back into Slack. This module owns that boundary.

Contract (verified against faultmaven/modules/case/api/routes.py on 2026-06-22):

* **Create a case** — ``POST /api/v1/cases`` with a JSON ``CaseCreateRequest``
  body. We pass only ``title`` (null → backend auto-titles ``Case-MMDD-N``); we
  do *not* seed ``initial_message`` — the user's text is delivered by the first
  *turn* instead, so it isn't recorded twice and isn't bound by the 4000-char
  ``initial_message`` limit. We also never pass the Slack ``thread_ts`` as
  ``session_id`` (the backend validates that against its session service); the
  thread→case mapping lives in :mod:`store`.
* **Submit a turn** — ``POST /api/v1/cases/{case_id}/turns`` as
  ``multipart/form-data``. At least one of ``query`` / ``files`` /
  ``pasted_content`` is required.

Auth is lazy and self-healing: the token is acquired on first use, re-acquired
once on a 401, and never fatal at startup — so the agent boots even when the
backend is briefly down. Three credential shapes are supported, in precedence
order:

1. **Refresh grant** (``FAULTMAVEN_REFRESH_TOKEN`` / persisted credential) — the
   cloud path (ADR-012 D10). Under ``AUTH_MODE=oauth`` dev-login 404s, so the
   agent holds a provisioned refresh token and mints its own access tokens. Each
   renewal *rotates* the refresh token, so the new one is persisted before it is
   relied on and renewals are single-flighted.
2. **Preset access token** (``FAULTMAVEN_API_TOKEN``) — a static bearer that
   cannot be renewed.
3. **Dev-login** (``FAULTMAVEN_DEV_LOGIN_USERNAME``) — local auth mode only.

Kept synchronous to match Bolt's synchronous listener model.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Renew an access token this many seconds before it actually expires, so a turn
# never starts with a token that dies mid-request.
_ACCESS_EXPIRY_SKEW_SECONDS = 60.0
# How often the keepalive thread wakes to consider sliding the refresh window.
_KEEPALIVE_INTERVAL_SECONDS = 3600.0
# Renew when the refresh credential is within this long of expiring. The window
# is sliding but not infinite: an agent idle past the server's
# JWT_REFRESH_TOKEN_EXPIRY (default 7 days) would otherwise wake up locked out,
# needing an operator. A quiet weekend must not cost a re-bootstrap.
_REFRESH_RENEW_MARGIN_SECONDS = 2 * 24 * 3600.0
# Fallback cadence when the refresh token's expiry can't be read.
_REFRESH_BLIND_RENEW_SECONDS = 24 * 3600.0
# How long past the request timeout ``close()`` waits for ONE credential's
# in-flight renewal. Named rather than inlined so the per-credential budget is
# testable without a five-second test.
_CLOSE_DRAIN_HEADROOM_SECONDS = 5.0


def _jwt_claims(token: str) -> dict[str, Any] | None:
    """A JWT's claims, or None if it isn't a decodable JWT.

    Decode only — no signature check, and none is wanted: this reads claims off
    a token we already hold, to decide *when* to renew it and to check that the
    backend minted it for the tenant we expect. The backend remains the only
    authority on whether it is valid. Hand-rolled rather than pulling in a JWT
    library for two claims.
    """

    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # restore base64url padding
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error):
        return None
    return claims if isinstance(claims, dict) else None


def _jwt_expiry(token: str) -> float | None:
    """The ``exp`` claim of a JWT as a POSIX timestamp, or None."""

    claims = _jwt_claims(token)
    exp = claims.get("exp") if claims else None
    return float(exp) if isinstance(exp, (int, float)) else None


def _monotonic_at(when: datetime | None) -> float:
    """A monotonic stamp corresponding to a wall-clock instant in the past.

    Credential ages are compared against ``time.monotonic()`` (immune to clock
    steps), but a credential loaded from storage knows only when it was written.
    Projecting that wall-clock age back onto the monotonic clock keeps a
    restored credential's age honest. A future or missing timestamp yields
    "now", which is the conservative direction — it renews sooner, never later.
    """

    if when is None:
        return time.monotonic()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - when).total_seconds()
    return time.monotonic() - max(age, 0.0)


def _as_list(value: Any) -> list:
    """A list from a tolerated field, or ``[]`` — never ``list(scalar)`` (which
    would ``TypeError`` on schema drift past a committed-turn boundary)."""

    return list(value) if isinstance(value, list) else []


# The backend's ``x-error-code`` marking a 409 as an optimistic-concurrency
# version conflict, as opposed to the (unlabelled) terminal-case rejection that
# shares the status. Sent alongside x-expected-version / x-actual-version.
_VERSION_CONFLICT_CODE = "CASE_VERSION_CONFLICT"


class FaultMavenError(Exception):
    """Raised when the FaultMaven API cannot service a request."""


class FaultMavenAPIError(FaultMavenError):
    """An HTTP error response from the backend, with its status and detail.

    ``detail`` carries the backend's (truncated) error body — the Pydantic
    ``detail`` explaining *why* — so callers can log it and show a 4xx-specific
    message instead of a generic "try again" that would just reproduce the
    same rejection.
    """

    def __init__(self, message: str, *, status_code: int, detail: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class FaultMavenRateLimitError(FaultMavenAPIError):
    """The backend refused this request as backpressure (429).

    Distinguished from a generic 4xx because the advice is the opposite one: a
    429 reproduces on an *immediate* retry and succeeds on a later one, so
    "re-sending the same input won't help" is false and "try again" with no
    interval is useless — the wait is the only actionable part.

    ``retry_after`` is that wait in seconds, or ``None`` when the backend did
    not say. It is read from the ``Retry-After`` header first: that is the
    protocol-level value the limiter computes per caller, and it is present on
    every 429 the protection middleware sends, including the ones whose body is
    not JSON at all. The body's ``retry_after`` mirrors it and is the fallback.

    The distinction is not cosmetic. The per-session read and write buckets have
    very different windows — a write bucket refuses for ~60s, the hourly buckets
    for up to an hour — so a fixed "try again in a minute" would be a false
    promise on exactly the refusal a user is most likely to hit twice.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        detail: str = "",
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, detail=detail)
        self.retry_after = retry_after


class CaseNotFoundError(FaultMavenAPIError):
    """The case behind a thread no longer exists server-side (404).

    Distinguished so callers can evict the stale thread→case mapping — without
    that, every retry the generic error message suggests routes straight back
    to the same dead case_id and the thread is stuck forever.
    """


class CaseTerminalError(FaultMavenAPIError):
    """The case is terminal (``resolved``/``closed``) and refused this turn (409).

    A terminal case is read-only but still *online* (ADR-005's two axes: the
    workflow axis is done, the data tier is unchanged), so the backend still
    answers text-only questions about it — it rejects only evidence, state
    transitions, and file reclassification. Distinguished so a thread whose
    investigation has concluded gets an explanation of what it can still do,
    rather than a raw ``HTTP 409`` and "re-sending won't help", which reads as a
    malfunction when it is the case working as designed.

    NOT raised for the other 409 on this endpoint — an optimistic-concurrency
    version conflict, which is transient and genuinely worth retrying. See
    :data:`_VERSION_CONFLICT_CODE`.
    """


class CaseVersionConflictError(FaultMavenAPIError):
    """Another writer advanced the case mid-turn (409, OCC).

    The backend deliberately does not silently retry — an LLM turn is expensive
    and non-idempotent — so it surfaces the conflict for the client to decide.
    For us the turn did NOT commit, so unlike every other 409 a re-send is the
    correct advice.
    """


class FaultMavenCredentialError(FaultMavenError):
    """The refresh credential is dead; only an operator can restore service.

    Raised when the backend rejects the refresh token outright (expired past the
    sliding window, or revoked because a rotation was lost). Retrying cannot
    help — recovery is re-running the backend provisioning step and giving the
    agent the new token. Distinguished from a transient auth failure so that
    shows up in the logs as an operator action, not as noise.
    """


class _CredentialRejected(FaultMavenCredentialError):
    """The backend itself rejected the refresh token.

    Internal to the client: it separates "this credential is not accepted"
    (where trying a freshly provisioned seed can help) from every other
    credential failure, such as being unable to persist a rotation (where a
    retry would hit the same broken storage and consume the seed for nothing).
    Callers only ever see :class:`FaultMavenCredentialError`.
    """


class WorkspaceBindError(FaultMavenError):
    """The workspace could not be bound to a FaultMaven Team.

    ``retryable`` is False for the refusals a person cannot fix by trying again
    — chiefly the authority one. The bind needs BOTH ``ORG_MANAGE_USERS`` and
    ``ORG_MANAGE_SETTINGS`` (the second because it creates a Team), and an admin
    holding only the first completes the OAuth consent happily and is refused
    here. That path has to be told what is wrong, not offered a retry.
    """

    def __init__(
        self, message: str, *, status_code: int = 0, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    """What the server created for a workspace, as the agent needs it."""

    slack_team_id: str
    organization_id: str
    team_id: str
    team_name: str
    service_account_username: str
    refresh_token: str = field(repr=False)
    account_created: bool = False
    team_created: bool = False


class FaultMavenWorkspaceUnlinkedError(FaultMavenError):
    """This Slack workspace has no FaultMaven credential bound to it.

    Under a multi-tenant (cloud) backend this is a **refusal, not a fallback**.
    A workspace's cases are owned by *its* ``slack`` service account, and the
    organization those cases land in travels only in that account's token chain
    (ADR-013 D3; the backend's ``users`` table has no organization column). So
    answering an unbound workspace on the process-wide default credential would
    not degrade gracefully — it would file one customer's incident inside
    whatever organization that credential happens to carry. Refusing is the only
    safe direction; the fix is to bind the workspace.

    The process-wide default credential stays legitimate where there is exactly
    one tenant to be wrong about: Socket Mode and self-hosted deployments.
    """


class FaultMavenTimeoutError(FaultMavenError):
    """The client gave up waiting, but the backend may still complete the turn.

    Distinguished so the user-facing message can warn against blind re-sends
    (a resent message runs a duplicate turn against state the user never saw).
    """


@dataclass(slots=True)
class TurnResult:
    """Normalized turn response, ready to render into Block Kit.

    Only fields the backend's ``TurnResponse`` actually provides are modeled
    here. (Hypotheses and confidence are *not* part of the turn response — they
    live on the case and surface via the P2 reasoning timeline — so they are
    deliberately absent rather than parsed as perpetually dead ``None``s.)
    """

    agent_response: str
    case_state: str | None = None
    turn_number: int | None = None
    progress_made: bool = False
    milestones_completed: list[str] = field(default_factory=list)
    suggested_actions: list[dict[str, Any]] = field(default_factory=list)
    # Engine-derived assurance grade behind the case's identified cause
    # (no_root | mechanistic | confirmed). Present whenever a cause has been
    # stated. The narration in ``agent_response`` may voice that cause with
    # more certainty than it holds; this is the #572/INV-28 read-time label the
    # renderer places beside it so Slack never forwards the claim bare.
    cause_assurance: str | None = None
    cause_overclaim: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Credential:
    """One principal's credential, and the access token it is currently good for.

    A hosted deployment holds one of these **per Slack workspace**: ADR-013 maps
    a workspace to a Team inside the customer's Organization, and the agent
    authenticates as that workspace's ``slack`` service account so its cases are
    owned there and auto-share to the right Team.

    Only the *credential* is per-workspace. The HTTP connection pool and the
    keepalive scheduler are shared, because neither is tenanted — giving every
    workspace its own client would multiply threads and pools by the number of
    installs, and would need an eviction policy whose every eviction races the
    rotation below.

    Both locks are per-credential, preserving the disciplines the single-client
    version established while letting two *different* workspaces renew in
    parallel:

    * ``renew_lock`` is held ACROSS the refresh exchange. Presenting a refresh
      token consumes it, so two concurrent renewals of the SAME credential would
      each revoke the other's and lock this workspace out.
    * ``token_lock`` guards only the read-modify-write of the cached access
      token, so a racing thread can never observe a transiently blank bearer.
    """

    #: The Slack workspace (Bolt's ``team_id``) this credential authenticates
    #: as. ``""`` is the process-wide default: Socket Mode, self-hosted, and any
    #: deployment that has not bound per-workspace credentials.
    key: str = ""
    refresh_token: str = ""
    #: The configured one-time seed, kept for the documented lockout recovery —
    #: an operator who re-runs provisioning and restarts must not be defeated by
    #: the dead token still sitting in the store. See :meth:`_renew`.
    bootstrap_refresh_token: str = ""
    token: str = ""
    token_is_preset: bool = False
    dev_login_username: str = ""
    #: The FaultMaven organization this credential is expected to act within,
    #: recorded when the workspace was bound. Tenancy travels ONLY in the token
    #: chain, so nothing in the backend's database contradicts a credential
    #: minted against the wrong organization — this is the one place that can.
    #: See :meth:`FaultMavenClient._assert_expected_org`.
    organization_id: str = ""
    access_expires_at: float = 0.0
    #: When the credential we hold was obtained, for the keepalive's fallback
    #: cadence when a token's expiry can't be read.
    obtained_at: float = field(default_factory=time.monotonic)
    #: True when the live credential is newer than what is stored (a write
    #: failed). The keepalive retries the write, so a transient volume problem
    #: heals instead of becoming a restart-time lockout.
    unpersisted: bool = False
    renew_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    token_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def label(self) -> str:
        """How this credential is named in an operator-facing log line."""

        return f"workspace {self.key}" if self.key else "the default credential"


class FaultMavenClient:
    """Sync facade over the FaultMaven core API."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        dev_login_username: str = "",
        timeout: float = 120.0,
        refresh_token: str = "",
        credential_store: Any = None,
        oauth_client_id: str = "faultmaven-slack-agent",
        workspace_credentials: Any = None,
        require_workspace_binding: bool = False,
    ) -> None:
        # --- refresh-grant credential (ADR-012 D10) ---------------------------
        # The persisted credential wins over the configured one: rotation makes
        # the configured value a one-time seed that is revoked the moment it is
        # first used, so after the first renewal only the store is current.
        # ``bootstrap_refresh_token`` is kept so that an operator who re-runs
        # provisioning (the documented lockout recovery) and restarts the agent
        # is not defeated by the dead token still sitting in the store — see
        # :meth:`_renew`.
        self._credential_store = credential_store
        stored = credential_store.get() if credential_store is not None else None
        self._oauth_client_id = oauth_client_id
        self._keepalive_stop = threading.Event()
        self._keepalive: threading.Thread | None = None

        default_refresh = stored or refresh_token
        if default_refresh and credential_store is not None and not stored:
            # Seed the store from config on first boot, so the next restart
            # reads the rotated credential rather than the revoked seed.
            credential_store.put(default_refresh)

        # The process-wide default principal. In Socket Mode and self-hosted
        # deployments it is the only one; in a hosted multi-workspace deployment
        # it is the pre-binding fallback, and ``require_workspace_binding``
        # withdraws even that (see :class:`FaultMavenWorkspaceUnlinkedError`).
        self._default = _Credential(
            key="",
            refresh_token=default_refresh,
            bootstrap_refresh_token=refresh_token,
            # A preset token can't be re-acquired: never wipe it on a 401 (a
            # transient auth blip would otherwise discard it and every later
            # request would fail into a misleading dev-login error). Under the
            # refresh grant the access token IS re-acquirable, so it is not
            # preset.
            token="" if default_refresh else token,
            token_is_preset=bool(token) and not default_refresh,
            dev_login_username=dev_login_username,
        )

        # --- per-workspace credentials (ADR-013 D3) --------------------------
        # ``workspace_credentials`` is the durable binding store; ``_workspaces``
        # is only its in-process cache, so a credential's rotation state (and
        # its renew lock) has one home per process. Guarded because turns for
        # different workspaces land on different threads.
        self._workspace_credentials = workspace_credentials
        self._require_workspace_binding = require_workspace_binding
        self._workspaces: dict[str, _Credential] = {}
        self._workspaces_lock = threading.Lock()
        # Workspaces already warned about falling back to the default principal,
        # so a busy unbound workspace says it once rather than once per turn.
        self._warned_unbound: set[str] = set()

        self._timeout = timeout
        self._http = httpx.Client(base_url=base_url, timeout=timeout)

    # -- lifecycle ----------------------------------------------------------
    def startup(self) -> None:
        """Best-effort token bootstrap — never fatal.

        If a token can't be obtained now (backend down, or not in local auth
        mode), we log and move on; the first turn re-attempts lazily. Runs on a
        background daemon (see web.py), so it must swallow EVERYTHING: e.g. if
        shutdown closes the shared httpx client mid-login the request raises a
        bare ``RuntimeError`` (not a FaultMavenError), which would otherwise
        surface as an uncaught traceback in the daemon.
        """

        # Per-workspace credentials are refresh-grant by construction, so a
        # hosted deployment needs the keepalive even when the default principal
        # is a dev-login or has no credential at all.
        if self._default.refresh_token or self._workspace_credentials is not None:
            self._start_keepalive()
        if self._default.token:
            return
        if (
            self._workspace_credentials is not None
            and not self._default.refresh_token
            and not self._default.dev_login_username
        ):
            # Hosted, per-workspace-only: there is no default principal to
            # bootstrap, and that is the intended posture — not a failure.
            # Conditioned on the workspace store: WITHOUT it, no credential at
            # all is a misconfiguration, and swallowing it here would boot the
            # process silently and surface the problem as a generic error in
            # front of the first user.
            return
        try:
            self._ensure_token()
            logger.info(
                "Bootstrapped FaultMaven token via %s",
                "refresh grant" if self._default.refresh_token else "dev-login",
            )
        except Exception as exc:  # noqa: BLE001 — best-effort bootstrap, never fatal
            logger.warning("FaultMaven auth deferred: %s", exc)

    def close(self) -> None:
        self._keepalive_stop.set()
        keepalive = self._keepalive
        if keepalive is not None:
            # Bounded: the thread only ever waits on the stop event.
            keepalive.join(timeout=5.0)

        # Wait out an in-flight renewal before tearing down what it is using.
        # Closing the http client or the SQLite connection underneath one would
        # lose the rotated token — the presented one is already revoked, so that
        # is a lockout. A renewal is one request, bounded by the request
        # timeout; the keepalive join is not (it can be mid-request when the
        # stop event is set), which is why this waits separately.
        # Every credential renews independently, so every one of them has to be
        # drained — a rotation lost on one workspace locks out that workspace.
        with self._workspaces_lock:
            credentials = [self._default, *self._workspaces.values()]
        # Per credential, NOT one shared deadline: a renewal is one request, and
        # each credential's is independent. Sharing a budget means the first slow
        # renewal spends it and every later credential gets acquire(timeout=0.0)
        # — which returns False immediately on a held lock — so the HTTP client
        # is closed underneath their in-flight rotations. The presented token is
        # already revoked by then, so that is a lockout per workspace.
        budget = self._timeout + _CLOSE_DRAIN_HEADROOM_SECONDS
        held = []
        try:
            for cred in credentials:
                if cred.renew_lock.acquire(timeout=budget):
                    held.append(cred)
                else:
                    logger.warning(
                        "Closing the FaultMaven client with a renewal still in "
                        "flight for %s; a rotated credential may be lost",
                        cred.label,
                    )
            self._http.close()
            if self._credential_store is not None:
                self._credential_store.close()
            if self._workspace_credentials is not None:
                self._workspace_credentials.close()
        finally:
            for cred in held:
                cred.renew_lock.release()

    @property
    def authenticates_per_workspace(self) -> bool:
        """True when turns authenticate as per-workspace credentials only.

        A predicate rather than a string comparison against :attr:`auth_mode`,
        which is a human-readable diagnostic label: rewording it must not
        silently change what preflight checks.
        """

        if self._workspace_credentials is None:
            return False
        if self._require_workspace_binding:
            return True
        default = self._default
        return not (
            default.refresh_token or default.token or default.dev_login_username
        )

    @property
    def auth_mode(self) -> str:
        """How the default principal authenticates, for diagnostics.

        Read off the client's own state rather than re-derived from settings:
        the steady state after bootstrap is a persisted credential with the
        one-time seed cleared, which settings alone cannot distinguish from
        having no credential at all.
        """

        if self.authenticates_per_workspace:
            return "per-workspace refresh grants"
        cred = self._default
        if cred.refresh_token:
            return "refresh grant"
        if cred.token_is_preset:
            return "preset token"
        if cred.dev_login_username:
            return "dev-login bootstrap"
        return "no credential configured"

    def health(self) -> dict[str, Any]:
        """Probe the backend's top-level ``/health`` liveness endpoint.

        Used by the preflight doctor to confirm the API is reachable *before*
        the agent connects to Slack. It's public (no token) and side-effect free
        (creates no case), so it's safe to call on every startup check. We probe
        the app-wide ``/health`` (broad liveness — DB/LLM components) rather than
        the narrower case-service ``/api/v1/cases/health``.
        """

        try:
            resp = self._http.get("/health")
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise FaultMavenError(f"health check failed: {exc}") from exc
        except ValueError as exc:  # non-JSON 200 (proxy / login page / wrong host)
            raise FaultMavenError(
                f"health endpoint returned non-JSON (is FAULTMAVEN_API_URL "
                f"pointing at the API?): {exc}"
            ) from exc
        if not isinstance(body, dict):
            raise FaultMavenError(
                f"health endpoint returned {type(body).__name__}, not an object "
                "(is FAULTMAVEN_API_URL pointing at the API?)"
            )
        return body

    def verify_auth(self, team_id: str | None = None) -> None:
        """Confirm a principal's bearer token is actually accepted by the backend.

        ``_ensure_token`` only *obtains* a token (and short-circuits entirely
        when one is preset), so it can't catch a stale or wrong
        ``FAULTMAVEN_API_TOKEN`` — that surfaces as a 401 on the first real turn.
        This makes one authenticated call (``GET /api/v1/auth/me``) so preflight
        can fail fast instead. Raises :class:`FaultMavenError`; a message
        containing ``401`` means the token was rejected, any other status means
        the check was inconclusive (e.g. the endpoint isn't present).

        ``team_id`` checks one workspace's credential; omitted, it checks the
        default principal. Preflight iterates the bound workspaces, because a
        hosted deployment's default principal may be the one credential that is
        never used.
        """

        # The default principal is addressed directly rather than through
        # ``_credential_for``: for a *turn* an absent team_id is a refusal under
        # require_workspace_binding, but for a diagnostic it means "check the
        # default", which is a question preflight is entitled to ask.
        cred = self._credential_for(team_id) if team_id else self._default
        token = self._current_token(cred)
        try:
            resp = self._http.get(
                "/api/v1/auth/me", headers=self._auth_header(token)
            )
        except httpx.HTTPError as exc:
            raise FaultMavenError(f"auth check request failed: {exc}") from exc
        if resp.status_code == 401:
            raise FaultMavenError("token rejected (401): the bearer token is invalid")
        if resp.status_code != 200:
            raise FaultMavenError(
                f"auth check inconclusive (HTTP {resp.status_code} from /auth/me)"
            )

    # -- principals ---------------------------------------------------------
    def bound_workspaces(self) -> list[str]:
        """The Slack workspaces that have a FaultMaven credential bound.

        For preflight and diagnostics; reads the durable store, not the cache.
        """

        if self._workspace_credentials is None:
            return []
        return list(self._workspace_credentials.team_ids())

    def _credential_for(self, team_id: str | None) -> _Credential:
        """Resolve the principal a turn from ``team_id`` must authenticate as.

        Falls back to the process-wide default when the workspace has no binding
        — except under ``require_workspace_binding``, where an unbound workspace
        is refused rather than answered as the wrong tenant. See
        :class:`FaultMavenWorkspaceUnlinkedError` for why that is the safe
        direction and not an over-strict one.
        """

        if self._workspace_credentials is None:
            return self._default

        if not team_id:
            # Slack sent no workspace id (every listener derives
            # ``context.team_id or ""``, and an Enterprise Grid org-wide install
            # is the case that really produces it). An unidentifiable workspace
            # cannot be checked against a binding, so under
            # require_workspace_binding it is refused for exactly the reason a
            # *known* unbound workspace is: serving it on the default account
            # would file the case in whatever tenant that account carries. This
            # arm must stay ABOVE the fallback below — reversing them is a
            # silent bypass of the guard.
            if self._require_workspace_binding:
                raise FaultMavenWorkspaceUnlinkedError(
                    "Slack sent no workspace id for this turn, so it cannot be "
                    "matched to a FaultMaven organization"
                )
            return self._default

        with self._workspaces_lock:
            cached = self._workspaces.get(team_id)
            if cached is not None:
                return cached

        # Read outside the lock (it's I/O), then commit under it so two threads
        # racing a cold workspace still end up sharing ONE credential object —
        # two would mean two renew locks, which is the double-rotation lockout.
        record = self._workspace_credentials.get(team_id)

        with self._workspaces_lock:
            cached = self._workspaces.get(team_id)
            if cached is not None:
                return cached
            if record is None:
                if self._require_workspace_binding:
                    raise FaultMavenWorkspaceUnlinkedError(
                        f"Slack workspace {team_id} is not linked to a FaultMaven "
                        "organization"
                    )
                if team_id not in self._warned_unbound:
                    self._warned_unbound.add(team_id)
                    logger.warning(
                        "Slack workspace %s has no FaultMaven credential bound; "
                        "answering it as the default service account. Against a "
                        "multi-tenant backend this files the workspace's cases "
                        "in whatever organization that account carries — bind "
                        "the workspace, and set "
                        "FAULTMAVEN_REQUIRE_WORKSPACE_BINDING=true to refuse "
                        "instead of guessing.",
                        team_id,
                    )
                return self._default
            cred = _Credential(
                key=team_id,
                refresh_token=record.refresh_token,
                organization_id=record.organization_id,
                # Aged from when the token was STORED, not from now. The blind
                # renew cadence (used when a token's expiry can't be read) is
                # measured from this, so stamping it at cache time would restart
                # the clock on every process restart and cache miss — a
                # redeployed-daily agent would never reach the threshold and its
                # opaque credentials would age out to expiry unrenewed.
                obtained_at=_monotonic_at(record.updated_at),
            )
            self._workspaces[team_id] = cred
            return cred

    def _persist(self, cred: _Credential, refresh_token: str) -> None:
        """Commit a rotated refresh token to the store that owns it."""

        if cred.key:
            if self._workspace_credentials is not None:
                self._workspace_credentials.put_refresh_token(cred.key, refresh_token)
            return
        if self._credential_store is not None:
            self._credential_store.put(refresh_token)

    def _stored_refresh_token(self, cred: _Credential) -> str | None:
        """Whatever the durable store holds for this credential right now.

        For a workspace credential this also **reconciles the binding**: the
        cached ``_Credential`` outlives a re-bind, and its stale
        ``organization_id`` would then make :meth:`_assert_expected_org` reject
        the very token the re-bind provisioned — failing every turn until the
        process restarted, while blaming the backend for minting the wrong
        tenant. The store is the source of truth for a binding, so a changed
        organization is adopted here rather than fought.
        """

        if not cred.key:
            if self._credential_store is None:
                return None
            return self._credential_store.get()

        if self._workspace_credentials is None:
            return None
        record = self._workspace_credentials.get(cred.key)
        if record is None:
            # The binding is gone (uninstall). Nothing to fall back to, and the
            # caller must not keep serving this workspace off a cached token.
            self.forget_workspace(cred.key)
            return None
        if record.organization_id != cred.organization_id:
            logger.warning(
                "Workspace %s was re-bound from organization %s to %s; adopting "
                "the stored binding",
                cred.key,
                cred.organization_id or "(none)",
                record.organization_id,
            )
            cred.organization_id = record.organization_id
        return record.refresh_token

    def forget_workspace(self, team_id: str) -> None:
        """Drop a workspace's cached credential, so the next turn re-resolves it.

        The cache exists to give a workspace ONE renew lock per process; it is
        not a source of truth. An uninstall or a re-bind changes the binding
        underneath it, and continuing to serve turns from the cached copy would
        outlive the binding that authorized them.
        """

        with self._workspaces_lock:
            self._workspaces.pop(team_id, None)
        self._warned_unbound.discard(team_id)

    # -- auth ---------------------------------------------------------------
    def _ensure_token(self) -> None:
        """Acquire a token for the default principal (idempotent, thread-safe)."""

        self._current_token(self._default)

    def _current_token(self, cred: _Credential) -> str:
        """The bearer token, renewing or acquiring one as the mode requires."""

        if cred.refresh_token:
            return self._access_token_via_refresh_grant(cred)
        return self._dev_login_token(cred)

    # -- refresh grant (ADR-012 D10) ----------------------------------------
    def _access_token_via_refresh_grant(self, cred: _Credential) -> str:
        """A live access token, renewing it before it expires."""

        token = cred.token
        if token and time.monotonic() < cred.access_expires_at:
            return token
        return self._renew(cred, stale_access=token)

    def _renew(
        self, cred: _Credential, *, stale_access: str = "", force: bool = False
    ) -> str:
        """Exchange the refresh token for a new token pair. Single-flight.

        The lock is held across the request — the opposite discipline to
        dev-login below, and deliberately so: dev-login is idempotent, whereas
        presenting a refresh token *consumes* it. Two threads renewing at once
        would each revoke the other's credential and the loser would be locked
        out until an operator intervened.

        The lock is per-credential, so this serializes one workspace's renewals
        without making a busy workspace wait on another's.

        ``force`` renews even when the access token is still good. The keepalive
        needs it: its job is to slide the *refresh* window, which has nothing to
        do with how long the current access token has left.
        """

        # Captured before the lock so we can tell "someone else rotated while we
        # queued" (their token is now current, ours is revoked) from "we are the
        # renewing thread".
        intended = cred.refresh_token

        with cred.renew_lock:
            if cred.refresh_token != intended and cred.token:
                return cred.token
            if not force:
                current = cred.token
                if (
                    current
                    and current != stale_access
                    and time.monotonic() < cred.access_expires_at
                ):
                    return current

            try:
                return self._exchange_refresh_token(cred, cred.refresh_token)
            except _CredentialRejected:
                # Rejected means the credential we hold is no longer the live
                # one. Before declaring a lockout, try the two ways a newer one
                # can exist. (Only a backend rejection reaches here — a failed
                # persist no longer raises at all.)
                for candidate, source in self._alternative_credentials(cred):
                    logger.warning(
                        "FaultMaven credential rejected for %s; retrying with "
                        "the %s",
                        cred.label,
                        source,
                    )
                    try:
                        return self._exchange_refresh_token(cred, candidate)
                    except _CredentialRejected:
                        continue
                raise

    def _alternative_credentials(self, cred: _Credential) -> list[tuple[str, str]]:
        """Credentials worth trying after the one we hold was rejected.

        1. Whatever the store holds now. Another process may have rotated it —
           ``scripts/preflight.py`` authenticates, which under the refresh grant
           consumes a rotation. Without this, a valid token sitting in the store
           would be ignored and the running agent would stay locked out.
        2. The configured seed, for the documented recovery: the operator
           re-runs provisioning and restarts us while the store still holds the
           dead token. A per-workspace credential has no configured seed — its
           recovery is re-binding the workspace — so this arm is default-only.
        """

        tried = {cred.refresh_token}
        alternatives: list[tuple[str, str]] = []

        try:
            stored = self._stored_refresh_token(cred)
        except Exception as exc:  # noqa: BLE001 — unreadable store is not fatal here
            logger.warning("Could not re-read the credential store: %s", exc)
            stored = None
        if stored and stored not in tried:
            tried.add(stored)
            alternatives.append((stored, "credential now in the store"))

        seed = cred.bootstrap_refresh_token
        if seed and seed not in tried:
            alternatives.append((seed, "configured FAULTMAVEN_REFRESH_TOKEN"))

        return alternatives

    def _assert_expected_org(self, cred: _Credential, access_token: str) -> None:
        """Refuse a token minted for an organization this workspace is not bound to.

        Tenancy travels only in the token chain: the backend's ``users`` table
        has no organization column, so ``/auth/refresh`` re-attaches whatever
        ``organization_id`` the presented refresh token carried. Nothing in the
        database contradicts a credential provisioned against the wrong
        organization — the mistake is invisible until a customer finds their
        incidents inside another tenant.

        So the binding records the organization it was provisioned for, and this
        checks every minted access token against it. Refusing turns a silent
        cross-tenant misroute into a loud, fixable failure. A token whose claim
        is unreadable is not treated as a mismatch — the backend, not this
        decoder, is the authority on validity.
        """

        if not cred.organization_id:
            return
        claims = _jwt_claims(access_token)
        if claims is None:
            return
        actual = claims.get("organization_id")
        if actual == cred.organization_id:
            return
        if actual is None:
            # A decodable JWT that simply does not name a tenant is NOT the
            # opaque-token case above: the backend is speaking JWT and is not
            # saying which organization this is for, so the only cross-tenant
            # check in the system has nothing to compare. Treating that as a
            # pass is the safe direction (the backend still enforces its own
            # scoping), but it must not be silent — a claim rename upstream
            # would otherwise disable this guard permanently and invisibly.
            logger.warning(
                "FaultMaven minted a token for %s carrying no organization "
                "claim; the cross-tenant check cannot run on it",
                cred.label,
            )
            return
        raise FaultMavenCredentialError(
            f"FaultMaven minted a token for organization {actual!r}, but "
            f"{cred.label} is bound to {cred.organization_id!r}. Refusing to use "
            "it: this workspace's cases would be filed inside another tenant. "
            "Re-provision the workspace's service account against the correct "
            "organization."
        )

    def _exchange_refresh_token(self, cred: _Credential, refresh_token: str) -> str:
        """POST the refresh grant, persist the rotated token, return access.

        Ordering is the whole point: the rotated refresh token is committed to
        durable storage BEFORE this returns and the access token gets used. The
        presented token is already revoked server-side by then, so a crash
        between the response and the commit would leave the agent with no way
        back in.
        """

        try:
            resp = self._http.post(
                "/api/v1/auth/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._oauth_client_id,
                },
            )
        except httpx.HTTPError as exc:
            raise FaultMavenError(f"token refresh request failed: {exc}") from exc

        if resp.status_code in (400, 401):
            raise _CredentialRejected(
                f"FaultMaven rejected the refresh credential for {cred.label} "
                f"(HTTP {resp.status_code}): {self._error_detail(resp)}. It has "
                "expired or was revoked (a lost rotation, or two agents sharing "
                "one credential). Re-run the backend provisioning step "
                "(scripts/auth/provision_service_account.py) and set the new "
                "FAULTMAVEN_REFRESH_TOKEN."
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FaultMavenError(f"token refresh failed: {exc}") from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise FaultMavenError(f"token refresh returned non-JSON: {exc}") from exc

        access = body.get("access_token", "")
        rotated = body.get("refresh_token", "")
        if not access:
            raise FaultMavenError("token refresh returned no access_token")
        if not rotated:
            raise FaultMavenError("token refresh returned no refresh_token")

        # Before the rotation is committed or the access token is cached: a
        # cross-tenant credential must not be persisted as this workspace's, and
        # must never reach a request. The presented token is already revoked, so
        # this workspace is locked out either way — loudly, and without having
        # filed anything in the wrong organization.
        self._assert_expected_org(cred, access)

        # Persist BEFORE use — but never at the cost of the token itself. The
        # rotated credential is the only live one (the presented token is
        # already revoked server-side), so discarding it on a write error would
        # turn a momentarily full or read-only volume into a permanent lockout
        # that recovering the disk cannot undo. Keep it, say so loudly, and
        # retry the write later: the process stays usable, and a restart before
        # the write succeeds is the only casualty.
        try:
            self._persist(cred, rotated)
            cred.unpersisted = False
        except Exception as exc:  # noqa: BLE001 — degraded, not fatal
            cred.unpersisted = True
            logger.error(
                "Rotated FaultMaven refresh token for %s could NOT be persisted "
                "(%s). The agent keeps working, but a restart before this "
                "write succeeds needs re-provisioning. Check the volume.",
                cred.label,
                exc,
            )

        expires_in = body.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, (int, float)) else 900.0
        with cred.token_lock:
            cred.refresh_token = rotated
            cred.token = access
            cred.obtained_at = time.monotonic()
            cred.access_expires_at = cred.obtained_at + max(
                lifetime - _ACCESS_EXPIRY_SKEW_SECONDS, 0.0
            )
        logger.info(
            "Renewed FaultMaven access token for %s via the refresh grant",
            cred.label,
        )
        return access

    def _start_keepalive(self) -> None:
        """Slide the refresh window even when no one is talking to the agent.

        Renewal is otherwise driven by traffic, and the refresh credential
        expires on wall-clock time. A workspace that goes quiet for longer than
        the server's refresh window would come back to a locked-out agent, which
        only an operator can fix.

        One thread covers every credential: the work is a sub-second expiry
        check per credential once an hour, so a per-workspace thread would buy
        nothing and cost one thread per install.
        """

        if self._keepalive is not None:
            return
        thread = threading.Thread(
            target=self._keepalive_loop,
            name="fm-credential-keepalive",
            daemon=True,
        )
        self._keepalive = thread
        thread.start()

    def _live_credentials(self) -> list[_Credential]:
        """Every credential whose refresh window this process must keep sliding.

        Resolved from the durable bindings, not just the in-process cache: the
        cache is populated by traffic and is empty at boot, so a bound workspace
        that has not taken a turn since the last restart would never be renewed
        — and a workspace quiet for longer than the server's refresh window is
        exactly the lockout the keepalive exists to prevent.
        """

        credentials = [self._default]
        seen = set()
        for team_id in self.bound_workspaces():
            if team_id in seen:
                continue
            seen.add(team_id)
            try:
                credentials.append(self._credential_for(team_id))
            except Exception as exc:  # noqa: BLE001 — one bad row must not stop the rest
                logger.warning(
                    "Could not resolve the credential for workspace %s: %s",
                    team_id,
                    exc,
                )
        with self._workspaces_lock:
            for cred in self._workspaces.values():
                if cred.key not in seen:
                    credentials.append(cred)
        return credentials

    def _keepalive_loop(self) -> None:
        while not self._keepalive_stop.wait(_KEEPALIVE_INTERVAL_SECONDS):
            try:
                credentials = self._live_credentials()
            except Exception as exc:  # noqa: BLE001 — a daemon must not die
                logger.warning("Could not enumerate FaultMaven credentials: %s", exc)
                continue
            for cred in credentials:
                if not cred.refresh_token:
                    continue
                try:
                    self._retry_pending_persist(cred)
                    if self._refresh_credential_is_due(cred):
                        # force: the access token's remaining life is irrelevant
                        # — this renewal exists to slide the refresh window.
                        self._renew(cred, force=True)
                except Exception as exc:  # noqa: BLE001 — a daemon must not die
                    # Per credential, so one workspace's dead credential cannot
                    # stop every other workspace's window from sliding.
                    logger.warning(
                        "FaultMaven credential keepalive failed for %s: %s",
                        cred.label,
                        exc,
                    )

    def _retry_pending_persist(self, cred: _Credential) -> None:
        """Re-attempt a write that failed earlier, so the disk error can heal."""

        if not cred.unpersisted:
            return
        token = cred.refresh_token
        try:
            self._persist(cred, token)
        except Exception as exc:  # noqa: BLE001 — still broken; try again later
            logger.warning(
                "FaultMaven credential for %s still not persistable: %s",
                cred.label,
                exc,
            )
            return
        cred.unpersisted = False
        logger.info(
            "Rotated FaultMaven refresh token for %s persisted after an "
            "earlier failure",
            cred.label,
        )

    def _refresh_credential_is_due(self, cred: _Credential) -> bool:
        """True when the refresh credential is close enough to expiry to renew."""

        expires_at = _jwt_expiry(cred.refresh_token)
        if expires_at is None:
            # Expiry unreadable (not a JWT, or an opaque token): fall back to a
            # fixed cadence rather than assuming the credential is fine until it
            # silently isn't.
            age = time.monotonic() - cred.obtained_at
            return age >= _REFRESH_BLIND_RENEW_SECONDS
        return (expires_at - time.time()) <= _REFRESH_RENEW_MARGIN_SECONDS

    def _dev_login_token(self, cred: _Credential) -> str:
        """The bearer token, acquiring one via dev-login if absent.

        The lock is NOT held across ``_dev_login`` (a network call bounded only
        by the request timeout): holding it would serialize every cold-start
        turn behind one another during an auth outage (N×timeout pile-up). The
        login runs lock-free and the result is compare-and-set — so racing
        threads may each log in (bounded, parallel) but the token is never
        blanked, so no thread sends the empty bearer the lock-free-but-blanking
        original produced.
        """

        token = cred.token
        if token:
            return token
        if not cred.dev_login_username:
            raise FaultMavenError(
                "no FAULTMAVEN_API_TOKEN configured and dev-login disabled"
            )
        fresh = self._dev_login(cred.dev_login_username)  # outside the lock
        with cred.token_lock:
            if not cred.token:
                cred.token = fresh
            return cred.token

    def _reauth(self, cred: _Credential, stale: str) -> str:
        """Re-acquire a token after a 401, unless another thread already did.

        Under the refresh grant this renews (the access token expired earlier
        than its stated lifetime — clock skew, or a server-side revocation).

        Otherwise, same discipline as :meth:`_dev_login_token`: ``_dev_login``
        runs outside
        the lock (no serial pile-up), and the result is compare-and-swap on the
        ``stale`` token so a token another thread already refreshed wins and the
        token is never transiently blanked.
        """

        if cred.refresh_token:
            return self._renew(cred, stale_access=stale)

        current = cred.token
        if current and current != stale:
            return current  # another thread already refreshed
        fresh = self._dev_login(cred.dev_login_username)  # outside the lock
        with cred.token_lock:
            if cred.token == stale or not cred.token:
                cred.token = fresh
            return cred.token

    def _dev_login(self, username: str) -> str:
        try:
            resp = self._http.post(
                "/api/v1/auth/dev-login", json={"username": username}
            )
        except httpx.HTTPError as exc:
            raise FaultMavenError(f"dev-login request failed: {exc}") from exc
        # 404 = backend isn't in local auth mode; a real token is required.
        if resp.status_code == 404:
            raise FaultMavenError(
                "dev-login unavailable (backend not in local auth mode); "
                "set FAULTMAVEN_API_TOKEN for this backend"
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FaultMavenError(f"dev-login failed: {exc}") from exc
        token = resp.json().get("access_token", "")
        if not token:
            raise FaultMavenError("dev-login returned no access_token")
        return token

    @staticmethod
    def _auth_header(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _post(
        self,
        url: str,
        *,
        cred: _Credential,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: list | None = None,
    ) -> httpx.Response:
        """POST with the bearer token, re-authenticating once on a 401.

        Handles the long-lived-process case where the token expires (dev-login
        tokens default to a 1-hour TTL): a 401 triggers exactly one re-login +
        retry, so turns keep working without a restart.
        """

        token = self._current_token(cred)
        resp = self._http.post(
            url, json=json, data=data, files=files,
            headers=self._auth_header(token),
        )
        if resp.status_code == 401:
            if not cred.refresh_token and (
                cred.token_is_preset or not cred.dev_login_username
            ):
                # Nothing to re-acquire — surface the 401 as-is (the operator
                # must rotate FAULTMAVEN_API_TOKEN), keeping the token in place
                # so a transient backend auth blip self-heals.
                logger.error(
                    "FaultMaven rejected the configured token (401); "
                    "check FAULTMAVEN_API_TOKEN"
                )
                return resp
            logger.info("FaultMaven token rejected (401); re-authenticating")
            token = self._reauth(cred, token)
            resp = self._http.post(
                url, json=json, data=data, files=files,
                headers=self._auth_header(token),
            )
        return resp

    @staticmethod
    def _error_detail(resp: httpx.Response) -> str:
        """The backend's error explanation, truncated — for logs and messages."""

        try:
            body = resp.json()
            detail = (
                FaultMavenClient._explanation(body) if isinstance(body, dict) else None
            )
            text = detail if isinstance(detail, str) else resp.text
        except ValueError:
            text = resp.text
        return " ".join((text or "").split())[:300]

    @staticmethod
    def _explanation(body: dict[str, Any]) -> str | None:
        """The human-readable half of an error body, in either shape.

        Most routes answer FastAPI's ``{"detail": ...}``. The OAuth token and
        revocation endpoints answer RFC 6749 §5.2 instead —
        ``{"error", "error_description"}`` — as of API contract 2.0.0, and
        reading only ``detail`` there yielded None and fell back to dumping the
        raw JSON body at whoever was reading the log.

        Nothing validates the body against a model here on purpose: this runs
        on the *error* path, where being strict would mean an unparseable
        refusal becomes an exception instead of a message.
        """

        for key in ("detail", "message"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value

        code = body.get("error")
        description = body.get("error_description")
        if isinstance(code, str) and code:
            # The code is what an operator acts on ("invalid_grant" means
            # re-provision); the description says which instance of it this was.
            if isinstance(description, str) and description:
                return f"{code}: {description}"
            return code
        if isinstance(description, str) and description:
            return description
        return None

    def _raise_for_status(
        self, resp: httpx.Response, op: str, *, not_found_is_case: bool = False
    ) -> None:
        """Map an HTTP error onto the typed exception hierarchy."""

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = self._error_detail(resp)
            message = f"{op} failed: HTTP {resp.status_code}: {detail}"
            if not_found_is_case and resp.status_code == 404:
                raise CaseNotFoundError(
                    message, status_code=404, detail=detail
                ) from exc
            if resp.status_code == 429:
                raise FaultMavenRateLimitError(
                    message,
                    status_code=429,
                    detail=detail,
                    retry_after=self._retry_after(resp),
                ) from exc
            raise FaultMavenAPIError(
                message, status_code=resp.status_code, detail=detail
            ) from exc

    @staticmethod
    def _retry_after(resp: httpx.Response) -> int | None:
        """How long the backend asked us to wait, in seconds, if it said.

        The ``Retry-After`` header is preferred over the body's ``retry_after``:
        it is the protocol-level value, it is present even when the body is not
        JSON, and it is what the limiter actually computed for this caller.

        Only the delta-seconds form is read. ``Retry-After`` also permits an
        HTTP-date, which the backend does not send; parsing it here would be
        dead code, and guessing at a malformed value would be worse than saying
        nothing — ``None`` degrades to advice without an interval, which is
        honest, whereas a wrong number tells the user to come back too early
        and earns them a second refusal.
        """

        raw = resp.headers.get("retry-after")
        if raw is None:
            try:
                body = resp.json()
            except ValueError:
                body = None
            raw = body.get("retry_after") if isinstance(body, dict) else None

        try:
            seconds = int(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return seconds if seconds >= 0 else None

    # -- workspace binding (ADR-013 D3) -------------------------------------
    def exchange_authorization_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> tuple[str, str]:
        """Exchange an admin's authorization code for ``(access, refresh)``.

        The verifier is held server-side and sent only here, which is what makes
        a code that leaks (an access log line, a ``Referer``) unredeemable by
        whoever it leaked to.

        ``redirect_uri`` must be byte-identical to the one sent to ``/authorize``
        — the server compares the two strings directly, so a trailing slash that
        differs is an ``invalid_grant``, not a near miss.
        """

        try:
            resp = self._http.post(
                "/api/v1/auth/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                    "client_id": self._oauth_client_id,
                },
            )
        except httpx.HTTPError as exc:
            raise WorkspaceBindError(
                f"could not reach FaultMaven to exchange the code: {exc}",
                retryable=True,
            ) from exc
        if resp.status_code != 200:
            raise WorkspaceBindError(
                f"FaultMaven refused the authorization code "
                f"({resp.status_code}): {self._error_detail(resp)}",
                status_code=resp.status_code,
            )
        try:
            body = resp.json()
        except ValueError as exc:
            # A 200 whose body is not JSON is a proxy or gateway page, not the
            # token endpoint. Typed like every other failure here so the caller
            # renders a page instead of a bare ValueError.
            raise WorkspaceBindError(
                f"code exchange returned a non-JSON body: {exc}"
            ) from exc
        access, refresh = body.get("access_token", ""), body.get("refresh_token", "")
        if not access:
            raise WorkspaceBindError("code exchange returned no access_token")
        return access, refresh

    def bind_workspace(
        self,
        *,
        admin_access_token: str,
        slack_team_id: str,
        team_name: str,
        slack_enterprise_id: str | None = None,
    ) -> WorkspaceBinding:
        """Bind a Slack workspace to a Team, as the authenticated admin.

        The organization is taken from ``admin_access_token``'s own claim
        server-side; there is deliberately no way to name one here, so this
        cannot bind a workspace into a tenant the admin does not administer.

        The returned refresh token is the workspace service account's, issued
        once. The admin's bearer is a *parameter*, never stored on this client:
        it carries org-admin authority far beyond this call, so it lives only as
        long as the caller's stack frame.
        """

        payload: dict[str, Any] = {
            "slack_team_id": slack_team_id,
            "team_name": team_name,
        }
        if slack_enterprise_id:
            payload["slack_enterprise_id"] = slack_enterprise_id

        try:
            resp = self._http.post(
                "/api/v1/admin/integrations/slack/workspaces",
                json=payload,
                headers=self._auth_header(admin_access_token),
            )
        except httpx.HTTPError as exc:
            raise WorkspaceBindError(
                f"could not reach FaultMaven to bind the workspace: {exc}",
                retryable=True,
            ) from exc

        if resp.status_code == 403:
            raise WorkspaceBindError(
                "Your FaultMaven account can sign in but is not allowed to bind a "
                "workspace. This needs both 'manage users' and 'manage settings' "
                "in your organization — ask an organization owner.",
                status_code=403,
            )
        if resp.status_code == 409:
            raise WorkspaceBindError(
                f"This Slack workspace is already bound elsewhere: "
                f"{self._error_detail(resp)}",
                status_code=409,
            )
        if resp.status_code == 503:
            raise WorkspaceBindError(
                "FaultMaven is not configured for team management, so this "
                "workspace cannot be bound yet.",
                status_code=503,
                retryable=True,
            )
        if resp.status_code >= 400:
            raise WorkspaceBindError(
                f"FaultMaven refused the bind ({resp.status_code}): "
                f"{self._error_detail(resp)}",
                status_code=resp.status_code,
                retryable=resp.status_code >= 500,
            )

        # Past this point the server has ALREADY created the account, the team
        # and the memberships, and minted a once-only credential. Anything we
        # reject from here is unrecoverable by retry — the next attempt gets a
        # 409 — so every field the caller depends on is checked here, where the
        # error can still name what happened, rather than deeper where it would
        # surface as an AttributeError on a half-built binding.
        try:
            body = resp.json()
        except ValueError as exc:
            raise WorkspaceBindError(
                "The workspace was created in FaultMaven, but its response could "
                "not be read, so the credential is lost. An administrator must "
                "re-issue it.",
                status_code=resp.status_code,
            ) from exc

        missing = [
            name
            for name in ("refresh_token", "organization_id", "team_id")
            if not body.get(name)
        ]
        if missing:
            raise WorkspaceBindError(
                "The workspace was created in FaultMaven, but its response was "
                f"missing {', '.join(missing)}, so it cannot be used here. An "
                "administrator must re-issue the workspace credential.",
                status_code=resp.status_code,
            )
        refresh = body["refresh_token"]
        return WorkspaceBinding(
            # The Slack workspace id is OURS — the one the completed install
            # established — not whatever the server echoes. They should agree;
            # if they ever did not, storing the echo would file the credential
            # under a key no Slack event ever looks up.
            slack_team_id=slack_team_id,
            organization_id=body.get("organization_id", ""),
            team_id=body.get("team_id", ""),
            team_name=body.get("team_name", team_name),
            service_account_username=body.get("service_account_username", ""),
            refresh_token=refresh,
            account_created=bool(body.get("account_created")),
            team_created=bool(body.get("team_created")),
        )

    def revoke_token(self, token: str, *, token_type_hint: str) -> bool:
        """Revoke a token, returning whether the server confirmed it.

        Best-effort by contract, but the two tokens differ in how much a failure
        matters: an access token dies on its own within minutes, while a live
        refresh token is a standing org-admin credential. Callers log the first
        and shout about the second.
        """

        if not token:
            return True
        try:
            resp = self._http.post(
                "/api/v1/auth/oauth/revoke",
                json={
                    "token": token,
                    "token_type_hint": token_type_hint,
                    "client_id": self._oauth_client_id,
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("Token revocation request failed: %s", exc)
            return False
        return resp.status_code < 400

    # -- core calls ---------------------------------------------------------
    def create_case(
        self,
        *,
        title: str | None = None,
        initial_message: str | None = None,
        team_id: str | None = None,
    ) -> str:
        """Create a case and return its ``case_id``.

        ``initial_message`` is supported but unused by the Slack agent (see the
        module docstring) — the first turn carries the user's text.

        ``team_id`` selects the Slack workspace whose service account owns the
        case. The owner decides both the organization the case lands in and, via
        that account's Team membership, who it auto-shares to (ADR-013 D3) — so
        passing the wrong one is a tenancy error, not a labelling one.
        """

        cred = self._credential_for(team_id)
        body: dict[str, Any] = {"title": title}
        if initial_message is not None:
            body["initial_message"] = initial_message
        try:
            resp = self._post("/api/v1/cases", cred=cred, json=body)
        except httpx.TimeoutException as exc:
            raise FaultMavenTimeoutError(
                f"create_case timed out after {self._timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise FaultMavenError(f"create_case failed: {exc}") from exc
        self._raise_for_status(resp, "create_case")

        case_id = resp.json().get("case_id")
        if not case_id:
            raise FaultMavenError("create_case response had no case_id")
        return case_id

    def submit_turn(
        self,
        case_id: str,
        *,
        query: str | None = None,
        pasted_content: str | None = None,
        files: list[tuple[str, bytes, str]] | None = None,
        intent_type: str | None = None,
        intent_data: dict[str, Any] | None = None,
        input_type: str | None = None,
        source_url: str | None = None,
        observed_at: str | None = None,
        team_id: str | None = None,
    ) -> TurnResult:
        """Submit one turn (multipart) and return the normalized result.

        ``files`` items are ``(filename, content, content_type)`` tuples.

        ``observed_at`` is an ISO-8601 instant saying when ``pasted_content``
        was observed — distinct from when it was submitted. The backend
        defaults to submission time when it is absent, which reads a
        two-hour-old alert as current.
        """

        form: dict[str, str] = {}
        if query:
            form["query"] = query
        if pasted_content:
            form["pasted_content"] = pasted_content
        if intent_type:
            form["intent_type"] = intent_type
        if intent_data is not None:
            form["intent_data"] = json.dumps(intent_data)
        if input_type:
            form["input_type"] = input_type
        if source_url:
            form["source_url"] = source_url
        if observed_at:
            form["observed_at"] = observed_at

        file_parts = (
            [("files", (name, content, ctype)) for name, content, ctype in files]
            if files
            else None
        )

        if not form and not file_parts:
            raise FaultMavenError(
                "a turn needs at least one of query / pasted_content / files"
            )

        cred = self._credential_for(team_id)
        start = time.monotonic()
        polled = False
        try:
            resp = self._post(
                f"/api/v1/cases/{case_id}/turns",
                cred=cred,
                data=form,
                files=file_parts,
            )
            # The current backend answers turns synchronously (200). The 202 +
            # Location poll is kept as a forward-compatible safety net only.
            # The poll shares the POST's time budget: self._timeout is the
            # upper bound for the WHOLE turn (see config.py), not per leg.
            if resp.status_code == 202:
                polled = True
                resp = self._poll(
                    resp.headers.get("Location", ""),
                    cred=cred,
                    deadline=start + self._timeout,
                )
        except httpx.TimeoutException as exc:
            # The backend may still complete (and commit) this turn — typed so
            # the user-facing message can warn against a blind re-send.
            raise FaultMavenTimeoutError(
                f"submit_turn timed out after {self._timeout}s; the turn may "
                "still complete on the backend"
            ) from exc
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            # The full request body was already sent and we failed while reading
            # the RESPONSE, so the backend may have committed this turn — same
            # "indeterminate, don't blind-resend" class as a read timeout. NOTE:
            # a write-phase failure (WriteError) means the body never fully
            # landed → the turn did NOT commit → it belongs in the retryable
            # branch below (with ConnectError), NOT here, or a mid-upload reset
            # would tell the user not to retry a turn that never ran.
            raise FaultMavenTimeoutError(
                f"submit_turn lost the connection after sending ({exc}); the "
                "turn may still complete on the backend"
            ) from exc
        except httpx.HTTPError as exc:
            raise FaultMavenError(f"submit_turn failed: {exc}") from exc
        # A 404 means "case deleted" only on the turn POST itself. A 404 from
        # the polled Location URL is the STATUS resource expiring/moving — the
        # case may be alive, so it must not trigger the caller's mapping
        # eviction. A gateway 502/504 means an upstream was forwarded then timed
        # out — the same indeterminate/maybe-committed class as a read timeout,
        # so it is raised as FaultMavenTimeoutError HERE (in the client, for both
        # the POST and poll paths) rather than re-derived from a status code in
        # the UI layer.
        try:
            self._raise_for_status(
                resp, "submit_turn", not_found_is_case=not polled
            )
        except FaultMavenAPIError as exc:
            if exc.status_code in (502, 504):
                raise FaultMavenTimeoutError(
                    f"submit_turn hit a gateway timeout (HTTP {exc.status_code}); "
                    "the turn may still complete on the backend"
                ) from exc
            if exc.status_code == 409:
                # Several unrelated conflicts share this status, and they need
                # different advice. The two we recognize are the OCC version
                # conflict (labelled) and the terminal-case rejection (the route
                # raises it with no ``x-error-code``).
                #
                # The terminal case is therefore identified by the header being
                # ABSENT — not by "isn't the OCC code". Other middleware emits
                # labelled 409s on this path (the deduplication middleware sends
                # ``DUPLICATE_REQUEST`` + ``Retry-After``, and it skips only
                # multipart, so a text-only turn is in scope); treating those as
                # terminal would tell a user with a live case that their
                # investigation is closed. Anything labelled but unrecognized
                # falls through to the generic 4xx handling, which is the honest
                # answer for a conflict we don't model.
                #
                # Matching the prose of ``detail`` for "closed" instead would
                # break on any rewording of a message we don't own.
                error_code = resp.headers.get("x-error-code")
                if error_code == _VERSION_CONFLICT_CODE:
                    # The label is authoritative wherever it appears, so it is
                    # honored on the polled path too — an async turn that hits an
                    # OCC conflict mid-execution must still be re-sendable.
                    raise CaseVersionConflictError(
                        str(exc), status_code=409, detail=exc.detail
                    ) from exc
                # Unlabelled, and only off the POST itself: like the 404 above,
                # "this case is terminal" is a claim about the turn submission.
                # Asserting it from an unexpected 409 on the status resource
                # would tell a user with a live case that it is closed.
                if not error_code and not polled:
                    raise CaseTerminalError(
                        str(exc), status_code=409, detail=exc.detail
                    ) from exc
            raise

        # Status was 2xx here — the turn committed. Parsing must NEVER raise
        # past this point (a JSONDecodeError from a 200 non-JSON body, or a
        # scalar where a list is expected, would surface as a "try again" on a
        # committed turn); degrade to an empty body the tolerant parser renders.
        return self._parse_turn(self._json_or_empty(resp))

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _json_or_empty(resp: httpx.Response) -> dict[str, Any]:
        """The body as a dict, or ``{}`` for a non-JSON / non-object body.

        Used only after a 2xx (the turn committed): a committed turn must always
        render *something*, never raise a parse error into a retry-advising path.
        """

        try:
            body = resp.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _poll(
        self, location: str, *, cred: _Credential, deadline: float | None = None
    ) -> httpx.Response:
        """Poll an async-turn ``Location`` until it returns a result.

        ``deadline`` (monotonic) is the whole TURN's budget — set by
        submit_turn before its POST, so POST time plus polling never exceeds
        the configured single-turn bound. Both the inter-poll sleep and each
        GET's timeout are clamped to what remains.
        """

        if not location:
            raise FaultMavenError("202 response without a Location header")
        if deadline is None:
            deadline = time.monotonic() + self._timeout

        token = self._current_token(cred)
        delay = 1.5
        reauthed = False
        while time.monotonic() < deadline:
            if delay:
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            remaining = max(1.0, deadline - time.monotonic())
            resp = self._http.get(
                location,
                headers=self._auth_header(token),
                timeout=min(self._timeout, remaining),
            )
            if (
                resp.status_code == 401
                and not reauthed
                and (
                    cred.refresh_token
                    or (cred.dev_login_username and not cred.token_is_preset)
                )
            ):
                # A token can expire mid-poll on a long async turn — a 15-minute
                # access token under the refresh grant as easily as a 1h
                # dev-login one. Re-acquire once, same as _post. The endpoint is
                # known-ready — retry immediately, don't burn budget sleeping.
                logger.info("token rejected (401) mid-poll; re-authenticating")
                token = self._reauth(cred, token)
                reauthed = True
                delay = 0.0
                continue
            if resp.status_code != 202:
                return resp
            delay = min(max(delay, 1.5) * 1.5, 10.0)
        raise FaultMavenTimeoutError("timed out polling for async turn result")

    @staticmethod
    def _parse_turn(body: dict[str, Any]) -> TurnResult:
        """Map a ``TurnResponse`` body onto :class:`TurnResult`, tolerantly."""

        response = (
            body.get("agent_response")
            or body.get("summary")
            or body.get("message")
            or "FaultMaven returned no message for this turn."
        )
        if not isinstance(response, str):
            # Schema drift (e.g. a structured message object) must degrade to
            # displayable text, not TypeError deep inside a render/fallback
            # path where it would strand the thread's placeholder.
            response = json.dumps(response, default=str)
        cause_assurance = body.get("cause_assurance")
        if not isinstance(cause_assurance, str):
            cause_assurance = None
        cause_overclaim = body.get("cause_overclaim")
        if not isinstance(cause_overclaim, bool):
            cause_overclaim = None
        return TurnResult(
            agent_response=response,
            case_state=body.get("case_state"),
            turn_number=body.get("turn_number"),
            progress_made=bool(body.get("progress_made", False)),
            milestones_completed=_as_list(body.get("milestones_completed")),
            suggested_actions=_as_list(body.get("suggested_actions")),
            cause_assurance=cause_assurance,
            cause_overclaim=cause_overclaim,
            raw=body,
        )
