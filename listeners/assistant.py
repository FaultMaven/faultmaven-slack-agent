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
from slack_files import download_message_files
from store import CaseStore

from ._turn import (
    UNREADABLE_FILES_TEXT,
    Dedup,
    deliver_turn_result,
    offload_turn,
    resolve_query,
    run_turn,
    try_begin_turn,
    turn_error_text,
)

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

        def post(text: str, blocks: list[dict] | None = None) -> bool:
            """Guarded say(): log-and-False instead of raising, so a posting
            failure can never fall through to a handler that blames the turn."""

            try:
                if blocks is None:
                    say(text)
                else:
                    say(text=text, blocks=blocks)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("assistant post failed in %s: %s", channel, exc)
                return False

        def turn_work() -> None:
            try:
                # download_message_files no-ops (returns []) when there are no
                # files.
                files = download_message_files(client.token, payload)

                query = resolve_query(
                    payload.get("text"), downloaded_files=bool(files)
                )
                if query is None:
                    # No text and nothing ingestible — don't open a blank case;
                    # say why (mirrors the shortcut's decline instead of a
                    # generic error).
                    post(
                        UNREADABLE_FILES_TEXT
                        if payload.get("files")
                        else "Tell me what's going on — describe a symptom, "
                        "paste an error, or attach a log."
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
            except Exception as exc:  # noqa: BLE001
                logger.exception("assistant user_message failed: %s", exc)
                post(turn_error_text(exc))
                return

            # The turn is committed — deliver_turn_result owns the
            # never-say-try-again degradation from here (every post guarded).
            # Stamp the case pointer only on the opening reply (thread = case).
            opening_case_id = (
                store.get(team_id, channel, thread_ts) if first_turn else None
            )
            deliver_turn_result(post, result, case_id=opening_case_id)

        # set_status shows the native "investigating" indicator immediately, so
        # the offloaded download/turn has visible feedback in front of it. It's
        # cosmetic, and it can fail on a thread that wasn't opened via
        # assistant_thread_started (e.g. a DM summons rooted by the events
        # handler), so a status failure must never abort the actual turn.
        try:
            set_status("is investigating…")
        except Exception as status_exc:  # noqa: BLE001
            logger.warning("set_status failed; continuing turn: %s", status_exc)

        # Offload the slow part (downloads up to 20s/file + a turn up to the
        # 120s API timeout) to a tracked daemon, exactly like the channel
        # surfaces: Bolt's listener executor defaults to FIVE workers, and five
        # concurrent Assistant turns would otherwise starve every ack() in the
        # app (buttons, shortcuts) past Slack's 3-second window. offload_turn
        # itself releases the gate if the worker can't start.
        offload_turn(
            turn_work, team_id=team_id, channel=channel, thread_ts=thread_ts
        )

    return assistant
