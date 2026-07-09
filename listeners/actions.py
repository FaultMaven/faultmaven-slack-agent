"""Interactive suggested-action buttons (P2).

When a user clicks a DECIDE button rendered on a turn result (the only clickable
suggestion type — RUN and FREE_SPEECH are text), Slack delivers a
``block_actions`` payload (over Socket Mode). We recover the case for that thread,
submit the encoded turn, echo the choice as the clicker's turn, post the next
result in-thread, and strip the clicked buttons so the same action can't be
double-submitted.
"""

from __future__ import annotations

import json
from logging import Logger

from slack_bolt import Ack, App, BoltContext
from slack_sdk import WebClient

from faultmaven import FaultMavenClient, TurnResult
from rendering import SUGGESTED_ACTION_PATTERN, build_turn_blocks
from store import CaseStore

from ._turn import run_gated


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


def _plain(text: str) -> str:
    """Neutralize mrkdwn/entities so a label can't break the echo's formatting."""

    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for ch in "*_~`":
        text = text.replace(ch, "")
    return text


def _disable_actions(client: WebClient, body: dict) -> None:
    """Strip the buttons off the clicked message so the choice can't be re-sent."""

    message = body["message"]
    kept = [b for b in message.get("blocks", []) if b.get("type") != "actions"]
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

        def work() -> None:
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
                _disable_actions(client, body)

                # A button click posts no user message on its own, so consecutive
                # FaultMaven replies would pile up. Echo the choice as the
                # clicker's turn, so the thread reads as an exchange:
                #   [FM question] → "> @user chose X" → [FM reply].
                user_id = (body.get("user") or {}).get("id")
                if user_id and label:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f"> <@{user_id}> chose *{_plain(label)}*",
                    )

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

        # A click advances the case, so it's a turn — reserve the thread and run
        # in the background. If one is already running, the click is dropped.
        if not run_gated(
            client, team_id=team_id, channel=channel, thread_ts=thread_ts,
            skip_ts=None, work=work,
        ):
            # Notify only the clicker, ephemerally, so rapid clicks don't pile
            # visible notices into the thread (mirrors the quiet ⏭️ a dropped
            # text reply gets). Don't advise re-clicking: when the in-flight turn
            # replies it strips these buttons and posts fresh ones, so this exact
            # button is gone — the clicker just waits and acts on what comes next.
            user_id = (body.get("user") or {}).get("id")
            if user_id:
                try:
                    client.chat_postEphemeral(
                        channel=channel,
                        thread_ts=thread_ts,
                        user=user_id,
                        text=":hourglass_flowing_sand: I'm still finishing your "
                        "previous step — hang on, I'll reply here in a moment.",
                    )
                except Exception:  # noqa: BLE001 — a notice must never raise on the drop path
                    pass
