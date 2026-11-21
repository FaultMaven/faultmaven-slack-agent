"""The assertion the bind sends, which the Cloud API verifies before it reads
anything about the workspace (faultmaven-cloud#83).

The agent signs, with the standard library, that it completed this workspace's
Slack install and the admin's FaultMaven sign-in. These tests check its claims
and signature, that it reaches the bind and nothing else, that a missing key
stops the flow before the code exchange, and that the encoder agrees with PyJWT
byte for byte (a known-answer vector the Cloud API's tests hold too).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from binding import complete_bind, sign_bind_assertion
from faultmaven import BindFailureReason, FaultMavenClient, WorkspaceBindError
from sqlalchemy import create_engine

from pending_binds import BIND_COOKIE_NAME, PendingBindStore
from tests.test_install_binding import (
    ADMIN_ACCESS,
    BIND_KEY,
    BIND_PATH,
    REVOKE_PATH,
    TOKEN_PATH,
    admin_access_token,
    bind_client,
    make_stores,
    open_bind,
)
from tests.test_workspace_credentials import jwt_with_enterprise

__all__ = ["bind_client"]  # a fixture, used by name below

BOUND = {"slack_team_id": "T1", "team_id": "fmteam", "refresh_token": jwt_with_enterprise("e")}


def _decoded(token: str, key: SecretStr) -> tuple[dict, dict]:
    """Header and claims, after checking the HS256 signature with ``key``."""
    header, payload, signature = token.split(".")
    expected = hmac.new(
        key.get_secret_value().encode(), f"{header}.{payload}".encode(), hashlib.sha256
    ).digest()
    assert base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)) == expected

    def part(seg: str) -> dict:
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))

    return part(header), part(payload)


def _run(tmp_path, *, on_bind, token=None, key=BIND_KEY, enterprise_id=""):
    """The real ``complete_bind`` over the real client and a mock FaultMaven.
    Returns the outcome, each request's ``(path, json body)`` and the revokes."""
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        seen.append((request.url.path, body))
        if request.url.path == TOKEN_PATH:
            return httpx.Response(
                200, json={"access_token": token or ADMIN_ACCESS, "refresh_token": "admin-rt"}
            )
        if request.url.path == BIND_PATH:
            return on_bind(request)
        return httpx.Response(200)

    client = FaultMavenClient("http://test")
    client._http.close()
    client._http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    pending, creds = make_stores(tmp_path)
    opened = pending.create(
        team_id="T1", enterprise_id=enterprise_id, installer_user_id="U1", team_name="Ops"
    )
    record = pending.consume(state=opened.state, bind_id=opened.bind_id)
    try:
        outcome = complete_bind(
            fm=client, workspace_credentials=creds, record=record,
            code="c", redirect_uri="https://x/cb", assertion_key=key,
        )
    except WorkspaceBindError as exc:
        outcome = exc
    finally:
        client._http.close()
    revokes = [(b["token"], b["token_type_hint"]) for p, b in seen if p == REVOKE_PATH]
    return outcome, seen, revokes


def _sent_assertion(seen) -> str:
    [body] = [b for p, b in seen if p == BIND_PATH]
    return body["slack_workspace_assertion"]


@pytest.mark.parametrize("grid", ["", "E0123GRID"])
def test_the_bind_carries_an_assertion_for_this_admin_and_workspace(tmp_path, grid):
    outcome, seen, _ = _run(
        tmp_path, on_bind=lambda r: httpx.Response(200, json=BOUND), enterprise_id=grid
    )

    assert outcome == "e"
    header, claims = _decoded(_sent_assertion(seen), BIND_KEY)
    assert header == {"alg": "HS256", "typ": "JWT"}
    assert list(claims) == [
        "iss", "aud", "sub", "slack_team_id", "slack_enterprise_id", "iat", "exp", "jti",
    ]
    assert claims["iss"] == "faultmaven-slack-agent"
    assert claims["aud"] == "faultmaven-cloud:slack-workspace-bind"
    assert claims["sub"] == "fm-admin-1", "the admin access token's own sub"
    assert (claims["slack_team_id"], claims["slack_enterprise_id"]) == ("T1", grid)
    assert type(claims["iat"]) is int and claims["exp"] - claims["iat"] == 60
    assert len(base64.urlsafe_b64decode(claims["jti"] + "==")) == 16


