"""Render a :class:`~faultmaven.client.TurnResult` into Slack Block Kit.

P0 renders a clean, scannable card: the agent's message, suggested next steps,
and a context line (case state / turn). Suggested actions render as text in P0;
they become interactive buttons in P2. (Hypotheses and confidence are not part
of the turn response — they surface via the P2 reasoning timeline — so they are
not rendered here.)
"""

from __future__ import annotations

import json
import re
from typing import Any, Pattern

from faultmaven.client import TurnResult
from slack_mrkdwn import escape_mrkdwn, to_mrkdwn

# Slack section ``mrkdwn`` text tops out at 3000 chars; stay safely under.
_SECTION_LIMIT = 2900
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
# Case states in which the investigation has concluded.
_TERMINAL_STATES = {"resolved", "closed"}

# Read-time cause-assurance labels (#572 / INV-28). The cause claim lives in the
# LLM's free-text ``agent_response``, outside every engine truth surface; the
# grade is the graph-derived truth. Only the held-back grades are labeled —
# ``confirmed`` (counterfactually verified) needs no qualifier, so it is absent.
_ASSURANCE_LABELS = {
    "mechanistic": "Cause assurance: mechanistic — identified from evidence, not yet counterfactually confirmed",
    "no_root": "Cause assurance: unvalidated — stated by the assistant, not validated in the causal analysis",
}

# action_id prefix for suggested-action buttons. Each button gets a unique
# "<prefix>:<index>" id (Slack requires action_ids be unique within a message);
# the interactions handler matches the prefix via SUGGESTED_ACTION_PATTERN.
# Button ``value`` is JSON encoding the turn to submit when clicked:
# {"q": query, "it": intent_type, "id": intent_data}.
SUGGESTED_ACTION_ID = "fm_suggested_action"
SUGGESTED_ACTION_PATTERN: Pattern[str] = re.compile(
    rf"^{re.escape(SUGGESTED_ACTION_ID)}:"
)
_MAX_BUTTONS = 10
_BUTTON_VALUE_LIMIT = 1900  # Slack caps button value at 2000 chars
# A RUN command rendered in a code span; a bullet must stay readable and one
# oversized payload must not blow the whole section past the 3000-char limit.
_COMMAND_LIMIT = 600


def clean_mention(text: str) -> str:
    """Strip ``<@U123>`` bot mentions and surrounding whitespace."""

    return _MENTION_RE.sub("", text or "").strip()


