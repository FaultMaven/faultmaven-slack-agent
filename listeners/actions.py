"""Interactive suggested-action buttons (P2).

When a user clicks a DECIDE button rendered on a turn result (the only clickable
suggestion type — RUN and FREE_SPEECH are text), Slack delivers a
``block_actions`` payload (over Socket Mode). We recover the case for that thread,
submit the encoded turn, echo the choice as the clicker's turn, and post the next
result in-thread. Slack has no disabled state for message buttons, so the buttons
are swapped for a "working…" note the moment the click lands (a long turn would
otherwise leave them clickable for its whole duration), restored if the submit
fails (retry stays one click away), and stripped for good once the turn commits.
"""

from __future__ import annotations

import json
from logging import Logger

from slack_bolt import Ack, App, BoltContext
from slack_sdk import WebClient

from faultmaven import CaseNotFoundError, FaultMavenClient, TurnResult
from rendering import SUGGESTED_ACTION_PATTERN
from store import CaseStore

from ._turn import (
    CASE_GONE_TEXT,
    deliver_turn_result,
    retry_may_help,
    run_gated,
    turn_error_text,
    unlink_stale_case,
)


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


def _stripped_blocks(message: dict) -> tuple[list[dict], str]:
    """The message's blocks without the ``actions`` block, plus fallback text.

    Defensive: if the surface delivered a thinner message whose blocks carry no
    section (so removing the ``actions`` block would leave the question blank),
    rebuild a section from the message's fallback ``text`` — the question must
    never vanish, leaving only the echoed choice with nothing it answered.
    """

    text = message.get("text") or "FaultMaven"
    kept = [b for b in message.get("blocks", []) if b.get("type") != "actions"]
    if not any(b.get("type") == "section" for b in kept):
        kept.insert(0, {"type": "section", "text": {"type": "mrkdwn", "text": text}})
    return kept, text


def _rewrite(client: WebClient, body: dict, blocks: list[dict], text: str) -> None:
    """Replace the clicked message's blocks in place (the one ``chat_update``)."""

    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        blocks=blocks,
        text=text,
    )


def _disable_actions(client: WebClient, body: dict) -> None:
    """Strip the buttons off the clicked message so the choice can't be re-sent,
    keeping the question itself visible."""

    kept, text = _stripped_blocks(body["message"])
    _rewrite(client, body, kept, text)


def _show_working(client: WebClient, body: dict, label: str) -> None:
    """Swap the buttons for a "working…" note the moment a click lands.

    Slack message buttons cannot be disabled, so removing them is the only way
    to make a click unrepeatable while the turn runs. The note doubles as
    instant feedback that the click registered. If the turn then fails a way a
    retry could survive, :func:`_restore_actions` puts the buttons back.
    """

    kept, text = _stripped_blocks(body["message"])
    working = (
        f":hourglass_flowing_sand: Working on *{_plain(label)}*…"
        if label
        else ":hourglass_flowing_sand: Working on it…"
    )
    kept.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": working}]}
    )
    _rewrite(client, body, kept, text)


