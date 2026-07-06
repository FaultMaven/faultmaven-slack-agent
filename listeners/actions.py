"""Interactive suggested-action buttons (P2).

When a user clicks a DECIDE / FREE_SPEECH button rendered on a turn result, Slack
delivers a ``block_actions`` payload (over Socket Mode). We recover the case for
that thread, submit the encoded turn, post the next result in-thread, and disable
the clicked buttons so the same action can't be double-submitted.
"""

from __future__ import annotations

import json
from logging import Logger

from slack_bolt import Ack, App, BoltContext
from slack_sdk import WebClient

from faultmaven import FaultMavenClient, TurnResult
from rendering import SUGGESTED_ACTION_PATTERN, build_turn_blocks
from store import CaseStore

from ._turn import end_turn, try_begin_turn


def apply_action(
    fm: FaultMavenClient, case_id: str, value_json: str
) -> TurnResult:
    """Submit the turn encoded in a button's ``value`` and return the result."""

    value = json.loads(value_json)
    return fm.submit_turn(
        case_id,
        query=value.get("q"),
        intent_type=value.get("it"),
        intent_data=value.get("id"),
    )


def _disable_actions(client: WebClient, body: dict, label: str) -> None:
    """Rewrite the clicked message without its buttons, noting the choice."""

    message = body["message"]
    kept = [b for b in message.get("blocks", []) if b.get("type") != "actions"]
    kept.append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f":point_right: _You chose: {label}_"}
            ],
        }
    )
    client.chat_update(
        channel=body["channel"]["id"],
        ts=message["ts"],
        blocks=kept,
        text=message.get("text", "FaultMaven"),
    )


def register_actions(app: App, fm: FaultMavenClient, store: CaseStore) -> None:
    @app.action(SUGGESTED_ACTION_PATTERN)
    def on_suggested_action(
        ack: Ack,
        body: dict,
        context: BoltContext,
        client: WebClient,
        logger: Logger,
    ) -> None:
        ack()
        channel = body["channel"]["id"]
        message = body["message"]
        thread_ts = message.get("thread_ts") or message["ts"]
        team_id = context.team_id or ""

        # A click advances the case, so it's a turn — reserve the thread. If one
        # is already running, tell the clicker to retry once it's replied.
        if not try_begin_turn(
            client, team_id=team_id, channel=channel, thread_ts=thread_ts
        ):
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=":hourglass_flowing_sand: Still working on the previous turn "
                "— click that again once I've replied.",
            )
            return
        try:
            action = body["actions"][0]
            label = action.get("text", {}).get("text", "")
            case_id = store.get(team_id, channel, thread_ts)
            if not case_id:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=":warning: I lost track of this investigation's case. "
                    "Please @mention me to continue.",
                )
                return

            result = apply_action(fm, case_id, action["value"])
            _disable_actions(client, body, label)
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=result.agent_response[:300],
                blocks=build_turn_blocks(result),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("suggested-action failed: %s", exc)
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=":warning: That action hit an error. "
                    "Please try again or @mention me.",
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            end_turn(team_id, channel, thread_ts)
