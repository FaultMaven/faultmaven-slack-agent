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

**Lifecycle is event-driven.** :meth:`~WorkspaceCredentialStore.bind` is called
by the install flow (:mod:`binding`, once an organization admin has consented),
and :meth:`~WorkspaceCredentialStore.unbind` by the ``app_uninstalled`` /
``tokens_revoked`` listeners in :mod:`listeners.lifecycle`, so a credential no
longer outlives the installation that authorized it. An operator can still write
either directly: the client re-reads this store whenever a credential is
rejected, so a change takes effect on a running process without a restart.

One gap remains, bounded and noisy rather than silent: Slack delivers an
uninstall to **one** replica, and another replica holding the workspace in its
in-process cache keeps renewing that credential until its next rotation, when
:meth:`put_refresh_token` finds no row and raises. Closing it properly needs
cross-replica invalidation, which is the same "recovery, not prevention" posture
as the rotation race below.

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
    inspect,
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
    #: When this row was last written. The client ages a restored credential
    #: from this rather than from when it happened to load it, so the renewal
    #: cadence is not reset by a restart.
    updated_at: datetime | None = None


class WorkspaceCredentialStore:
    """Slack ``team_id`` → the FaultMaven credential the agent acts as for it."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._metadata = MetaData()
        self._table = Table(
            "fm_workspace_credentials",
            self._metadata,
            # Keyed on team_id ALONE, matching the server's binding key
            # (faultmaven-cloud `POST /api/v1/admin/integrations/slack/
            # workspaces`, whose service account username derives from the
            # workspace id and whose `slack_enterprise_id` is explicitly
            # "recorded but not part of the binding key, so a workspace that
            # later joins a Grid keeps its binding").
            #
            # A composite (enterprise_id, team_id) key would diverge from that
            # the moment a customer converts to an Enterprise Grid: Slack starts
            # sending an enterprise_id, the server still holds one binding under
            # the team id, and this store would miss the row — turning a bound
            # workspace into an unbound one on an event we do not control.
            Column("team_id", String(32), primary_key=True),
            # Recorded, not keyed: worth knowing which Grid a workspace belongs
            # to, never worth failing a lookup over.
            Column("enterprise_id", String(32), nullable=False, server_default=""),
            Column("organization_id", String(64), nullable=False),
            Column("faultmaven_team_id", String(64), nullable=True),
            Column("refresh_token", Text, nullable=False),
            Column("updated_at", DateTime, nullable=False),
        )
        self._migrate_from_composite_key(engine)
        self._metadata.create_all(engine)

    # -- migration ----------------------------------------------------------
    def _migrate_from_composite_key(self, engine: Engine) -> None:
        """Re-key an existing table from ``(enterprise_id, team_id)`` to ``team_id``.

        The first shipped version of this table keyed on both columns. That
        diverges from the server, which binds on the workspace alone and records
        the Grid id without keying on it — so a workspace joining a Grid would
        stop resolving here while the server still considered it bound.

        ``create_all`` is ``checkfirst`` and silently no-ops on an existing
        table, so without this an already-deployed database would keep the old
        shape forever and the fix would appear to have been applied. Worse, the
        new ``WHERE team_id = ?`` writes could match two rows there and put one
        workspace's rotated token into a row bound to a different organization.

        Idempotent: it inspects the live primary key and returns immediately
        unless the old shape is actually present.
        """

        inspector = inspect(engine)
        if not inspector.has_table(self._table.name):
            return
        pk = set(
            inspector.get_pk_constraint(self._table.name).get(
                "constrained_columns", []
            )
        )
        if pk == {"team_id"}:
            return
        if "enterprise_id" not in pk:
            # Some other shape entirely — not ours to rewrite. Say so rather
            # than silently proceeding against a table we do not recognise.
            raise RuntimeError(
                f"{self._table.name} has an unexpected primary key {sorted(pk)}; "
                "refusing to migrate it automatically"
            )

        legacy = f"{self._table.name}__legacy"
        logger.warning(
            "Migrating %s from a composite (enterprise_id, team_id) key to "
            "team_id, to match the server's binding key",
            self._table.name,
        )
        with engine.begin() as conn:
            conn.exec_driver_sql(
                f"ALTER TABLE {self._table.name} RENAME TO {legacy}"
            )
            self._table.create(conn)
            # A workspace could have rows under both "" and a Grid id. Keep the
            # most recently written one: it is the live binding, and the other
            # is a leftover from before the conversion.
            conn.exec_driver_sql(
                f"""
                INSERT INTO {self._table.name}
                    (team_id, enterprise_id, organization_id,
                     faultmaven_team_id, refresh_token, updated_at)
                SELECT team_id, enterprise_id, organization_id,
                       faultmaven_team_id, refresh_token, updated_at
                FROM {legacy} l
                WHERE l.updated_at = (
                    SELECT MAX(l2.updated_at) FROM {legacy} l2
                    WHERE l2.team_id = l.team_id
                )
                """
            )
            conn.exec_driver_sql(f"DROP TABLE {legacy}")
        logger.info("Migration of %s complete", self._table.name)

    # -- reads --------------------------------------------------------------
    def get(self, team_id: str) -> WorkspaceCredential | None:
        """The workspace's binding, or None if it has never been bound.

        Takes no ``enterprise_id``: the binding is keyed on the workspace alone,
        so a workspace keeps its binding when it joins or leaves a Grid.
        """

        with self._engine.connect() as conn:
            row = conn.execute(
                select(
                    self._table.c.team_id,
                    self._table.c.organization_id,
                    self._table.c.refresh_token,
                    self._table.c.enterprise_id,
                    self._table.c.faultmaven_team_id,
                    self._table.c.updated_at,
                ).where(self._table.c.team_id == team_id)
            ).first()
        if row is None:
            return None
        return WorkspaceCredential(
            team_id=row.team_id,
            organization_id=row.organization_id,
            refresh_token=row.refresh_token,
            enterprise_id=row.enterprise_id,
            faultmaven_team_id=row.faultmaven_team_id,
            updated_at=row.updated_at,
        )

    def team_ids(self) -> list[str]:
        """Every bound workspace, for preflight and diagnostics.

        Needs no Grid filter now that the key is the workspace alone: every row
        listed here is a row :meth:`get` can resolve.
        """

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
                self._table.delete().where(self._table.c.team_id == team_id)
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

    def put_refresh_token(self, team_id: str, refresh_token: str) -> None:
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
                .where(self._table.c.team_id == team_id)
                .values(
                    refresh_token=refresh_token,
                    updated_at=datetime.now(timezone.utc),
                )
            )
        if result.rowcount == 0:
            raise KeyError(f"no FaultMaven credential bound for workspace {team_id}")

    def unbind(self, team_id: str) -> None:
        """Drop a workspace's binding (uninstall / token revocation).

        The workspace's *cases* are unaffected — they are Team artifacts owned by
        the service account, not by the installation.
        """

        with self._engine.begin() as conn:
            conn.execute(
                self._table.delete().where(self._table.c.team_id == team_id)
            )

    def close(self) -> None:
        """No-op: this store does not own its engine.

        The engine is created by :func:`oauth_store.build_oauth_stores` and is
        shared with Bolt's installation and state stores, which outlive any one
        client — so disposing it here would pull the pool out from under them.
        Whoever built it disposes it (``scripts/preflight.py`` does; the long
        running server holds it for the process lifetime by design).
        """