def _restore_actions(client: WebClient, body: dict) -> None:
    """Put the clicked message back exactly as delivered (buttons included).

    Runs only after a failure a retry could survive: the decision was not
    applied, so the retry path stays one click away. Leaving the "working…"
    state there instead would tell the thread the choice went through.
    """

    message = body["message"]
    _rewrite(
        client, body, message.get("blocks", []), message.get("text") or "FaultMaven"
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

        def post(text: str, blocks: list[dict] | None = None) -> bool:
            """Threaded post that reports failure instead of raising."""

            try:
                if blocks is None:
                    client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts, text=text
                    )
                else:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=text,
                        blocks=blocks,
                    )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("post failed in %s: %s", channel, exc)
                return False

        def work() -> None:
            action = body["actions"][0]
            label = action.get("text", {}).get("text", "")

            def settle(*, restore: bool) -> None:
                """Leave the clicked message in its final state. Never raises.

                Best-effort by necessity — the turn's outcome is already posted
                in-thread, so a failed update here costs the message's cosmetic
                state, never the answer. Logged, because the visible result is a
                stale "working…" note.
                """

                try:
                    if restore:
                        _restore_actions(client, body)
                    else:
                        _disable_actions(client, body)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "could not settle clicked buttons in %s: %s", channel, exc
                    )

            # Take the buttons down BEFORE the submit — a turn can run for
            # minutes, and Slack buttons can't be disabled, so this is the
            # only way a second click can't land meanwhile. Best-effort: if
            # the update fails, the per-thread gate still serializes turns
            # (a re-click just gets the ephemeral busy notice, as before).
            try:
                _show_working(client, body, label)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "could not hide clicked buttons in %s: %s", channel, exc
                )

            # Whether a failure leaves the buttons re-armed. Default False:
            # buttons come back ONLY where a re-click can genuinely succeed
            # (see :func:`retry_may_help`). Re-arming after a permanent failure
            # offers a retry that can only fail again — and after a DELETED case
            # it is worse than that: the thread has just been unlinked, so a
            # later click would land this stale decision on whatever fresh case
            # the thread opens next.
            restore = False

            def attempt() -> TurnResult | None:
                """Run the click's turn, or post why it failed and return None.

                Every pre-commit exit lives here — including the store read,
                which is inside the try because an unguarded raise would strand
                the message at "working…" with no buttons and no reply.
                """

                nonlocal restore
                case_id = None
                try:
                    case_id = store.get(team_id, channel, thread_ts)
                    if not case_id:
                        # No mapping to submit against, and the next @mention
                        # opens a fresh case — same stale-decision hazard as a
                        # deleted case, so the buttons stay down.
                        post(
                            ":warning: I lost track of this investigation's "
                            "case. Please @mention me to continue."
                        )
                        return None
                    return apply_action(fm, case_id, action["value"])
                except CaseNotFoundError:
                    # Only apply_action raises this, so case_id is set.
                    unlink_stale_case(store, team_id, channel, thread_ts, case_id)
                    post(CASE_GONE_TEXT)
                    return None
                except Exception as exc:  # noqa: BLE001
                    logger.exception("suggested-action failed: %s", exc)
                    post(turn_error_text(exc))
                    restore = retry_may_help(exc)
                    return None

            # Everything below is presentation. The two failure regimes get
            # opposite treatment: a failed SUBMIT may re-arm the buttons, but
            # once the backend committed the decision, no Slack-side failure may
            # claim the action errored or hand back a button — that invites
            # double-submitting the same decision.
            result = attempt()
            if result is None:
                settle(restore=restore)
                return

            # A button click posts no user message on its own, so consecutive
            # FaultMaven replies would pile up. Echo the choice as the
            # clicker's turn, so the thread reads as an exchange:
            #   [FM question] → "> @user chose X" → [FM reply].
            # Cosmetic — its failure must not cost the reply below.
            user_id = (body.get("user") or {}).get("id")
            if user_id and label:
                post(f"> <@{user_id}> chose *{_plain(label)}*")

            # The substantive output, via the shared committed-turn ladder:
            # rendering guarded, blocks post, then escaped plain-text fallback
            # — a render failure must not skip the fallback or the button
            # strip below.
            deliver_turn_result(post, result)

            # Settle the clicked message last: replace the transient "working…"
            # note with the clean stripped state (question kept, buttons gone
            # for good). Last so a settle failure can't discard the reply.
            settle(restore=False)

        # A click advances the case, so it's a turn — reserve the thread and run
        # in the background. If one is already running, the click is dropped (not
        # queued): its decision is lost, so tell the clicker to redo it.
        if not run_gated(
            client, team_id=team_id, channel=channel, thread_ts=thread_ts,
            skip_ts=None, work=work,
        ):
            # Ephemeral (clicker-only) so rapid clicks don't pile notices into the
            # thread — and a transient notice suits a transient busy state (a
            # persistent one would go stale the moment the turn finishes). A hard
            # failure to post is logged rather than silently swallowed.
            user_id = (body.get("user") or {}).get("id")
            try:
                client.chat_postEphemeral(
                    channel=channel,
                    thread_ts=thread_ts,
                    user=user_id,
                    text=":hourglass_flowing_sand: I was mid-step, so that didn't "
                    "register — I'll reply shortly; redo your choice afterward if "
                    "it still applies.",
                )
            except Exception as exc:  # noqa: BLE001 — a notice must never raise on the drop path
                logger.warning(
                    "Couldn't post the busy notice to %s in %s (%s).",
                    user_id or "?", channel, exc,
                )
