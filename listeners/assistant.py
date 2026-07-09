"""Assistant container — the 1:1 AI side-panel investigation surface.

On thread start we greet with a short capability ("can-do") overview — static
orientation the agent owns, never routed to the engine, so it's always correct
and costs no turn. We deliberately do NOT set Slack "suggested prompts": every
message here becomes an investigation turn, and prompt chips like "what can you
do?" would submit a non-incident the engine can't answer. Then run one
FaultMaven turn per user message with a live status indicator.
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

# Static capability overview shown once on thread start. Native Slack mrkdwn
# (single *bold*, • bullets, :emoji:) — it is posted verbatim via say(), not run
# through the Markdown→mrkdwn converter. It orients the user without asking the
# engine to describe itself.
_GREETING = (
    ":wave: *I'm FaultMaven* — a troubleshooting copilot. I help you find the "
    "*root cause* of an incident by connecting what you share to your runbooks, "
    "docs, and past fixes.\n\n"
    "*Here's what I can do*\n"
    "• :mag: *Investigate* — paste an error, a log line, or describe a symptom "
    "and I'll work toward the cause\n"
    "• :bar_chart: *Read your evidence* — logs, metrics, stack traces, config; "
    "I'll pull out what matters\n"
    "• :books: *Use your knowledge* — I search your runbooks and past "
    "resolutions for relevant fixes\n"
    "• :clipboard: *Keep the case* — I track hypotheses and evidence and build a "
    "report as we go\n\n"
    "*To start, just tell me what's wrong* — an error message, a failing "
    "service, or something like \"checkout latency spiked after this morning's "
    "deploy.\""
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
