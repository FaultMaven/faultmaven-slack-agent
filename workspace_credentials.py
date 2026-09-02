"""Per-workspace FaultMaven credentials — the Slack workspace → tenant binding.

ADR-013 D3 maps a **Slack workspace to a FaultMaven Team** inside the customer's
**Organization**, and the agent authenticates as that workspace's ``slack``
service account so its cases are owned there and auto-share to the right Team.
This module is where those per-workspace credentials live.

**Why beside the installation, not on the pod's volume.** The binding is
per-install, exactly like the bot token in :mod:`oauth_store` — so it belongs in
the same ``SLACK_DATABASE_URL`` database, which is shared across replicas and
survives a pod moving. The single-row SQLite :class:`credentials.CredentialStore`
stays for the *process-wide default* credential (Socket Mode, self-hosted), which
is genuinely one-per-process state.

**Tenancy travels only in the token chain.** The backend's ``users`` table has no
organization column: ``/auth/refresh`` re-attaches whatever ``organization_id``
the presented refresh token carried. So nothing server-side contradicts a
credential provisioned against the wrong organization. That is why
``organization_id`` is stored here alongside the token and asserted against every
minted access token (``FaultMavenClient._assert_expected_org``) — this row is the
only place the intended tenant is written down.

**Rotation across replicas.** The refresh grant rotates: presenting a token
revokes it. A client serializes its own renewals per credential, but that lock is
per-process, so two replicas renewing the *same* workspace credential can still
revoke each other. The recovery is already in place and works precisely because
this store is shared: a rejected credential is retried against whatever the store
holds *now* (``FaultMavenClient._alternative_credentials``), which is what the
other replica wrote. Recovery, not prevention — a deployment that wants
prevention needs row-level locking held across the token exchange.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    select,
)
from sqlalchemy.engine import Engine

logger = logging.getLogger("faultmaven.slack.workspace_credentials")

#: Placeholder for the non-Grid case. Slack sends no ``enterprise_id`` for an
#: ordinary workspace install, and NULL would not work in a primary key.
NO_ENTERPRISE = ""


@dataclass(frozen=True, slots=True)
class WorkspaceCredential:
    """One workspace's binding: which tenant it acts as, and with what token."""

    team_id: str
    organization_id: str
    refresh_token: str
    enterprise_id: str = NO_ENTERPRISE
    #: The FaultMaven Team the workspace maps to. Recorded for diagnostics and
    #: for the install-time provisioning flow; the agent itself never sends it —
    #: sharing is resolved server-side from the service account's membership.
    faultmaven_team_id: str | None = None


class WorkspaceCredentialStore:
    """Slack ``team_id`` → the FaultMaven credential the agent acts as for it."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._metadata = MetaData()
        self._table = Table(
            "fm_workspace_credentials",
            self._metadata,
            # Composite from the start: an Enterprise Grid install surfaces its
            # workspaces under an enterprise, and widening a primary key on a
            # live table holding credentials is a migration worth not needing.
            # Grid itself is out of scope here (an org-wide install has no
            # team_id at install time — see docs/design.md §10.1).
            Column("enterprise_id", String(32), primary_key=True),
            Column("team_id", String(32), primary_key=True),
            Column("organization_id", String(64), nullable=False),
            Column("faultmaven_team_id", String(64), nullable=True),
            Column("refresh_token", Text, nullable=False),
            Column("updated_at", DateTime, nullable=False),
        )
        self._metadata.create_all(engine)

    # -- reads --------------------------------------------------------------
    def get(
        self, team_id: str, *, enterprise_id: str = NO_ENTERPRISE
    ) -> WorkspaceCredential | None:
        """The workspace's binding, or None if it has never been bound."""

        with self._engine.connect() as conn:
            row = conn.execute(
                select(
                    self._table.c.team_id,
                    self._table.c.organization_id,
                    self._table.c.refresh_token,
                    self._table.c.enterprise_id,
                    self._table.c.faultmaven_team_id,
                ).where(
                    self._table.c.team_id == team_id,
                    self._table.c.enterprise_id == enterprise_id,
                )
            ).first()
        if row is None:
            return None
        return WorkspaceCredential(
            team_id=row.team_id,
            organization_id=row.organization_id,
            refresh_token=row.refresh_token,
            enterprise_id=row.enterprise_id,
            faultmaven_team_id=row.faultmaven_team_id,
        )

    def team_ids(self) -> list[str]:
        """Every bound workspace, for preflight and diagnostics."""

        with self._engine.connect() as conn:
            return [
                row.team_id
                for row in conn.execute(select(self._table.c.team_id)).all()
            ]

    # -- writes -------------------------------------------------------------
    def bind(
        self,
        *,
        team_id: str,
        organization_id: str,
        refresh_token: str,
        enterprise_id: str = NO_ENTERPRISE,
        faultmaven_team_id: str | None = None,
    ) -> None:
        """Bind a workspace to a tenant, replacing any previous binding.

        Idempotent, so a reinstall re-binds rather than duplicating. Refuses the
        pieces that would produce a live-looking row the agent cannot use: a
        binding without an organization could not be checked against the token's
        claim, which is the whole guard against a cross-tenant misroute.
        """

        if not team_id:
            raise ValueError("team_id is required")
        if not organization_id:
            raise ValueError("organization_id is required")
        if not refresh_token:
            raise ValueError("refusing to persist an empty refresh token")

        with self._engine.begin() as conn:
            conn.execute(
                self._table.delete().where(
                    self._table.c.team_id == team_id,
                    self._table.c.enterprise_id == enterprise_id,
                )
            )
            conn.execute(
                self._table.insert().values(
                    enterprise_id=enterprise_id,
                    team_id=team_id,
                    organization_id=organization_id,
                    faultmaven_team_id=faultmaven_team_id,
                    refresh_token=refresh_token,
                    updated_at=datetime.now(timezone.utc),
                )
            )
        logger.info(
            "Bound Slack workspace %s to FaultMaven organization %s",
            team_id,
            organization_id,
        )

    def put_refresh_token(
        self, team_id: str, refresh_token: str, *, enterprise_id: str = NO_ENTERPRISE
    ) -> None:
        """Commit a rotated refresh token, before the agent relies on it.

        Callers rely on this being durable on return: the token it replaces is
        already revoked server-side, so a rotation used before it is committed is
        a lockout if the process dies in between.

        Only ever an UPDATE — a rotation must not resurrect a binding that was
        deleted (an uninstall) as a row with no organization to check against.
        """

        if not refresh_token:
            raise ValueError("refusing to persist an empty refresh token")
        with self._engine.begin() as conn:
            result = conn.execute(
                self._table.update()
                .where(
                    self._table.c.team_id == team_id,
                    self._table.c.enterprise_id == enterprise_id,
                )
                .values(
                    refresh_token=refresh_token,
                    updated_at=datetime.now(timezone.utc),
                )
            )
        if result.rowcount == 0:
            raise KeyError(f"no FaultMaven credential bound for workspace {team_id}")

    def unbind(self, team_id: str, *, enterprise_id: str = NO_ENTERPRISE) -> None:
        """Drop a workspace's binding (uninstall / token revocation).

        The workspace's *cases* are unaffected — they are Team artifacts owned by
        the service account, not by the installation.
        """

        with self._engine.begin() as conn:
            conn.execute(
                self._table.delete().where(
                    self._table.c.team_id == team_id,
                    self._table.c.enterprise_id == enterprise_id,
                )
            )

    def close(self) -> None:
        """No-op: the engine is owned by :mod:`oauth_store`, which closes it."""
