"""The bind page's advice and footer follow the failure's declared reason
(faultmaven#2121).

Every raise site, and every published ``x-error-code``, is driven through the
real ``/faultmaven/callback`` route, client and ``complete_bind``. Each asserts
the exact message and footer: no status alone picks a specific claim, and no
failure after the bind, or of unknown outcome, says nothing was changed.
"""

from __future__ import annotations

from html import escape

import httpx
import pytest
from sqlalchemy import create_engine

from binding import complete_bind
from faultmaven import BindFailureReason, FaultMavenClient
from faultmaven.client import _BIND_ERROR_CODES
from pending_binds import BIND_COOKIE_NAME, PendingBindStore
from tests.test_install_binding import BIND_PATH, TOKEN_PATH, bind_client, open_bind
from tests.test_workspace_credentials import jwt_with_enterprise

__all__ = ["bind_client"]  # a fixture, used by name below

#: The codes the bind route publishes, each at its status.
PUBLISHED_CODES = {
    (403, "INSTALLER_NOT_IN_ENTERPRISE"),
    (409, "WORKSPACE_BOUND_UNDER_OTHER_TEAM_NAME"),
    (409, "NOT_ADMIN_OF_BOUND_TEAM"),
    (409, "TEAM_NAME_TAKEN"),
    (409, "WORKSPACE_NOT_BINDABLE_HERE"),
    (500, "CREDENTIAL_NOT_MINTED"),
    (500, "TEAM_MEMBERSHIP_NOT_WRITTEN"),
    (500, "BIND_AUDIT_NOT_WRITTEN"),
    (500, "BIND_FAILED"),
    (503, "MULTI_TENANCY_NOT_ENABLED"),
    (503, "USER_STORE_NOT_CONFIGURED"),
    (503, "OAUTH_NOT_CONFIGURED"),
}

LOGS = "Ask a FaultMaven administrator to check the server logs for the reason."
MESSAGE = {
    "unreachable": "FaultMaven could not be reached. Try again in a few minutes; if it "
    "keeps failing, ask your FaultMaven administrator to check that FaultMaven is up.",
    "sign-in": "FaultMaven did not accept the sign-in for this connection. Re-run the "
    "installation to sign in again.",
    "unavailable": "FaultMaven is unavailable right now. Try again in a few minutes; if "
    "it keeps failing, ask your FaultMaven administrator.",
    "not-set-up": "This FaultMaven deployment is not set up to connect Slack workspaces "
    "yet. Ask your FaultMaven administrator to finish its setup.",
    "403": f"FaultMaven refused to let your account connect this workspace. {LOGS}",
    "other-name": "This Slack workspace is already connected to one of your FaultMaven "
    "teams, under a different name. Rename that team to match, or disconnect it, then "
    "re-run the installation.",
    "not-admin": "This Slack workspace is already connected to a FaultMaven team you "
    "don't administer. Ask that team's administrator to change it.",
    "name-taken": "FaultMaven already has a team or account with the name this "
    "workspace needs. Ask a FaultMaven administrator to free the name.",
    "not-here": f"This workspace can't be connected here. {LOGS}",
    "other": f"FaultMaven could not connect this workspace. {LOGS}",
    "interrupted": "The connection to FaultMaven was interrupted before it finished.",
    "server-error": "FaultMaven hit an error while connecting this workspace.",
    "not-stored": "This workspace was connected in FaultMaven, but the agent could not "
    "store its credential, so it can't be used yet.",
    "unusable": "This workspace was connected in FaultMaven, but the agent could not "
    "keep its credential, so it can't be used yet. Ask a FaultMaven administrator to "
    "re-issue the workspace credential.",
    "unexplained": "Something went wrong finishing the connection to FaultMaven.",
}
UNCHANGED = "Nothing was changed in FaultMaven."
FOOTER = {
    "retry": f"{UNCHANGED} Re-run the installation to try again.",
    "fix-first": f"{UNCHANGED} Re-running the installation won't change this until "
    "it's fixed.",
    "re-run-when-stored": "The workspace was created in FaultMaven. Once the agent can "
    "store credentials again, re-run the installation: it issues a fresh credential.",
    "will-not-fix": "The workspace was created in FaultMaven, so re-running the "
    "installation will not fix this.",
    "unknown": "FaultMaven may or may not have connected the workspace. Re-running the "
    "installation is safe: it either finishes the connection or says what is in the way.",
}
ROW = {  # message -> footer, one per row of the table
    "unreachable": "retry", "sign-in": "retry", "unavailable": "retry",
    "not-set-up": "fix-first", "403": "fix-first", "other-name": "fix-first",
    "not-admin": "fix-first", "name-taken": "fix-first", "not-here": "fix-first",
    "other": "fix-first", "interrupted": "unknown", "server-error": "unknown",
    "not-stored": "re-run-when-stored", "unusable": "will-not-fix",
    "unexplained": "unknown",
}


