"""Application settings for the FaultMaven Slack Agent.

Sourced from environment variables (or a local ``.env`` during development) and
validated on startup, so a missing token fails fast rather than on the first
inbound Slack event. The singleton is built lazily via :func:`get_settings` so
tests can patch the environment before the first access.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Bot scopes requested at install time. Kept in lockstep with manifest.json's
# oauth_config.bot scopes — the manifest is what Slack shows on the consent
# screen; this list is what Bolt's OAuthSettings sends in the authorize URL, and
# a mismatch yields an install that silently lacks a scope a listener needs.
DEFAULT_BOT_SCOPES: tuple[str, ...] = (
    "assistant:write",
    "chat:write",
    "commands",
    "reactions:write",
    "app_mentions:read",
    "files:read",
    "im:history",
    "channels:history",
    "groups:history",
)


class Settings(BaseSettings):
    """Validated runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Transport ---------------------------------------------------------
    # "http"   → HTTP/Events + multi-workspace OAuth (the hosted, production
    #            transport; installs into many workspaces, Marketplace-eligible).
    # "socket" → Socket Mode against a single dev app (local development only;
    #            no public URL, not multi-workspace).
    slack_transport: str = Field(
        default="socket", validation_alias="SLACK_TRANSPORT"
    )

    # --- Slack credentials -------------------------------------------------
    # Bot User OAuth token ("xoxb-..."): the single static bot token used by
    # Socket Mode. In HTTP/OAuth mode there is NO static bot token — per-team
    # tokens come from the InstallationStore — so this is optional there.
    slack_bot_token: str = Field(default="", validation_alias="SLACK_BOT_TOKEN")
    # Signing secret: verifies inbound HTTP requests (required in HTTP mode).
    slack_signing_secret: str = Field(
        default="", validation_alias="SLACK_SIGNING_SECRET"
    )
    # App-level token ("xapp-..."): required for Socket Mode.
    slack_app_token: str = Field(default="", validation_alias="SLACK_APP_TOKEN")

    # --- Slack OAuth (HTTP mode) -------------------------------------------
    # From "Basic Information → App Credentials". Drive the /slack/install →
    # /slack/oauth_redirect distribution flow that yields per-team bot tokens.
    slack_client_id: str = Field(default="", validation_alias="SLACK_CLIENT_ID")
    slack_client_secret: str = Field(
        default="", validation_alias="SLACK_CLIENT_SECRET"
    )
    # SQLAlchemy URL backing the per-team InstallationStore + OAuthStateStore.
    # REQUIRED in http mode (validated below) — there is deliberately no default:
    # a silent SQLite fallback on the container's ephemeral disk would drop every
    # workspace's bot token on the first restart. A Postgres URL in the cluster;
    # for local http testing, set it explicitly (e.g. sqlite:///data/oauth.db).
    slack_database_url: str = Field(
        default="", validation_alias="SLACK_DATABASE_URL"
    )
    # Optional explicit redirect URI. If empty, Bolt derives it from the inbound
    # request host + the redirect path — correct behind a well-configured proxy,
    # but pin it here when the public host differs from what the app sees.
    slack_oauth_redirect_uri: str = Field(
        default="", validation_alias="SLACK_OAUTH_REDIRECT_URI"
    )
    # Port the HTTP transport binds inside the container (k8s maps the Service
    # to it; the public HTTPS termination happens at the ingress).
    http_port: int = Field(default=3000, validation_alias="HTTP_PORT")

    # --- FaultMaven core API -----------------------------------------------
    faultmaven_api_url: str = Field(
        default="http://localhost:8090", validation_alias="FAULTMAVEN_API_URL"
    )
    # Pre-obtained bearer token. If empty, the agent bootstraps one via
    # /auth/dev-login using ``faultmaven_dev_login_username`` (local auth mode).
    faultmaven_api_token: str = Field(
        default="", validation_alias="FAULTMAVEN_API_TOKEN"
    )
    faultmaven_dev_login_username: str = Field(
        default="admin", validation_alias="FAULTMAVEN_DEV_LOGIN_USERNAME"
    )
    # Upper bound for one turn (incl. 202+poll). Runs behind the Slack ack, so
    # it can comfortably exceed Slack's 3s budget.
    faultmaven_request_timeout: float = Field(
        default=120.0, validation_alias="FAULTMAVEN_REQUEST_TIMEOUT"
    )

    # --- Local state -------------------------------------------------------
    # SQLite file backing the thread→case map (the source of truth for "which
    # FaultMaven case is this Slack thread").
    case_store_path: str = Field(
        default="data/cases.db", validation_alias="CASE_STORE_PATH"
    )

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @field_validator("slack_transport")
    @classmethod
    def _normalize_transport(cls, value: str) -> str:
        mode = value.strip().lower()
        if mode not in ("http", "socket"):
            raise ValueError(
                f"SLACK_TRANSPORT must be 'http' or 'socket', got {value!r}"
            )
        return mode

    @field_validator("slack_bot_token")
    @classmethod
    def _validate_bot_token(cls, value: str) -> str:
        # Optional overall (HTTP/OAuth mode has no static bot token — per-team
        # tokens come from the InstallationStore). Validate the shape only when
        # one is actually provided; the transport check below enforces presence
        # for Socket Mode.
        if value and not value.startswith(("xoxb-", "xoxp-")):
            raise ValueError(
                "SLACK_BOT_TOKEN must start with 'xoxb-' or 'xoxp-'"
            )
        return value

    @model_validator(mode="after")
    def _validate_transport_requirements(self) -> "Settings":
        """Fail fast at startup when a transport is missing its credentials.

        Each transport needs a disjoint credential set; deferring the check to
        the first inbound Slack event would surface it as an opaque runtime
        error instead of a clear boot failure.
        """

        if self.slack_transport == "socket":
            missing = [
                name
                for name, val in (
                    ("SLACK_BOT_TOKEN", self.slack_bot_token),
                    ("SLACK_APP_TOKEN", self.slack_app_token),
                )
                if not val
            ]
            if missing:
                raise ValueError(
                    "Socket Mode requires "
                    + " and ".join(missing)
                    + " (set SLACK_TRANSPORT=http for the hosted OAuth transport)"
                )
        else:  # http
            missing = [
                name
                for name, val in (
                    ("SLACK_CLIENT_ID", self.slack_client_id),
                    ("SLACK_CLIENT_SECRET", self.slack_client_secret),
                    ("SLACK_SIGNING_SECRET", self.slack_signing_secret),
                    # Required so per-team bot tokens land in durable storage,
                    # never an ephemeral pod-local SQLite file.
                    ("SLACK_DATABASE_URL", self.slack_database_url),
                )
                if not val
            ]
            if missing:
                raise ValueError(
                    "HTTP/OAuth transport requires "
                    + ", ".join(missing)
                    + " (from the app's Basic Information → App Credentials)"
                )
        return self

    @field_validator("faultmaven_api_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        # logging.basicConfig only accepts the uppercase names; a lowercase
        # LOG_LEVEL=debug would otherwise crash at startup with an opaque
        # "Unknown level" ValueError that never names the setting.
        level = value.strip().upper()
        if level not in logging.getLevelNamesMapping():
            raise ValueError(
                f"LOG_LEVEL must be one of "
                f"{sorted(logging.getLevelNamesMapping())}, got {value!r}"
            )
        return level

    @field_validator("case_store_path")
    @classmethod
    def _anchor_store_path(cls, value: str) -> str:
        # The thread→case map is the source of truth for which case a thread
        # belongs to. Anchor a relative path to the repo (this file's parent),
        # not the cwd — starting the agent from a different directory would
        # otherwise silently fork an empty store and every active thread would
        # lose its investigation.
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path
        return str(path)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""

    return Settings()  # type: ignore[call-arg]
