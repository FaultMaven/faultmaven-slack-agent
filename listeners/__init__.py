"""Slack listener registration."""

from __future__ import annotations

from slack_bolt import App

from faultmaven import FaultMavenClient
from store import CaseStore
from workspace_credentials import WorkspaceCredentialStore

from .actions import register_actions
from .assistant import build_assistant
from .events import register_events
from .home import register_home
from .lifecycle import register_lifecycle
from .shortcuts import register_shortcuts


def register_listeners(
    app: App,
    fm: FaultMavenClient,
    store: CaseStore,
    workspace_credentials: WorkspaceCredentialStore | None = None,
) -> None:
    """Wire all listeners to the Bolt app with shared dependencies.

    ``workspace_credentials`` is present only on the HTTP transport, which is
    the one with an installation store and per-workspace bindings to tear down;
    socket mode passes None and registers no lifecycle listeners.
    """

    app.assistant(build_assistant(fm, store))
    register_events(app, fm, store)
    register_actions(app, fm, store)
    register_shortcuts(app, fm, store)
    register_home(app)
    if workspace_credentials is not None:
        register_lifecycle(app, fm, workspace_credentials)
