"""Assistant container — the 1:1 AI side-panel investigation surface (the "Chat" tab).

On thread start we greet with a capability overview — static orientation the
agent owns, never routed to the engine. We deliberately do NOT set Slack
"suggested prompts": every message here becomes an investigation turn, and a
prompt chip like "what can you do?" would submit a non-incident the engine isn't
built to answer. Then run one FaultMaven turn per user message with a live status
indicator.
"""

from __future__ import annotations

from logging import Logger

from slack_bolt import Assistant, BoltContext, Say, SetStatus
from slack_sdk import WebClient

from faultmaven import FaultMavenClient
from rendering import build_turn_blocks
from slack_files import download_message_files
from store import CaseStore

from ._turn import Dedup, end_turn, resolve_query, run_turn, try_begin_turn

# Short conversational opener posted once on thread start. Kept deliberately
# brief: the "Agent Overview" (assistant_description) already sits at the top of
# the Chat tab describing what FaultMaven does, so the greeting only needs to
# invite the user to start — repeating the capability list here would say the
# same thing twice in one view. Native Slack mrkdwn, posted verbatim via say().
_GREETING = (
    ":wave: Ready when you are — paste an error, share a file, or just tell me "
    "what's wrong, and I'll take it from there."
)


def build_assistant(fm: FaultMavenClient, store: CaseStore) -> Assistant:
    """Construct the Assistant with FaultMaven-backed handlers."""

    assistant = Assistant()
    dedup = Dedup()

    @assistant.thread_started
    def thread_started(say: Say, logger: Logger) -> None:
        try:
            say(_GREETING)
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
            # It's cosmetic, and it can fail on a thread that wasn't opened via
            # assistant_thread_started (e.g. a DM summons rooted by the events
            # handler), so a status failure must never abort the actual turn.
            try:
                set_status("is investigating…")
            except Exception as status_exc:  # noqa: BLE001
                logger.warning("set_status failed; continuing turn: %s", status_exc)
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
