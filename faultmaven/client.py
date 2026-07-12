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

Auth is lazy and self-healing: the token is acquired on first use (or via an
optional local-mode dev-login bootstrap), re-acquired once on a 401, and never
fatal at startup — so the agent boots even when the backend is briefly down or
configured for an auth mode where dev-login isn't available.

Kept synchronous to match Bolt's synchronous listener model.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _as_list(value: Any) -> list:
    """A list from a tolerated field, or ``[]`` — never ``list(scalar)`` (which
    would ``TypeError`` on schema drift past a committed-turn boundary)."""

    return list(value) if isinstance(value, list) else []


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
    ) -> None:
        self._token = token
        # A preset token can't be re-acquired: never wipe it on a 401 (a
        # transient auth blip would otherwise discard it and every later
        # request would fail into a misleading dev-login error).
        self._token_is_preset = bool(token)
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

        if self._token:
            return
        try:
            self._ensure_token()
            logger.info("Bootstrapped FaultMaven token via dev-login")
        except Exception as exc:  # noqa: BLE001 — best-effort bootstrap, never fatal
            logger.warning("FaultMaven auth deferred: %s", exc)

    def close(self) -> None:
        self._http.close()

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
        """Re-login after a 401, unless another thread already refreshed.

        Same discipline as :meth:`_current_token`: ``_dev_login`` runs outside
        the lock (no serial pile-up), and the result is compare-and-swap on the
        ``stale`` token so a token another thread already refreshed wins and the
        token is never transiently blanked.
        """

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
            if self._token_is_preset or not self._dev_login_username:
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
            detail = body.get("detail") if isinstance(body, dict) else None
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
    ) -> TurnResult:
        """Submit one turn (multipart) and return the normalized result.

        ``files`` items are ``(filename, content, content_type)`` tuples.
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
                and self._dev_login_username
                and not self._token_is_preset
            ):
                # A dev-login token (1h TTL) can expire mid-poll on a long
                # async turn; re-login once, same as _post. The endpoint is
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
        return TurnResult(
            agent_response=response,
            case_state=body.get("case_state"),
            turn_number=body.get("turn_number"),
            progress_made=bool(body.get("progress_made", False)),
            milestones_completed=_as_list(body.get("milestones_completed")),
            suggested_actions=_as_list(body.get("suggested_actions")),
            raw=body,
        )
