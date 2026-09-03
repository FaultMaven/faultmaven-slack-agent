"""Install-time workspace binding — the join between two OAuth flows.

The Slack install and the FaultMaven consent are each unremarkable. The risk is
the seam, and one attack in particular:

    An attacker installs the app into **their own** Slack workspace, which is
    entirely legitimate and yields a FaultMaven authorize URL. They forward that
    URL to an admin of a **victim** organization. The dashboard consent screen
    cannot name a workspace — it renders the client name and a caller-supplied
    scope string — so nothing on it looks wrong, and the victim approves. If the
    callback trusted ``state`` alone, the attacker's workspace would be bound
    into the victim's tenant: a service account on a Team inside their
    organization, receiving the attacker's Slack traffic.

Every test below exists because of that, or because of a way the same authority
could leak: the cookie half, single use, expiry, where ``slack_team_id`` is read
from, and whether the admin's borrowed org-admin token outlives the one call it
was obtained for.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

import install_pages
from app import _bind_cookie
import app as app_module
from binding import (
    InstallerAuthority,
    authorize_url,
    code_challenge_for,
    complete_bind,
    installer_authority,
)
from faultmaven import WorkspaceBindError, WorkspaceBinding
from pending_binds import BIND_COOKIE_NAME, PendingBindStore
from workspace_credentials import WorkspaceCredentialStore


def make_stores(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth.db'}")
    return PendingBindStore(engine), WorkspaceCredentialStore(engine)


def open_bind(pending, *, team_id="T-ATTACKER", name="Attacker Corp"):
    return pending.create(
        team_id=team_id, enterprise_id="", installer_user_id="U1", team_name=name
    )


class FakeFM:
    """Records what the flow did with the admin's borrowed authority."""

    def __init__(self, *, bind_error: Exception | None = None):
        self.bind_error = bind_error
        self.exchanged: list[tuple] = []
        self.bound: list[dict] = []
        self.revoked: list[tuple[str, str]] = []
        self.forgotten: list[str] = []
        self.revoke_ok = True

    def forget_workspace(self, team_id):
        self.forgotten.append(team_id)

    def exchange_authorization_code(self, *, code, code_verifier, redirect_uri):
        self.exchanged.append((code, code_verifier, redirect_uri))
        return "admin-access", "admin-refresh"

    def bind_workspace(self, *, admin_access_token, slack_team_id, team_name,
                       slack_enterprise_id=None):
        self.bound.append(
            {
                "token": admin_access_token,
                "slack_team_id": slack_team_id,
                "team_name": team_name,
                "slack_enterprise_id": slack_enterprise_id,
            }
        )
        if self.bind_error:
            raise self.bind_error
        return WorkspaceBinding(
            slack_team_id=slack_team_id,
            organization_id="org-victim",
            team_id="fmteam-1",
            team_name=team_name,
            service_account_username=f"slack-{slack_team_id}",
            refresh_token="sa-refresh",
        )

    def revoke_token(self, token, *, token_type_hint):
        self.revoked.append((token, token_type_hint))
        return self.revoke_ok


# -- the attack ---------------------------------------------------------------
def test_a_forwarded_authorize_url_cannot_bind_into_the_victims_org(tmp_path):
    """THE test. The attacker holds the state (it is in the URL they forward)
    but not the cookie, which lives only in their own browser. Without both, the
    callback must refuse — otherwise the victim's approval binds the attacker's
    workspace into the victim's organization."""
    pending, _ = make_stores(tmp_path)
    record = open_bind(pending)

    # The victim's browser: correct state, no cookie of ours.
    assert pending.consume(state=record.state, bind_id="") is None
    # And an attacker guessing at the cookie does no better.
    assert pending.consume(state=record.state, bind_id="not-the-cookie") is None

    # The record is still unspent, so the real installer can still finish.
    assert pending.consume(state=record.state, bind_id=record.bind_id) is not None


def test_the_cookie_alone_is_not_enough_either(tmp_path):
    """Both halves, both directions: a cookie without the matching state is as
    useless as a state without the cookie."""
    pending, _ = make_stores(tmp_path)
    record = open_bind(pending)

    assert pending.consume(state="", bind_id=record.bind_id) is None
    assert pending.consume(state="wrong-state", bind_id=record.bind_id) is None


