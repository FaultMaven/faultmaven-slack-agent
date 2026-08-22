"""Refresh-grant authentication (ADR-012 D10).

Under ``AUTH_MODE=oauth`` dev-login 404s, so the agent authenticates with a
provisioned refresh token and mints its own access tokens. The grant *rotates*:
each renewal revokes the token it was handed, so at every moment exactly one
credential is live and losing it costs an operator round-trip. That turns a set
of ordinary-looking details into correctness requirements, all pinned here:

* the rotated token is persisted before it is relied on — and, if that write
  fails, kept in memory and re-written later rather than discarded (dropping it
  would make a transient disk error a permanent lockout);
* renewals are single-flight, and shutdown waits for one in flight;
* a rejected credential is retried against the store (another process may have
  rotated it) and the configured seed before a lockout is declared;
* renewal is reachable from every request path, not just ``_post``.

Uses httpx.MockTransport, so the real request shaping is exercised without a
live backend.
"""

from __future__ import annotations

import base64
import json
import threading
import time

import httpx
import pytest

from credentials import CredentialStore
from faultmaven.client import (
    FaultMavenClient,
    FaultMavenCredentialError,
    FaultMavenError,
    _jwt_expiry,
)


class FakeStore:
    """In-memory stand-in for :class:`credentials.CredentialStore`."""

    def __init__(self, token: str | None = None, fail_on_put: bool = False):
        self.token = token
        self.fail_on_put = fail_on_put
        self.puts: list[str] = []
        self.closed = False

    def get(self) -> str | None:
        return self.token

    def put(self, refresh_token: str) -> None:
        if self.fail_on_put:
            raise OSError("disk full")
        self.puts.append(refresh_token)
        self.token = refresh_token

    def close(self) -> None:
        self.closed = True


def jwt_with_exp(exp: float) -> str:
    """A token shaped like a JWT — only the ``exp`` claim matters here."""

    def seg(data: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        return raw.rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg({'exp': exp})}.signature"