def _chunk(text: str) -> list[str]:
    """Split long text on paragraph boundaries to fit section limits."""

    if len(text) <= _SECTION_LIMIT:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > _SECTION_LIMIT and current:
            chunks.append(current)
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current)
    # Hard-wrap any single oversized paragraph — on a line break when one
    # exists in the back half, so mrkdwn spans and Slack entities aren't cut
    # mid-sequence (a severed <url|text> renders as angle-bracket garbage).
    out: list[str] = []
    for c in chunks:
        while len(c) > _SECTION_LIMIT:
            cut = c.rfind("\n", _SECTION_LIMIT // 2, _SECTION_LIMIT)
            cut = cut + 1 if cut != -1 else _SECTION_LIMIT
            out.append(c[:cut].rstrip("\n") or c[:cut])
            c = c[cut:]
        out.append(c)
    return _balance_fences(out)


def _balance_fences(chunks: list[str]) -> list[str]:
    """Re-close/reopen a code fence split across chunks.

    A fence cut in half renders half the code as plain mrkdwn (log asterisks
    become bold/italics) and drops a stray ``` mid-message. Closing the open
    fence at the chunk edge and reopening it in the next keeps every chunk
    self-contained. The +8 chars stay inside the 3000-char headroom above
    ``_SECTION_LIMIT``.
    """

    out: list[str] = []
    open_fence = False
    for chunk in chunks:
        starts_open = open_fence
        if chunk.count("```") % 2 == 1:
            open_fence = not open_fence
        if starts_open:
            chunk = "```\n" + chunk
        if open_fence:
            chunk = chunk + "\n```"
        out.append(chunk)
    return out


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _action_label(action: dict[str, Any]) -> str:
    return (
        action.get("label")
        or action.get("title")
        or action.get("description")
        or "(action)"
    )


def _format_evidence(action: dict[str, Any]) -> str:
    """Render an evidence ask, appending acquisition hints when present."""

    label = to_mrkdwn(_action_label(action))
    hints = action.get("hints")
    if isinstance(hints, list) and hints:
        joined = escape_mrkdwn(", ".join(str(h) for h in hints))
        return f"• {label} — _{joined}_"
    if isinstance(hints, str) and hints.strip():
        return f"• {label} — _{escape_mrkdwn(hints.strip())}_"
    return f"• {label}"


def _format_action(action: dict[str, Any]) -> str:
    """Render a suggested action as an mrkdwn bullet, by type.

    RUN → a copy-run command in a code span (never rewritten as mrkdwn).
    FREE_SPEECH → a prompt to answer in your own words (a hint, not a command).
    DECIDE → the choice, described. Its terse ``label`` ("Documentation") is only
    a chip; the fuller ``body`` ("Treat as documentation or notes.") is what makes
    the choice legible, so it rides after an em dash — matching the Copilot, which
    renders ``label — body``. A DECIDE with a submittable ``payload``/``intent``
    also renders as a button (§_action_value); this line is its description, and
    the sole rendering for a DECIDE the button path can't encode.
    """

    a_type = (action.get("type") or action.get("action_type") or "").upper()
    if a_type == "RUN":
        command = str(
            action.get("payload") or action.get("body") or _action_label(action)
        )
        # A backtick inside the payload would close the code span and re-enable
        # mrkdwn parsing for the rest of the line; entities must not go live.
        command = escape_mrkdwn(command.replace("`", "'"))
        if len(command) > _COMMAND_LIMIT:
            command = command[: _COMMAND_LIMIT - 1] + "…"
        return f"• *Run:* `{command}`"  # command stays literal in the code span
    label = to_mrkdwn(_action_label(action))
    body = action.get("body")
    if a_type == "DECIDE":
        if body:
            return f"• :white_check_mark: *{label}* — {to_mrkdwn(str(body))}"
        return f"• :white_check_mark: *{label}*"
    # FREE_SPEECH (or any other non-clickable) — a "tell me in your own words" prompt.
    return f"• :speech_balloon: {label}"


def _action_value(action: dict[str, Any]) -> str | None:
    """Encode a submittable button value, or None if the action isn't clickable.

    **Only DECIDE is a button**, and it is clickable whenever it carries
    something to submit — a ``payload`` (the exact message a click sends) or an
    ``intent`` (a routable operation). Two DECIDE shapes exist and *both* are
    buttons:

    * **Intent-bearing** — state-machine gates (resolution/close confirmations,
      disposition transitions) and file-classification clarifications
      (``file_reclassification``, carrying the target file_id + DataType). The
      ``intent`` is replayed verbatim, flagged ``user_confirmed``, so the
      engine routes it deterministically — a clarification click re-labels the
      file server-side instead of being read as a free-text analysis request
      (issue #27).
    * **Payload-only** — runbook and regenerate-summary. These carry *no*
      ``intent``; a click submits the ``payload`` text as a plain query,
      exactly as the Copilot does — the engine matches those payloads verbatim
      on terminal turns. Requiring an intent here (the old rule) stranded this
      family as un-clickable "Decision: …" bullets.

    RUN (copy-run locally), FREE_SPEECH (answer in your own words), and EVIDENCE
    are **not** submittable, and a bare DECIDE with neither payload nor intent has
    nothing to send — all fall back to text. ``evidence_need`` intents are also
    non-submittable (NOT_IMPLEMENTED server-side); oversized values fall back to
    text rather than truncate.
    """

    a_type = (action.get("type") or action.get("action_type") or "").upper()
    if a_type != "DECIDE":
        return None
    intent = action.get("intent") or {}
    intent_type = intent.get("type")
    if intent_type == "evidence_need":
        return None
    payload = action.get("payload")
    # Nothing to submit → not a button (e.g. a label-only DECIDE); render as text.
    if not payload and not intent_type:
        return None
    query = payload or _action_label(action)
    value: dict[str, Any] = {"q": query}
    if intent_type:
        value["it"] = intent_type
        value["id"] = {**intent, "user_confirmed": True}
    encoded = json.dumps(value)
    return encoded if len(encoded) <= _BUTTON_VALUE_LIMIT else None


def _make_button(action: dict[str, Any]) -> dict[str, Any] | None:
    """Build a Block Kit button for a submittable action, or None."""

    encoded = _action_value(action)
    if encoded is None:
        return None
    button: dict[str, Any] = {
        "type": "button",
        "action_id": SUGGESTED_ACTION_ID,
        "text": {
            "type": "plain_text",
            "text": _action_label(action)[:75],
            "emoji": True,
        },
        "value": encoded,
    }
    if (action.get("type") or "").upper() == "DECIDE":
        button["style"] = "primary"
    return button


def build_turn_blocks(
    result: TurnResult, *, case_id: str | None = None
) -> list[dict[str, Any]]:
    """Render a turn result as Block Kit blocks.

    Honors FaultMaven's soundness posture (design §7.3): while the case is still
    open and the engine is asking for data, the missing-data request is surfaced
    in its own prominent block — never buried among other suggestions — and the
    context line states the investigation is ongoing so an in-progress turn is
    not mistaken for a verdict.

    DECIDE actions become interactive buttons (handled by
    listeners/actions.py); RUN renders as a copyable code block; EVIDENCE asks
    render as the prominent text section above.

    ``case_id`` is passed **only on the case-opening reply** (thread = case, so
    once is enough). It renders as a quiet, selectable line in the context row
    so the case record can be located in the backend/dashboard; later turns omit
    it to keep the thread clean.
    """

    blocks: list[dict[str, Any]] = []

    # No ":robot_face: *FaultMaven*" header — Slack already shows the app's name
    # and icon above every message, so it's redundant. The reply is the response
    # text, converted from the engine's standard Markdown to Slack mrkdwn (the
    # drop-if-busy path prepends an @mention of the replier).
    parts = _chunk(to_mrkdwn(result.agent_response))
    for part in parts:
        blocks.append(_section(part))

    evidence: list[dict[str, Any]] = []
    buttons: list[dict[str, Any]] = []
    text_actions: list[dict[str, Any]] = []
    for action in result.suggested_actions:
        a_type = (action.get("type") or action.get("action_type") or "").upper()
        if a_type == "EVIDENCE":
            evidence.append(action)
            continue
        button = _make_button(action) if len(buttons) < _MAX_BUTTONS else None
        if button is not None:
            buttons.append(button)
            # A button face shows only the terse label ("Something else"); when
            # the action carries a fuller ``body``, also describe it in the step
            # list so the choice reads with its meaning, not as a bare chip. A
            # body-less DECIDE (a confirmation gate — "Close case") needs no line;
            # the button label says it all.
            if action.get("body"):
                text_actions.append(action)
        else:
            text_actions.append(action)

    terminal = (result.case_state or "").lower() in _TERMINAL_STATES

    # These aggregate sections are unbounded input (one bullet per suggested
    # action), so they go through _chunk like the response body — an oversized
    # single section fails the whole post with invalid_blocks.
    if evidence:
        lines = "\n".join(_format_evidence(a) for a in evidence)
        for part in _chunk(f":mag: *To move forward, FaultMaven needs:*\n{lines}"):
            blocks.append(_section(part))

    if text_actions:
        lines = "\n".join(_format_action(a) for a in text_actions)
        for part in _chunk(f"*Suggested next steps*\n{lines}"):
            blocks.append(_section(part))

    # action_id must be unique within a message; suffix each button by index.
    # The handler matches the shared prefix (see SUGGESTED_ACTION_PATTERN).
    for index, button in enumerate(buttons):
        button["action_id"] = f"{SUGGESTED_ACTION_ID}:{index}"
    # Slack allows at most 5 elements per actions block.
    for i in range(0, len(buttons), 5):
        blocks.append({"type": "actions", "elements": buttons[i : i + 5]})

    context: list[dict[str, Any]] = []
    if case_id:
        # Quiet, selectable case pointer — root-only (see docstring).
        context.append({"type": "mrkdwn", "text": f":card_index_dividers: `{case_id}`"})
    if result.case_state:
        context.append(
            {"type": "mrkdwn", "text": f"State: `{result.case_state}`"}
        )
    # Only label the cause on a terminal turn — the point where the conclusion is
    # a settled disposition the user acts on (mirrors the copilot, which labels
    # only the terminal Root Cause row). An RCC can be minted mid-investigation
    # (an early LLM-authored cause grades no_root); labeling it on every
    # subsequent turn would repeat the same qualifier as noise.
    assurance_label = _ASSURANCE_LABELS.get(result.cause_assurance or "")
    if assurance_label and terminal:
        if result.cause_overclaim:
            assurance_label += " ⚠ (stated more certainly than the evidence supports)"
        context.append({"type": "mrkdwn", "text": assurance_label})
    if result.turn_number is not None:
        context.append({"type": "mrkdwn", "text": f"Turn {result.turn_number}"})
    if evidence and not terminal:
        context.append(
            {"type": "mrkdwn", "text": "Investigation in progress — gathering evidence"}
        )
    if context:
        blocks.append({"type": "context", "elements": context})

    return blocks
