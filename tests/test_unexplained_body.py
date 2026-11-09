"""A JSON body with no error explanation is never dumped (faultmaven#2591).

A token response is such a body, and so is a gateway echoing the request: a
refusal quoting it put an admin's tokens, a PKCE verifier or a refresh token
into a message the agent logs.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from sqlalchemy import create_engine

from binding import complete_bind
from faultmaven import FaultMavenClient, WorkspaceBindError
from faultmaven.client import _CredentialRejected
from pending_binds import BIND_COOKIE_NAME, PendingBindStore
from tests.test_install_binding import REVOKE_PATH, TOKEN_PATH, bind_client, open_bind
from tests.test_refresh_grant import make_client

__all__ = ["bind_client"]  # a fixture, used by name below

TOKENS = {"access_token": "ACCESS-SECRET", "refresh_token": "REFRESH-SECRET"}
UNEXPLAINED = "a JSON body with no error explanation"


def _client(handler) -> FaultMavenClient:
    client = FaultMavenClient("http://test")
    client._http.close()
    client._http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    return client


def _fastapi_422(sent: dict) -> dict:
    """FastAPI's default validation body, whose ``input`` echoes the request."""
    return {"detail": [{"type": "missing", "loc": ["body", "client_id"], "msg": "Field required",
                        "input": sent, "url": "https://errors.pydantic.dev/2/v/missing"}]}


def _echo(status: int, shape=lambda sent: sent):
    """A gateway answering with the request it was sent, in ``shape``."""
    return lambda request: httpx.Response(status, json=shape(json.loads(request.content)))


@pytest.mark.parametrize(
    "token_body",
    [pytest.param(TOKENS, id="tokens"), pytest.param({**TOKENS, "error": None}, id="error-null")],
)
def test_a_201_token_body_is_refused_unlogged_and_both_tokens_are_revoked(
    bind_client, tmp_path, monkeypatch, caplog, token_body
):
    """Through the real route, client and ``complete_bind``: web.py logs the
    refusal at WARNING, and the refused exchange's tokens reach its revocation."""
    import web

    caplog.set_level(logging.DEBUG)
    revokes: list[tuple[str, str]] = []
    refused: list[WorkspaceBindError] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(201, json=token_body)
        if request.url.path == REVOKE_PATH:
            body = json.loads(request.content)
            revokes.append((body["token"], body["token_type_hint"]))
        return httpx.Response(200)

    def real_complete(*, fm, **kwargs):  # the route's own client is replaced
        try:
            return complete_bind(fm=_client(handler), **kwargs)
        except WorkspaceBindError as exc:
            refused.append(exc)
            raise

    monkeypatch.setattr(web, "complete_bind", real_complete)
    record = open_bind(PendingBindStore(create_engine(f"sqlite:///{tmp_path / 'oauth.db'}")))
    bind_client.cookies.set(BIND_COOKIE_NAME, record.bind_id)
    resp = bind_client.get(
        "/faultmaven/callback", params={"code": "good-code", "state": record.state}
    )

    assert resp.status_code == 200 and "could not connect this workspace" in resp.text
    [exc] = refused
    assert exc.status_code == 201
    assert exc.issued == ("", ""), "the error that travels on to the log holds no token"
    logged = [rec.getMessage() for rec in caplog.records]
    assert any("Workspace bind failed" in m and UNEXPLAINED in m for m in logged)
    for secret in TOKENS.values():
        assert secret not in str(exc) and secret not in repr(exc)
        assert not [m for m in logged if secret in m]
    assert revokes == [("REFRESH-SECRET", "refresh_token"), ("ACCESS-SECRET", "access_token")]


@pytest.mark.parametrize(
    ("status", "shape", "shown"),
    [
        pytest.param(400, lambda sent: sent, UNEXPLAINED, id="echo"),
        pytest.param(422, _fastapi_422, "body.client_id: Field required", id="fastapi-422"),
    ],
)
def test_an_exchange_refusal_echoing_the_request_withholds_the_code_and_verifier(
    status, shape, shown
):
    with pytest.raises(WorkspaceBindError) as refused:
        _client(_echo(status, shape)).exchange_authorization_code(
            code="CODE-SECRET", code_verifier="VERIFIER-SECRET", redirect_uri="https://x/cb"
        )

    message = str(refused.value)
    assert shown in message
    assert "VERIFIER-SECRET" not in message and "CODE-SECRET" not in message


@pytest.mark.parametrize(
    ("shape", "shown"),
    [
        pytest.param(lambda sent: sent, UNEXPLAINED, id="echo"),
        pytest.param(_fastapi_422, "body.client_id: Field required", id="fastapi-422"),
    ],
)
def test_a_refresh_refusal_echoing_the_request_does_not_carry_the_refresh_token(shape, shown):
    client = make_client(_echo(400, shape), refresh_token="REFRESH-SECRET")

    with pytest.raises(_CredentialRejected) as rejected:
        client._current_token(client._default)

    assert shown in str(rejected.value)
    assert "REFRESH-SECRET" not in str(rejected.value)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        pytest.param(400, {"json": {"detail": "gone"}}, "gone", id="detail"),
        pytest.param(
            400, {"json": {"error": "invalid_grant", "error_description": "expired"}},
            "invalid_grant: expired", id="rfc6749",
        ),
        pytest.param(
            502, {"text": "<html>Bad Gateway</html>"}, "<html>Bad Gateway</html>", id="html"
        ),
        pytest.param(
            422, {"json": _fastapi_422({"query": "SECRET"})}, "body.client_id: Field required",
            id="list-detail",
        ),
        *[
            pytest.param(s, {"json": {**TOKENS, "token_type": "bearer"}}, UNEXPLAINED, id=f"keys-{s}")
            for s in (200, 201, 400)
        ],
        pytest.param(400, {"json": {"SECRET-TOK": {"a": 1}}}, UNEXPLAINED, id="value-keyed"),
        pytest.param(400, {"json": {}}, UNEXPLAINED, id="empty"),
        *[
            pytest.param(400, {"json": {key: value, **TOKENS}}, UNEXPLAINED, id=f"{key}-{value!r}")
            for key, value in [("detail", None), ("error", ""), ("message", 5),
                               ("detail", {"token": "SECRET"})]
        ],
    ],
)
def test_error_detail_keeps_an_explanation_or_text_and_never_quotes_a_json_object(
    status, body, expected
):
    assert FaultMavenClient._error_detail(httpx.Response(status, **body)) == expected