def test_state_and_cookie_must_name_the_same_record(tmp_path):
    """Two installs in flight must not be combinable — an attacker with their own
    valid cookie must not be able to redeem someone else's state."""
    pending, _ = make_stores(tmp_path)
    mine = open_bind(pending, team_id="T-MINE")
    theirs = open_bind(pending, team_id="T-THEIRS")

    assert pending.consume(state=theirs.state, bind_id=mine.bind_id) is None


def test_the_two_halves_are_independent_secrets(tmp_path):
    """Deriving one from the other would collapse them into a single secret and
    hand the forwarded URL the authority the pair exists to split."""
    pending, _ = make_stores(tmp_path)
    record = open_bind(pending)

    assert record.state != record.bind_id
    assert record.code_verifier not in (record.state, record.bind_id)
    assert len(record.state) >= 32 and len(record.bind_id) >= 32


# -- replay, expiry, races ----------------------------------------------------
def test_a_record_is_single_use(tmp_path):
    pending, _ = make_stores(tmp_path)
    record = open_bind(pending)

    assert pending.consume(state=record.state, bind_id=record.bind_id) is not None
    assert pending.consume(state=record.state, bind_id=record.bind_id) is None


def test_an_expired_record_is_refused(tmp_path):
    pending, _ = make_stores(tmp_path)
    record = open_bind(pending)
    with pending._engine.begin() as conn:
        conn.execute(
            pending._table.update().values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )

    assert pending.consume(state=record.state, bind_id=record.bind_id) is None


