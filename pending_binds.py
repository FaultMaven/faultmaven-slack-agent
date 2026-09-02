"""Pending workspace binds — the state that ties the two OAuth legs together.

Binding a workspace chains **two** OAuth flows: Slack's install, then
FaultMaven's admin consent. The dangerous part is not either flow, it is the
join between them. Without a binding that a third party cannot forge or replay,
this attack works:

1. An attacker installs the Slack app into **their own** workspace — entirely
   legitimate, and it yields a FaultMaven authorize URL.
2. They forward that URL to an admin of a **victim** organization.
3. The victim is signed in to FaultMaven. The consent screen cannot name the
   workspace (it renders the client name and a caller-supplied scope string), so
   nothing on it looks wrong. They approve.
4. The code lands on our callback and we bind the **attacker's workspace into
   the victim's tenant** — a `slack` service account inside their organization,
   on a Team, receiving the attacker's Slack traffic.

What stops it is that the record below is addressed by a value held **only in
the installing admin's browser**, as a cookie, and separately from the ``state``
that travels in the URL. Forwarding the URL carries the state but not the
cookie, so step 3 lands on a callback that refuses. Both halves are required and
neither is guessable.

Three further properties, each load-bearing:

* **The workspace id is read from here, never from the request.** The bind call
  names the workspace the *completed Slack install* established. A team id taken
  from a query parameter would hand the attacker back the same attack.
* **Single-use, including on failure.** A record is consumed before the token
  exchange, so a refused bind (the admin lacks ``ORG_MANAGE_SETTINGS``, say)
  cannot be retried against a record that is still redeemable.
* **The PKCE verifier never leaves the server.** It lives in this row, not in a
  cookie and not in ``state`` — which is what makes a leaked code (an access log,
  a ``Referer``) unredeemable by whoever leaked it.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, Column, DateTime, MetaData, String, Table, select
from sqlalchemy.engine import Engine

logger = logging.getLogger("faultmaven.slack.pending_binds")

#: How long an admin has to finish the FaultMaven leg. Generous enough to cover
#: signing in to the dashboard first (the authorize page bounces an anonymous
#: visitor through login and back), tight enough that an abandoned record is not
#: a standing invitation.
PENDING_BIND_TTL_SECONDS = 15 * 60

#: Name of the cookie carrying the record id. The ``__Host-`` prefix is not
#: decoration: it forbids a ``Domain`` attribute, so a sibling host under
#: ``faultmaven.ai`` cannot set (or overwrite) this cookie for us. Cookie
#: tossing from a neighbouring subdomain is exactly how an attacker would try to
#: supply the second half of the pair they are missing.
BIND_COOKIE_NAME = "__Host-fm_bind"


@dataclass(frozen=True, slots=True)
class PendingBind:
    """One install waiting for its admin to authorize the bind."""

    bind_id: str
    state: str
    code_verifier: str
    team_id: str
    enterprise_id: str
    installer_user_id: str
    team_name: str


class PendingBindStore:
    """Short-lived records joining a Slack install to a FaultMaven consent."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._metadata = MetaData()
        self._table = Table(
            "fm_pending_binds",
            self._metadata,
            Column("bind_id", String(64), primary_key=True),
            # Indexed separately from the id: the callback arrives knowing the
            # state (from the URL) and the id (from the cookie), and must check
            # that they name the SAME record.
            Column("state", String(64), nullable=False, unique=True),
            Column("code_verifier", String(128), nullable=False),
            Column("team_id", String(32), nullable=False),
            Column("enterprise_id", String(32), nullable=False, server_default=""),
            Column("installer_user_id", String(32), nullable=False),
            Column("team_name", String(200), nullable=False),
            Column("expires_at", DateTime, nullable=False),
            Column("consumed", Boolean, nullable=False, server_default="0"),
        )
        self._metadata.create_all(engine)

    def create(
        self,
        *,
        team_id: str,
        enterprise_id: str,
        installer_user_id: str,
        team_name: str,
    ) -> PendingBind:
        """Open a pending bind and return it, secrets included.

        ``bind_id`` and ``state`` are independent 256-bit values: one travels in
        the cookie, one in the URL, and the callback requires both to name this
        row. Deriving one from the other would collapse them into a single
        secret and give the URL alone the authority the pair is meant to split.
        """

        record = PendingBind(
            bind_id=secrets.token_urlsafe(32),
            state=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(64),
            team_id=team_id,
            enterprise_id=enterprise_id or "",
            installer_user_id=installer_user_id,
            team_name=team_name,
        )
        with self._engine.begin() as conn:
            conn.execute(
                self._table.insert().values(
                    bind_id=record.bind_id,
                    state=record.state,
                    code_verifier=record.code_verifier,
                    team_id=record.team_id,
                    enterprise_id=record.enterprise_id,
                    installer_user_id=record.installer_user_id,
                    team_name=record.team_name,
                    expires_at=datetime.now(timezone.utc)
                    + timedelta(seconds=PENDING_BIND_TTL_SECONDS),
                    consumed=False,
                )
            )
        return record

    def consume(self, *, state: str, bind_id: str) -> PendingBind | None:
        """Claim the record named by BOTH halves, or return None.

        Atomic: the UPDATE that flips ``consumed`` is the claim, so two requests
        racing the same record cannot both proceed. Returns None for every
        failure — unknown state, mismatched cookie, expired, already used — and
        deliberately does not distinguish them to the caller, because the caller
        renders a message to whoever is holding the browser and the difference
        between "wrong cookie" and "no such state" is information only an
        attacker is probing for.
        """

        if not state or not bind_id:
            return None
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            row = conn.execute(
                select(self._table).where(
                    self._table.c.state == state,
                    self._table.c.bind_id == bind_id,
                    self._table.c.consumed.is_(False),
                )
            ).first()
            if row is None:
                return None
            expires_at = row.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                return None
            claimed = conn.execute(
                self._table.update()
                .where(
                    self._table.c.bind_id == row.bind_id,
                    self._table.c.consumed.is_(False),
                )
                .values(consumed=True)
            )
            if claimed.rowcount != 1:
                # Another request claimed it between the read and the update.
                return None
        return PendingBind(
            bind_id=row.bind_id,
            state=row.state,
            code_verifier=row.code_verifier,
            team_id=row.team_id,
            enterprise_id=row.enterprise_id,
            installer_user_id=row.installer_user_id,
            team_name=row.team_name,
        )

    def purge_expired(self) -> int:
        """Delete records past their TTL. Consumed rows go too — they are spent.

        Each row holds a live PKCE verifier and a state secret, so leaving spent
        ones behind means retaining OAuth secrets indefinitely in a database
        shared with the installation store. Called at startup; a process that
        runs for months is the case this exists for.
        """

        with self._engine.begin() as conn:
            result = conn.execute(
                self._table.delete().where(
                    self._table.c.expires_at <= datetime.now(timezone.utc)
                )
            )
        removed = result.rowcount or 0
        if removed:
            logger.info("Purged %d expired pending bind(s)", removed)
        return removed

    def close(self) -> None:
        """No-op: the engine belongs to :mod:`oauth_store`, which outlives this."""
