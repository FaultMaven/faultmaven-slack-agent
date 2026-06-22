"""Assistant container — the 1:1 AI side-panel investigation surface.

Mirrors the Bolt assistant pattern: greet + suggested prompts on thread start,
then run one FaultMaven turn per user message with a live status indicator.
"""

from __future__ import annotations

from logging import Logger

from slack_bolt import Assistant, BoltContext, Say, SetStatus, SetSuggestedPrompts

from faultmaven import FaultMavenClient
from rendering import build_turn_blocks
from store import CaseStore

from ._turn import Dedup, run_turn

_SUGGESTED_PROMPTS = [
    {
        "title": "Investigate an error",
        "message": "I'm seeing this error and need help finding the cause:\n",
    },
    {
        "title": "Summarize an incident",
        "message": "Summarize what's happened in this incident so far.",
    },
    {
        "title": "What changed recently?",
        "message": "What recent deploys or config changes could explain this?",
    },
    {
        "title": "Search our runbooks",
        "message": "Is there a runbook for this symptom?",
    },
]


def build_assistant(fm: FaultMavenClient, store: CaseStore) -> Assistant:
    """Construct the Assistant with FaultMaven-backed handlers."""

    assistant = Assistant()
    dedup = Dedup()

    @assistant.thread_started
    def thread_started(
        say: Say,
        set_suggested_prompts: SetSuggestedPrompts,
        logger: Logger,
    ) -> None:
        try:
            say(
                "Hi — I'm FaultMaven. Describe a symptom, paste an error or log, "
                "and I'll help you investigate. What's going on?"
            )
            set_suggested_prompts(prompts=_SUGGESTED_PROMPTS)
        except Exception as exc:  # noqa: BLE001
            logger.exception("assistant_thread_started failed: %s", exc)

    @assistant.user_message
    def user_message(
        payload: dict,
        context: BoltContext,
        set_status: SetStatus,
        say: Say,
        logger: Logger,
    ) -> None:
        try:
            if dedup.is_duplicate(
                f"{payload.get('channel')}:{payload.get('ts')}"
            ):
                return
            set_status("is investigating…")
            result = run_turn(
                fm,
                store,
                team_id=context.team_id or "",
                channel_id=payload["channel"],
                thread_ts=payload["thread_ts"],
                text=payload.get("text", ""),
            )
            say(
                text=result.agent_response[:300],
                blocks=build_turn_blocks(result),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("assistant user_message failed: %s", exc)
            say(
                ":warning: FaultMaven hit an error on that turn. "
                "Please try again."
            )

    return assistant
