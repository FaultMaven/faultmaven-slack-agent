"""Per-workspace FaultMaven credentials (ADR-013 D3).

A Slack workspace maps to a FaultMaven Team inside the customer's Organization,
and the agent authenticates as *that workspace's* ``slack`` service account. The
credential is the only thing that carries the tenant: the backend's ``users``
table has no organization column, so ``/auth/refresh`` re-attaches whatever
``organization_id`` the presented refresh token held. Everything that follows is
a consequence of that, and is pinned here:

* a turn authenticates as the credential bound to its workspace, so its case is
  owned in the right organization and auto-shares to the right Team;
* two workspaces never share a credential object — sharing one would mean
  sharing its renew lock's *subject*, and the refresh grant rotates, so a shared
  credential is a mutual-revocation lockout;
* an unbound workspace is REFUSED under ``require_workspace_binding`` rather
  than answered as the default account, which would file one customer's incident
  inside another tenant;
* a token minted for the wrong organization is refused, because nothing
  server-side would contradict it.

Uses httpx.MockTransport, so real request shaping is exercised without a backend.
"""

from __future__ import annotations

import base64
import json
import threading

import httpx
import pytest
from sqlalchemy import create_engine

from faultmaven.client import (
    FaultMavenClient,
    FaultMavenCredentialError,
    FaultMavenWorkspaceUnlinkedError,
)
from workspace_credentials import WorkspaceCredentialStore


def make_store(tmp_path) -> WorkspaceCredentialStore:
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth.db'}")
    return WorkspaceCredentialStore(engine)


def jwt_with_org(org: str | None) -> str:
    """A token shaped like a JWT — only ``organization_id`` matters here."""

    def seg(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    claims = {"exp": 4102444800}
    if org is not None:
        claims["organization_id"] = org
    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.signature"


def make_client(handler, *, workspaces=None, require=False, **kwargs):
    client = FaultMavenClient(
        "http://test",
        workspace_credentials=workspaces,
        require_workspace_binding=require,
        **kwargs,
    )
    client._http = httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )
    return client


def token_response(access="at-1", refresh="rt-2", expires_in=900):
    return httpx.Response(
        200,
        json={
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in": expires_in,
        },
    )


# -- the binding store --------------------------------------------------------
def test_bind_round_trips_the_tenant_and_the_token(tmp_path):
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-1")

    record = store.get("T1")
    assert record is not None
    assert record.organization_id == "org-a"
    assert record.refresh_token == "rt-1"


def test_an_unbound_workspace_reads_as_none(tmp_path):
    assert make_store(tmp_path).get("T-nope") is None


def test_rebinding_replaces_rather_than_duplicates(tmp_path):
    """A reinstall re-binds the same workspace; it must not leave two rows."""
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-1")
    store.bind(team_id="T1", organization_id="org-b", refresh_token="rt-2")

    assert store.get("T1").organization_id == "org-b"
    assert store.team_ids() == ["T1"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"team_id": "", "organization_id": "org-a", "refresh_token": "rt"},
        {"team_id": "T1", "organization_id": "", "refresh_token": "rt"},
        {"team_id": "T1", "organization_id": "org-a", "refresh_token": ""},
    ],
)
def test_bind_refuses_an_unusable_binding(tmp_path, kwargs):
    """Each missing piece would produce a row the agent cannot safely use — an
    organization-less binding above all, since it is what the token claim is
    checked against."""
    with pytest.raises(ValueError):
        make_store(tmp_path).bind(**kwargs)


def test_a_rotation_never_resurrects_an_uninstalled_workspace(tmp_path):
    """put_refresh_token is UPDATE-only. An in-flight rotation landing after an
    uninstall must not re-create the row — it would have no organization to be
    checked against, which is exactly the state the guard exists to prevent."""
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-1")
    store.unbind("T1")

    with pytest.raises(KeyError):
        store.put_refresh_token("T1", "rt-2")
    assert store.get("T1") is None


def test_put_refresh_token_refuses_an_empty_token(tmp_path):
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-1")
    with pytest.raises(ValueError):
        store.put_refresh_token("T1", "")


# -- resolution ---------------------------------------------------------------
def test_a_turn_authenticates_as_its_own_workspace(tmp_path):
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")
    presented: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            presented.append(json.loads(request.content)["refresh_token"])
            return token_response()
        return httpx.Response(200, json={"case_id": "case_abc"})

    client = make_client(handler, workspaces=store, refresh_token="default-rt")
    client.create_case(team_id="T1")

    assert presented == ["rt-t1"], "the workspace's credential, not the default"