def test_only_one_of_two_racing_callbacks_claims_the_record(tmp_path):
    """The claim is the UPDATE, so a duplicated callback cannot bind twice."""
    pending, _ = make_stores(tmp_path)
    record = open_bind(pending)

    results = []
    barrier = threading.Barrier(6)

    def claim():
        barrier.wait(timeout=5)
        results.append(pending.consume(state=record.state, bind_id=record.bind_id))

    threads = [threading.Thread(target=claim) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert sum(1 for r in results if r is not None) == 1


# -- what the bind is told ----------------------------------------------------
def test_the_workspace_comes_from_the_record_not_the_request(tmp_path):
    """The bind names the workspace the completed Slack install established. A
    team id read from a query parameter would hand the attack straight back."""
    pending, creds = make_stores(tmp_path)
    record = open_bind(pending, team_id="T-REAL")
    claimed = pending.consume(state=record.state, bind_id=record.bind_id)
    fm = FakeFM()

    complete_bind(
        fm=fm, workspace_credentials=creds, record=claimed,
        code="c", redirect_uri="https://slack.faultmaven.ai/faultmaven/callback",
    )

    assert fm.bound[0]["slack_team_id"] == "T-REAL"


def test_the_authorize_url_is_built_only_from_config_and_record(tmp_path):
    pending, _ = make_stores(tmp_path)
    record = open_bind(pending)

    url = authorize_url(
        dashboard_url="https://app.faultmaven.ai/",
        client_id="faultmaven-slack-agent",
        redirect_uri="https://slack.faultmaven.ai/faultmaven/callback",
        record=record,
    )

    assert url.startswith("https://app.faultmaven.ai/auth/authorize?")
    assert f"state={record.state}" in url
    assert "code_challenge_method=S256" in url
    # The verifier itself must never travel.
    assert record.code_verifier not in url
    assert code_challenge_for(record.code_verifier) in url


# -- the borrowed admin authority --------------------------------------------
def test_the_admin_tokens_are_revoked_after_a_successful_bind(tmp_path):
    """The bearer carries the admin's whole organization authority. It exists for
    one call and must not survive it."""
    pending, creds = make_stores(tmp_path)
    record = pending.consume(
        **{"state": (r := open_bind(pending)).state, "bind_id": r.bind_id}
    )
    fm = FakeFM()

    complete_bind(
        fm=fm, workspace_credentials=creds, record=record,
        code="c", redirect_uri="https://x/cb",
    )

    assert ("admin-refresh", "refresh_token") in fm.revoked
    assert ("admin-access", "access_token") in fm.revoked


def test_the_admin_tokens_are_revoked_even_when_the_bind_is_refused(tmp_path):
    """The likeliest failure: the endpoint needs ORG_MANAGE_USERS *and*
    ORG_MANAGE_SETTINGS, so an admin with only the first consents happily and is
    refused here. Their token must not be left live because of it."""
    pending, creds = make_stores(tmp_path)
    r = open_bind(pending)
    record = pending.consume(state=r.state, bind_id=r.bind_id)
    fm = FakeFM(bind_error=WorkspaceBindError("needs manage settings", status_code=403))

    with pytest.raises(WorkspaceBindError):
        complete_bind(
            fm=fm, workspace_credentials=creds, record=record,
            code="c", redirect_uri="https://x/cb",
        )

    assert ("admin-refresh", "refresh_token") in fm.revoked
    assert ("admin-access", "access_token") in fm.revoked
    assert creds.get("T-ATTACKER") is None, "a refused bind stores no credential"


def test_only_the_service_account_credential_is_persisted(tmp_path):
    """What lands in the store is the workspace account's own token — never the
    admin's."""
    pending, creds = make_stores(tmp_path)
    r = open_bind(pending, team_id="T-OK")
    record = pending.consume(state=r.state, bind_id=r.bind_id)
    fm = FakeFM()

    complete_bind(
        fm=fm, workspace_credentials=creds, record=record,
        code="c", redirect_uri="https://x/cb",
    )

    stored = creds.get("T-OK")
    assert stored.refresh_token == "sa-refresh"
    assert stored.organization_id == "org-victim"
    assert stored.refresh_token not in ("admin-access", "admin-refresh")


# -- the cookie ---------------------------------------------------------------
def test_the_bind_cookie_cannot_be_set_by_a_sibling_subdomain():
    """``__Host-`` forbids a Domain attribute, so no other host under
    faultmaven.ai can set or overwrite this cookie — cookie-tossing is how an
    attacker would supply the half of the pair they lack."""
    header = _bind_cookie("abc123")

    assert header.startswith(f"{BIND_COOKIE_NAME}=abc123;")
    assert BIND_COOKIE_NAME.startswith("__Host-")
    assert "Domain=" not in header
    assert "Path=/" in header
    assert "HttpOnly" in header and "Secure" in header
    # Lax, not Strict: the cookie must still arrive on the top-level redirect
    # back from FaultMaven, or every legitimate bind is refused.
    assert "SameSite=Lax" in header


# -- what a browser is shown --------------------------------------------------
def test_the_confirm_page_names_the_workspace_being_admitted():
    """The dashboard consent screen renders the client name and a caller-supplied
    scope string, so it is evidence of nothing about which workspace is
    involved. This page is the only place a human can catch the wrong one."""
    html = install_pages.confirm_page(
        workspace_name="Acme Ops", team_id="T123", authorize_url="https://x/y"
    )

    assert "Acme Ops" in html and "T123" in html


def test_pages_escape_everything_they_render():
    html = install_pages.confirm_page(
        workspace_name="<img src=x onerror=alert(1)>",
        team_id="T1",
        authorize_url="https://x/y?a=1&b=2",
    )

    assert "<img src=x" not in html
    assert "&lt;img" in html
    assert "a=1&amp;b=2" in html


# -- the route, end to end ----------------------------------------------------
@pytest.fixture
def bind_client(monkeypatch, tmp_path):
    """The HTTP transport with install-binding configured, fully offline."""
    import config

    for key, value in {
        "SLACK_TRANSPORT": "http",
        "SLACK_CLIENT_ID": "123.456",
        "SLACK_CLIENT_SECRET": "secret",
        "SLACK_SIGNING_SECRET": "signsign",
        "SLACK_DATABASE_URL": f"sqlite:///{tmp_path / 'oauth.db'}",
        "CASE_STORE_PATH": str(tmp_path / "cases.db"),
        "FAULTMAVEN_API_TOKEN": "preset-token",
        "FAULTMAVEN_DASHBOARD_URL": "https://app.faultmaven.ai",
        "FAULTMAVEN_OAUTH_REDIRECT_URI": "https://slack.faultmaven.ai/faultmaven/callback",
    }.items():
        monkeypatch.setenv(key, value)
    config.get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from web import create_fastapi_app

    app = create_fastapi_app()
    with TestClient(app) as client:
        yield client
    config.get_settings.cache_clear()


def test_the_callback_refuses_a_forwarded_link_with_no_cookie(bind_client):
    """The victim's browser, end to end: correct state in the URL, no cookie.
    Must refuse — and must not have opened a case, an account, or a binding."""
    resp = bind_client.get(
        "/faultmaven/callback",
        params={"code": "stolen", "state": "whatever"},
    )

    assert resp.status_code == 400
    assert "no longer valid" in resp.text


def test_the_callback_refuses_an_unknown_cookie(bind_client):
    bind_client.cookies.set(BIND_COOKIE_NAME, "made-up")
    resp = bind_client.get(
        "/faultmaven/callback", params={"code": "c", "state": "s"}
    )

    assert resp.status_code == 400


def test_a_declined_consent_reports_plainly_and_spends_the_link(bind_client, tmp_path):
    """No ``code`` on a LIVE record means the admin declined on the consent
    screen. Needs a real record and its cookie — without them the request lands
    on the no-record branch instead and this proves nothing."""
    pending = PendingBindStore(create_engine(f"sqlite:///{tmp_path / 'oauth.db'}"))
    record = pending.create(
        team_id="T-DECLINED", enterprise_id="", installer_user_id="U1",
        team_name="Acme Ops",
    )

    bind_client.cookies.set(BIND_COOKIE_NAME, record.bind_id)
    resp = bind_client.get("/faultmaven/callback", params={"state": record.state})

    assert resp.status_code == 200
    assert "access was not granted" in resp.text
    # The link is spent: declining must not leave a redeemable record behind.
    assert pending.consume(state=record.state, bind_id=record.bind_id) is None


def test_the_callback_never_leaks_its_query_string_onward(bind_client):
    """The page is reached with ?code= in the URL; a Referer would carry it."""
    resp = bind_client.get(
        "/faultmaven/callback", params={"code": "c", "state": "s"}
    )

    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"


def test_the_callback_binds_when_state_and_cookie_agree(bind_client, tmp_path, monkeypatch):
    """The positive path — without it, a route that refused *everything* would
    pass every test above. Proves the cookie is actually read and the record it
    names is the one handed to the bind."""
    import web

    # Same database the app opened, so this record is the app's record.
    pending = PendingBindStore(create_engine(f"sqlite:///{tmp_path / 'oauth.db'}"))
    record = pending.create(
        team_id="T-REAL", enterprise_id="", installer_user_id="U1",
        team_name="Acme Ops",
    )

    seen = {}

    def fake_complete(*, fm, workspace_credentials, record, code, redirect_uri):
        seen["team_id"] = record.team_id
        seen["code"] = code
        seen["redirect_uri"] = redirect_uri
        return "org-acme"

    monkeypatch.setattr(web, "complete_bind", fake_complete)

    bind_client.cookies.set(BIND_COOKIE_NAME, record.bind_id)
    resp = bind_client.get(
        "/faultmaven/callback", params={"code": "good-code", "state": record.state}
    )

    assert resp.status_code == 200
    assert "Workspace connected" in resp.text
    assert "org-acme" in resp.text
    assert seen["team_id"] == "T-REAL", "the workspace came from the record"
    assert seen["code"] == "good-code"
    assert seen["redirect_uri"] == "https://slack.faultmaven.ai/faultmaven/callback"


def test_a_rebind_drops_the_cached_credential(tmp_path):
    """The client caches a credential per workspace and a re-bind replaces the
    row underneath it. Without invalidation, turns keep authenticating as the
    PREVIOUS service account — filing cases in the previous organization — until
    that token happens to be rejected. ``forget_workspace`` exists for this."""
    pending, creds = make_stores(tmp_path)
    r = open_bind(pending, team_id="T-REBOUND")
    record = pending.consume(state=r.state, bind_id=r.bind_id)
    fm = FakeFM()

    complete_bind(
        fm=fm, workspace_credentials=creds, record=record,
        code="c", redirect_uri="https://x/cb",
    )

    assert fm.forgotten == ["T-REBOUND"]


def test_a_local_store_failure_after_the_server_bind_is_reported_as_final(tmp_path):
    """The server-side bind already succeeded and its credential is issued once,
    so this is NOT retryable — the next attempt is refused as already-bound.
    Telling the admin 'nothing was changed, try again' would be false and a dead
    end."""
    pending, creds = make_stores(tmp_path)
    r = open_bind(pending, team_id="T-LOST")
    record = pending.consume(state=r.state, bind_id=r.bind_id)
    fm = FakeFM()

    def explode(**kwargs):
        raise OSError("disk full")

    creds.bind = explode

    with pytest.raises(WorkspaceBindError) as caught:
        complete_bind(
            fm=fm, workspace_credentials=creds, record=record,
            code="c", redirect_uri="https://x/cb",
        )

    message = str(caught.value)
    assert "re-installing will not fix it" in message
    assert "re-issue" in message
    # And the borrowed admin authority is still given back.
    assert ("admin-refresh", "refresh_token") in fm.revoked


def test_a_409_does_not_leak_another_tenants_details_into_the_page(tmp_path):
    """`bind_workspace` embeds up to 300 chars of the backend's raw response,
    which on a cross-org conflict can name another tenant's organization. That
    belongs in the log, not in a browser."""
    from binding import bind_failure_message

    exc = WorkspaceBindError(
        "This Slack workspace is already bound elsewhere: organization "
        "org-someone-else team 'Their Secret Project'",
        status_code=409,
    )

    message = bind_failure_message(exc)

    assert "org-someone-else" not in message
    assert "Their Secret Project" not in message
    assert "already connected" in message


def test_the_consent_scope_names_what_the_grant_actually_does():
    """The module's own comment: 'a client that lies here is lying to the person
    deciding'. The token is used to create a service account and a team, so
    identity-only scopes would understate it on the one screen that shows it."""
    from binding import BIND_SCOPE

    assert "service-account" in BIND_SCOPE
    assert "create-team" in BIND_SCOPE


def test_expired_records_are_purged_so_spent_secrets_do_not_accumulate(tmp_path):
    """Every row holds a live PKCE verifier and a state secret. Nothing else
    deletes them, so without this a long-running process retains spent OAuth
    secrets indefinitely in the same database as the installation store."""
    pending, _ = make_stores(tmp_path)
    stale = open_bind(pending, team_id="T-OLD")
    fresh = open_bind(pending, team_id="T-NEW")
    with pending._engine.begin() as conn:
        conn.execute(
            pending._table.update()
            .where(pending._table.c.bind_id == stale.bind_id)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )

    assert pending.purge_expired() == 1
    # The live one is untouched and still redeemable.
    assert pending.consume(state=fresh.state, bind_id=fresh.bind_id) is not None


class _RecordingPendingBinds:
    """Counts records created, so a refusal that leaves state behind is visible."""

    def __init__(self):
        self.created = 0

    def create(self, **kwargs):
        self.created += 1
        return SimpleNamespace(
            bind_id="b1", state="s1", code_verifier="v1",
            team_id=kwargs.get("team_id", "T1"),
            team_name=kwargs.get("team_name", "Acme"),
            enterprise_id=kwargs.get("enterprise_id", ""),
        )

# -- the installer must administer the workspace (users:read / users.info) ----
#
# Both sides of the join have to consent. The FaultMaven leg is gated on
# organization authority; without this the *Slack* leg is gated on nothing, and
# a FaultMaven organization admin who happens to be an ordinary member of a
# workspace could admit that workspace's investigations into their organization
# with nobody who administers the workspace involved.



class _UsersInfoClient:
    """A WebClient stand-in whose ``users.info`` answers however a test needs."""

    def __init__(self, *, user=None, error=None):
        self._user = user
        self._error = error
        self.asked = []

    def users_info(self, *, user):
        self.asked.append(user)
        if self._error is not None:
            raise self._error
        return {"ok": True, "user": self._user}


def test_a_workspace_admin_may_bind():
    client = _UsersInfoClient(user={"id": "U1", "is_admin": True})
    assert installer_authority(client, "U1") is InstallerAuthority.ADMIN
    assert client.asked == ["U1"]


def test_an_owner_without_the_admin_flag_may_bind():
    """``is_owner`` / ``is_primary_owner`` are independent booleans in the
    payload, not implied by ``is_admin`` — and an Owner is unambiguously
    entitled to this decision."""
    for flag in ("is_owner", "is_primary_owner"):
        client = _UsersInfoClient(user={"id": "U1", flag: True})
        assert installer_authority(client, "U1") is InstallerAuthority.ADMIN


def test_an_ordinary_member_may_not_bind():
    client = _UsersInfoClient(user={"id": "U1", "is_admin": False})
    assert installer_authority(client, "U1") is InstallerAuthority.NOT_ADMIN


def test_a_missing_scope_is_unknown_not_a_refusal():
    """``missing_scope`` is what an app installed before ``users:read`` looks
    like. It is not evidence about this person, so it must not be reported as
    'you are not an admin' — the fix is a reinstall, not finding an admin."""
    from slack_sdk.errors import SlackApiError

    client = _UsersInfoClient(
        error=SlackApiError("missing_scope", {"ok": False, "error": "missing_scope"})
    )
    assert installer_authority(client, "U1") is InstallerAuthority.UNKNOWN


def test_an_unreachable_slack_is_unknown():
    client = _UsersInfoClient(error=RuntimeError("connection reset"))
    assert installer_authority(client, "U1") is InstallerAuthority.UNKNOWN


def test_no_installer_is_unknown():
    """An Enterprise Grid org-wide install carries no installer to check."""
    client = _UsersInfoClient(user={"is_admin": True})
    assert installer_authority(client, "") is InstallerAuthority.UNKNOWN
    assert client.asked == []


def _success_callback(monkeypatch, authority, pending_binds):
    """The real ``on_success``, with the authority check answering as told."""

    monkeypatch.setattr(app_module, "installer_authority", lambda _c, _u: authority)
    monkeypatch.setattr(app_module, "make_web_client", lambda *_a, **_k: object())
    settings = SimpleNamespace(
        install_binding_enabled=True,
        faultmaven_require_workspace_binding=False,
        faultmaven_dashboard_url="https://dash.example",
        faultmaven_oauth_client_id="faultmaven-slack-agent",
        faultmaven_oauth_redirect_uri="https://slack.example/faultmaven/callback",
    )
    return app_module._install_callbacks(settings, pending_binds).success


def _success_args():
    installation = SimpleNamespace(
        team_id="T1", team_name="Acme", enterprise_id=None, user_id="U1",
        bot_token="xoxb-1",
    )
    oauth_settings = SimpleNamespace(
        state_utils=SimpleNamespace(
            build_set_cookie_for_deletion=lambda: "slack-app-oauth-state=; Max-Age=0"
        )
    )
    return SimpleNamespace(
        installation=installation, settings=oauth_settings, request=None, default=None
    )


def test_the_bind_is_offered_to_an_admin(monkeypatch):
    """The positive path: an admin gets the confirm page and a pending record."""
    binds = _RecordingPendingBinds()
    on_success = _success_callback(monkeypatch, InstallerAuthority.ADMIN, binds)

    response = on_success(_success_args())

    assert response.status == 200
    assert "Connect this Slack workspace" in response.body
    assert binds.created == 1


def test_a_non_admin_is_told_who_can_finish_and_leaves_no_pending_bind(monkeypatch):
    """Refused before the record is created, so nothing is left behind."""
    binds = _RecordingPendingBinds()
    on_success = _success_callback(monkeypatch, InstallerAuthority.NOT_ADMIN, binds)

    response = on_success(_success_args())

    assert response.status == 200
    assert "Workspace Owner or Admin" in response.body
    assert "Connect this Slack workspace" not in response.body
    assert binds.created == 0


def test_an_unknown_authority_refuses_too_but_says_something_different(monkeypatch):
    """Fail closed: admitting a workspace on an authority nobody established is
    the one direction that cannot be undone by reading a page."""
    binds = _RecordingPendingBinds()
    on_success = _success_callback(monkeypatch, InstallerAuthority.UNKNOWN, binds)

    response = on_success(_success_args())

    assert response.status == 200
    assert "could not confirm" in response.body
    assert "Workspace Owner or Admin" not in response.body
    assert binds.created == 0


def test_an_enterprise_grid_org_owner_may_bind():
    """Grid reports organization authority under ``enterprise_user``.

    A Grid Org Owner is routinely a plain member of any given workspace, so the
    top-level flags are all false. Reading only those told the highest authority
    in the organization to go and find a Workspace Admin.
    """
    for flag in ("is_admin", "is_owner", "is_primary_owner"):
        client = _UsersInfoClient(
            user={"id": "U1", "is_admin": False, "enterprise_user": {flag: True}}
        )
        assert installer_authority(client, "U1") is InstallerAuthority.ADMIN


def test_a_grid_member_with_no_authority_at_either_level_may_not_bind():
    client = _UsersInfoClient(
        user={"id": "U1", "is_admin": False, "enterprise_user": {"is_admin": False}}
    )
    assert installer_authority(client, "U1") is InstallerAuthority.NOT_ADMIN


def test_refusal_pages_promise_a_working_app_only_when_one_is_promised():
    """With FAULTMAVEN_REQUIRE_WORKSPACE_BINDING on, an unbound workspace is
    refused every turn — so "the app works" is a promise of a bot that errors on
    first mention."""
    for page in (install_pages.not_admin_page, install_pages.authority_unknown_page):
        lenient = page(workspace_name="Acme", binding_required=False)
        strict = page(workspace_name="Acme", binding_required=True)
        assert "the app works" in lenient
        assert "the app works" not in strict
        assert "will not answer in this workspace" in strict