def test_without_a_key_nothing_is_exchanged(tmp_path):
    """A2's backstop: the flow is not offered without a key, and if reached
    anyway it stops before the code exchange, with existing copy."""
    outcome, seen, _ = _run(tmp_path, on_bind=None, key=SecretStr(""))

    assert isinstance(outcome, WorkspaceBindError)
    assert outcome.reason is BindFailureReason.NOT_SET_UP
    assert seen == [], "the code was not exchanged"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("opaque-admin-token", id="not-a-jwt"),
        pytest.param(admin_access_token(sub=""), id="empty-sub"),
        pytest.param(jwt_with_enterprise("e"), id="no-sub"),
    ],
)
def test_an_access_token_without_a_readable_sub_is_refused_and_revoked(tmp_path, token):
    outcome, seen, revokes = _run(tmp_path, on_bind=None, token=token)

    assert isinstance(outcome, WorkspaceBindError)
    assert outcome.reason is BindFailureReason.OTHER_REFUSAL
    assert BIND_PATH not in [p for p, _ in seen]
    assert revokes == [("admin-rt", "refresh_token"), (token, "access_token")]


def _echo(status, shape, *, as_json):
    """A gateway or backend quoting ``shape(assertion)`` back."""

    def on_bind(request):
        quoted = shape(json.loads(request.content)["slack_workspace_assertion"])
        if as_json:
            return httpx.Response(status, json={"detail": f"rejected {quoted}"})
        return httpx.Response(status, text=f"<html>bad request: {quoted}</html>")

    return on_bind


#: Each quote puts the piece it leaks inside the 300 characters an error keeps.
ECHOES = [
    pytest.param(_echo(403, lambda a: a, as_json=True), id="whole"),
    pytest.param(_echo(400, lambda a: a[-120:], as_json=False), id="tail"),
    pytest.param(_echo(403, lambda a: a.split(".")[1], as_json=True), id="payload"),
    pytest.param(_echo(502, lambda a: a.split(".")[2], as_json=False), id="signature"),
]


@pytest.mark.parametrize("on_bind", ECHOES)
def test_an_echoed_assertion_reaches_no_error_text_or_log_line(tmp_path, caplog, on_bind):
    """A3: the payload and the signature are each withheld, whole or quoted alone."""
    caplog.set_level(logging.DEBUG)

    outcome, seen, _ = _run(tmp_path, on_bind=on_bind)

    payload, signature = _sent_assertion(seen).split(".")[1:]
    assert isinstance(outcome, WorkspaceBindError)
    assert "<withheld>" in str(outcome), "the echo was there to withhold"
    for text in (str(outcome), repr(outcome), caplog.text):
        assert payload not in text and signature not in text
    assert BIND_KEY.get_secret_value() not in caplog.text


def test_the_routes_warning_withholds_an_echoed_signature(
    bind_client, tmp_path, monkeypatch, caplog
):
    """Through the callback route, whose WARNING logs the refusal's text."""
    import web

    caplog.set_level(logging.DEBUG)
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(
                200, json={"access_token": ADMIN_ACCESS, "refresh_token": "admin-rt"}
            )
        if request.url.path == BIND_PATH:
            sent.append(json.loads(request.content)["slack_workspace_assertion"])
            return httpx.Response(403, json={"detail": f"bad signature {sent[0].split('.')[2]}"})
        return httpx.Response(200)

    def real_complete(*, fm, **kwargs):
        client = FaultMavenClient("http://test")
        client._http.close()
        client._http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
        return complete_bind(fm=client, **kwargs)

    monkeypatch.setattr(web, "complete_bind", real_complete)
    record = open_bind(PendingBindStore(create_engine(f"sqlite:///{tmp_path / 'oauth.db'}")))
    bind_client.cookies.set(BIND_COOKIE_NAME, record.bind_id)
    resp = bind_client.get(
        "/faultmaven/callback", params={"code": "good-code", "state": record.state}
    )

    [assertion] = sent
    warned = [r.getMessage() for r in caplog.records if "Workspace bind failed" in r.getMessage()]
    assert len(warned) == 1 and "<withheld>" in warned[0]
    for piece in assertion.split(".")[1:]:
        assert piece not in caplog.text and piece not in resp.text