def _raise(error):
    def handler(request):
        raise error("SENTINEL", request=request)

    return handler


def _status(status, *, code=None, **body):
    headers = {"x-error-code": code} if code else {}
    return lambda request: httpx.Response(status, headers=headers, **body)


TOKENS = _status(200, json={"access_token": "admin-at", "refresh_token": "admin-rt"})
BOUND = {"slack_team_id": "T1", "team_id": "fmteam", "refresh_token": jwt_with_enterprise("e")}


def _bound(**changes):
    body = {k: v for k, v in {**BOUND, **changes}.items() if v is not None}
    return _status(200, json=body)


EXCHANGE = [
    pytest.param(handler, None, row, id=f"exchange-{name}")
    for name, handler, row in [
        ("connect-error", _raise(httpx.ConnectError), "unreachable"),
        ("pool-timeout", _raise(httpx.PoolTimeout), "unreachable"),
        ("read-timeout", _raise(httpx.ReadTimeout), "interrupted"),
        ("400-invalid-grant", _status(400, json={"error": "invalid_grant"}), "sign-in"),
        ("401-invalid-grant", _status(401, json={"error": "invalid_grant"}), "sign-in"),
        ("400-invalid-request", _status(400, json={"error": "invalid_request"}), "other"),
        ("403", _status(403, json={"detail": "SENTINEL"}), "other"),
        ("429", _status(429), "unavailable"),
        ("500", _status(500), "unavailable"),
        ("503", _status(503, json={"detail": "SENTINEL"}), "unavailable"),
        ("200-html", _status(200, text="<html>SENTINEL</html>"), "other"),
        ("200-list", _status(200, json=["SENTINEL"]), "other"),
        ("no-access-token", _status(200, json={"refresh_token": "admin-rt"}), "other"),
    ]
]

BIND = [
    pytest.param(TOKENS, handler, row, id=f"bind-{name}")
    for name, handler, row in [
        ("connect-error", _raise(httpx.ConnectError), "unreachable"),
        ("connect-timeout", _raise(httpx.ConnectTimeout), "unreachable"),
        ("pool-timeout", _raise(httpx.PoolTimeout), "unreachable"),
        ("unsupported-protocol", _raise(httpx.UnsupportedProtocol), "unreachable"),
        ("proxy-error", _raise(httpx.ProxyError), "unreachable"),
        ("read-timeout", _raise(httpx.ReadTimeout), "interrupted"),
        ("read-error", _raise(httpx.ReadError), "interrupted"),
        ("protocol-error", _raise(httpx.RemoteProtocolError), "interrupted"),
        ("500", _status(500, json={"detail": "SENTINEL"}), "server-error"),
        ("502", _status(502, text="SENTINEL"), "server-error"),
        ("504", _status(504), "server-error"),
        ("403", _status(403), "403"),
        ("409", _status(409, json={"detail": "SENTINEL"}), "other"),
        ("409-unknown-code", _status(409, code="IDEMPOTENCY_IN_FLIGHT"), "other"),
        ("503", _status(503), "unavailable"),
        ("503-unknown-code", _status(503, code="SOMETHING_ELSE"), "unavailable"),
        ("429", _status(429), "unavailable"),
        ("400", _status(400, json={"detail": "SENTINEL"}), "other"),
        ("422", _status(422), "other"),
        ("302", _status(302), "unexplained"),
        ("200-html", _status(200, text="<html>SENTINEL</html>"), "unexplained"),
        ("200-list", _status(200, json=["SENTINEL"]), "unexplained"),
        ("200-null", _status(200, text="null"), "unexplained"),
        ("200-neither-field", _status(200, json={"status": "SENTINEL"}), "unexplained"),
        ("no-refresh-token", _bound(refresh_token=None), "unusable"),
        ("no-team-id", _bound(team_id=None), "unusable"),
        ("refresh-token-not-a-string", _bound(refresh_token=12345), "unusable"),
        ("no-enterprise", _bound(refresh_token=jwt_with_enterprise(None)), "unusable"),
    ]
]

