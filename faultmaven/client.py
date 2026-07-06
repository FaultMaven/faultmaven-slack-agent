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
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class FaultMavenError(Exception):
    """Raised when the FaultMaven API cannot service a request."""


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
        self._dev_login_username = dev_login_username
        self._timeout = timeout
        self._http = httpx.Client(base_url=base_url, timeout=timeout)

    # -- lifecycle ----------------------------------------------------------
    def startup(self) -> None:
        """Best-effort token bootstrap — never fatal.

        If a token can't be obtained now (backend down, or not in local auth
        mode), we log and move on; the first turn re-attempts lazily.
        """

        if self._token:
            return
        try:
            self._ensure_token()
            logger.info("Bootstrapped FaultMaven token via dev-login")
        except FaultMavenError as exc:
            logger.warning("FaultMaven auth deferred: %s", exc)

    def close(self) -> None:
        self._http.close()

    def health(self) -> dict[str, Any]:
        """Probe the backend's top-level ``/health`` liveness endpoint.

        Used by the preflight doctor to confirm the API is reachable *before*
        the agent connects to Slack. It's public (no token) and side-effect free
        (creates no case), so it's safe to call on every startup check. We probe
        the app-wide ``/health`` (broad liveness — DB/LLM components) rather than
        the case-service ``/api/v1/cases/health``, which is a narrower signal.
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
        if self._token:
            return
        if not self._dev_login_username:
            raise FaultMavenError(
                "no FAULTMAVEN_API_TOKEN configured and dev-login disabled"
            )
        self._token = self._dev_login(self._dev_login_username)

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

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

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

        self._ensure_token()
        resp = self._http.post(
            url, json=json, data=data, files=files, headers=self._headers()
        )
        if resp.status_code == 401 and self._dev_login_username:
            logger.info("FaultMaven token rejected (401); re-authenticating")
            self._token = ""
            self._ensure_token()
            resp = self._http.post(
                url, json=json, data=data, files=files, headers=self._headers()
            )
        return resp

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
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FaultMavenError(f"create_case failed: {exc}") from exc

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

        try:
            resp = self._post(
                f"/api/v1/cases/{case_id}/turns", data=form, files=file_parts
            )
            # The current backend answers turns synchronously (200). The 202 +
            # Location poll is kept as a forward-compatible safety net only.
            if resp.status_code == 202:
                resp = self._poll(resp.headers.get("Location", ""))
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FaultMavenError(f"submit_turn failed: {exc}") from exc

        return self._parse_turn(resp.json())

    # -- helpers ------------------------------------------------------------
    def _poll(self, location: str) -> httpx.Response:
        """Poll an async-turn ``Location`` until it returns a result."""

        if not location:
            raise FaultMavenError("202 response without a Location header")

        deadline = time.monotonic() + self._timeout
        delay = 1.5
        while time.monotonic() < deadline:
            time.sleep(delay)
            resp = self._http.get(location, headers=self._headers())
            if resp.status_code != 202:
                return resp
            delay = min(delay * 1.5, 10.0)
        raise FaultMavenError("timed out polling for async turn result")

    @staticmethod
    def _parse_turn(body: dict[str, Any]) -> TurnResult:
        """Map a ``TurnResponse`` body onto :class:`TurnResult`, tolerantly."""

        return TurnResult(
            agent_response=(
                body.get("agent_response")
                or body.get("summary")
                or body.get("message")
                or "FaultMaven returned no message for this turn."
            ),
            case_state=body.get("case_state"),
            turn_number=body.get("turn_number"),
            progress_made=bool(body.get("progress_made", False)),
            milestones_completed=list(body.get("milestones_completed") or []),
            suggested_actions=list(body.get("suggested_actions") or []),
            raw=body,
        )
