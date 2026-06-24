"""FaultMaven API client — contract + auth resilience.

Uses httpx.MockTransport to stand in for the FaultMaven backend, so these tests
exercise the real request/response shaping without a live server.
"""

from __future__ import annotations

import json

import httpx
import pytest

from faultmaven.client import FaultMavenClient, FaultMavenError, TurnResult


def make_client(handler, *, token: str = "", dev: str = "") -> FaultMavenClient:
    client = FaultMavenClient("http://test", token=token, dev_login_username=dev)
    client._http = httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )
    return client


# -- create_case --------------------------------------------------------------
def test_create_case_sends_json_body_without_initial_message():
    """Regression for the duplicate-opening-message bug: the agent must create
    the case with title only and NOT seed initial_message."""

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(201, json={"case_id": "c1", "state": "inquiry"})

    client = make_client(handler, token="tok")
    case_id = client.create_case(title=None)

    assert case_id == "c1"
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/v1/cases")
    assert seen["body"] == {"title": None}  # no initial_message
    assert seen["auth"] == "Bearer tok"


def test_create_case_raises_when_no_case_id():
    client = make_client(lambda req: httpx.Response(201, json={}), token="tok")
    with pytest.raises(FaultMavenError, match="no case_id"):
        client.create_case(title=None)


# -- submit_turn --------------------------------------------------------------
def test_submit_turn_sends_form_fields():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "agent_response": "looking into it",
                "turn_number": 1,
                "case_state": "investigating",
            },
        )

    client = make_client(handler, token="tok")
    result = client.submit_turn("c1", query="why down", pasted_content="ERR x")

    assert isinstance(result, TurnResult)
    assert result.case_state == "investigating"
    assert "query" in seen["body"] and "why+down" in seen["body"]
    assert "pasted_content" in seen["body"]


def test_submit_turn_multipart_when_files_present():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ct"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(200, json={"agent_response": "ok"})

    client = make_client(handler, token="tok")
    client.submit_turn("c1", query="see log", files=[("err.log", b"boom", "text/plain")])

    assert seen["ct"].startswith("multipart/form-data")
    assert b"err.log" in seen["body"] and b"boom" in seen["body"]


def test_submit_turn_requires_at_least_one_input():
    client = make_client(lambda req: httpx.Response(200, json={}), token="tok")
    with pytest.raises(FaultMavenError, match="at least one"):
        client.submit_turn("c1")


# -- auth resilience ----------------------------------------------------------
def test_401_triggers_single_reauth_and_retry():
    state = {"posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dev-login"):
            return httpx.Response(200, json={"access_token": "fresh"})
        state["posts"] += 1
        if state["posts"] == 1:
            return httpx.Response(401, json={})
        return httpx.Response(201, json={"case_id": "c9"})

    client = make_client(handler, dev="admin")
    assert client.create_case(title=None) == "c9"
    assert state["posts"] == 2  # one 401, one successful retry


def test_dev_login_404_raises_clear_error():
    client = make_client(lambda req: httpx.Response(404, json={}), dev="admin")
    with pytest.raises(FaultMavenError, match="local auth mode"):
        client._dev_login("admin")


def test_startup_is_non_fatal_when_auth_unavailable():
    client = make_client(lambda req: httpx.Response(404, json={}), dev="admin")
    client.startup()  # must not raise
    assert client._token == ""


def test_ensure_token_errors_without_token_or_devlogin():
    client = make_client(lambda req: httpx.Response(200, json={}))
    with pytest.raises(FaultMavenError, match="no FAULTMAVEN_API_TOKEN"):
        client._ensure_token()


# -- response parsing ---------------------------------------------------------
def test_parse_turn_omits_hypotheses_and_confidence():
    """These are not part of TurnResponse; they must not be modeled as dead
    fields the renderer would advertise."""

    result = FaultMavenClient._parse_turn(
        {
            "agent_response": "a",
            "case_state": "inquiry",
            "hypotheses": ["h"],
            "confidence": 0.9,
            "milestones_completed": ["symptom_verified"],
            "suggested_actions": [{"type": "RUN", "payload": "ls"}],
        }
    )
    assert not hasattr(result, "hypotheses")
    assert not hasattr(result, "confidence")
    assert result.milestones_completed == ["symptom_verified"]
    assert result.suggested_actions == [{"type": "RUN", "payload": "ls"}]


def test_parse_turn_defaults_for_empty_body():
    result = FaultMavenClient._parse_turn({})
    assert result.agent_response  # non-empty fallback string
    assert result.milestones_completed == [] and result.suggested_actions == []
