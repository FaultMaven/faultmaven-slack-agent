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

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Slack credentials -------------------------------------------------
    # Bot User OAuth token ("xoxb-..."): used to call the Slack Web API.
    slack_bot_token: str = Field(..., validation_alias="SLACK_BOT_TOKEN")
    # Signing secret: verifies inbound requests (used by Bolt in HTTP mode).
    slack_signing_secret: str = Field(
        default="", validation_alias="SLACK_SIGNING_SECRET"
    )
    # App-level token ("xapp-..."): required for Socket Mode (the P0 dev transport).
    slack_app_token: str = Field(default="", validation_alias="SLACK_APP_TOKEN")

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

    @field_validator("slack_bot_token")
    @classmethod
    def _validate_bot_token(cls, value: str) -> str:
        if not value.startswith(("xoxb-", "xoxp-")):
            raise ValueError(
                "SLACK_BOT_TOKEN must start with 'xoxb-' or 'xoxp-'"
            )
        return value

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