def test_two_workspaces_never_share_a_credential(tmp_path):
    """The refresh grant rotates: one credential object shared by two workspaces
    would mean each revoking the other's token."""
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")
    store.bind(team_id="T2", organization_id="org-b", refresh_token="rt-t2")
    presented: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            token = json.loads(request.content)["refresh_token"]
            presented.append(token)
            return token_response(access=f"at-{token}", refresh=f"next-{token}")
        return httpx.Response(200, json={"case_id": "case_abc"})

    client = make_client(handler, workspaces=store, refresh_token="default-rt")
    client.create_case(team_id="T1")
    client.create_case(team_id="T2")

    assert presented == ["rt-t1", "rt-t2"]
    assert client._credential_for("T1") is not client._credential_for("T2")
    assert client._credential_for("T1").organization_id == "org-a"
    assert client._credential_for("T2").organization_id == "org-b"


def test_a_cold_workspace_resolves_to_one_credential_under_concurrency(tmp_path):
    """Two threads racing an unseen workspace must converge on ONE object: two
    would carry two renew locks, which is the double-rotation lockout."""
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")
    client = make_client(lambda r: token_response(), workspaces=store)

    resolved: list = []
    barrier = threading.Barrier(8)

    def resolve():
        barrier.wait(timeout=5)
        resolved.append(client._credential_for("T1"))

    threads = [threading.Thread(target=resolve) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(resolved) == 8
    assert all(c is resolved[0] for c in resolved)


def test_a_rotation_is_persisted_against_the_workspace_not_the_default(tmp_path):
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")

    client = make_client(
        lambda r: token_response(refresh="rotated-t1"),
        workspaces=store,
        refresh_token="default-rt",
    )
    client._current_token(client._credential_for("T1"))

    assert store.get("T1").refresh_token == "rotated-t1"
    assert client._default.refresh_token == "default-rt", "default untouched"


# -- the unbound workspace ----------------------------------------------------
def test_an_unbound_workspace_falls_back_when_binding_is_not_required(tmp_path):
    """The interim posture for a deployment still running one shared account."""
    store = make_store(tmp_path)
    client = make_client(
        lambda r: token_response(), workspaces=store, refresh_token="default-rt"
    )

    assert client._credential_for("T-unbound") is client._default


def test_the_fallback_warns_once_per_workspace(tmp_path, caplog):
    """Loud enough for an operator to notice, quiet enough not to flood."""
    store = make_store(tmp_path)
    client = make_client(
        lambda r: token_response(), workspaces=store, refresh_token="default-rt"
    )

    with caplog.at_level("WARNING"):
        for _ in range(3):
            client._credential_for("T-unbound")

    warnings = [r for r in caplog.records if "no FaultMaven credential" in r.message]
    assert len(warnings) == 1


def test_an_unbound_workspace_is_refused_when_binding_is_required(tmp_path):
    """Against a multi-tenant backend, answering on the default account would
    file this workspace's case inside whatever tenant that account carries."""
    store = make_store(tmp_path)
    client = make_client(
        lambda r: token_response(),
        workspaces=store,
        refresh_token="default-rt",
        require=True,
    )

    with pytest.raises(FaultMavenWorkspaceUnlinkedError, match="T-unbound"):
        client.create_case(team_id="T-unbound")


# -- the cross-tenant guard ---------------------------------------------------
def test_a_token_minted_for_another_organization_is_refused(tmp_path):
    """Tenancy travels only in the token chain, so a credential provisioned
    against the wrong organization is invisible server-side. This is the only
    place the intended tenant is written down, so it is the only place that can
    catch it."""
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")
    client = make_client(
        lambda r: token_response(access=jwt_with_org("org-WRONG")),
        workspaces=store,
    )

    with pytest.raises(FaultMavenCredentialError, match="org-WRONG"):
        client.create_case(team_id="T1")


def test_a_mismatched_token_is_refused_before_it_is_persisted(tmp_path):
    """Refusing after storing it would leave the wrong-tenant credential behind
    to be used on the next restart."""
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")
    client = make_client(
        lambda r: token_response(access=jwt_with_org("org-WRONG"), refresh="rotated"),
        workspaces=store,
    )

    with pytest.raises(FaultMavenCredentialError):
        client.create_case(team_id="T1")
    assert store.get("T1").refresh_token == "rt-t1", "rotation not committed"


def test_a_token_for_the_bound_organization_is_accepted(tmp_path):
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return token_response(access=jwt_with_org("org-a"))
        return httpx.Response(200, json={"case_id": "case_abc"})

    client = make_client(handler, workspaces=store)
    assert client.create_case(team_id="T1") == "case_abc"


def test_an_unreadable_claim_is_not_treated_as_a_mismatch(tmp_path):
    """The backend, not this decoder, is the authority on a token's validity —
    an opaque token must not be read as a cross-tenant credential."""
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return token_response(access="not-a-jwt")
        return httpx.Response(200, json={"case_id": "case_abc"})

    client = make_client(handler, workspaces=store)
    assert client.create_case(team_id="T1") == "case_abc"


# -- lifecycle ----------------------------------------------------------------
def test_close_drains_every_workspace_credential(tmp_path):
    """Each credential renews independently, so a rotation lost on any one of
    them locks out that workspace."""
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")

    started = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        release.wait(timeout=5)
        return token_response(refresh="rotated-t1")

    client = make_client(handler, workspaces=store)
    cred = client._credential_for("T1")

    renewal = threading.Thread(target=lambda: client._current_token(cred))
    renewal.start()
    started.wait(timeout=5)

    closer = threading.Thread(target=client.close)
    closer.start()
    closer.join(timeout=0.5)
    assert closer.is_alive(), "close() returned while a renewal was in flight"

    release.set()
    renewal.join(timeout=5)
    closer.join(timeout=5)
    assert store.get("T1").refresh_token == "rotated-t1"


def test_bound_workspaces_are_listed_for_preflight(tmp_path):
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-1")
    store.bind(team_id="T2", organization_id="org-b", refresh_token="rt-2")

    client = make_client(lambda r: token_response(), workspaces=store)
    assert sorted(client.bound_workspaces()) == ["T1", "T2"]


# -- what the room sees -------------------------------------------------------
def test_an_unlinked_workspace_is_told_the_install_is_unfinished():
    """Not "FaultMaven hit an error": nothing is broken, the install is simply
    not finished, and the fix belongs to a FaultMaven org admin."""
    from listeners import _turn

    text = _turn.turn_error_text(FaultMavenWorkspaceUnlinkedError("T1"))

    assert text == _turn.WORKSPACE_UNLINKED_TEXT
    assert "error" not in text.lower()


def test_an_unlinked_workspace_does_not_invite_a_retry():
    """The binding outlives our restart, so re-sending reproduces it forever —
    and ``retry_may_help`` is what decides whether a button is re-armed."""
    from listeners import _turn

    assert _turn.retry_may_help(FaultMavenWorkspaceUnlinkedError("T1")) is False


def test_the_unlinked_refusal_survives_a_shutdown_race():
    """During drain the generic advice is "resend in a minute" — a promise the
    restart cannot keep for a workspace that is still unbound afterwards."""
    from listeners import _turn

    _turn._shutting_down.set()
    try:
        assert (
            _turn.turn_error_text(FaultMavenWorkspaceUnlinkedError("T1"))
            == _turn.WORKSPACE_UNLINKED_TEXT
        )
    finally:
        _turn._shutting_down.clear()


def test_requiring_a_binding_under_socket_mode_is_refused_at_boot(monkeypatch):
    """Bindings live in the OAuth store, which Socket Mode has not got — so this
    combination would refuse every turn forever. Fail at boot, not at the first
    incident."""
    from config import Settings

    with pytest.raises(ValueError, match="SLACK_TRANSPORT=http"):
        Settings(
            SLACK_TRANSPORT="socket",
            SLACK_BOT_TOKEN="xoxb-x",
            SLACK_APP_TOKEN="xapp-x",
            FAULTMAVEN_REQUIRE_WORKSPACE_BINDING=True,
        )


# -- end to end through the turn pipeline -------------------------------------
def test_two_workspaces_open_cases_under_their_own_service_accounts(tmp_path):
    """The whole point, exercised through ``run_turn`` rather than the client
    API: two workspaces asking the same question must reach the backend as two
    different principals, because the principal is what decides the owning
    organization and the Team the case auto-shares to."""
    from listeners._turn import run_turn
    from store import CaseStore

    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")
    store.bind(team_id="T2", organization_id="org-b", refresh_token="rt-t2")

    # token → the access token minted for it, so a request's bearer identifies
    # which workspace's credential produced it.
    minted = {"rt-t1": "at-org-a", "rt-t2": "at-org-b"}
    bearers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            presented = json.loads(request.content)["refresh_token"]
            return token_response(
                access=minted[presented], refresh=presented  # no rotation churn
            )
        bearers.append(request.headers["authorization"])
        if request.url.path == "/api/v1/cases":
            return httpx.Response(200, json={"case_id": f"case_{len(bearers)}"})
        return httpx.Response(200, json={"agent_response": "ack"})

    fm = make_client(handler, workspaces=store, require=True)
    cases = CaseStore(str(tmp_path / "cases.db"))
    try:
        common = dict(channel_id="C1", thread_ts="1.0", text="disk is full")
        run_turn(fm, cases, team_id="T1", **common)
        run_turn(fm, cases, team_id="T2", **common)
    finally:
        cases.close()

    assert bearers == [
        "Bearer at-org-a",  # T1 create_case
        "Bearer at-org-a",  # T1 submit_turn
        "Bearer at-org-b",  # T2 create_case
        "Bearer at-org-b",  # T2 submit_turn
    ]


def test_an_unlinked_workspace_never_reaches_the_backend(tmp_path):
    """The refusal has to happen before the case is created — a case opened on
    the fallback account is already in the wrong tenant by the time anyone
    notices."""
    from listeners._turn import run_turn
    from store import CaseStore

    store = make_store(tmp_path)
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return token_response()
        reached.append(request.url.path)
        return httpx.Response(200, json={"case_id": "case_leaked"})

    fm = make_client(
        handler, workspaces=store, refresh_token="default-rt", require=True
    )
    cases = CaseStore(str(tmp_path / "cases.db"))
    try:
        with pytest.raises(FaultMavenWorkspaceUnlinkedError):
            run_turn(
                fm, cases, team_id="T-unbound", channel_id="C1",
                thread_ts="1.0", text="disk is full",
            )
    finally:
        cases.close()

    assert reached == [], "no case may be opened for an unbound workspace"


# -- regressions from the #59 review ------------------------------------------
def test_the_keepalive_covers_a_workspace_that_has_not_taken_a_turn(tmp_path):
    """The cache is filled by traffic and is empty at boot, so keying the
    keepalive off it means a bound-but-quiet workspace never has its refresh
    window slid — the exact lockout the keepalive exists to prevent."""
    store = make_store(tmp_path)
    store.bind(team_id="T-quiet", organization_id="org-a", refresh_token="rt-quiet")

    client = make_client(lambda r: token_response(), workspaces=store)
    assert client._workspaces == {}, "nothing has taken a turn yet"

    keys = {c.key for c in client._live_credentials()}
    assert "T-quiet" in keys


def test_a_turnless_workspace_is_actually_renewed_by_the_keepalive(tmp_path):
    store = make_store(tmp_path)
    store.bind(team_id="T-quiet", organization_id="org-a", refresh_token="rt-quiet")
    presented: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        presented.append(json.loads(request.content)["refresh_token"])
        return token_response(refresh="rt-slid")

    client = make_client(handler, workspaces=store)
    for cred in client._live_credentials():
        if cred.refresh_token:
            client._renew(cred, force=True)

    assert presented == ["rt-quiet"]
    assert store.get("T-quiet").refresh_token == "rt-slid"


def test_a_turn_with_no_workspace_id_is_refused_not_defaulted(tmp_path):
    """Every listener derives ``context.team_id or ""``, so an empty id really
    reaches here. Serving it on the default account is the same cross-tenant
    misroute as serving a known-unbound workspace."""
    store = make_store(tmp_path)
    client = make_client(
        lambda r: token_response(),
        workspaces=store,
        refresh_token="default-rt",
        require=True,
    )

    with pytest.raises(FaultMavenWorkspaceUnlinkedError):
        client.create_case(team_id="")


def test_close_gives_every_credential_its_own_drain_budget(tmp_path, monkeypatch):
    """One shared deadline means the first slow renewal spends it and every
    later credential gets acquire(timeout=0.0) — which fails instantly on a held
    lock — so the HTTP client is torn down under their in-flight rotations, and
    a rotation lost there is a lockout for that workspace."""
    from faultmaven import client as client_mod

    monkeypatch.setattr(client_mod, "_CLOSE_DRAIN_HEADROOM_SECONDS", 0.2)

    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")
    store.bind(team_id="T2", organization_id="org-b", refresh_token="rt-t2")

    client = make_client(lambda r: token_response(), workspaces=store, timeout=0.05)
    first, second = client._credential_for("T1"), client._credential_for("T2")
    budget = 0.25  # timeout + headroom

    # T1 outlasts its own budget, so close() gives up on it. T2 is released
    # after a SHARED clock would already be exhausted but well inside its own
    # budget — so it is drained iff the budget is per-credential.
    first.renew_lock.acquire()
    second.renew_lock.acquire()
    threading.Timer(budget * 1.4, second.renew_lock.release).start()

    undrained: list[str] = []
    real_warning = client_mod.logger.warning

    def capture(msg, *args):
        if "renewal still in flight" in msg:
            undrained.append(args[0])
        else:
            real_warning(msg, *args)

    monkeypatch.setattr(client_mod.logger, "warning", capture)

    closer = threading.Thread(target=client.close)
    closer.start()
    closer.join(timeout=5)
    first.renew_lock.release()

    assert not closer.is_alive()
    assert undrained == [first.label], (
        "T2 must get its own budget, not the remainder of a shared one "
        f"(undrained={undrained})"
    )


def test_a_rebound_workspace_adopts_its_new_organization(tmp_path):
    """The cached credential outlives a re-bind. Without reconciliation its
    stale organization makes the cross-tenant guard reject the very token the
    re-bind provisioned — every turn failing until a restart, while blaming the
    backend for minting the wrong tenant."""
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-old")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            presented = json.loads(request.content)["refresh_token"]
            if presented == "rt-old":
                return httpx.Response(401, json={"detail": "revoked"})
            return token_response(access=jwt_with_org("org-b"), refresh="rt-new2")
        return httpx.Response(200, json={"case_id": "case_abc"})

    client = make_client(handler, workspaces=store)
    client._credential_for("T1")  # cache it against org-a

    store.bind(team_id="T1", organization_id="org-b", refresh_token="rt-new")

    assert client.create_case(team_id="T1") == "case_abc"
    assert client._credential_for("T1").organization_id == "org-b"


def test_an_unbound_workspace_drops_its_cached_credential(tmp_path):
    """After an uninstall the cached copy must not keep serving turns that the
    binding no longer authorizes."""
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")
    client = make_client(
        lambda r: httpx.Response(401, json={"detail": "revoked"}), workspaces=store
    )
    cred = client._credential_for("T1")
    store.unbind("T1")

    with pytest.raises(FaultMavenCredentialError):
        client._renew(cred)
    assert "T1" not in client._workspaces


def test_a_restored_credential_is_aged_from_when_it_was_stored(tmp_path):
    """Stamping the age at cache time restarts the blind-renew clock on every
    restart, so an opaque credential ages out to expiry unrenewed."""
    import time as _time
    from datetime import datetime, timedelta, timezone

    from faultmaven.client import _REFRESH_BLIND_RENEW_SECONDS

    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="opaque-token")
    # Backdate the row past the blind-renew threshold.
    with store._engine.begin() as conn:
        conn.execute(
            store._table.update().values(
                updated_at=datetime.now(timezone.utc)
                - timedelta(seconds=_REFRESH_BLIND_RENEW_SECONDS + 3600)
            )
        )

    client = make_client(lambda r: token_response(), workspaces=store)
    cred = client._credential_for("T1")

    assert cred.obtained_at < _time.monotonic() - _REFRESH_BLIND_RENEW_SECONDS
    assert client._refresh_credential_is_due(cred) is True


