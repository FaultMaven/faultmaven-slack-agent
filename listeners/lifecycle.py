"""Install lifecycle — forget a workspace when it removes the app.

Two Slack events say the app can no longer act for a workspace, and both must
tear down *two* separate pieces of per-install state:

* the **Slack installation** (bot token, user tokens) in Bolt's
  ``InstallationStore``, and
* the **FaultMaven binding** — the workspace's service-account credential in
  :mod:`workspace_credentials`, plus the copy the client is holding in memory.

Bolt ships the first half and does not enable it: ``enable_token_revocation_listeners``
is opt-in, so before this module an uninstalled workspace left a live bot token
in the database as well as a live FaultMaven credential. The library half is
*called* here rather than reimplemented, so ``delete_installation`` /
``delete_bot`` / ``delete_all`` stay whatever the SDK says they are.

**Called, not co-registered.** ``App.dispatch`` returns after the first matching
listener produces a response, and an event listener always produces one — so
registering Bolt's built-in alongside ours means whichever went on first is the
only one that ever runs. ``enable_token_revocation_listeners`` is therefore not
used; one listener per event runs both halves in a chosen order.

**``tokens_revoked`` is not an uninstall.** Its payload splits into
``tokens.oauth`` (individual users' tokens) and ``tokens.bot`` (the bot's). A
user revoking their own token leaves the app installed and the bot working — so
unbinding on any ``tokens_revoked`` would destroy a live workspace's FaultMaven
binding because one person disconnected their account. The credential is issued
once per bind, so that is not a self-healing mistake: it takes an organization
admin re-running the install to recover. Only a **bot**-token revocation is
treated as the app being gone.

**Neither half can strand the other**: each is guarded separately, so a failing
installation teardown still removes the binding and vice versa. The order —
binding first, then installation — is therefore not load-bearing today, and is
pinned by a test anyway so that if a guard is ever dropped the failure falls on
the safer side. The FaultMaven credential is the more dangerous leftover: a
standing service-account credential inside a customer's organization, where the
bot token Slack has already revoked is inert.

**Within the binding: read, unbind, revoke, forget.** The record is read first
because revoking needs the token the delete is about to remove. Deleting the row
makes the credential unreachable here; **revoking** makes it unusable anywhere,
which is what actually retires a standing service-account credential.

The store row goes before the in-memory copy so a turn racing this teardown is
more likely to re-read the store and find nothing. It does not *close* that race
— ``FaultMavenClient._credential_for`` reads the store outside its cache lock,
so a turn that read the row just before the delete can still cache it just
after. The revocation is what makes that harmless: the cached copy no longer
authenticates.

**What survives, deliberately.** The workspace's *cases* are untouched: they
belong to the service account inside the customer's organization, not to the
Slack installation. A reinstall re-binds to the same derived account
(``slack-<team_id>``, find-or-create) in the same organization, so the history
is still there and still owned correctly.

**Other replicas.** Slack delivers the event to one replica. Another replica
holding the workspace cached finds out at its next rotation: ``put_refresh_token``
is UPDATE-only, so the missing row makes ``FaultMavenClient`` discard the cached
credential and refuse the workspace rather than keep renewing it (an earlier
version of this note claimed that already happened — it did not; the failed
write was swallowed and the renewal succeeded, so the replica served the
uninstalled workspace until it restarted). The revocation above closes the
window before that rotation is due.
"""

from __future__ import annotations

from logging import Logger

from slack_bolt import App, BoltContext

from faultmaven import FaultMavenClient
from workspace_credentials import WorkspaceCredentialStore


