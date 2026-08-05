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


def _jwt_expiry(token: str) -> float | None:
    """The ``exp`` claim of a JWT as a POSIX timestamp, or None.

    Decode only — no signature check, and none is wanted: this reads the expiry
    of a token we already hold to decide *when* to renew it. The backend remains
    the only authority on whether it is valid. Hand-rolled rather than pulling
    in a JWT library for one claim.
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
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, (int, float)) else None


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
    ) -> None:
        # --- refresh-grant credential (ADR-012 D10) ---------------------------
        # The persisted credential wins over the configured one: rotation makes
        # the configured value a one-time seed that is revoked the moment it is
        # first used, so after the first renewal only the store is current.
        # ``_bootstrap_refresh_token`` is kept so that an operator who re-runs
        # provisioning (the documented lockout recovery) and restarts the agent
        # is not defeated by the dead token still sitting in the store — see
        # :meth:`_renew`.
        self._credential_store = credential_store
        self._bootstrap_refresh_token = refresh_token
        stored = credential_store.get() if credential_store is not None else None
        self._refresh_token = stored or refresh_token
        self._oauth_client_id = oauth_client_id
        self._access_expires_at = 0.0
        # True when the live credential is newer than what's on disk (a write
        # failed). The keepalive retries the write, so a transient volume
        # problem heals instead of becoming a restart-time lockout.
        self._credential_unpersisted = False
        # When the credential we hold was obtained, for the keepalive's fallback
        # cadence when a token's expiry can't be read.
        self._credential_obtained_at = time.monotonic()
        # Held ACROSS the renewal request, unlike the token lock below. Renewal
        # is single-use: two concurrent renewals with the same credential mean
        # the second presents a token the first already revoked. Serializing
        # costs one request's latency; not serializing costs a lockout.
        self._renew_lock = threading.Lock()
        self._keepalive_stop = threading.Event()
        self._keepalive: threading.Thread | None = None
        if self._refresh_token and credential_store is not None and not stored:
            # Seed the store from config on first boot, so the next restart
            # reads the rotated credential rather than the revoked seed.
            credential_store.put(self._refresh_token)

        self._token = "" if self._refresh_token else token
        # A preset token can't be re-acquired: never wipe it on a 401 (a
        # transient auth blip would otherwise discard it and every later
        # request would fail into a misleading dev-login error). Under the
        # refresh grant the access token IS re-acquirable, so it is not preset.
        self._token_is_preset = bool(token) and not self._refresh_token
        self._dev_login_username = dev_login_username
        self._timeout = timeout
        # The gate is per-thread but this client is shared, so concurrent turns
        # on different Slack threads race token acquisition/reauth. Serialize
        # the read-modify-write of ``self._token`` so one thread can't blank a
        # token another is about to send (empty bearer → spurious 401).
        self._token_lock = threading.Lock()
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

        if self._refresh_token:
            self._start_keepalive()
        if self._token:
            return
        try:
            self._ensure_token()
            logger.info(
                "Bootstrapped FaultMaven token via %s",
                "refresh grant" if self._refresh_token else "dev-login",
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
        acquired = self._renew_lock.acquire(timeout=self._timeout + 5.0)
        try:
            if not acquired:
                logger.warning(
                    "Closing the FaultMaven client with a renewal still in "
                    "flight; a rotated credential may be lost"
                )
            self._http.close()
            if self._credential_store is not None:
                self._credential_store.close()
        finally:
            if acquired:
                self._renew_lock.release()

    @property
    def auth_mode(self) -> str:
        """How this client authenticates, for diagnostics.

        Read off the client's own state rather than re-derived from settings:
        the steady state after bootstrap is a persisted credential with the
        one-time seed cleared, which settings alone cannot distinguish from
        having no credential at all.
        """

        if self._refresh_token:
            return "refresh grant"
        if self._token_is_preset:
            return "preset token"
        if self._dev_login_username:
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

    def verify_auth(self) -> None:
        """Confirm the current bearer token is actually accepted by the backend.

        ``_ensure_token`` only *obtains* a token (and short-circuits entirely
        when one is preset), so it can't catch a stale or wrong
        ``FAULTMAVEN_API_TOKEN`` — that surfaces as a 401 on the first real turn.
        This makes one authenticated call (``GET /api/v1/auth/me``) so preflight
        can fail fast instead. Raises :class:`FaultMavenError`; a message
        containing ``401`` means the token was rejected, any other status means
        the check was inconclusive (e.g. the endpoint isn't present).
        """

        self._ensure_token()
        try:
            resp = self._http.get("/api/v1/auth/me", headers=self._headers())
        except httpx.HTTPError as exc:
            raise FaultMavenError(f"auth check request failed: {exc}") from exc
        if resp.status_code == 401:
            raise FaultMavenError("token rejected (401): the bearer token is invalid")
        if resp.status_code != 200:
            raise FaultMavenError(
                f"auth check inconclusive (HTTP {resp.status_code} from /auth/me)"
            )

    # -- auth ---------------------------------------------------------------
    def _ensure_token(self) -> None:
        """Acquire a token if we don't have one (idempotent, thread-safe)."""

        self._current_token()

    def _current_token(self) -> str:
        """The bearer token, renewing or acquiring one as the mode requires."""

        if self._refresh_token:
            return self._access_token_via_refresh_grant()
        return self._dev_login_token()

    # -- refresh grant (ADR-012 D10) ----------------------------------------
    def _access_token_via_refresh_grant(self) -> str:
        """A live access token, renewing it before it expires."""

        token = self._token
        if token and time.monotonic() < self._access_expires_at:
            return token
        return self._renew(stale_access=token)

    def _renew(self, *, stale_access: str = "", force: bool = False) -> str:
        """Exchange the refresh token for a new token pair. Single-flight.

        The lock is held across the request — the opposite discipline to
        dev-login below, and deliberately so: dev-login is idempotent, whereas
        presenting a refresh token *consumes* it. Two threads renewing at once
        would each revoke the other's credential and the loser would be locked
        out until an operator intervened.

        ``force`` renews even when the access token is still good. The keepalive
        needs it: its job is to slide the *refresh* window, which has nothing to
        do with how long the current access token has left.
        """

        # Captured before the lock so we can tell "someone else rotated while we
        # queued" (their token is now current, ours is revoked) from "we are the
        # renewing thread".
        intended = self._refresh_token

        with self._renew_lock:
            if self._refresh_token != intended and self._token:
                return self._token
            if not force:
                current = self._token
                if (
                    current
                    and current != stale_access
                    and time.monotonic() < self._access_expires_at
                ):
                    return current

            try:
                return self._exchange_refresh_token(self._refresh_token)
            except _CredentialRejected:
                # Rejected means the credential we hold is no longer the live
                # one. Before declaring a lockout, try the two ways a newer one
                # can exist. (Only a backend rejection reaches here — a failed
                # persist no longer raises at all.)
                for candidate, source in self._alternative_credentials():
                    logger.warning(
                        "FaultMaven credential rejected; retrying with the %s",
                        source,
                    )
                    try:
                        return self._exchange_refresh_token(candidate)
                    except _CredentialRejected:
                        continue
                raise

    def _alternative_credentials(self) -> list[tuple[str, str]]:
        """Credentials worth trying after the one we hold was rejected.

        1. Whatever is on disk now. Another process may have rotated it —
           ``scripts/preflight.py`` authenticates, which under the refresh grant
           consumes a rotation. Without this, a valid token sitting in the store
           would be ignored and the running agent would stay locked out.
        2. The configured seed, for the documented recovery: the operator
           re-runs provisioning and restarts us while the store still holds the
           dead token.
        """

        tried = {self._refresh_token}
        alternatives: list[tuple[str, str]] = []

        if self._credential_store is not None:
            try:
                stored = self._credential_store.get()
            except Exception as exc:  # noqa: BLE001 — unreadable store is not fatal here
                logger.warning("Could not re-read the credential store: %s", exc)
                stored = None
            if stored and stored not in tried:
                tried.add(stored)
                alternatives.append((stored, "credential now in the store"))

        seed = self._bootstrap_refresh_token
        if seed and seed not in tried:
            alternatives.append((seed, "configured FAULTMAVEN_REFRESH_TOKEN"))

        return alternatives

    def _exchange_refresh_token(self, refresh_token: str) -> str:
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
                f"FaultMaven rejected the refresh credential (HTTP "
                f"{resp.status_code}): {self._error_detail(resp)}. It has expired "
                "or was revoked (a lost rotation, or two agents sharing one "
                "credential). Re-run the backend provisioning step "
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

        # Persist BEFORE use — but never at the cost of the token itself. The
        # rotated credential is the only live one (the presented token is
        # already revoked server-side), so discarding it on a write error would
        # turn a momentarily full or read-only volume into a permanent lockout
        # that recovering the disk cannot undo. Keep it, say so loudly, and
        # retry the write later: the process stays usable, and a restart before
        # the write succeeds is the only casualty.
        if self._credential_store is not None:
            try:
                self._credential_store.put(rotated)
                self._credential_unpersisted = False
            except Exception as exc:  # noqa: BLE001 — degraded, not fatal
                self._credential_unpersisted = True
                logger.error(
                    "Rotated FaultMaven refresh token could NOT be persisted "
                    "(%s). The agent keeps working, but a restart before this "
                    "write succeeds needs re-provisioning. Check the volume.",
                    exc,
                )

        expires_in = body.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, (int, float)) else 900.0
        with self._token_lock:
            self._refresh_token = rotated
            self._token = access
            self._credential_obtained_at = time.monotonic()
            self._access_expires_at = self._credential_obtained_at + max(
                lifetime - _ACCESS_EXPIRY_SKEW_SECONDS, 0.0
            )
        logger.info("Renewed FaultMaven access token via the refresh grant")
        return access

    def _start_keepalive(self) -> None:
        """Slide the refresh window even when no one is talking to the agent.

        Renewal is otherwise driven by traffic, and the refresh credential
        expires on wall-clock time. A workspace that goes quiet for longer than
        the server's refresh window would come back to a locked-out agent, which
        only an operator can fix.
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

    def _keepalive_loop(self) -> None:
        while not self._keepalive_stop.wait(_KEEPALIVE_INTERVAL_SECONDS):
            try:
                self._retry_pending_persist()
                if self._refresh_credential_is_due():
                    # force: the access token's remaining life is irrelevant —
                    # this renewal exists to slide the refresh window.
                    self._renew(force=True)
            except Exception as exc:  # noqa: BLE001 — a daemon must not die
                logger.warning("FaultMaven credential keepalive failed: %s", exc)

    def _retry_pending_persist(self) -> None:
        """Re-attempt a write that failed earlier, so the disk error can heal."""

        if not self._credential_unpersisted or self._credential_store is None:
            return
        token = self._refresh_token
        try:
            self._credential_store.put(token)
        except Exception as exc:  # noqa: BLE001 — still broken; try again later
            logger.warning("FaultMaven credential still not persistable: %s", exc)
            return
        self._credential_unpersisted = False
        logger.info("Rotated FaultMaven refresh token persisted after an earlier failure")

    def _refresh_credential_is_due(self) -> bool:
        """True when the refresh credential is close enough to expiry to renew."""

        expires_at = _jwt_expiry(self._refresh_token)
        if expires_at is None:
            # Expiry unreadable (not a JWT, or an opaque token): fall back to a
            # fixed cadence rather than assuming the credential is fine until it
            # silently isn't.
            age = time.monotonic() - self._credential_obtained_at
            return age >= _REFRESH_BLIND_RENEW_SECONDS
        return (expires_at - time.time()) <= _REFRESH_RENEW_MARGIN_SECONDS

    def _dev_login_token(self) -> str:
        """The bearer token, acquiring one via dev-login if absent.

        The lock is NOT held across ``_dev_login`` (a network call bounded only
        by the request timeout): holding it would serialize every cold-start
        turn behind one another during an auth outage (N×timeout pile-up). The
        login runs lock-free and the result is compare-and-set — so racing
        threads may each log in (bounded, parallel) but the token is never
        blanked, so no thread sends the empty bearer the lock-free-but-blanking
        original produced.
        """

        token = self._token
        if token:
            return token
        if not self._dev_login_username:
            raise FaultMavenError(
                "no FAULTMAVEN_API_TOKEN configured and dev-login disabled"
            )
        fresh = self._dev_login(self._dev_login_username)  # outside the lock
        with self._token_lock:
            if not self._token:
                self._token = fresh
            return self._token

    def _reauth(self, stale: str) -> str:
        """Re-acquire a token after a 401, unless another thread already did.

        Under the refresh grant this renews (the access token expired earlier
        than its stated lifetime — clock skew, or a server-side revocation).

        Otherwise, same discipline as :meth:`_dev_login_token`: ``_dev_login``
        runs outside
        the lock (no serial pile-up), and the result is compare-and-swap on the
        ``stale`` token so a token another thread already refreshed wins and the
        token is never transiently blanked.
        """

        if self._refresh_token:
            return self._renew(stale_access=stale)

        current = self._token
        if current and current != stale:
            return current  # another thread already refreshed
        fresh = self._dev_login(self._dev_login_username)  # outside the lock
        with self._token_lock:
            if self._token == stale or not self._token:
                self._token = fresh
            return self._token

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

    def _headers(self) -> dict[str, str]:
        return self._auth_header(self._token)

    def _post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: list | None = None,
    ) -> httpx.Response:
        """POST with the bearer token, re-authenticating once on a 401.

        Handles the long-lived-process case where the token expires (dev-login
        tokens default to a 1-hour TTL): a 401 triggers exactly one re-login +
        retry, so turns keep working without a restart.
        """

        token = self._current_token()
        resp = self._http.post(
            url, json=json, data=data, files=files,
            headers=self._auth_header(token),
        )
        if resp.status_code == 401:
            if not self._refresh_token and (
                self._token_is_preset or not self._dev_login_username
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
            token = self._reauth(token)
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
                (body.get("detail") or body.get("message"))
                if isinstance(body, dict)
                else None
            )
            text = detail if isinstance(detail, str) else resp.text
        except ValueError:
            text = resp.text
        return " ".join((text or "").split())[:300]

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
            raise FaultMavenAPIError(
                message, status_code=resp.status_code, detail=detail
            ) from exc

    # -- core calls ---------------------------------------------------------
    def create_case(
        self, *, title: str | None = None, initial_message: str | None = None
    ) -> str:
        """Create a case and return its ``case_id``.

        ``initial_message`` is supported but unused by the Slack agent (see the
        module docstring) — the first turn carries the user's text.
        """

        body: dict[str, Any] = {"title": title}
        if initial_message is not None:
            body["initial_message"] = initial_message
        try:
            resp = self._post("/api/v1/cases", json=body)
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

        start = time.monotonic()
        polled = False
        try:
            resp = self._post(
                f"/api/v1/cases/{case_id}/turns", data=form, files=file_parts
            )
            # The current backend answers turns synchronously (200). The 202 +
            # Location poll is kept as a forward-compatible safety net only.
            # The poll shares the POST's time budget: self._timeout is the
            # upper bound for the WHOLE turn (see config.py), not per leg.
            if resp.status_code == 202:
                polled = True
                resp = self._poll(
                    resp.headers.get("Location", ""),
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
        self, location: str, *, deadline: float | None = None
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

        token = self._current_token()
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
                    self._refresh_token
                    or (self._dev_login_username and not self._token_is_preset)
                )
            ):
                # A token can expire mid-poll on a long async turn — a 15-minute
                # access token under the refresh grant as easily as a 1h
                # dev-login one. Re-acquire once, same as _post. The endpoint is
                # known-ready — retry immediately, don't burn budget sleeping.
                logger.info("token rejected (401) mid-poll; re-authenticating")
                token = self._reauth(token)
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
