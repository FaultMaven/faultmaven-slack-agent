"""Binding a Slack workspace to a FaultMaven team at install (ADR-017 §D6).

The workspace is admitted to the **installer's enterprise** — the isolation
boundary — and becomes a team inside it. No organization is a precondition:
the organization answers "who pays for this account?", and it decides nothing
about who may see a case.

The install flow, and why it is shaped this way:

1. **Slack install completes.** Bolt hands us the workspace and the installer.
   We open a :class:`pending_binds.PendingBind`, set its id in a ``__Host-``
   cookie, and show the installer a page naming the workspace they are about to
   admit into a FaultMaven enterprise.
2. **The admin authorizes on the dashboard.** They are sent to FaultMaven's
   consent screen with PKCE and the record's ``state``.
3. **They land back here.** We require the ``state`` from the URL and the id
   from the cookie to name the *same* live record, exchange the code, bind, keep
   the service account's refresh token, and revoke the admin's tokens.

**Why the confirmation happens before the FaultMaven leg, not after.** The
dashboard's consent screen cannot describe this grant: it renders the client
name and a *caller-supplied* scope string, so nothing on it is evidence about
which workspace is involved. Something has to tell a human "you are admitting
workspace X", and the only place that can is here. Doing it *before* also means
the admin's bearer never has to survive across a request — we obtain it, use it
once, and revoke it inside a single handler.

**What the admin's token is, and is not.** It carries their full FaultMaven
authority, and the server decides from its own claim which enterprise the
workspace joins. It is never written down: not to the credential store, not to
a cookie, not to a log. What is persisted is only the workspace service
account's own refresh token, which is scoped to that account.

**Both sides of the join must consent.** The FaultMaven side is covered by the
admin signing in and authorizing. The *Slack* side is not: without a check, a
FaultMaven user who is an ordinary member of a workspace could admit that
workspace's investigations into their enterprise on their own, with nobody who
administers the workspace involved. :func:`installer_authority` closes that —
the flow is offered only to a Workspace Owner or Admin.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from enum import Enum
from urllib.parse import urlencode

from pydantic import SecretStr
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from faultmaven import BindFailureReason, FaultMavenClient, WorkspaceBindError
from faultmaven.client import _jwt_claims
from pending_binds import PendingBind

logger = logging.getLogger("faultmaven.slack.binding")

#: Sent to the consent screen, which renders it to the admin. Nothing enforces
#: OAuth scope in FaultMaven today — the minted token's scopes are a fixed list
#: — so this is descriptive text, not a constraint. Written honestly anyway: it
#: is the one sentence the dashboard shows, and a client that lies here is
#: lying to the person deciding.
BIND_SCOPE = (
    "openid profile email "
    "slack:bind-workspace(create-service-account,create-team,add-members)"
)


def code_challenge_for(verifier: str) -> str:
    """The S256 PKCE challenge for a verifier (base64url, unpadded)."""

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorize_url(
    *, dashboard_url: str, client_id: str, redirect_uri: str, record: PendingBind
) -> str:
    """The dashboard consent URL for this pending bind.

    Built entirely from configuration and the stored record — never from request
    input. A ``redirect_uri`` taken from the request would be the whole attack
    the redirect allowlist exists to stop, re-introduced one layer up.
    """

    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": record.state,
            "code_challenge": code_challenge_for(record.code_verifier),
            "code_challenge_method": "S256",
            "scope": BIND_SCOPE,
        }
    )
    return f"{dashboard_url.rstrip('/')}/auth/authorize?{query}"


#: How long a bind assertion is valid: it is sent the moment it is signed.
BIND_ASSERTION_LIFETIME_SECONDS = 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def sign_bind_assertion(
    *,
    key: SecretStr,
    subject: str,
    slack_team_id: str,
    slack_enterprise_id: str,
    now: int | None = None,
    jti: str | None = None,
) -> str:
    """The assertion the Cloud API requires to bind a workspace
    (faultmaven-cloud#83): a compact JWS, HS256, saying this agent completed the
    Slack install of ``slack_team_id`` and the FaultMaven sign-in of
    ``subject``. Byte-identical to what PyJWT signs for the same claims, which a
    known-answer test pins."""

    issued = int(time.time()) if now is None else now
    claims = {
        "iss": "faultmaven-slack-agent",
        "aud": "faultmaven-cloud:slack-workspace-bind",
        "sub": subject,
        "slack_team_id": slack_team_id,
        "slack_enterprise_id": slack_enterprise_id,
        "iat": issued,
        "exp": issued + BIND_ASSERTION_LIFETIME_SECONDS,
        "jti": secrets.token_urlsafe(16) if jti is None else jti,
    }
    header = _b64url(b'{"alg":"HS256","typ":"JWT"}')
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    digest = hmac.new(key.get_secret_value().encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(digest)}"


def complete_bind(
    *,
    fm: FaultMavenClient,
    workspace_credentials,
    record: PendingBind,
    code: str,
    redirect_uri: str,
    assertion_key: SecretStr,
) -> str:
    """Exchange the code, bind the workspace, persist the credential.

    Returns the FaultMaven enterprise id the workspace was bound to.

    The bind carries an assertion signed with ``assertion_key`` for the admin's
    FaultMaven user id (the access token's ``sub``) and the record's workspace.
    Without a key nothing is exchanged: the Cloud API would refuse the bind.

    The admin's tokens are revoked in a ``finally``, so they do not outlive this
    call on any path — including the one where the bind is refused, which is the
    path most likely to be reached (an admin whose account may not connect a
    workspace consents successfully and is refused here).

    A missing access token is refused here, inside the ``try``, not in the
    exchange, so a refresh token issued beside it is revoked too. A refused
    exchange hands back the tokens its body carried (``exc.issued``), so a
    status other than 200 that issued them is revoked as well (faultmaven#2591).
    The residual, not revoked because no token reaches this frame: a body that
    is not JSON (a proxy page), and JSON that is not an object, or whose token
    is not a string.
    """

    if not assertion_key.get_secret_value():
        raise WorkspaceBindError(
            "SLACK_BIND_ASSERTION_KEY is not set, so the bind cannot be signed",
            reason=BindFailureReason.NOT_SET_UP,
        )
    access = refresh = ""
    try:
        try:
            access, refresh = fm.exchange_authorization_code(
                code=code, code_verifier=record.code_verifier, redirect_uri=redirect_uri
            )
        except WorkspaceBindError as exc:
            # Cleared once read: the exception travels on to a log, the tokens
            # only to the revocation below.
            access, refresh = exc.issued
            exc.issued = ("", "")
            raise
        if not access:
            raise WorkspaceBindError(
                "code exchange returned no access_token",
                reason=BindFailureReason.OTHER_REFUSAL,
            )
        subject = (_jwt_claims(access) or {}).get("sub")
        if not isinstance(subject, str) or not subject:
            raise WorkspaceBindError(
                "the admin access token carries no readable sub claim",
                reason=BindFailureReason.OTHER_REFUSAL,
            )
        binding = fm.bind_workspace(
            admin_access_token=access,
            slack_team_id=record.team_id,
            team_name=record.team_name,
            slack_enterprise_id=record.enterprise_id or None,
            assertion=sign_bind_assertion(
                key=assertion_key,
                subject=subject,
                slack_team_id=record.team_id,
                slack_enterprise_id=record.enterprise_id or "",
            ),
        )
        # Keyed on the RECORD's workspace id — the one the completed Slack
        # install established, and the one Slack events will present. Storing a
        # server-echoed value instead would risk a row no lookup ever finds.
        try:
            workspace_credentials.bind(
                team_id=record.team_id,
                # The FaultMaven tenant, from the issued credential's own
                # claim. ``enterprise_id`` beside it is Slack's Enterprise Grid
                # id, from the install — two different enterprises, and the
                # only place in this repository they meet.
                fm_enterprise_id=binding.fm_enterprise_id,
                refresh_token=binding.refresh_token,
                enterprise_id=record.enterprise_id or "",
                faultmaven_team_id=binding.team_id,
            )
        except Exception as exc:
            # The server-side bind already succeeded and its credential is
            # issued once, so it is lost. A re-run of the install, once the
            # store works again, issues a fresh one. Say so, loudly, instead of
            # letting the caller tell an admin "nothing was changed".
            logger.error(
                "Workspace %s was bound in FaultMaven (enterprise %s, team %s) "
                "but its credential could not be stored locally (%s). The "
                "credential is issued once and is now lost; re-run the install "
                "once the store works, or re-issue it for service account %s.",
                record.team_id,
                binding.fm_enterprise_id,
                binding.team_id,
                # The class only: a database error's text quotes its bound
                # parameters, the credential among them.
                type(exc).__name__,
                binding.service_account_username,
            )
            raise WorkspaceBindError(
                "the bound workspace's credential could not be stored: "
                f"{type(exc).__name__}",
                reason=BindFailureReason.CREDENTIAL_NOT_STORED,
            ) from exc

        # The client caches a credential per workspace, and a re-bind replaces
        # the row underneath it. Without this, turns keep authenticating as the
        # PREVIOUS service account — filing cases in the previous enterprise —
        # until that token happens to be rejected.
        fm.forget_workspace(record.team_id)
        logger.info(
            "Bound Slack workspace %s to FaultMaven enterprise %s (team %s, "
            "account %s, account_created=%s team_created=%s)",
            binding.slack_team_id,
            binding.fm_enterprise_id,
            binding.team_name,
            binding.service_account_username,
            binding.account_created,
            binding.team_created,
        )
        return binding.fm_enterprise_id
    finally:
        # A live refresh token is a standing org-admin credential, so its
        # revocation failing is worth an operator's attention; an access token
        # expires on its own in minutes. Each revoke is guarded on its own.
        if not _revoke(fm, refresh, hint="refresh_token"):
            logger.error(
                "Could not revoke the admin refresh token issued for the bind of "
                "workspace %s. It remains valid until it expires — revoke it "
                "from FaultMaven if this matters.",
                record.team_id,
            )
        if not _revoke(fm, access, hint="access_token"):
            logger.warning(
                "Could not revoke the admin access token for workspace %s "
                "(expires on its own shortly)",
                record.team_id,
            )


def _revoke(fm: FaultMavenClient, token: str, *, hint: str) -> bool:
    """``fm.revoke_token``, with a raise read as a failure to revoke.

    Guarded as ``listeners/lifecycle.py`` guards the same call. A token httpx
    cannot encode (a lone surrogate) raises outside ``httpx.HTTPError``, and
    that must neither skip ``complete_bind``'s other revoke nor replace its
    result or exception. Only the exception's type is logged: an encode error's
    message quotes a character of the token.
    """

    try:
        return fm.revoke_token(token, token_type_hint=hint)
    except Exception as exc:  # noqa: BLE001 — the other revoke must still run
        logger.warning("Revoking the admin %s raised %s", hint, type(exc).__name__)
        return False


def bind_failure_message(exc: Exception) -> tuple[str, str]:
    """The ``(message, footer)`` the admin's page shows when a bind fails.

    Both come from the failure's declared reason, never from its status or its
    string, which may carry library or backend text and stays in the log. An
    exception that is not a bind failure has no declared stage, so its footer
    claims nothing about what FaultMaven did.
    """

    reason = (
        exc.reason
        if isinstance(exc, WorkspaceBindError)
        else BindFailureReason.UNEXPLAINED
    )
    return reason.message, reason.footer


class InstallerAuthority(Enum):
    """Whether the person who installed the app may bind this workspace."""

    #: A Workspace Owner or Admin — the bind may be offered.
    ADMIN = "admin"
    #: Slack answered, and this person administers nothing.
    NOT_ADMIN = "not_admin"
    #: Slack did not answer (missing scope, rate limit, network, deleted user).
    #: Distinct from NOT_ADMIN because it is a different thing to tell someone
    #: and a different thing to fix.
    UNKNOWN = "unknown"


def installer_authority(client: WebClient, user_id: str) -> InstallerAuthority:
    """Whether ``user_id`` administers the workspace they just installed into.

    Reads ``users.info`` (needs the ``users:read`` bot scope, granted by the
    same install whose token is used here — so an app whose Slack configuration
    predates that scope returns :data:`~InstallerAuthority.UNKNOWN` until the
    manifest is pushed and the app reinstalled).

    ``is_primary_owner`` and ``is_owner`` are checked alongside ``is_admin``
    rather than assumed to imply it: they are independent booleans in the
    payload, and an Owner who somehow lacks the admin flag is unambiguously
    entitled to this decision. The same three are read again under
    ``enterprise_user``, where Slack's Enterprise Grid reports **Grid
    organization** authority — a Grid Org Owner is commonly a plain member of
    the workspace they are installing into.

    Never raises. Every failure is :data:`~InstallerAuthority.UNKNOWN`, which
    the caller refuses on — the safe direction, since the alternative is
    admitting a workspace on an authority nobody established.
    """

    if not user_id:
        # Slack gave us an installation with no installer (an org-wide Grid
        # install is the case that produces it). There is no one to check.
        return InstallerAuthority.UNKNOWN
    try:
        response = client.users_info(user=user_id)
    except SlackApiError as exc:
        logger.warning(
            "Could not check whether installer %s administers this workspace: "
            "users.info returned %s. The bind will not be offered.",
            user_id,
            exc.response.get("error"),
        )
        return InstallerAuthority.UNKNOWN
    except Exception as exc:  # noqa: BLE001 — network, DNS, a broken client
        logger.warning(
            "Could not reach Slack to check installer %s: %s. The bind will "
            "not be offered.",
            user_id,
            exc,
        )
        return InstallerAuthority.UNKNOWN

    user = response.get("user") or {}
    # Two levels, because Grid puts them in two places. The top-level flags are
    # workspace authority; ``enterprise_user`` carries Slack GRID ORGANIZATION
    # authority, and a Grid Org Owner is routinely an ordinary member of any
    # given workspace. Reading only the top level tells the highest authority in
    # the Grid to go and find an admin.
    for scope in (user, user.get("enterprise_user") or {}):
        if any(
            scope.get(flag)
            for flag in ("is_admin", "is_owner", "is_primary_owner")
        ):
            return InstallerAuthority.ADMIN
    return InstallerAuthority.NOT_ADMIN
