"""Multi-workspace OAuth persistence: the InstallationStore + OAuthStateStore.

The HTTP/OAuth transport (``docs/design.md`` §10.1) installs FaultMaven into
many Slack workspaces. Each install yields a per-team bot token (``xoxb``) that
Bolt must look up on every inbound request to authorize the right workspace, and
each ``/slack/install`` mints a short-lived state value the redirect must
validate against CSRF. Both live in a SQL database so they survive restarts and
are shared across replicas — keyed by ``team_id``/``enterprise_id``, never
cross-read between workspaces (§10.3 tenant isolation).

One ``SLACK_DATABASE_URL`` drives both stores: a repo-anchored SQLite file for
local development and tests, a Postgres URL in the cluster. The SQLAlchemy stores
speak both without code changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from slack_sdk.oauth.installation_store.sqlalchemy import (
    SQLAlchemyInstallationStore,
)
from slack_sdk.oauth.state_store.sqlalchemy import SQLAlchemyOAuthStateStore
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

logger = logging.getLogger("faultmaven.slack.oauth")

# How long an in-flight OAuth ``state`` stays valid between /slack/install and
# /slack/oauth_redirect. Slack's own default is 10 minutes; a user who consents
# slower than this just restarts the install, so a tight window is the safer
# CSRF posture.
_STATE_EXPIRATION_SECONDS = 600


@dataclass(slots=True)
class OAuthStores:
    """The pair of stores Bolt's ``OAuthSettings`` needs, plus their engine."""

    engine: Engine
    installation_store: SQLAlchemyInstallationStore
    state_store: SQLAlchemyOAuthStateStore


def build_oauth_stores(*, database_url: str, client_id: str) -> OAuthStores:
    """Create the installation + state stores against ``database_url``.

    Tables are created if absent (idempotent), so a fresh database — SQLite file
    or an empty Postgres schema — boots without a manual migration step. The
    installation store is scoped to ``client_id`` so two apps sharing a database
    can't read each other's tokens.
    """

    # SQLite under a threaded web server: the same engine is used across worker
    # threads, so disable the single-thread guard (the stores open short-lived
    # connections per call; SQLite serializes writes itself). Harmless for
    # Postgres, which ignores the connect arg.
    connect_args = (
        {"check_same_thread": False}
        if database_url.startswith("sqlite")
        else {}
    )
    engine = create_engine(database_url, connect_args=connect_args)

    installation_store = SQLAlchemyInstallationStore(
        client_id=client_id, engine=engine, logger=logger
    )
    state_store = SQLAlchemyOAuthStateStore(
        expiration_seconds=_STATE_EXPIRATION_SECONDS,
        engine=engine,
        logger=logger,
    )

    # Idempotent DDL. ``metadata`` on each store carries its table definitions;
    # create_all() no-ops for tables that already exist.
    installation_store.metadata.create_all(engine)
    state_store.metadata.create_all(engine)

    logger.info(
        "OAuth stores ready (%s)",
        engine.url.render_as_string(hide_password=True),
    )
    return OAuthStores(
        engine=engine,
        installation_store=installation_store,
        state_store=state_store,
    )
