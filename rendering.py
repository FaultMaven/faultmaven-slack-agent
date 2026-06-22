"""Render a :class:`~faultmaven.client.TurnResult` into Slack Block Kit.

P0 renders a clean, scannable card: the agent's message, suggested next steps,
and a context line (case state / turn). Suggested actions render as text in P0;
they become interactive buttons in P2. (Hypotheses and confidence are not part
of the turn response — they surface via the P2 reasoning timeline — so they are
not rendered here.)
"""

from __future__ import annotations

import re
from typing import Any

from faultmaven.client import TurnResult

# Slack section ``mrkdwn`` text tops out at 3000 chars; stay safely under.
_SECTION_LIMIT = 2900
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
# Case states in which the investigation has concluded.
_TERMINAL_STATES = {"resolved", "closed"}


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


def _partition_actions(
    actions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split suggested actions into (evidence_asks, everything_else)."""

    evidence: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for action in actions:
        a_type = (action.get("type") or action.get("action_type") or "").upper()
        (evidence if a_type == "EVIDENCE" else other).append(action)
    return evidence, other


def _format_evidence(action: dict[str, Any]) -> str:
    """Render an evidence ask, appending acquisition hints when present."""

    label = _action_label(action)
    hints = action.get("hints")
    if isinstance(hints, str) and hints.strip():
        return f"• {label} — _{hints.strip()}_"
    return f"• {label}"


def _format_action(action: dict[str, Any]) -> str:
    """Render a non-evidence suggested action as an mrkdwn bullet, by type."""

    a_type = (action.get("type") or action.get("action_type") or "").upper()
    label = _action_label(action)
    if a_type == "RUN":
        command = action.get("payload") or action.get("body") or label
        return f"• *Run:* `{command}`"
    if a_type == "DECIDE":
        return f"• :white_check_mark: *Decision:* {label}"
    return f"• {label}"


def build_turn_blocks(result: TurnResult) -> list[dict[str, Any]]:
    """Render a turn result as Block Kit blocks.

    Honors FaultMaven's soundness posture (design §7.3): while the case is still
    open and the engine is asking for data, the missing-data request is surfaced
    in its own prominent block — never buried among other suggestions — and the
    context line states the investigation is ongoing so an in-progress turn is
    not mistaken for a verdict.
    """

    blocks: list[dict[str, Any]] = []

    parts = _chunk(result.agent_response)
    blocks.append(_section(f":robot_face: *FaultMaven*\n{parts[0]}"))
    for extra in parts[1:]:
        blocks.append(_section(extra))

    evidence, other_actions = _partition_actions(result.suggested_actions)
    terminal = (result.case_state or "").lower() in _TERMINAL_STATES

    if evidence:
        lines = "\n".join(_format_evidence(a) for a in evidence)
        blocks.append(
            _section(f":mag: *To move forward, FaultMaven needs:*\n{lines}")
        )

    if other_actions:
        lines = "\n".join(_format_action(a) for a in other_actions)
        blocks.append(_section(f"*Suggested next steps*\n{lines}"))

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
