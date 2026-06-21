"""Client for FaultMaven's core orchestration API.

The Slack agent is a thin *translation* layer: it turns a Slack thread into a
diagnostic request, hands it to FaultMaven's investigation engine, and renders
the result back into Block Kit. This module owns that boundary.

It ships with a built-in **mock mode** so the agent is runnable end-to-end for
the Slack Agent Builder Challenge without a live FaultMaven backend. Mock mode
activates automatically when no ``FAULTMAVEN_API_KEY`` is configured, and is
also used as a graceful fallback if the live API is unreachable — the user
always gets a coherent reply in-thread rather than a silent failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import get_settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConversationTurn:
    """A single message of thread history sent to the orchestration engine."""

    role: str  # "user" or "assistant"
    text: str
    user_id: str | None = None


@dataclass(slots=True)
class InvestigationRequest:
    """Everything the engine needs to advance one turn of an investigation.

    The Slack ``thread_ts`` is used verbatim as the engine's session id, so a
    thread maps one-to-one onto a FaultMaven case/investigation.
    """

    session_id: str  # Slack thread_ts — stable per investigation
    prompt: str  # The newest user message (mention text, shortcut payload)
    history: list[ConversationTurn] = field(default_factory=list)
    channel_id: str | None = None
    user_id: str | None = None


@dataclass(slots=True)
class InvestigationResult:
    """Normalized engine response, ready to render into Block Kit."""

    summary: str
    hypotheses: list[str] = field(default_factory=list)
    suggested_actions: list[str] = field(default_factory=list)
    case_url: str | None = None
    confidence: float | None = None
    is_mock: bool = False


class FaultMavenClient:
    """Async client wrapping the FaultMaven core orchestration API.

    A single instance owns one pooled :class:`httpx.AsyncClient`; create it at
    application startup and close it on shutdown.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.faultmaven_api_url
        self._api_key = settings.faultmaven_api_key
        self._timeout = settings.faultmaven_request_timeout
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle ----------------------------------------------------------
    async def startup(self) -> None:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=self._timeout,
            )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def mock_mode(self) -> bool:
        """True when no live backend is configured."""

        return not self._api_key

    # -- core call ----------------------------------------------------------
    async def investigate(self, req: InvestigationRequest) -> InvestigationResult:
        """Advance the investigation by one turn.

        Never raises on transport/HTTP failure: a mock result is returned so
        the Slack thread always receives a reply. Programming errors (e.g. a
        malformed response body) still surface in logs.
        """

        if self.mock_mode:
            logger.info("FaultMaven mock mode active (no API key); returning stub result")
            return self._mock_result(req)

        if self._client is None:
            await self.startup()
        assert self._client is not None  # for type-checkers

        payload = {
            "session_id": req.session_id,
            "message": req.prompt,
            "history": [
                {"role": t.role, "content": t.text, "user_id": t.user_id}
                for t in req.history
            ],
            "metadata": {
                "source": "slack",
                "channel_id": req.channel_id,
                "user_id": req.user_id,
            },
        }

        try:
            response = await self._client.post("/api/v1/investigations/turn", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "FaultMaven API call failed (%s); falling back to mock result", exc
            )
            return self._mock_result(req, degraded=True)

        return self._parse_result(response.json())

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _parse_result(body: dict[str, Any]) -> InvestigationResult:
        """Map the engine's JSON response onto :class:`InvestigationResult`."""

        return InvestigationResult(
            summary=body.get("summary") or body.get("message") or "No summary returned.",
            hypotheses=list(body.get("hypotheses", [])),
            suggested_actions=list(body.get("suggested_actions", [])),
            case_url=body.get("case_url"),
            confidence=body.get("confidence"),
            is_mock=False,
        )

    @staticmethod
    def _mock_result(
        req: InvestigationRequest, *, degraded: bool = False
    ) -> InvestigationResult:
        """Produce a deterministic, plausible result for demos/fallback."""

        prefix = (
            ":warning: _Live FaultMaven backend unreachable — showing a mock "
            "investigation so the thread stays responsive._\n\n"
            if degraded
            else ""
        )
        turn_count = len([t for t in req.history if t.role == "user"]) + 1
        summary = (
            f"{prefix}I've ingested the latest signal for this thread "
            f"(turn {turn_count}). Based on the symptoms described, the most "
            f"likely cause is a downstream dependency degradation rather than "
            f"a code regression."
        )
        return InvestigationResult(
            summary=summary,
            hypotheses=[
                "Upstream dependency latency spike (p99 > SLO)",
                "Connection-pool exhaustion under retry amplification",
                "Recent config/deploy change altering timeout budgets",
            ],
            suggested_actions=[
                "Check the dependency's error-rate dashboard for the affected window",
                "Correlate the onset time with the most recent deploy/config change",
                "Capture a thread dump or pool-saturation metric and paste it here",
            ],
            case_url=None,
            confidence=0.62,
            is_mock=True,
        )
