"""App Home — FaultMaven's static "front page".

The Home tab is the one always-available, non-conversational surface: it renders
whenever a user opens the app's Home, independent of the assistant/DM flow (which
only greets inside an assistant thread). So it's the reliable place for the
product's positioning — what FaultMaven is, what it does, and how to start.

Native Slack mrkdwn (single ``*bold*``, ``•`` bullets, ``:emoji:``) — published
verbatim, not run through the Markdown→mrkdwn converter.
"""

from __future__ import annotations

from logging import Logger

from slack_bolt import App
from slack_sdk import WebClient

_HOME_BLOCKS = [
    {
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": "FaultMaven — your AI troubleshooting copilot",
            "emoji": True,
        },
    },
    {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                ":wave: I work a problem the way a seasoned engineer does — "
                "*goal-driven, methodical, evidence-based,* and *self-learning* — "
                "and I never forget what I learn."
            ),
        },
    },
    {"type": "divider"},
    {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                "*What I can do*\n"
                "• :mag: *Share a file* — a log, an error, a config — ask me about "
                'it, or "does this look right?"\n'
                "• :warning: *Hit a problem* — I'll investigate for the root cause\n"
                "• :bulb: *Stuck on a fix* — I'll propose one\n"
                "• :books: *Wrap up* — I'll write it up so it's reusable next time"
            ),
        },
    },
    {"type": "divider"},
    {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                "*How we work*\n"
                "You bring the data — logs, errors, whatever you've got, noise and "
                "all. I pull out the *evidence* and tell you what's still missing. "
                "You approve and execute — you're always in control."
            ),
        },
    },
    {"type": "divider"},
    {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                "*Start* — open the *Messages* tab and tell me what's wrong. Each "
                "new problem opens its own thread, so separate investigations "
                "stay separate."
            ),
        },
    },
]


def build_home_view() -> dict:
    """The Block Kit Home-tab view."""

    return {"type": "home", "blocks": _HOME_BLOCKS}


def register_home(app: App) -> None:
    @app.event("app_home_opened")
    def on_app_home_opened(
        event: dict, client: WebClient, logger: Logger
    ) -> None:
        # app_home_opened fires for BOTH the Home and Messages tabs; only the
        # Home tab renders a published view.
        if event.get("tab") != "home":
            return
        try:
            client.views_publish(user_id=event["user"], view=build_home_view())
        except Exception as exc:  # noqa: BLE001 — a home render must never crash the app
            logger.exception("app_home publish failed: %s", exc)
