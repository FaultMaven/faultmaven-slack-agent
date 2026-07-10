"""Convert standard Markdown (what the FaultMaven engine emits) to Slack *mrkdwn*.

Slack does **not** speak CommonMark. It has its own dialect:

| Markdown            | Slack mrkdwn        |
|---------------------|---------------------|
| ``**bold**``        | ``*bold*``          |
| ``*italic*``        | ``_italic_``        |
| ``***both***``      | ``*_both_*``        |
| ``# Heading``       | ``*Heading*`` (no headings) |
| ``- item`` / ``* item`` | ``• item``      |
| ``[text](url)``     | ``<url|text>``      |
| ``~~strike~~``      | ``~strike~``        |

Posting raw Markdown into a Slack ``mrkdwn`` block renders the *syntax literally*
(``**Deployment logs:**`` shows the asterisks). This translates the common LLM
constructs. Deliberate non-goals / known limits (all render as harmless literal
text, never a crash): ``__bold__`` is left alone so Python dunders like
``__init__`` aren't mangled; a URL containing ``)`` truncates; tables and HTML
pass through (Slack renders neither anyway).

Ordering matters: code and links are *stashed* first so nothing rewrites their
insides; headings are neutralised *before* the emphasis passes so a bold heading
doesn't get double-wrapped.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
# [text](url) or [text](url "title") or [text](<url>); URL excludes space, >, |.
_LINK = re.compile(r"\[([^\]]+)\]\(\s*<?([^\s>|]+?)>?(?:\s+\"[^\"]*\")?\s*\)")
_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*$")
_BOLD_ITALIC = re.compile(r"\*\*\*(.+?)\*\*\*")
_BOLD = re.compile(r"\*\*(.+?)\*\*")  # only ** — __ is left alone (protects dunders)
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")
_STRIKE = re.compile(r"~~(.+?)~~")
_BULLET = re.compile(r"(?m)^(\s*)[-*+]\s+")
# CommonMark autolink: <https://url> with NO label. Safe to keep live through
# the escape pass — with no label there is nothing to spoof (the rendered text
# IS the destination), unlike <url|label> which stays neutralized.
_AUTOLINK = re.compile(r"<(https?://[^\s<>|]+)>")

# Control-char sentinels that won't occur in real text (any that leak in from the
# input are stripped up front, so a stash index can never be spoofed).
_HOLD_L, _HOLD_R = "\x00", "\x01"          # delimit a stashed (code/link) index
_BOLD_L, _BOLD_R = "\x02", "\x03"          # mark bold spans across the emphasis passes
_HOLD_RE = re.compile(_HOLD_L + r"(\d+)" + _HOLD_R)
_STRIP_SENTINELS = str.maketrans({c: None for c in (_HOLD_L, _HOLD_R, _BOLD_L, _BOLD_R)})


def escape_mrkdwn(text: str) -> str:
    """Escape Slack's entity openers (``&`` ``<``) in untrusted text.

    Slack parses ``<...>`` sequences — ``<!channel>`` broadcasts, ``<@U...>``
    mentions, ``<url|label>`` links — in every mrkdwn field, including code
    spans. The engine's reply quotes untrusted evidence (pasted alerts, logs)
    verbatim, so without this a crafted log line echoed by the LLM could ping
    the whole channel or spoof a link under the bot's identity.

    ``>`` is deliberately left alone: an entity can only ever start with ``<``
    (which is always escaped here), so a bare ``>`` is inert — and escaping it
    would break the ``> quote`` blockquotes the engine legitimately emits.
    """

    return text.replace("&", "&amp;").replace("<", "&lt;")


def to_mrkdwn(text: str) -> str:
    """Best-effort Markdown → Slack mrkdwn. Leaves already-mrkdwn text intact."""

    if not text:
        return text
    text = text.translate(_STRIP_SENTINELS)  # never let input spoof a sentinel

    stash: list[str] = []

    def _hold(value: str) -> str:
        stash.append(value)
        return f"{_HOLD_L}{len(stash) - 1}{_HOLD_R}"

    # 0. Keep label-less autolinks (<https://url> — common LLM Markdown) live
    #    by stashing them ahead of the escape; Slack wants & inside entities
    #    escaped. Then neutralize Slack entities in everything else BEFORE any
    #    conversion: this path is untrusted (LLM output seeded from user
    #    evidence). The <url|text> links this converter emits are built after
    #    the escape, so they stay live too.
    text = _AUTOLINK.sub(lambda m: _hold(f"<{m.group(1).replace('&', '&amp;')}>"), text)
    text = escape_mrkdwn(text)

    # 1. Protect code and links so no later pass rewrites their insides.
    text = _FENCE.sub(lambda m: _hold("```\n" + m.group(1).rstrip("\n") + "\n```"), text)
    # An unterminated multi-line fence (e.g. a token-cap-truncated reply) runs
    # to EOF — stash it too, or the emphasis/bullet passes would rewrite its
    # contents while Slack still renders it as a code block. Same-line pairs
    # (```cmd```) are left for the inline-code pass, which already stashes them.
    dangling = text.find("```")
    if dangling != -1:
        after = text[dangling + 3 :]
        newline = after.find("\n")
        if newline != -1 and "```" not in after[:newline]:
            body = after[newline + 1 :]
            text = text[:dangling] + _hold("```\n" + body.rstrip("\n") + "\n```")
    text = _INLINE_CODE.sub(lambda m: _hold("`" + m.group(1) + "`"), text)
    text = _LINK.sub(lambda m: _hold(f"<{m.group(2)}|{m.group(1)}>"), text)

    # 2. Headings BEFORE emphasis: strip inner * so a bold heading isn't
    #    double-wrapped, and mark the line bold via sentinels (Slack has no #).
    text = _HEADING.sub(lambda m: _BOLD_L + m.group(1).replace("*", "").strip() + _BOLD_R, text)

    # 3. Emphasis: bold-italic, then bold, then italic (sentinels shield bold
    #    from the italic pass so their asterisks never cross).
    text = _BOLD_ITALIC.sub(lambda m: f"{_BOLD_L}_{m.group(1)}_{_BOLD_R}", text)
    text = _BOLD.sub(lambda m: f"{_BOLD_L}{m.group(1)}{_BOLD_R}", text)
    text = _ITALIC.sub(r"_\1_", text)
    text = text.replace(_BOLD_L, "*").replace(_BOLD_R, "*")
    text = _STRIKE.sub(r"~\1~", text)

    # 4. Bullets, then restore stashes (bounds-checked — never IndexError).
    text = _BULLET.sub(r"\1• ", text)
    text = _HOLD_RE.sub(
        lambda m: stash[int(m.group(1))] if int(m.group(1)) < len(stash) else m.group(0),
        text,
    )
    return text
