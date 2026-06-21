"""Application settings for the FaultMaven Slack Agent.

All configuration is sourced from environment variables (or a local ``.env``
file during development) and validated on startup. Importing this module is
cheap; the singleton :data:`settings` is built lazily via
:func:`get_settings` so tests can override the environment before the first
access.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime configuration.

    Fails fast at import/startup time if a required secret is missing, which
    is preferable to discovering a missing token on the first inbound Slack
    event (when we only have ~3 seconds to respond).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Slack credentials -------------------------------------------------
    # Bot User OAuth token ("xoxb-..."): used to call the Slack Web API.
    slack_bot_token: str = Field(..., validation_alias="SLACK_BOT_TOKEN")
    # Signing secret: used to verify the authenticity of inbound requests.
    slack_signing_secret: str = Field(..., validation_alias="SLACK_SIGNING_SECRET")

    # --- FaultMaven core orchestration API ---------------------------------
    faultmaven_api_url: str = Field(
        default="http://localhost:8090",
        validation_alias="FAULTMAVEN_API_URL",
    )
    faultmaven_api_key: str = Field(
        default="",
        validation_alias="FAULTMAVEN_API_KEY",
    )
    # Upper bound for a single orchestration call. The Slack-facing webhook is
    # already ACK'd by the time we call FaultMaven, so this can comfortably
    # exceed Slack's 3s budget.
    faultmaven_request_timeout: float = Field(
        default=60.0,
        validation_alias="FAULTMAVEN_REQUEST_TIMEOUT",
    )

    # --- Replay protection -------------------------------------------------
    # Reject signed requests whose timestamp is older than this (seconds).
    slack_request_max_age: int = Field(
        default=60 * 5,
        validation_alias="SLACK_REQUEST_MAX_AGE",
    )

    # --- Server ------------------------------------------------------------
    host: str = Field(default="0.0.0.0", validation_alias="HOST")
    port: int = Field(default=3000, validation_alias="PORT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @field_validator("slack_bot_token")
    @classmethod
    def _validate_bot_token(cls, value: str) -> str:
        if not value.startswith(("xoxb-", "xoxp-")):
            raise ValueError(
                "SLACK_BOT_TOKEN must be a Slack bot/user token "
                "(starts with 'xoxb-' or 'xoxp-')"
            )
        return value

    @field_validator("faultmaven_api_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so repeated imports don't re-parse the environment. Tests can call
    ``get_settings.cache_clear()`` after patching ``os.environ``.
    """

    return Settings()  # type: ignore[call-arg]
