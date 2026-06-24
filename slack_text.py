"""Render a Slack message into readable text for the FaultMaven engine.

Monitoring alerts (Datadog/PagerDuty/Grafana) are rich Block Kit or legacy
attachments — ``message.text`` is usually just a short fallback. The real signal
(service, metric, threshold, severity, links) lives in ``message.blocks`` /
``message.attachments``. We walk those into plain text so the shortcut seed isn't
a useless stub (design §5.4 / §4.3).
"""

from __future__ import annotations

from typing import Any


def message_to_text(message: dict[str, Any]) -> str:
    """Extract readable text from a Slack message (blocks + attachments + text)."""

    parts: list[str] = []

    for block in message.get("blocks") or []:
        parts.extend(_block_text(block))

    for attachment in message.get("attachments") or []:
        parts.extend(_attachment_text(attachment))

    # Fall back to the plain text only if nothing richer was found.
    if not parts and message.get("text"):
        parts.append(str(message["text"]))

    text = "\n".join(p.strip() for p in parts if p and p.strip())
    return text or str(message.get("text") or "").strip()


def _block_text(block: dict[str, Any]) -> list[str]:
    block_type = block.get("type")
    out: list[str] = []
    if block_type in ("section", "header"):
        text = (block.get("text") or {}).get("text")
        if text:
            out.append(text)
        for field in block.get("fields") or []:
            field_text = field.get("text")
            if field_text:
                out.append(field_text)
    elif block_type == "context":
        for element in block.get("elements") or []:
            text = element.get("text")
            if text:
                out.append(text)
    elif block_type == "rich_text":
        rich = _rich_text(block)
        if rich:
            out.append(rich)
    return [o for o in out if o]


def _rich_text(block: dict[str, Any]) -> str:
    """Flatten a rich_text block's nested text/link elements."""

    chunks: list[str] = []
    for section in block.get("elements") or []:
        for element in section.get("elements") or []:
            etype = element.get("type")
            if etype == "text" and element.get("text"):
                chunks.append(element["text"])
            elif etype == "link":
                chunks.append(element.get("text") or element.get("url") or "")
        chunks.append("\n")
    return "".join(chunks).strip()


def _attachment_text(attachment: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("pretext", "title", "text"):
        value = attachment.get(key)
        if value:
            out.append(str(value))
    for field in attachment.get("fields") or []:
        title = (field.get("title") or "").strip()
        value = (field.get("value") or "").strip()
        if title and value:
            out.append(f"{title}: {value}")
        elif title or value:
            out.append(title or value)
    # Only fall back to the flat fallback string if the structured parts were empty.
    if not out and attachment.get("fallback"):
        out.append(str(attachment["fallback"]))
    return out
