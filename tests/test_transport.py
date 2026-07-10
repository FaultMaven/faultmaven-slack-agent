"""HTTP/OAuth transport: config validation, OAuth stores, and the FastAPI app.

Covers the P5 transport surface (docs/design.md §10, §16):
* per-transport credential validation fails fast at boot;
* the SQLAlchemy Installation/OAuthState stores round-trip;
* the FastAPI app exposes /health + the three /slack/* routes with the right
  status codes (unsigned events rejected, bad redirect 4xx — never 500);
* build_app() selects OAuth vs. static-token wiring by transport.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from slack_sdk.oauth.installation_store.models.installation import Installation

import config
from config import Settings


def _settings(monkeypatch, **env) -> Settings:
    """Build Settings from an explicit env, ignoring any local .env."""

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


# --- transport credential validation ----------------------------------------


def test_socket_mode_requires_both_tokens(monkeypatch):
    with pytest.raises(ValueError, match="SLACK_APP_TOKEN"):
        _settings(
            monkeypatch,
            SLACK_TRANSPORT="socket",
            SLACK_BOT_TOKEN="xoxb-x",
            SLACK_APP_TOKEN="",
        )


def test_http_mode_requires_oauth_credentials(monkeypatch):
    with pytest.raises(ValueError, match="SLACK_CLIENT_ID"):
        _settings(
            monkeypatch,
            SLACK_TRANSPORT="http",
            SLACK_CLIENT_ID="",
            SLACK_CLIENT_SECRET="s",
            SLACK_SIGNING_SECRET="sign",
        )


def test_http_mode_valid_settings_construct(monkeypatch):
    s = _settings(
        monkeypatch,
        SLACK_TRANSPORT="http",
        SLACK_CLIENT_ID="123.456",
        SLACK_CLIENT_SECRET="secret",
        SLACK_SIGNING_SECRET="signsign",
    )
    assert s.slack_transport == "http"
    assert s.slack_bot_token == ""  # no static bot token in OAuth mode


def test_unknown_transport_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="SLACK_TRANSPORT"):
        _settings(monkeypatch, SLACK_TRANSPORT="carrier-pigeon")


def test_bot_token_shape_validated_when_present(monkeypatch):
    with pytest.raises(ValueError, match="xoxb-"):
        _settings(
            monkeypatch,
            SLACK_TRANSPORT="socket",
            SLACK_BOT_TOKEN="not-a-token",
            SLACK_APP_TOKEN="xapp-x",
        )


def test_default_oauth_db_url_is_repo_anchored_sqlite(monkeypatch):
    s = _settings(
        monkeypatch,
        SLACK_TRANSPORT="http",
        SLACK_CLIENT_ID="123.456",
        SLACK_CLIENT_SECRET="secret",
        SLACK_SIGNING_SECRET="signsign",
        SLACK_DATABASE_URL="",
    )
    assert s.slack_database_url.startswith("sqlite:///")
    assert s.slack_database_url.endswith("data/slack_oauth.db")


def test_explicit_postgres_url_passes_through(monkeypatch):
    url = "postgresql://u:p@db:5432/faultmaven_slack"
    s = _settings(
        monkeypatch,
        SLACK_TRANSPORT="http",
        SLACK_CLIENT_ID="123.456",
        SLACK_CLIENT_SECRET="secret",
        SLACK_SIGNING_SECRET="signsign",
        SLACK_DATABASE_URL=url,
    )
    assert s.slack_database_url == url


# --- OAuth stores ------------------------------------------------------------


def test_oauth_stores_create_tables_and_roundtrip(tmp_path):
    from oauth_store import build_oauth_stores

    url = f"sqlite:///{tmp_path / 'oauth.db'}"
    stores = build_oauth_stores(database_url=url, client_id="123.456")

    inst = Installation(
        app_id="A1",
        enterprise_id=None,
        team_id="T1",
        user_id="U1",
        bot_token="xoxb-team1",
        bot_id="B1",
        bot_user_id="BU1",
    )
    stores.installation_store.save(inst)
    got = stores.installation_store.find_bot(enterprise_id=None, team_id="T1")
    assert got is not None and got.bot_token == "xoxb-team1"

    # A different team is not readable (tenant isolation at the store layer).
    assert stores.installation_store.find_bot(enterprise_id=None, team_id="T2") is None

    state = stores.state_store.issue()
    assert stores.state_store.consume(state) is True
    # A state is single-use — a replayed redirect must fail.
    assert stores.state_store.consume(state) is False


# --- FastAPI app -------------------------------------------------------------


@pytest.fixture
def http_client(monkeypatch, tmp_path):
    """A TestClient over the HTTP transport app, fully hermetic.

    FAULTMAVEN_API_TOKEN is preset so the FM client's startup short-circuits
    (no network), keeping the app build offline and fast.
    """

    monkeypatch.setenv("SLACK_TRANSPORT", "http")
    monkeypatch.setenv("SLACK_CLIENT_ID", "123.456")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "signsign")
    monkeypatch.setenv("SLACK_DATABASE_URL", f"sqlite:///{tmp_path / 'oauth.db'}")
    monkeypatch.setenv("CASE_STORE_PATH", str(tmp_path / "cases.db"))
    monkeypatch.setenv("FAULTMAVEN_API_TOKEN", "preset-token")
    config.get_settings.cache_clear()

    from web import create_fastapi_app

    with TestClient(create_fastapi_app()) as client:
        yield client
    config.get_settings.cache_clear()


def test_health_is_ok_and_dependency_free(http_client):
    resp = http_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_install_page_renders(http_client):
    resp = http_client.get("/slack/install", follow_redirects=False)
    assert resp.status_code == 200


def test_oauth_redirect_without_code_is_client_error(http_client):
    resp = http_client.get("/slack/oauth_redirect", follow_redirects=False)
    assert 400 <= resp.status_code < 500  # not a 500


def test_unsigned_event_is_rejected(http_client):
    resp = http_client.post(
        "/slack/events", json={"type": "url_verification", "challenge": "x"}
    )
    assert resp.status_code == 401  # signature verification, not a 500


# --- transport selection in build_app ---------------------------------------


def test_build_app_http_wires_oauth_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_TRANSPORT", "http")
    monkeypatch.setenv("SLACK_CLIENT_ID", "123.456")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "signsign")
    monkeypatch.setenv("SLACK_DATABASE_URL", f"sqlite:///{tmp_path / 'o.db'}")
    monkeypatch.setenv("CASE_STORE_PATH", str(tmp_path / "c.db"))
    monkeypatch.setenv("FAULTMAVEN_API_TOKEN", "preset-token")
    config.get_settings.cache_clear()
    try:
        from app import build_app

        bolt_app, store, fm, settings = build_app()
        try:
            assert settings.slack_transport == "http"
            assert bolt_app.oauth_flow is not None
        finally:
            store.close()
            fm.close()
    finally:
        config.get_settings.cache_clear()


def test_build_app_socket_uses_static_token(monkeypatch, tmp_path):
    # Socket mode passes a static bot token; Bolt eagerly verifies it via
    # auth.test at construction (real, desirable prod behavior). Stub that one
    # network call so the test exercises transport WIRING with a fake token.
    from slack_sdk.web.client import WebClient

    monkeypatch.setattr(
        WebClient,
        "auth_test",
        lambda self, **kw: {"ok": True, "team_id": "T1", "user_id": "U1"},
    )
    monkeypatch.setenv("SLACK_TRANSPORT", "socket")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-static")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-static")
    monkeypatch.setenv("CASE_STORE_PATH", str(tmp_path / "c.db"))
    monkeypatch.setenv("FAULTMAVEN_API_TOKEN", "preset-token")
    config.get_settings.cache_clear()
    try:
        from app import build_app

        bolt_app, store, fm, settings = build_app()
        try:
            assert settings.slack_transport == "socket"
            assert bolt_app.oauth_flow is None
        finally:
            store.close()
            fm.close()
    finally:
        config.get_settings.cache_clear()
