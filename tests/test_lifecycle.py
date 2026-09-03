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
from unittest.mock import MagicMock

import pytest
from slack_bolt import App
from slack_bolt.request import BoltRequest
from slack_sdk.oauth.installation_store import InstallationStore

from listeners import register_listeners
from listeners.lifecycle import register_lifecycle

TEAM = "T0B9XNZDR44"


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
    fm = MagicMock()
    credentials = MagicMock()
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
    app.listener_runner.listener_executor.shutdown(wait=True)
    return response


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