def _forget_binding(
    fm: FaultMavenClient,
    workspace_credentials: WorkspaceCredentialStore,
    context: BoltContext,
    logger: Logger,
    *,
    reason: str,
) -> None:
    """Drop a workspace's FaultMaven binding, durably then in memory.

    Never raises: this runs on an event Slack will not redeliver usefully, and
    a teardown that fails half-way is worth a log line, not a 500 that leaves
    the *installation* half undone too.
    """

    team_id = context.team_id
    if not team_id:
        # An Enterprise Grid org-wide install carries no team_id, and the
        # binding is keyed on the workspace alone — so there is nothing to look
        # up. Say so rather than no-op silently; Grid is out of scope until the
        # binding is keyed on (enterprise_id, team_id) as well.
        logger.warning(
            "%s carried no team_id (enterprise_id=%s), so no FaultMaven "
            "binding could be resolved. If this is an Enterprise Grid org-wide "
            "install, remove the binding by hand.",
            reason,
            context.enterprise_id,
        )
        return

    # Read before the delete: revoking needs the token, and after the DELETE
    # there is nowhere left to get it from.
    record = None
    try:
        record = workspace_credentials.get(team_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not read the FaultMaven binding for workspace %s before "
            "tearing it down after %s; it will be deleted without revoking its "
            "credential, which stays valid until it expires",
            team_id,
            reason,
        )

    try:
        workspace_credentials.unbind(team_id)
    except Exception:  # noqa: BLE001 — a teardown must not fail the event
        logger.exception(
            "Could not remove the FaultMaven binding for workspace %s after "
            "%s; it may still hold a live service-account credential",
            team_id,
            reason,
        )
        return

    # Deleting the row makes the credential unreachable *here*; revoking makes
    # it unusable *anywhere*. Without this the service-account refresh token
    # stays valid inside the customer's organization for its full lifetime —
    # usable by a replica still holding it, by a database backup, or by anything
    # that ever logged it. `binding.complete_bind` already revokes the admin's
    # tokens through the same call for the same reason.
    if record is not None and record.refresh_token:
        try:
            revoked = fm.revoke_token(
                record.refresh_token, token_type_hint="refresh_token"
            )
        except Exception:  # noqa: BLE001 — the local teardown must still finish
            revoked = False
            logger.exception(
                "Revoking the FaultMaven credential for workspace %s raised",
                team_id,
            )
        if not revoked:
            logger.error(
                "Could not revoke the FaultMaven service-account credential for "
                "workspace %s after %s. Its local binding is gone, but the token "
                "remains valid until it expires — revoke it from FaultMaven if "
                "that matters.",
                team_id,
                reason,
            )

    # Last, and guarded like the rest: a raise here would skip the caller's
    # installation teardown and leave a stale bot token behind.
    try:
        fm.forget_workspace(team_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not drop the cached credential for workspace %s; this "
            "replica may keep serving it until its next renewal, which now "
            "fails and discards it",
            team_id,
        )
        return

    logger.info(
        "Removed the FaultMaven binding for Slack workspace %s after %s "
        "(its cases are unaffected)",
        team_id,
        reason,
    )


def register_lifecycle(
    app: App,
    fm: FaultMavenClient,
    workspace_credentials: WorkspaceCredentialStore,
) -> None:
    """Wire uninstall/revocation cleanup. HTTP transport only.

    Requires an ``installation_store`` — the SDK's built-in accessors raise
    without one — which is exactly the multi-workspace OAuth deployment that has
    bindings to tear down. Socket mode has one static token and no
    ``WorkspaceCredentialStore``, so the caller does not register this.
    """

    # Resolved once, at registration: the accessors raise BoltError when no
    # installation store is wired, and that belongs at wiring time rather than
    # inside an event three weeks later.
    sdk_uninstalled = app.default_app_uninstalled_event_listener()
    sdk_revoked = app.default_tokens_revoked_event_listener()

    def _sdk(handler, logger: Logger, reason: str, **kwargs) -> None:
        """Run the SDK's installation teardown, surviving its failure."""

        try:
            handler(**kwargs)
        except Exception:  # noqa: BLE001 — the binding half must still run
            logger.exception(
                "Could not clear the Slack installation after %s; a stale bot "
                "token may survive in the installation store",
                reason,
            )

    @app.event("app_uninstalled")
    def forget_uninstalled_workspace(context: BoltContext, logger: Logger) -> None:
        _forget_binding(
            fm, workspace_credentials, context, logger, reason="app_uninstalled"
        )
        _sdk(sdk_uninstalled, logger, "app_uninstalled", context=context)

    @app.event("tokens_revoked")
    def forget_revoked_workspace(
        event: dict, context: BoltContext, logger: Logger
    ) -> None:
        # Bot tokens only. A `tokens.oauth`-only payload is one user
        # disconnecting their account, which leaves the app installed — the SDK
        # half still drops that user's row, and the binding must stay.
        if event.get("tokens", {}).get("bot"):
            _forget_binding(
                fm,
                workspace_credentials,
                context,
                logger,
                reason="tokens_revoked (bot token)",
            )
        _sdk(sdk_revoked, logger, "tokens_revoked", event=event, context=context)
