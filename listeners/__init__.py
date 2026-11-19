"""Slack listener registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from slack_bolt import App

from faultmaven import FaultMavenClient
from store import CaseStore

from ._turn import configure_requester_key
from .actions import register_actions, register_data_box
from .assistant import build_assistant
from .events import register_events
from .home import register_home
from .lifecycle import register_lifecycle
from .shortcuts import register_shortcuts

if TYPE_CHECKING:  # annotation only — keeps SQLAlchemy off socket mode's import path
    from workspace_credentials import WorkspaceCredentialStore


def register_listeners(
    app: App,
    fm: FaultMavenClient,
    store: CaseStore,
    workspace_credentials: WorkspaceCredentialStore | None = None,
    *,
    requester_key: str,
) -> None:
    """Wire all listeners to the Bolt app with shared dependencies.

    ``requester_key`` is ``Settings.requester_key``, the HMAC key of every
    turn's opaque requester (:func:`listeners._turn.requester_for`); "" sends
    none.

    ``workspace_credentials`` is present only on the HTTP transport, which is
    the one with an installation store and per-workspace bindings to tear down;
    socket mode passes None and registers no lifecycle listeners.
    """

    configure_requester_key(requester_key)
    app.assistant(build_assistant(fm, store))
    register_events(app, fm, store)
    register_actions(app, fm, store)
    register_data_box(app, fm, store)
    register_shortcuts(app, fm, store)
    register_home(app)
    if workspace_credentials is not None:
        register_lifecycle(app, fm, workspace_credentials)
