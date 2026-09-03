"""Uninstall / token-revocation cleanup, driven through real Bolt dispatch.

These go through ``App.dispatch`` rather than calling the handlers directly, and
that is the point. ``dispatch`` returns after the **first** matching listener
produces a response, and an event listener always produces one — so a version
that registered Bolt's built-in revocation listeners alongside ours looked
correct, logged nothing, and silently never ran the FaultMaven half. Only a test
that dispatches a real payload through a real ``App`` can see that; a test that
calls the handler proves the handler works and nothing about whether it runs.

The behaviour with the sharpest edge is the ``tokens_revoked`` split: a payload
carrying only ``tokens.oauth`` is one person disconnecting their own account, and
the app is still installed. Unbinding there would destroy a live workspace's
FaultMaven credential — which is issued once per bind, so recovering means an
organization admin re-running the whole install.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from slack_bolt import App
from slack_bolt.request import BoltRequest
from slack_sdk.oauth.installation_store import InstallationStore

from listeners import register_listeners
from listeners.lifecycle import register_lifecycle

TEAM = "T0B9XNZDR44"


class _RecordingExecutor:
    """Bolt's listener executor, remembering what it was asked to run.

    Bolt hands the listener to a pool and returns, so a test asserting straight
    after ``dispatch`` races it. The futures are the only exact handle on "that
    listener has finished" — the pool is shared and five wide, so neither its
    idleness nor a barrier task answers the question.
    """

    def __init__(self, inner):
        self._inner = inner
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        future = self._inner.submit(fn, *args, **kwargs)
        self.submitted.append(future)
        return future

    def __getattr__(self, name):
        return getattr(self._inner, name)



@pytest.fixture
def wiring():
    """A real Bolt app with the lifecycle listeners wired to doubles."""

    installation_store = MagicMock(spec=InstallationStore)
    app = App(
        signing_secret="s",
        installation_store=installation_store,
        request_verification_enabled=False,
        token_verification_enabled=False,
        ssl_check_enabled=False,
        url_verification_enabled=False,
    )
    app.listener_runner.listener_executor = _RecordingExecutor(
        app.listener_runner.listener_executor
    )
    fm = MagicMock()
    fm.revoke_token.return_value = True
    credentials = MagicMock()
    credentials.get.return_value = SimpleNamespace(
        team_id=TEAM, refresh_token="rt-live", organization_id="org-1"
    )
    register_lifecycle(app, fm, credentials)
    return app, fm, credentials, installation_store


def _dispatch(app: App, event: dict, *, team_id: str | None = TEAM, **top):
    """Dispatch one event and wait for the listener to actually finish.

    Bolt's default ``process_before_response=False`` acks immediately and runs
    the listener on a thread pool, so ``dispatch`` returns before any of this
    work has happened — asserting straight after it is a race that passes on the
    fast paths and fails on the slow ones. Draining the executor keeps the
    production code path (the app is built exactly as ``app.py`` builds it)
    while making the assertions deterministic.
    """

    body = {
        "token": "z",
        "api_app_id": "A1",
        "type": "event_callback",
        "event_id": "Ev1",
        "event_time": 1,
        "event": event,
        **top,
    }
    if team_id is not None:
        body["team_id"] = team_id
    response = app.dispatch(
        BoltRequest(
            body=json.dumps(body), headers={"content-type": ["application/json"]}
        )
    )
    _drain(app)
    return response


def _drain(app: App) -> None:
    """Block until the dispatched listener has actually finished.

    Waits on the futures the fixture recorded rather than on a barrier task:
    Bolt's pool has five workers, so a barrier submitted after the listener can
    be picked up by a *different* thread and return while the listener is still
    running — a drain that usually works, which is the worst kind.

    ``executor.shutdown()`` would be exact but permanent: the pool belongs to
    the App the fixture built, so a second ``_dispatch`` against the same
    fixture would die with "cannot schedule new futures after shutdown" — an
    error about the harness, raised by a test about the listener.
    """

    for future in list(app.listener_runner.listener_executor.submitted):
        future.result(timeout=10)


# -- app_uninstalled ---------------------------------------------------------


def test_uninstall_removes_the_binding_and_the_installation(wiring):
    """The positive path: both halves run for one event."""
    app, fm, credentials, installation_store = wiring

    assert _dispatch(app, {"type": "app_uninstalled"}).status == 200

    credentials.unbind.assert_called_once_with(TEAM)
    fm.forget_workspace.assert_called_once_with(TEAM)
    installation_store.delete_all.assert_called_once_with(
        enterprise_id=None, team_id=TEAM
    )


def test_uninstall_drops_the_stored_row_before_the_cached_copy(wiring):
    """Forgetting first would let a racing turn re-read the store and re-cache."""
    app, fm, credentials, _ = wiring
    order: list[str] = []
    credentials.unbind.side_effect = lambda _t: order.append("unbind")
    fm.forget_workspace.side_effect = lambda _t: order.append("forget")

    _dispatch(app, {"type": "app_uninstalled"})

    assert order == ["unbind", "forget"]


def test_uninstall_still_clears_the_installation_when_unbinding_fails(wiring):
    """A teardown that fails half-way must not strand the other half."""
    app, fm, credentials, installation_store = wiring
    credentials.unbind.side_effect = RuntimeError("database is gone")

    assert _dispatch(app, {"type": "app_uninstalled"}).status == 200

    fm.forget_workspace.assert_not_called()  # the row may survive; keep serving it
    installation_store.delete_all.assert_called_once()


def test_uninstall_still_removes_the_binding_when_the_sdk_half_fails(wiring):
    """Each half is guarded separately, so neither can strand the other."""
    app, fm, credentials, installation_store = wiring
    installation_store.delete_all.side_effect = RuntimeError("nope")

    assert _dispatch(app, {"type": "app_uninstalled"}).status == 200

    credentials.unbind.assert_called_once_with(TEAM)
    fm.forget_workspace.assert_called_once_with(TEAM)


def test_uninstall_tears_the_binding_down_before_the_installation(wiring):
    """Pinned so the order stays the safer one if a guard is ever dropped.

    With both halves guarded the order is not observable through their results,
    which is exactly why it needs asserting directly: a future edit that removes
    one guard should fail here rather than silently start leaving a live
    FaultMaven credential behind whenever the SDK half throws. The credential is
    the more dangerous leftover — a standing service-account credential inside a
    customer's organization, where the bot token Slack has already revoked is
    inert.
    """
    app, fm, credentials, installation_store = wiring
    order: list[str] = []
    credentials.unbind.side_effect = lambda _t: order.append("binding")
    installation_store.delete_all.side_effect = lambda **_k: order.append(
        "installation"
    )

    _dispatch(app, {"type": "app_uninstalled"})

    assert order == ["binding", "installation"]


def test_uninstall_without_a_team_id_touches_no_binding(wiring):
    """An Enterprise Grid org-wide install carries no team_id, and the binding is
    keyed on the workspace alone — so there is nothing to resolve."""
    app, fm, credentials, _ = wiring

    assert (
        _dispatch(
            app, {"type": "app_uninstalled"}, team_id=None, enterprise_id="E1"
        ).status
        == 200
    )

    credentials.unbind.assert_not_called()
    fm.forget_workspace.assert_not_called()


# -- tokens_revoked ----------------------------------------------------------


def test_bot_token_revocation_removes_the_binding(wiring):
    app, fm, credentials, installation_store = wiring

    assert (
        _dispatch(
            app, {"type": "tokens_revoked", "tokens": {"bot": ["B123"]}}
        ).status
        == 200
    )

    credentials.unbind.assert_called_once_with(TEAM)
    fm.forget_workspace.assert_called_once_with(TEAM)
    installation_store.delete_bot.assert_called_once_with(
        enterprise_id=None, team_id=TEAM
    )


def test_a_user_revoking_their_own_token_keeps_the_binding(wiring):
    """The guard with the sharpest edge: the app is still installed.

    Unbinding here would destroy a live workspace's FaultMaven credential over
    one person disconnecting their account — and that credential is issued once
    per bind, so it takes an org admin re-running the install to get it back.
    """
    app, fm, credentials, installation_store = wiring

    assert (
        _dispatch(
            app, {"type": "tokens_revoked", "tokens": {"oauth": ["U123"]}}
        ).status
        == 200
    )

    credentials.unbind.assert_not_called()
    fm.forget_workspace.assert_not_called()
    # The SDK half still runs: that user's installation row does go.
    installation_store.delete_installation.assert_called_once_with(
        enterprise_id=None, team_id=TEAM, user_id="U123"
    )


def test_a_revocation_carrying_both_removes_the_binding(wiring):
    app, fm, credentials, _ = wiring

    _dispatch(
        app,
        {"type": "tokens_revoked", "tokens": {"oauth": ["U123"], "bot": ["B123"]}},
    )

    credentials.unbind.assert_called_once_with(TEAM)
    fm.forget_workspace.assert_called_once_with(TEAM)


# -- wiring ------------------------------------------------------------------


def test_socket_mode_registers_no_lifecycle_listeners():
    """Socket mode has one static token and no bindings, so there is nothing to
    tear down — and no installation store for the SDK half to use.

    Asserted by event name rather than "no listeners at all": the other
    registrars legitimately subscribe to ``app_mention``, ``message`` and
    ``app_home_opened`` on this transport too."""
    app = MagicMock()
    register_listeners(app, MagicMock(), MagicMock(), None)
    subscribed = [c.args[0] for c in app.event.call_args_list if c.args]
    assert "app_uninstalled" not in subscribed
    assert "tokens_revoked" not in subscribed


# -- revoking the credential, not just deleting the row ----------------------


def test_uninstall_revokes_the_service_account_credential(wiring):
    """Deleting the row makes the credential unreachable here; revoking makes it
    unusable anywhere.

    Without this the service-account refresh token stays valid inside the
    customer's organization for its full lifetime — usable by a replica still
    holding it, by a database backup, or by anything that logged it.
    """
    app, fm, credentials, _ = wiring

    _dispatch(app, {"type": "app_uninstalled"})

    fm.revoke_token.assert_called_once_with(
        "rt-live", token_type_hint="refresh_token"
    )


def test_the_credential_is_read_before_it_is_deleted(wiring):
    """Revoking needs the token, and after the DELETE there is nowhere to get it."""
    app, fm, credentials, _ = wiring
    order: list[str] = []
    credentials.get.side_effect = lambda _t: (
        order.append("read")
        or SimpleNamespace(team_id=TEAM, refresh_token="rt-live", organization_id="o")
    )
    credentials.unbind.side_effect = lambda _t: order.append("unbind")
    fm.revoke_token.side_effect = lambda *_a, **_k: order.append("revoke") or True

    _dispatch(app, {"type": "app_uninstalled"})

    assert order == ["read", "unbind", "revoke"]


def test_a_failed_revocation_does_not_abort_the_teardown(wiring):
    """The local binding still goes, and the installation half still runs."""
    app, fm, credentials, installation_store = wiring
    fm.revoke_token.side_effect = RuntimeError("backend down")

    assert _dispatch(app, {"type": "app_uninstalled"}).status == 200

    credentials.unbind.assert_called_once_with(TEAM)
    fm.forget_workspace.assert_called_once_with(TEAM)
    installation_store.delete_all.assert_called_once()


def test_an_unreadable_binding_still_unbinds(wiring):
    """Nothing to revoke is not a reason to leave the row behind."""
    app, fm, credentials, installation_store = wiring
    credentials.get.side_effect = RuntimeError("database is gone")

    assert _dispatch(app, {"type": "app_uninstalled"}).status == 200

    credentials.unbind.assert_called_once_with(TEAM)
    fm.revoke_token.assert_not_called()
    installation_store.delete_all.assert_called_once()


def test_forget_workspace_raising_does_not_strand_the_installation(wiring):
    """`_forget_binding` says it never raises — this is the half that used to.

    `fm.forget_workspace()` and its log line sat outside the guard, so a raise
    there propagated out of the listener and skipped the SDK teardown entirely,
    leaving a live bot token behind. The failure fell on the unsafe side.
    """
    app, fm, credentials, installation_store = wiring
    fm.forget_workspace.side_effect = RuntimeError("client torn down")

    assert _dispatch(app, {"type": "app_uninstalled"}).status == 200

    installation_store.delete_all.assert_called_once()