def make_client(handler, *, refresh_token: str = "seed-rt", store=None, **kwargs):
    client = FaultMavenClient(
        "http://test",
        refresh_token=refresh_token,
        credential_store=store,
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


# -- the exchange -------------------------------------------------------------
def test_renewal_posts_the_refresh_grant():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return token_response()

    client = make_client(handler)

    assert client._current_token() == "at-1"
    assert seen["url"] == "http://test/api/v1/auth/oauth/token"
    assert seen["body"]["grant_type"] == "refresh_token"
    assert seen["body"]["refresh_token"] == "seed-rt"
    assert seen["body"]["client_id"] == "faultmaven-slack-agent"


def test_access_token_is_reused_until_it_nears_expiry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return token_response()

    client = make_client(handler)

    assert client._current_token() == "at-1"
    assert client._current_token() == "at-1"
    assert calls["n"] == 1


def test_short_lived_access_token_is_renewed_on_next_use():
    """A lifetime inside the renewal skew must not be treated as usable."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return token_response(access=f"at-{calls['n']}", expires_in=1)

    client = make_client(handler)

    assert client._current_token() == "at-1"
    assert client._current_token() == "at-2"


# -- rotation persistence -----------------------------------------------------
def test_rotated_token_is_persisted():
    store = FakeStore(token="stored-rt")
    client = make_client(lambda r: token_response(refresh="rt-next"), store=store)

    client._current_token()

    assert store.puts == ["rt-next"]
    assert client._refresh_token == "rt-next"


def test_persisted_credential_wins_over_the_configured_seed():
    """The configured token is a one-time seed; after the first rotation it is
    revoked, so a restart must use the store, not the env var."""
    store = FakeStore(token="rotated-rt")
    client = make_client(lambda r: token_response(), refresh_token="seed-rt", store=store)

    assert client._refresh_token == "rotated-rt"


def test_configured_seed_is_written_to_an_empty_store():
    """First boot: the seed lands in the store so the next restart reads it."""
    store = FakeStore(token=None)
    make_client(lambda r: token_response(), refresh_token="seed-rt", store=store)

    assert store.puts == ["seed-rt"]


# -- single-flight ------------------------------------------------------------
def test_concurrent_renewals_issue_exactly_one_request():
    """Two renewals with the same credential would revoke each other."""
    calls = {"n": 0}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            calls["n"] += 1
        time.sleep(0.05)  # widen the race window
        return token_response()

    client = make_client(handler)
    results: list[str] = []

    threads = [
        threading.Thread(target=lambda: results.append(client._current_token()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1
    assert results == ["at-1"] * 8


# -- failure handling ---------------------------------------------------------
#: How the token endpoint refuses a grant under API contract 2.0.0: RFC 6749
#: §5.2 at **400**, not FastAPI's `{"detail": ...}` at 401. Both halves moved
#: together, so pairing this body with a 401 would describe a server that never
#: existed — 1.0.0 sent 401 + `detail`, 2.0.0 sends 400 + this.
#:
#: The turn and poll mocks below keep `detail`, because every route other than
#: /auth/oauth/{token,revoke} still answers that shape. A mock using one shape
#: everywhere would stop describing the server either way.
REJECTED_GRANT = {
    "error": "invalid_grant",
    "error_description": "Refresh token expired or revoked",
}


@pytest.mark.parametrize("status", [400, 401])
def test_rejected_credential_names_the_recovery_step(status):
    client = make_client(
        lambda r: httpx.Response(status, json=REJECTED_GRANT)
    )

    with pytest.raises(FaultMavenCredentialError) as exc:
        client._current_token()

    assert "provision_service_account" in str(exc.value)


def test_the_refusal_reaches_the_operator_in_the_rfc_shape():
    """A refusal must still say WHY under contract 2.0.0.

    `_error_detail` read only `detail` and `message`. The token endpoint stopped
    sending either — it answers RFC 6749 §5.2 — so the explanation silently
    became a dump of the raw JSON body, on the one message an operator reads to
    learn that a service-account credential needs re-provisioning. Nothing in
    the type system or the drift gate would have said so: the models are
    generated, but this path reads a dict.
    """
    client = make_client(lambda r: httpx.Response(400, json=REJECTED_GRANT))

    with pytest.raises(FaultMavenCredentialError) as exc:
        client._current_token()

    message = str(exc.value)
    assert "invalid_grant" in message
    assert "Refresh token expired or revoked" in message
    # Not the raw body dumped verbatim.
    assert '{"error"' not in message


def test_a_detail_body_is_still_read():
    """Every route but /auth/oauth/{token,revoke} still answers `detail`."""
    client = make_client(
        lambda r: httpx.Response(400, json={"detail": "something else entirely"})
    )

    with pytest.raises(FaultMavenCredentialError) as exc:
        client._current_token()

    assert "something else entirely" in str(exc.value)


def test_rejected_stored_credential_falls_back_to_a_fresh_seed():
    """The documented recovery: an operator re-provisions and restarts us with a
    new seed while the store still holds the dead token."""
    presented: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["refresh_token"]
        presented.append(token)
        if token == "dead-rt":
            return httpx.Response(400, json=REJECTED_GRANT)
        return token_response()

    store = FakeStore(token="dead-rt")
    client = make_client(handler, refresh_token="fresh-seed", store=store)

    assert client._current_token() == "at-1"
    assert presented == ["dead-rt", "fresh-seed"]


def test_rejected_credential_with_no_alternative_does_not_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json=REJECTED_GRANT)

    store = FakeStore(token="same-rt")
    client = make_client(handler, refresh_token="same-rt", store=store)

    with pytest.raises(FaultMavenCredentialError):
        client._current_token()
    assert calls["n"] == 1


def test_renewal_without_a_rotated_token_is_an_error():
    """The backend rotates unconditionally; a response without a new refresh
    token means we would silently keep using a revoked one."""
    client = make_client(
        lambda r: httpx.Response(200, json={"access_token": "at-1", "expires_in": 900})
    )

    with pytest.raises(FaultMavenError, match="no refresh_token"):
        client._current_token()


# -- interaction with the request path ---------------------------------------
def test_401_on_a_turn_renews_and_retries_once():
    calls = {"token": 0, "case": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/oauth/token"):
            calls["token"] += 1
            return token_response(access=f"at-{calls['token']}")
        calls["case"] += 1
        if calls["case"] == 1:
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(201, json={"case_id": "c1", "state": "inquiry"})

    client = make_client(handler)
    assert client.create_case(title="t") == "c1"
    assert calls["token"] == 2
    assert calls["case"] == 2


def test_refresh_grant_takes_precedence_over_dev_login():
    """A cloud deployment keeps FAULTMAVEN_DEV_LOGIN_USERNAME set in config; the
    refresh credential must still win (dev-login 404s there)."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return token_response()

    client = make_client(handler, dev_login_username="slack-agent")
    client._current_token()

    assert paths == ["/api/v1/auth/oauth/token"]


# -- keepalive ----------------------------------------------------------------
def test_credential_is_due_for_renewal_near_expiry():
    client = make_client(lambda r: token_response())

    client._refresh_token = jwt_with_exp(time.time() + 3600)
    assert client._refresh_credential_is_due() is True


def test_credential_is_not_due_when_the_window_is_wide_open():
    client = make_client(lambda r: token_response())

    client._refresh_token = jwt_with_exp(time.time() + 7 * 24 * 3600)
    assert client._refresh_credential_is_due() is False


def test_jwt_expiry_reads_exp_and_tolerates_junk():
    assert _jwt_expiry(jwt_with_exp(1234567890)) == 1234567890
    assert _jwt_expiry("not-a-jwt") is None
    assert _jwt_expiry("a.b.c") is None
    assert _jwt_expiry("") is None


# -- the real store -----------------------------------------------------------
def test_credential_store_round_trips_across_instances(tmp_path):
    """Restart safety: what one process persists, the next process reads."""
    path = str(tmp_path / "credentials.db")

    first = CredentialStore(path)
    first.put("rt-1")
    first.put("rt-2")  # rotation overwrites, never accumulates
    first.close()

    second = CredentialStore(path)
    assert second.get() == "rt-2"
    second.close()


def test_credential_store_starts_empty(tmp_path):
    store = CredentialStore(str(tmp_path / "credentials.db"))
    assert store.get() is None
    store.close()


def test_credential_store_refuses_an_empty_token(tmp_path):
    store = CredentialStore(str(tmp_path / "credentials.db"))
    with pytest.raises(ValueError):
        store.put("")
    store.close()


# -- user-facing failure ------------------------------------------------------
def test_credential_failure_message_points_at_an_admin():
    """A dead credential is not the user's problem to retry."""
    from listeners._turn import CREDENTIAL_ERROR_TEXT, turn_error_text

    text = turn_error_text(FaultMavenCredentialError("expired"))

    assert text == CREDENTIAL_ERROR_TEXT
    assert "retrying won't help" in text.lower()


# -- client wiring ------------------------------------------------------------
def _agent_settings(monkeypatch, tmp_path, **env):
    from config import Settings

    monkeypatch.setenv("SLACK_TRANSPORT", "socket")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-x")
    monkeypatch.setenv("CREDENTIAL_STORE_PATH", str(tmp_path / "credentials.db"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_client_factory_wires_the_credential_store(monkeypatch, tmp_path):
    from app import make_fault_client

    settings = _agent_settings(monkeypatch, tmp_path, FAULTMAVEN_REFRESH_TOKEN="seed")
    client = make_fault_client(settings)

    assert client._credential_store is not None
    assert client._refresh_token == "seed"
    client.close()


def test_client_factory_skips_the_store_without_a_credential(monkeypatch, tmp_path):
    """A dev-login deployment must not create a credential file it never uses."""
    from app import make_fault_client

    settings = _agent_settings(monkeypatch, tmp_path)
    client = make_fault_client(settings)

    assert client._credential_store is None
    assert not (tmp_path / "credentials.db").exists()
    client.close()


def test_client_factory_uses_an_existing_store_without_the_seed(monkeypatch, tmp_path):
    """An operator who clears the one-time seed after bootstrap keeps working."""
    from app import make_fault_client

    path = tmp_path / "credentials.db"
    seeded = CredentialStore(str(path))
    seeded.put("rotated-rt")
    seeded.close()

    settings = _agent_settings(monkeypatch, tmp_path)
    client = make_fault_client(settings)

    assert client._refresh_token == "rotated-rt"
    client.close()


# -- keepalive renews for real ------------------------------------------------
def test_keepalive_renews_even_with_a_live_access_token():
    """The keepalive slides the REFRESH window; the access token's remaining
    life is irrelevant. Short-circuiting on a fresh access token would let an
    idle agent's credential expire — the exact lockout this exists to prevent."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return token_response(access=f"at-{calls['n']}", refresh=f"rt-{calls['n']}")

    client = make_client(handler)
    client._current_token()  # a live access token, good for ~15 minutes
    assert calls["n"] == 1

    client._renew(force=True)

    assert calls["n"] == 2


def test_persist_failure_does_not_burn_the_configured_seed():
    """A storage failure is not a rejected credential.

    Only a backend rejection may try an alternative credential; a failed write
    must not spend the operator's fresh seed on a retry that would hit the same
    broken volume.
    """
    presented: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        presented.append(json.loads(request.content)["refresh_token"])
        return token_response()

    store = FakeStore(token="stored-rt", fail_on_put=True)
    client = make_client(handler, refresh_token="fresh-seed", store=store)

    client._current_token()

    assert presented == ["stored-rt"]


# -- a write failure must not become a lockout --------------------------------
def test_persist_failure_keeps_the_rotated_token_in_play():
    """The rotated token is the only live one — the presented one is already
    revoked. Discarding it on a write error turns a momentarily full volume into
    a permanent lockout that recovering the disk cannot undo."""
    store = FakeStore(token="stored-rt", fail_on_put=True)
    client = make_client(lambda r: token_response(refresh="rt-next"), store=store)

    assert client._current_token() == "at-1"
    assert client._refresh_token == "rt-next"
    assert client._credential_unpersisted is True


def test_a_pending_write_is_retried_and_heals():
    """Once the volume recovers, the credential lands on disk without a restart."""
    store = FakeStore(token="stored-rt", fail_on_put=True)
    client = make_client(lambda r: token_response(refresh="rt-next"), store=store)
    client._current_token()

    store.fail_on_put = False  # disk recovers
    client._retry_pending_persist()

    assert store.puts == ["rt-next"]
    assert client._credential_unpersisted is False


# -- out-of-band rotation -----------------------------------------------------
def test_rejected_credential_falls_back_to_the_store():
    """Another process may have rotated it — preflight authenticates, which
    under the refresh grant consumes a rotation. A valid token sitting in the
    store must not be ignored while the agent declares itself locked out."""
    presented: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["refresh_token"]
        presented.append(token)
        if token == "stale-rt":
            return httpx.Response(400, json=REJECTED_GRANT)
        return token_response()

    store = FakeStore(token="stale-rt")
    client = make_client(handler, refresh_token="", store=store)
    store.token = "rotated-by-preflight"  # another process moved it on

    assert client._current_token() == "at-1"
    assert presented == ["stale-rt", "rotated-by-preflight"]


def test_store_then_seed_are_both_tried_before_declaring_lockout():
    presented: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["refresh_token"]
        presented.append(token)
        if token == "good-seed":
            return token_response()
        return httpx.Response(400, json=REJECTED_GRANT)

    store = FakeStore(token="dead-a")
    client = make_client(handler, refresh_token="good-seed", store=store)
    store.token = "dead-b"

    assert client._current_token() == "at-1"
    assert presented == ["dead-a", "dead-b", "good-seed"]


def test_lockout_is_still_reported_when_nothing_works():
    store = FakeStore(token="dead-a")
    client = make_client(
        lambda r: httpx.Response(400, json=REJECTED_GRANT),
        refresh_token="dead-b",
        store=store,
    )

    with pytest.raises(FaultMavenCredentialError):
        client._current_token()


# -- renewal is reachable from every path -------------------------------------
def test_mid_poll_401_renews_under_the_refresh_grant():
    """A 15-minute access token can expire inside a ≤120s poll. The dev-login
    gate on this branch made it unreachable in the natural oauth config."""
    calls = {"token": 0, "poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/oauth/token"):
            calls["token"] += 1
            return token_response(access=f"at-{calls['token']}")
        calls["poll"] += 1
        if calls["poll"] == 1:
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json={"agent_response": "done"})

    client = make_client(handler, dev_login_username="")
    resp = client._poll("/api/v1/cases/c1/turns/1")

    assert resp.status_code == 200
    assert calls["token"] == 2


# -- shutdown -----------------------------------------------------------------
def test_close_waits_for_an_in_flight_renewal():
    """Closing the http client or the store under a renewal loses the rotated
    token — and the presented one is already revoked."""
    started = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        release.wait(timeout=5)
        return token_response()

    store = FakeStore(token="rt-0")
    client = make_client(handler, store=store)

    renewal = threading.Thread(target=client._current_token)
    renewal.start()
    started.wait(timeout=5)

    closer = threading.Thread(target=client.close)
    closer.start()
    closer.join(timeout=0.5)
    assert closer.is_alive(), "close() returned while a renewal was in flight"

    release.set()
    renewal.join(timeout=5)
    closer.join(timeout=5)
    assert store.puts == ["rt-2"]
    assert store.closed is True


# -- diagnostics --------------------------------------------------------------
def test_auth_mode_reports_the_refresh_grant_without_a_configured_seed():
    """The steady state after bootstrap: credential in the store, seed cleared.
    Settings alone can't tell that from having no credential at all."""
    client = make_client(
        lambda r: token_response(), refresh_token="", store=FakeStore(token="rt-0")
    )

    assert client.auth_mode == "refresh grant"


def test_explicit_preset_token_is_not_shadowed_by_a_leftover_store(
    monkeypatch, tmp_path
):
    """A credentials.db left behind by an earlier deployment must not silently
    take over from an explicitly configured FAULTMAVEN_API_TOKEN."""
    from app import make_fault_client

    path = tmp_path / "credentials.db"
    leftover = CredentialStore(str(path))
    leftover.put("leftover-rt")
    leftover.close()

    settings = _agent_settings(monkeypatch, tmp_path, FAULTMAVEN_API_TOKEN="preset-tok")
    client = make_fault_client(settings)

    assert client._credential_store is None
    assert client._refresh_token == ""
    assert client._token == "preset-tok"
    assert client.auth_mode == "preset token"
    client.close()
