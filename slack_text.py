"""Render a Slack message into readable text for the FaultMaven engine.

Monitoring alerts (Datadog/PagerDuty/Grafana) are rich Block Kit or legacy
attachments — ``message.text`` is usually just a short fallback. The real signal
(service, metric, threshold, severity, links) lives in ``message.blocks`` /
``message.attachments``. We walk those into plain text so the shortcut seed isn't
a useless stub (design §5.4 / §4.3).

Parsing is defensive: these payloads are external and occasionally malformed
(e.g. a hand-built message with ``text`` as a string rather than a composition
object), so a bad shape degrades to the plain ``text`` rather than crashing.

TRANSPORT TRANSPARENCY: what we hand the engine must be what the user sees on
screen — byte-identical to what they would have pasted into the Copilot from
the same alert. Slack's wire format is not that: it carries entity markup
(``<url|label>``, ``<@U123>``, ``<!here>``) and escapes ``&`` ``<`` ``>`` in
mrkdwn. Left raw, an Alertmanager title reaches the engine as
``<http://…/#/alerts|[FIRING:1] etcdInsufficientMembers …>`` — the state marker
the investigation turns on, buried in markup nothing downstream parses — and a
log line containing ``&`` or ``<`` arrives corrupted. :func:`_render` undoes
both. It only *removes* transport encoding; it never adds framing, so the
engine cannot tell Slack from a Copilot paste.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# A Slack entity: <destination> or <destination|label>. The brackets are
# LITERAL here — genuine ``<`` in message content arrives as ``&lt;`` — which is
# why entities are unwrapped BEFORE the escapes are undone. Reverse that order
# and a log line reading ``&lt;stdin&gt;`` would be mistaken for an entity.
#
# ``\n`` is excluded from the body: Slack's own parser does not span lines, and
# allowing it here turned an unrelated ``<`` and a later ``>`` into a
# pseudo-entity. With a ``|`` between them everything before it — including the
# line break — was DELETED, silently merging two log lines and dropping the text
# in between. Data loss, not a rendering nit.
_ENTITY = re.compile(r"<([^<>\n]*)>")


def _unwrap_entities(text: str) -> str:
    """Replace Slack entity markup with the text Slack renders on screen."""

    def replace(match: "re.Match[str]") -> str:
        body, _, label = match.group(1).partition("|")
        if body.startswith("!"):
            # Broadcasts (<!here>), subteams (<!subteam^S1|@oncall>), and
            # formatters (<!date^…|Aug 4>) all prefer their label when present.
            return label or "@" + body[1:].split("^")[0]
        if body.startswith(("@", "#")):
            # User/channel refs: keep the sigil, prefer the readable label.
            # Unlabelled ones stay as the raw ID — resolving it would need an
            # API call, and a wrong guess is worse than a visible ID.
            return body[0] + (label or body[1:])
        # A link: the label IS what the user sees; the bare URL when unlabelled.
        return label or body

    return _ENTITY.sub(replace, text)


def _unescape(text: str) -> str:
    """Undo Slack's mrkdwn escaping — exactly the three entities it encodes.

    ``&amp;`` MUST be last: unescaping it first would turn a literal
    ``&amp;lt;`` (the user typed ``&lt;``) into ``<``.
    """

    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def render_slack_text(text: str) -> str:
    """Slack mrkdwn wire form → the text as displayed.

    Public because every inbound path needs it, not just this module's block
    walk: ``rendering.clean_mention`` applies it to the @mention / DM / thread
    routes so a forwarded alert reaches the engine the same way whichever one
    carried it.
    """

    return _unescape(_unwrap_entities(text))


# Internal alias kept for this module's own call sites.
_render = render_slack_text


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
            parts.append(_render(str(message["text"])))

        return "\n".join(p.strip() for p in parts if p and p.strip())
    except Exception:  # noqa: BLE001 — never let a malformed payload crash a turn
        logger.warning("message_to_text failed; using plain text", exc_info=True)
        # The degraded path renders too: a half-parsed payload is still Slack
        # wire form, and handing the engine raw markup is the failure this
        # module exists to prevent.
        return _render(str(message.get("text") or "")).strip()


def _composition_text(obj: Any) -> str | None:
    """A Block Kit text field may be a {type,text} object or (loosely) a string.

    ``mrkdwn`` objects carry Slack's wire encoding and are rendered.
    ``plain_text`` is LITERAL by Slack's contract and gets no transform at all —
    not even entity-unwrapping. ``<no value>``, ``<nil>`` and ``<stdin>`` are
    ordinary strings there, and stripping their brackets corrupts the content
    while also breaking the cross-shape invariant this module exists to hold
    (mrkdwn ``&lt;stdin&gt;`` renders as ``<stdin>``, so plain_text ``<stdin>``
    must too). An app that puts a real entity in plain_text has made a mistake;
    honouring the declared type is still the right response.

    A loose bare string is treated as mrkdwn, which is what apps that emit one mean.
    """

    if isinstance(obj, dict):
        text = obj.get("text")
        if not isinstance(text, str):
            return None
        if obj.get("type") == "plain_text":
            return text
        return _render(text)
    if isinstance(obj, str):
        return _render(obj)
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
                # Context elements are composition objects (or an image, which
                # has no text) — same mrkdwn/plain_text split as section text.
                element_text = _composition_text(element)
                if element_text:
                    out.append(element_text)
    elif block_type == "rich_text":
        rich = _rich_text(block)
        if rich:
            out.append(rich)
    return out


def _rich_text(block: dict[str, Any]) -> str:
    """Flatten a rich_text block's nested text/link elements, one line/section.

    rich_text is already structured — its ``text`` elements carry literal
    characters, not Slack's escaped wire form — so nothing here is rendered.
    Link elements take their label, matching what :func:`_unwrap_entities` does
    for the ``<url|label>`` form the flat-text paths carry, so an alert title
    reaches the engine the same way whichever shape the sender chose.
    """

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
    # Legacy attachments carry mrkdwn wire form throughout (this is the shape
    # Alertmanager's Slack receiver emits), so every string here is rendered.
    out: list[str] = []
    title_index: int | None = None
    for key in ("pretext", "title", "text"):
        value = attachment.get(key)
        if value:
            if key == "title":
                title_index = len(out)
            out.append(_render(str(value)))
    # The title's destination, on its own line under the title it annotates.
    #
    # Slack renders a linked title as a hyperlink, so unlike an INLINE entity
    # — where the label is prose the author wrote INSTEAD of the URL, and
    # ``_unwrap_entities`` is right to keep the label alone — ``title_link``
    # is a separate structural field whose URL is nowhere in the title text.
    # For Alertmanager's default Slack template it is the only actionable
    # field in the whole notification, and that template always sets ``title``,
    # so ``fallback`` (which renders it as ``<title> | <url>``, i.e. the
    # SENDER's own plain-text form, URL included) is never reached and the
    # link was dropped every time.
    #
    # This is the only place it can be fixed: the FaultMaven API is
    # client-agnostic — it cannot tell this agent from the Copilot and must
    # not have to — so there is no side channel for "the alert also had a
    # link". Anything a Slack reader can reach from the message has to arrive
    # in the content or not at all.
    #
    # Emitted bare (no invented punctuation), which is also how
    # ``_unwrap_entities`` degrades an UNLABELLED link. Gated on a title
    # because Slack ignores ``title_link`` without one, and skipped when some
    # other field already carried the URL so a forwarded alert never arrives
    # with it twice.
    link = attachment.get("title_link")
    if link and title_index is not None:
        url = _render(str(link)).strip()
        if url and not any(url in part for part in out):
            out.insert(title_index + 1, url)
    for field in attachment.get("fields") or []:
        if not isinstance(field, dict):
            continue
        title = _render(str(field.get("title") or "")).strip()
        value = _render(str(field.get("value") or "")).strip()
        if title and value:
            out.append(f"{title}: {value}")
        elif title or value:
            out.append(title or value)
    # Fall back to the flat fallback string only if the structured parts were empty.
    if not out and attachment.get("fallback"):
        out.append(_render(str(attachment["fallback"])))
    return out
