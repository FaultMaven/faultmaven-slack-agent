"""Assistant container — the 1:1 AI side-panel investigation surface.

Mirrors the Bolt assistant pattern: greet + suggested prompts on thread start,
then run one FaultMaven turn per user message with a live status indicator.
"""

from __future__ import annotations

from logging import Logger

from slack_bolt import Assistant, BoltContext, Say, SetStatus, SetSuggestedPrompts
from slack_sdk import WebClient

from faultmaven import FaultMavenClient
from rendering import build_turn_blocks
from slack_files import download_message_files
from store import CaseStore

from ._turn import Dedup, end_turn, resolve_query, run_turn, try_begin_turn

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
        client: WebClient,
        set_status: SetStatus,
        say: Say,
        logger: Logger,
    ) -> None:
        if dedup.is_duplicate(f"{payload.get('channel')}:{payload.get('ts')}"):
            return
        team_id = context.team_id or ""
        channel = payload["channel"]
        thread_ts = payload["thread_ts"]
        # One turn at a time per thread: a second message sent before the reply is
        # skipped (⏭️) rather than racing it (same rule as the channel surfaces).
        if not try_begin_turn(
            client, team_id=team_id, channel=channel, thread_ts=thread_ts,
            skip_ts=payload.get("ts"),
        ):
            return
        try:
            # set_status shows the native "investigating" indicator immediately,
            # so the file download below still has visible feedback in front of it.
            set_status("is investigating…")
            # download_message_files no-ops (returns []) when there are no files.
            files = download_message_files(client.token, payload)

            query = resolve_query(payload.get("text"), downloaded_files=bool(files))
            if query is None:
                # No text and nothing ingestible — don't open a blank case; say
                # why (mirrors the shortcut's decline instead of a generic error).
                say(
                    ":information_source: I couldn't read the attached file(s) "
                    "(too large, or I lack access). Paste the key text and I'll "
                    "take it from there."
                    if payload.get("files")
                    else "Tell me what's going on — describe a symptom, paste an "
                    "error, or attach a log."
                )
                return

            first_turn = store.get(team_id, channel, thread_ts) is None
            result = run_turn(
                fm,
                store,
                team_id=team_id,
                channel_id=channel,
                thread_ts=thread_ts,
                text=query,
                files=files or None,
            )
            # Stamp the case pointer only on the opening reply (thread = case).
            opening_case_id = (
                store.get(team_id, channel, thread_ts) if first_turn else None
            )
            say(
                text=result.agent_response[:300],
                blocks=build_turn_blocks(result, case_id=opening_case_id),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("assistant user_message failed: %s", exc)
            say(
                ":warning: FaultMaven hit an error on that turn. "
                "Please try again."
            )
        finally:
            end_turn(team_id, channel, thread_ts)

    return assistant
