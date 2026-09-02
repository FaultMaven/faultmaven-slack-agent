"""Binding a Slack workspace to a FaultMaven Team at install (ADR-013 §D3).

The install flow, and why it is shaped this way:

1. **Slack install completes.** Bolt hands us the workspace and the installer.
   We open a :class:`pending_binds.PendingBind`, set its id in a ``__Host-``
   cookie, and show the installer a page naming the workspace they are about to
   admit into a FaultMaven organization.
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

**What the admin's token is, and is not.** It carries their full organization
authority (the bind needs both ``ORG_MANAGE_USERS`` and ``ORG_MANAGE_SETTINGS``).
It is never written down: not to the credential store, not to a cookie, not to a
log. What is persisted is only the workspace service account's own refresh
token, which is scoped to that account.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from urllib.parse import urlencode

from faultmaven import FaultMavenClient, WorkspaceBindError
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


def complete_bind(
    *,
    fm: FaultMavenClient,
    workspace_credentials,
    record: PendingBind,
    code: str,
    redirect_uri: str,
) -> str:
    """Exchange the code, bind the workspace, persist the credential.

    Returns the organization id the workspace was bound to.

    The admin's tokens are revoked in a ``finally``, so they do not outlive this
    call on any path — including the one where the bind is refused, which is the
    path most likely to be reached (an admin without ``ORG_MANAGE_SETTINGS``
    consents successfully and is refused here).
    """

    access, refresh = fm.exchange_authorization_code(
        code=code, code_verifier=record.code_verifier, redirect_uri=redirect_uri
    )
    try:
        binding = fm.bind_workspace(
            admin_access_token=access,
            slack_team_id=record.team_id,
            team_name=record.team_name,
            slack_enterprise_id=record.enterprise_id or None,
        )
        # Keyed on the RECORD's workspace id — the one the completed Slack
        # install established, and the one Slack events will present. Storing a
        # server-echoed value instead would risk a row no lookup ever finds.
        try:
            workspace_credentials.bind(
                team_id=record.team_id,
                organization_id=binding.organization_id,
                refresh_token=binding.refresh_token,
                enterprise_id=record.enterprise_id or "",
                faultmaven_team_id=binding.team_id,
            )
        except Exception as exc:
            # The server-side bind already succeeded and its credential is
            # issued once, so this is not a retryable failure: the next attempt
            # is refused as already-bound. Say exactly that, loudly, instead of
            # letting the caller tell an admin "nothing was changed".
            logger.error(
                "Workspace %s was bound in FaultMaven (organization %s, team %s) "
                "but its credential could not be stored locally: %s. The "
                "credential is issued once and is now lost; re-issue it for "
                "service account %s.",
                record.team_id,
                binding.organization_id,
                binding.team_id,
                exc,
                binding.service_account_username,
            )
            raise WorkspaceBindError(
                "This workspace was connected in FaultMaven, but the agent could "
                "not store its credential — so it cannot be used yet, and "
                "re-installing will not fix it. Ask a FaultMaven administrator "
                "to re-issue the workspace credential.",
            ) from exc

        # The client caches a credential per workspace, and a re-bind replaces
        # the row underneath it. Without this, turns keep authenticating as the
        # PREVIOUS service account — filing cases in the previous organization —
        # until that token happens to be rejected.
        fm.forget_workspace(record.team_id)
        logger.info(
            "Bound Slack workspace %s to organization %s (team %s, account %s, "
            "account_created=%s team_created=%s)",
            binding.slack_team_id,
            binding.organization_id,
            binding.team_name,
            binding.service_account_username,
            binding.account_created,
            binding.team_created,
        )
        return binding.organization_id
    finally:
        # A live refresh token is a standing org-admin credential, so its
        # revocation failing is worth an operator's attention; an access token
        # expires on its own in minutes.
        if not fm.revoke_token(refresh, token_type_hint="refresh_token"):
            logger.error(
                "Could not revoke the admin refresh token issued for the bind of "
                "workspace %s. It remains valid until it expires — revoke it "
                "from FaultMaven if this matters.",
                record.team_id,
            )
        if not fm.revoke_token(access, token_type_hint="access_token"):
            logger.warning(
                "Could not revoke the admin access token for workspace %s "
                "(expires on its own shortly)",
                record.team_id,
            )


#: Bind failures whose message is written for a browser. Every other
#: ``WorkspaceBindError`` embeds up to 300 characters of the backend's raw
#: response — useful in a log, wrong on a page, and capable of naming another
#: tenant's organization on the 409.
_BROWSER_SAFE_BIND_STATUSES = frozenset({403, 503})


def bind_failure_message(exc: Exception) -> str:
    """What to show the admin in the browser when a bind fails.

    Deliberately says what to do next rather than what went wrong internally:
    the reader is an administrator in a browser, not an operator with logs. The
    detail is not lost — the caller logs the exception itself.
    """

    if isinstance(exc, WorkspaceBindError):
        if exc.status_code in _BROWSER_SAFE_BIND_STATUSES or exc.status_code == 0:
            return str(exc)
        if exc.status_code == 409:
            return (
                "This Slack workspace is already connected to a FaultMaven "
                "organization. If that is not the one you expect, a FaultMaven "
                "administrator has to disconnect it first."
            )
        return (
            "FaultMaven could not connect this workspace. Ask a FaultMaven "
            "administrator to check the server logs for the reason."
        )
    return (
        "Something went wrong finishing the connection to FaultMaven. "
        "Re-install the app to try again."
    )
