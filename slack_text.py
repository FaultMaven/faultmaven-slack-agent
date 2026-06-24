"""Render a Slack message into readable text for the FaultMaven engine.

Monitoring alerts (Datadog/PagerDuty/Grafana) are rich Block Kit or legacy
attachments — ``message.text`` is usually just a short fallback. The real signal
(service, metric, threshold, severity, links) lives in ``message.blocks`` /
``message.attachments``. We walk those into plain text so the shortcut seed isn't
a useless stub (design §5.4 / §4.3).

Parsing is defensive: these payloads are external and occasionally malformed
(e.g. a hand-built message with ``text`` as a string rather than a composition
object), so a bad shape degrades to the plain ``text`` rather than crashing.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def message_to_text(message: dict[str, Any]) -> str:
    """Extract readable text from a Slack message (blocks + attachments + text)."""

    try:
        parts: list[str] = []

        for block in message.get("blocks") or []:
            if isinstance(block, dict):
                parts.extend(_block_text(block))

        for attachment in message.get("attachments") or []:
            if isinstance(attachment, dict):
                parts.extend(_attachment_text(attachment))

        # Fall back to the plain text only if nothing richer was found.
        if not parts and message.get("text"):
            parts.append(str(message["text"]))

        return "\n".join(p.strip() for p in parts if p and p.strip())
    except Exception:  # noqa: BLE001 — never let a malformed payload crash a turn
        logger.warning("message_to_text failed; using plain text", exc_info=True)
        return str(message.get("text") or "").strip()


def _composition_text(obj: Any) -> str | None:
    """A Block Kit text field may be a {type,text} object or (loosely) a string."""

    if isinstance(obj, dict):
        text = obj.get("text")
        return text if isinstance(text, str) else None
    if isinstance(obj, str):
        return obj
    return None


def _block_text(block: dict[str, Any]) -> list[str]:
    block_type = block.get("type")
    out: list[str] = []
    if block_type in ("section", "header"):
        text = _composition_text(block.get("text"))
        if text:
            out.append(text)
        for field in block.get("fields") or []:
            if isinstance(field, dict):
                field_text = _composition_text(field)
                if field_text:
                    out.append(field_text)
    elif block_type == "context":
        for element in block.get("elements") or []:
            if isinstance(element, dict):
                element_text = element.get("text")
                if isinstance(element_text, str) and element_text:
                    out.append(element_text)
    elif block_type == "rich_text":
        rich = _rich_text(block)
        if rich:
            out.append(rich)
    return out


def _rich_text(block: dict[str, Any]) -> str:
    """Flatten a rich_text block's nested text/link elements, one line/section."""

    lines: list[str] = []
    for section in block.get("elements") or []:
        if not isinstance(section, dict):
            continue
        chunks: list[str] = []
        for element in section.get("elements") or []:
            if not isinstance(element, dict):
                continue
            etype = element.get("type")
            if etype == "text" and isinstance(element.get("text"), str):
                chunks.append(element["text"])
            elif etype == "link":
                chunks.append(element.get("text") or element.get("url") or "")
        line = "".join(chunks).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _attachment_text(attachment: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("pretext", "title", "text"):
        value = attachment.get(key)
        if value:
            out.append(str(value))
    for field in attachment.get("fields") or []:
        if not isinstance(field, dict):
            continue
        title = str(field.get("title") or "").strip()
        value = str(field.get("value") or "").strip()
        if title and value:
            out.append(f"{title}: {value}")
        elif title or value:
            out.append(title or value)
    # Fall back to the flat fallback string only if the structured parts were empty.
    if not out and attachment.get("fallback"):
        out.append(str(attachment["fallback"]))
    return out
