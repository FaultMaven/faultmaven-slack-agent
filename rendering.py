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

# Slack section ``mrkdwn`` text tops out at 3000 chars; stay safely under.
_SECTION_LIMIT = 2900
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
# Case states in which the investigation has concluded.
_TERMINAL_STATES = {"resolved", "closed"}

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
    # Hard-wrap any single oversized paragraph.
    out: list[str] = []
    for c in chunks:
        while len(c) > _SECTION_LIMIT:
            out.append(c[:_SECTION_LIMIT])
            c = c[_SECTION_LIMIT:]
        out.append(c)
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

    label = _action_label(action)
    hints = action.get("hints")
    if isinstance(hints, list) and hints:
        return f"• {label} — _{', '.join(str(h) for h in hints)}_"
    if isinstance(hints, str) and hints.strip():
        return f"• {label} — _{hints.strip()}_"
    return f"• {label}"


def _format_action(action: dict[str, Any]) -> str:
    """Render a non-clickable suggested action as an mrkdwn bullet, by type."""

    a_type = (action.get("type") or action.get("action_type") or "").upper()
    label = _action_label(action)
    if a_type == "RUN":
        command = action.get("payload") or action.get("body") or label
        return f"• *Run:* `{command}`"
    if a_type == "DECIDE":
        return f"• :white_check_mark: *Decision:* {label}"
    return f"• {label}"


def _action_value(action: dict[str, Any]) -> str | None:
    """Encode a submittable button value, or None if the action isn't one.

    DECIDE carries a backend ``intent`` (QueryIntent dict) we replay verbatim,
    flagged ``user_confirmed`` since the click is an explicit confirmation.
    FREE_SPEECH submits the suggested text as a plain conversation turn.
    ``evidence_need`` is never submittable (NOT_IMPLEMENTED server-side, design
    §6.3), and oversized values fall back to text rather than truncate.
    """

    a_type = (action.get("type") or action.get("action_type") or "").upper()
    payload = action.get("payload") or _action_label(action)
    if a_type == "DECIDE":
        intent = action.get("intent") or {}
        intent_type = intent.get("type")
        if not intent_type or intent_type == "evidence_need":
            return None
        value = {
            "q": payload,
            "it": intent_type,
            "id": {**intent, "user_confirmed": True},
        }
    elif a_type == "FREE_SPEECH":
        value = {"q": payload, "it": "conversation"}
    else:
        return None

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


def build_turn_blocks(result: TurnResult) -> list[dict[str, Any]]:
    """Render a turn result as Block Kit blocks.

    Honors FaultMaven's soundness posture (design §7.3): while the case is still
    open and the engine is asking for data, the missing-data request is surfaced
    in its own prominent block — never buried among other suggestions — and the
    context line states the investigation is ongoing so an in-progress turn is
    not mistaken for a verdict.

    DECIDE / FREE_SPEECH actions become interactive buttons (handled by
    listeners/actions.py); RUN renders as a copyable code block; EVIDENCE asks
    render as the prominent text section above.
    """

    blocks: list[dict[str, Any]] = []

    parts = _chunk(result.agent_response)
    blocks.append(_section(f":robot_face: *FaultMaven*\n{parts[0]}"))
    for extra in parts[1:]:
        blocks.append(_section(extra))

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
        else:
            text_actions.append(action)

    terminal = (result.case_state or "").lower() in _TERMINAL_STATES

    if evidence:
        lines = "\n".join(_format_evidence(a) for a in evidence)
        blocks.append(
            _section(f":mag: *To move forward, FaultMaven needs:*\n{lines}")
        )

    if text_actions:
        lines = "\n".join(_format_action(a) for a in text_actions)
        blocks.append(_section(f"*Suggested next steps*\n{lines}"))

    # action_id must be unique within a message; suffix each button by index.
    # The handler matches the shared prefix (see SUGGESTED_ACTION_PATTERN).
    for index, button in enumerate(buttons):
        button["action_id"] = f"{SUGGESTED_ACTION_ID}:{index}"
    # Slack allows at most 5 elements per actions block.
    for i in range(0, len(buttons), 5):
        blocks.append({"type": "actions", "elements": buttons[i : i + 5]})

    context: list[dict[str, Any]] = []
    if result.case_state:
        context.append(
            {"type": "mrkdwn", "text": f"State: `{result.case_state}`"}
        )
    if result.turn_number is not None:
        context.append({"type": "mrkdwn", "text": f"Turn {result.turn_number}"})
    if evidence and not terminal:
        context.append(
            {"type": "mrkdwn", "text": "Investigation in progress — gathering evidence"}
        )
    if context:
        blocks.append({"type": "context", "elements": context})

    return blocks