CODED = [
    pytest.param(TOKENS, _status(status, code=code), row, id=f"bind-{status}-{code}")
    for (status, code), row in {
        (403, "INSTALLER_NOT_IN_ENTERPRISE"): "403",
        (409, "WORKSPACE_BOUND_UNDER_OTHER_TEAM_NAME"): "other-name",
        (409, "NOT_ADMIN_OF_BOUND_TEAM"): "not-admin",
        (409, "TEAM_NAME_TAKEN"): "name-taken",
        (409, "WORKSPACE_NOT_BINDABLE_HERE"): "not-here",
        (500, "CREDENTIAL_NOT_MINTED"): "server-error",
        (500, "TEAM_MEMBERSHIP_NOT_WRITTEN"): "server-error",
        (500, "BIND_AUDIT_NOT_WRITTEN"): "server-error",
        (500, "BIND_FAILED"): "server-error",
        (503, "MULTI_TENANCY_NOT_ENABLED"): "not-set-up",
        (503, "USER_STORE_NOT_CONFIGURED"): "not-set-up",
        (503, "OAUTH_NOT_CONFIGURED"): "not-set-up",
    }.items()
]


class _BrokenStore:
    def bind(self, **kwargs):
        raise OSError("SENTINEL disk full")


def _callback(bind_client, tmp_path, monkeypatch, handler, *, store=None, forget=None):
    import web

    def real_complete(*, fm, workspace_credentials, **kwargs):
        client = FaultMavenClient("http://test")
        client._http.close()
        client._http = httpx.Client(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
        if forget is not None:
            client.forget_workspace = forget
        return complete_bind(
            fm=client, workspace_credentials=store or workspace_credentials, **kwargs
        )

    monkeypatch.setattr(web, "complete_bind", real_complete)
    record = open_bind(PendingBindStore(create_engine(f"sqlite:///{tmp_path / 'oauth.db'}")))
    bind_client.cookies.set(BIND_COOKIE_NAME, record.bind_id)
    return bind_client.get(
        "/faultmaven/callback", params={"code": "good-code", "state": record.state}
    )


def _routes(on_token, on_bind):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return on_token(request)
        if request.url.path == BIND_PATH:
            return on_bind(request)
        return httpx.Response(200)  # the revokes

    return handler


def _assert_row(resp, row):
    assert resp.status_code == 200
    assert f"<p>{escape(MESSAGE[row])}</p>" in resp.text
    assert f'<p class="muted">{escape(FOOTER[ROW[row]])}</p>' in resp.text
    assert "SENTINEL" not in resp.text


@pytest.mark.parametrize(("on_token", "on_bind", "row"), [*EXCHANGE, *BIND, *CODED])
def test_each_failure_shows_its_reasons_message_and_footer(
    bind_client, tmp_path, monkeypatch, on_token, on_bind, row
):
    resp = _callback(bind_client, tmp_path, monkeypatch, _routes(on_token, on_bind))

    _assert_row(resp, row)


def test_a_credential_the_agent_could_not_store_advises_a_re_run_once_it_can(
    bind_client, tmp_path, monkeypatch, caplog
):
    """A re-run of the install issues a fresh credential: it is the recovery.
    The store's error is logged by class only: a database error's text quotes
    its bound parameters, the credential among them."""
    resp = _callback(
        bind_client, tmp_path, monkeypatch, _routes(TOKENS, _bound()), store=_BrokenStore()
    )

    _assert_row(resp, "not-stored")
    assert "OSError" in caplog.text
    assert "SENTINEL" not in caplog.text


def test_an_exception_that_is_not_a_bind_failure_claims_nothing_about_faultmaven(
    bind_client, tmp_path, monkeypatch
):
    def forget(team_id):
        raise RuntimeError("SENTINEL")

    resp = _callback(
        bind_client, tmp_path, monkeypatch, _routes(TOKENS, _bound()), forget=forget
    )

    _assert_row(resp, "unexplained")


def test_every_published_code_has_its_own_row():
    """Pins this repository's copy of the published vocabulary: every code in
    it has its own row, and a code dropped from the table fails here instead of
    falling silently to its status's row. It cannot see a backend change."""
    assert set(_BIND_ERROR_CODES) == PUBLISHED_CODES
    for reason in _BIND_ERROR_CODES.values():
        assert reason not in (BindFailureReason.OTHER_REFUSAL, BindFailureReason.UNEXPLAINED)
