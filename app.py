"""FaultMaven Slack Agent — Bolt entrypoint (P0, Socket Mode).

Wires the Slack Bolt app (Assistant container + ``app_mention``) to the
FaultMaven core API and starts a Socket Mode connection — the fastest path to a
working end-to-end loop with no public URL. The HTTP + multi-workspace OAuth
transport lands in P5 (see ``docs/design.md`` §8, §14).
"""

from __future__ import annotations

import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import get_settings
from faultmaven import FaultMavenClient
from listeners import register_listeners
from store import CaseStore


def build_app() -> tuple[App, CaseStore, FaultMavenClient, str]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    fm = FaultMavenClient(
        settings.faultmaven_api_url,
        token=settings.faultmaven_api_token,
        dev_login_username=settings.faultmaven_dev_login_username,
        timeout=settings.faultmaven_request_timeout,
    )
    fm.startup()

    store = CaseStore(settings.case_store_path)

    app = App(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret or None,
    )
    register_listeners(app, fm, store)

    return app, store, fm, settings.slack_app_token


def main() -> None:
    app, store, fm, app_token = build_app()
    if not app_token:
        raise SystemExit(
            "SLACK_APP_TOKEN (xapp-...) is required for Socket Mode. "
            "Create one with the connections:write scope."
        )
    logging.getLogger("faultmaven.slack").info("FaultMaven Slack Agent starting")
    try:
        SocketModeHandler(app, app_token).start()
    finally:
        store.close()
        fm.close()


if __name__ == "__main__":
    main()