def test_a_grid_keyed_row_is_not_reported_as_a_resolvable_binding(tmp_path):
    """``get()`` looks up enterprise_id='' today, so listing a Grid row would
    report a workspace as bound that then resolves to nothing."""
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-1")
    store.bind(
        team_id="T-grid",
        organization_id="org-a",
        refresh_token="rt-g",
        enterprise_id="E1",
    )

    assert store.team_ids() == ["T1"]
    assert store.get("T-grid") is None
    assert store.get("T-grid", enterprise_id="E1") is not None


def test_a_token_with_no_organization_claim_is_flagged(tmp_path, caplog):
    """A decodable JWT that names no tenant is not the opaque-token case: the
    guard has nothing to compare, and a claim rename upstream would otherwise
    disable it permanently and invisibly."""
    store = make_store(tmp_path)
    store.bind(team_id="T1", organization_id="org-a", refresh_token="rt-t1")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return token_response(access=jwt_with_org(None))
        return httpx.Response(200, json={"case_id": "case_abc"})

    client = make_client(handler, workspaces=store)
    with caplog.at_level("WARNING"):
        assert client.create_case(team_id="T1") == "case_abc"

    assert any("no organization claim" in r.message for r in caplog.records)


def test_a_credential_less_deployment_still_says_so_at_boot(caplog):
    """Without a workspace store, no credential at all is a misconfiguration —
    booting silently defers it to the first user in the first channel."""
    client = make_client(lambda r: token_response(), refresh_token="")
    with caplog.at_level("WARNING"):
        client.startup()

    assert any("auth deferred" in r.message for r in caplog.records)
