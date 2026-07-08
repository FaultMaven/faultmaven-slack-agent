"""Convert standard Markdown (what the FaultMaven engine emits) to Slack *mrkdwn*.

Slack does **not** speak CommonMark. It has its own dialect:

| Markdown            | Slack mrkdwn        |
|---------------------|---------------------|
| ``**bold**``        | ``*bold*``          |
| ``*italic*`` / ``_i_`` | ``_italic_``     |
| ``# Heading``       | ``*Heading*`` (no headings) |
| ``- item`` / ``* item`` | ``• item``      |
| ``[text](url)``     | ``<url|text>``      |
| ``~~strike~~``      | ``~strike~``        |

Posting raw Markdown into a Slack ``mrkdwn`` block renders the *syntax literally*
(``**Deployment logs:**`` shows the asterisks). This translates the common LLM
constructs. Code spans / fenced blocks are protected first so their contents are
never rewritten; language tags on fences are dropped (Slack ignores them).
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")
_STRIKE = re.compile(r"~~(.+?)~~")
_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*$")
_BULLET = re.compile(r"(?m)^(\s*)[-*+]\s+")

# Control-char sentinels that won't occur in real text.
_HOLD_L, _HOLD_R = "\x00", "\x01"          # delimit a stashed-code index
_BOLD_L, _BOLD_R = "\x02", "\x03"          # mark bold spans across the italic pass
_HOLD_RE = re.compile(_HOLD_L + r"(\d+)" + _HOLD_R)


def to_mrkdwn(text: str) -> str:
    """Best-effort Markdown → Slack mrkdwn. Leaves already-mrkdwn text intact."""

    if not text:
        return text

    stash: list[str] = []

    def _hold(value: str) -> str:
        stash.append(value)
        return f"{_HOLD_L}{len(stash) - 1}{_HOLD_R}"

    # 1. Protect code so nothing inside it is rewritten.
    text = _FENCE.sub(lambda m: _hold("```\n" + m.group(1).rstrip("\n") + "\n```"), text)
    text = _INLINE_CODE.sub(lambda m: _hold("`" + m.group(1) + "`"), text)

    # 2. Inline formatting.
    text = _LINK.sub(r"<\2|\1>", text)
    text = _BOLD.sub(lambda m: f"{_BOLD_L}{m.group(1) or m.group(2)}{_BOLD_R}", text)
    text = _ITALIC.sub(r"_\1_", text)
    text = text.replace(_BOLD_L, "*").replace(_BOLD_R, "*")
    text = _STRIKE.sub(r"~\1~", text)

    # 3. Block formatting (Slack has no headings; use bold. Bullets → •).
    text = _HEADING.sub(r"*\1*", text)
    text = _BULLET.sub(r"\1• ", text)

    # 4. Restore protected code.
    text = _HOLD_RE.sub(lambda m: stash[int(m.group(1))], text)
    return text
