"""Slack listener registration."""

from slack_bolt import App

from faultmaven import FaultMavenClient
from store import CaseStore

from .actions import register_actions
from .assistant import build_assistant
from .events import register_events


def register_listeners(app: App, fm: FaultMavenClient, store: CaseStore) -> None:
    """Wire all listeners to the Bolt app with shared dependencies."""

    app.assistant(build_assistant(fm, store))
    register_events(app, fm, store)
    register_actions(app, fm, store)
