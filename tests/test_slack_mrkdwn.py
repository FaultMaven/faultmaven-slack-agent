"""Markdown → Slack mrkdwn conversion (the engine emits standard Markdown; Slack
speaks a different dialect, so raw Markdown renders its syntax literally)."""

from __future__ import annotations

from slack_mrkdwn import to_mrkdwn


def test_bold_double_to_single_asterisk():
    assert to_mrkdwn("the **root cause** here") == "the *root cause* here"
    assert to_mrkdwn("__also bold__") == "*also bold*"


def test_italic_to_underscore():
    assert to_mrkdwn("it was *OOMKilled* again") == "it was _OOMKilled_ again"


def test_heading_becomes_bold_line():
    assert to_mrkdwn("## Next steps") == "*Next steps*"
    assert to_mrkdwn("### Deep dive ###") == "*Deep dive*"


def test_bullets_become_slack_bullets():
    assert to_mrkdwn("- one\n- two") == "• one\n• two"
    assert to_mrkdwn("* a\n+ b") == "• a\n• b"


def test_the_reported_metadata_example():
    # The exact line the user saw rendered as literal syntax.
    src = "*   **Deployment logs:** Output from your CI/CD pipeline."
    assert to_mrkdwn(src) == "• *Deployment logs:* Output from your CI/CD pipeline."


def test_links_become_slack_links():
    assert to_mrkdwn("see [runbook](https://kb/oom) now") == "see <https://kb/oom|runbook> now"


def test_inline_code_is_protected():
    assert to_mrkdwn("run `kubectl get pods`") == "run `kubectl get pods`"
    # Markdown-looking text inside code is NOT rewritten.
    assert to_mrkdwn("`a **b** c`") == "`a **b** c`"


def test_fenced_code_is_protected_and_language_dropped():
    out = to_mrkdwn("```python\nx = 1  # **keep**\n```")
    assert out == "```\nx = 1  # **keep**\n```"


def test_numbered_lists_and_quotes_pass_through():
    assert to_mrkdwn("1. first\n2. second") == "1. first\n2. second"
    assert to_mrkdwn("> a quote") == "> a quote"


def test_plain_arithmetic_is_not_mangled():
    # Spaced asterisks are multiplication, not emphasis.
    assert to_mrkdwn("2 * 3 = 6 and x * y") == "2 * 3 = 6 and x * y"


def test_empty_and_none_safe():
    assert to_mrkdwn("") == ""
    assert to_mrkdwn(None) is None  # type: ignore[arg-type]
