"""FaultMaven API client — contract + auth resilience.

Uses httpx.MockTransport to stand in for the FaultMaven backend, so these tests
exercise the real request/response shaping without a live server.
"""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from faultmaven.client import (
    FaultMavenClient,
    FaultMavenError,
    FaultMavenTimeoutError,
    TurnResult,
)


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


def test_submit_turn_connection_lost_after_send_is_indeterminate():
    """A dropped connection AFTER the request is on the wire (the backend may
    have committed the turn) must raise the timeout/indeterminate class, not a
    generic error that would advise a blind re-send."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("peer closed connection", request=request)

    client = make_client(handler, token="tok")
    with pytest.raises(FaultMavenTimeoutError):
        client.submit_turn("c1", query="x")


def test_submit_turn_connect_error_stays_retryable():
    """A ConnectError never reached the backend, so it is NOT indeterminate —
    it must remain a plain FaultMavenError (retry is safe)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = make_client(handler, token="tok")
    with pytest.raises(FaultMavenError) as exc_info:
        client.submit_turn("c1", query="x")
    assert not isinstance(exc_info.value, FaultMavenTimeoutError)


def test_submit_turn_200_non_json_body_degrades_not_raises():
    """A committed (200) turn whose body isn't JSON (e.g. a proxy HTML page)
    must render a fallback, never raise a parse error into a 'try again' path."""

    client = make_client(
        lambda req: httpx.Response(200, text="<html>proxy</html>"), token="tok"
    )
    result = client.submit_turn("c1", query="x")
    assert isinstance(result, TurnResult)
    assert result.agent_response  # non-empty fallback, no exception


def test_parse_turn_tolerates_scalar_list_fields():
    """Schema drift shipping a scalar where a list is expected must not
    TypeError on a committed turn (list(5) would)."""

    result = FaultMavenClient._parse_turn(
        {"agent_response": "a", "milestones_completed": 5, "suggested_actions": True}
    )
    assert result.milestones_completed == []
    assert result.suggested_actions == []


# -- health -------------------------------------------------------------------
def test_health_hits_top_level_endpoint_without_auth():
    """The preflight probe must use the app-wide /health (broad liveness), not
    the narrower /cases/health; it's public, so no token is required."""

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "healthy"})

    client = make_client(handler)  # no token, no dev-login
    assert client.health() == {"status": "healthy"}
    assert seen["path"] == "/health"
    assert seen["auth"] is None  # unauthenticated


def test_health_raises_on_error_status():
    client = make_client(lambda req: httpx.Response(503, json={}))
    with pytest.raises(FaultMavenError, match="health check failed"):
        client.health()


def test_health_raises_on_non_json_body():
    """A 200 from a proxy/login page (HTML) must degrade to a clear error, not
    an unhandled JSONDecodeError escaping the httpx.HTTPError guard."""

    client = make_client(
        lambda req: httpx.Response(200, text="<html>login</html>")
    )
    with pytest.raises(FaultMavenError, match="non-JSON"):
        client.health()


def test_health_raises_on_non_dict_json():
    client = make_client(lambda req: httpx.Response(200, json=["healthy"]))
    with pytest.raises(FaultMavenError, match="not an object"):
        client.health()


# -- verify_auth --------------------------------------------------------------
def test_verify_auth_passes_on_200():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"username": "admin"})

    client = make_client(handler, token="tok")
    client.verify_auth()  # must not raise
    assert seen["path"] == "/api/v1/auth/me"
    assert seen["auth"] == "Bearer tok"


def test_verify_auth_flags_401_as_token_rejected():
    """The false-green guard: a preset-but-invalid token must surface as a 401,
    not pass silently (preflight keys on '401' in the message)."""

    client = make_client(lambda req: httpx.Response(401, json={}), token="stale")
    with pytest.raises(FaultMavenError, match="401"):
        client.verify_auth()


def test_verify_auth_inconclusive_on_other_status():
    # e.g. /auth/me missing on an older backend → inconclusive, not "rejected".
    client = make_client(lambda req: httpx.Response(404, json={}), token="tok")
    with pytest.raises(FaultMavenError, match="inconclusive"):
        client.verify_auth()


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


def test_reauth_is_compare_and_swap_single_login():
    """Two turn threads racing the same token expiry must perform exactly one
    dev-login (CAS on the stale token), and both get the same fresh token — so
    neither transiently blanks a token the other is about to send."""

    logins = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dev-login"):
            logins["n"] += 1
            return httpx.Response(200, json={"access_token": f"t{logins['n']}"})
        return httpx.Response(200, json={})

    client = make_client(handler, dev="admin")
    client._token = "stale"

    barrier = threading.Barrier(2)
    results: list[str] = []

    def go() -> None:
        barrier.wait()
        results.append(client._reauth("stale"))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert logins["n"] == 1  # CAS → only one re-login despite two racers
    assert results == ["t1", "t1"]  # both threads see the same fresh token


def test_current_token_single_login_under_concurrency():
    logins = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        logins["n"] += 1
        return httpx.Response(200, json={"access_token": f"t{logins['n']}"})

    client = make_client(handler, dev="admin")
    barrier = threading.Barrier(8)
    tokens: list[str] = []

    def go() -> None:
        barrier.wait()
        tokens.append(client._current_token())

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert logins["n"] == 1  # acquired once under the lock, shared by all
    assert set(tokens) == {"t1"}


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