@pytest.mark.parametrize("key,warned", [("", True), (BIND_KEY.get_secret_value(), False)])
def test_two_urls_without_a_key_are_named_at_startup(monkeypatch, tmp_path, caplog, key, warned):
    """Binding the operator asked for, turned off by a missing key, says so."""
    import config
    from web import create_fastapi_app

    for name, value in {
        "SLACK_TRANSPORT": "http",
        "SLACK_CLIENT_ID": "123.456",
        "SLACK_CLIENT_SECRET": "secret",
        "SLACK_SIGNING_SECRET": "signsign",
        "SLACK_DATABASE_URL": f"sqlite:///{tmp_path / 'oauth.db'}",
        "CASE_STORE_PATH": str(tmp_path / "cases.db"),
        "FAULTMAVEN_API_TOKEN": "preset-token",
        "FAULTMAVEN_DASHBOARD_URL": "https://app.faultmaven.ai",
        "FAULTMAVEN_OAUTH_REDIRECT_URI": "https://slack.faultmaven.ai/faultmaven/callback",
        "SLACK_BIND_ASSERTION_KEY": key,
    }.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()
    caplog.set_level(logging.WARNING)
    try:
        create_fastapi_app()
    finally:
        config.get_settings.cache_clear()

    named = [r for r in caplog.records if "SLACK_BIND_ASSERTION_KEY" in r.getMessage()]
    assert bool(named) is warned


#: The vector the Cloud API's tests hold, computed with PyJWT 2.15.1.
KNOWN_ANSWER_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJmYXVsdG1hdmVuLXNsYWNrLWFnZW50IiwiYXVkIjoiZmF1bHRtYXZlbi1jbG91ZDpzbGFjay13"
    "b3Jrc3BhY2UtYmluZCIsInN1YiI6ImZtLXVzZXItMDAwMSIsInNsYWNrX3RlYW1faWQiOiJUMDEyM0FCQ0Qi"
    "LCJzbGFja19lbnRlcnByaXNlX2lkIjoiIiwiaWF0IjoxNzY3MjI1NjAwLCJleHAiOjE3NjcyMjU2NjAsImp0"
    "aSI6IkFBRUNBd1FGQmdjSUNRb0xEQTBPRHcifQ."
    "3JtfJroWTeMxEuAGkSJRWLNqDLd53VI_5QIwekbqtn4"
)


def test_the_encoder_matches_pyjwt_byte_for_byte():
    token = sign_bind_assertion(
        key=SecretStr("known-answer-vector-key-not-a-secret"),
        subject="fm-user-0001",
        slack_team_id="T0123ABCD",
        slack_enterprise_id="",
        now=1767225600,
        jti="AAECAwQFBgcICQoLDA0ODw",
    )
    assert token == KNOWN_ANSWER_TOKEN


@pytest.fixture
def http_env(monkeypatch):
    for key, value in {
        "SLACK_TRANSPORT": "http",
        "SLACK_CLIENT_ID": "123.456",
        "SLACK_CLIENT_SECRET": "secret",
        "SLACK_SIGNING_SECRET": "signsign",
        "SLACK_DATABASE_URL": "sqlite:///unused.db",
        "FAULTMAVEN_DASHBOARD_URL": "https://app.faultmaven.ai",
        "FAULTMAVEN_OAUTH_REDIRECT_URI": "https://slack.faultmaven.ai/faultmaven/callback",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SLACK_BIND_ASSERTION_KEY", raising=False)


def test_binding_is_offered_only_with_a_key(http_env, monkeypatch):
    """A2: a keyless agent never starts a flow the Cloud API would refuse."""
    from config import Settings

    assert Settings(_env_file=None).install_binding_enabled is False
    monkeypatch.setenv("SLACK_BIND_ASSERTION_KEY", BIND_KEY.get_secret_value())
    assert Settings(_env_file=None).install_binding_enabled is True


@pytest.mark.parametrize(
    "value",
    ["k" * 31, "é" * 15 + "k", " " + "k" * 40, "k" * 40 + "\n"],
    ids=["31-bytes", "16-chars-31-bytes", "leading-space", "trailing-newline"],
)
def test_an_unusable_key_fails_at_load_without_printing_it(http_env, monkeypatch, value):
    """A7: at least 32 UTF-8 bytes, no surrounding whitespace."""
    from config import Settings

    monkeypatch.setenv("SLACK_BIND_ASSERTION_KEY", value)
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    assert value.strip() not in str(caught.value)


def test_a_key_that_is_not_utf8_fails_at_load_without_naming_its_bytes(http_env, monkeypatch):
    """A non-UTF-8 environment value arrives as a lone surrogate, and encoding it
    raises an error that names the character and its position."""
    from config import Settings

    monkeypatch.setenv("SLACK_BIND_ASSERTION_KEY", "k" * 20 + "\udcff" + "k" * 20)
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    text = str(caught.value)
    assert "must be valid UTF-8" in text
    assert "udcff" not in text and "position" not in text and "k" * 20 not in text
