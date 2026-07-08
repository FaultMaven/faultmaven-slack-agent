"""Markdown → Slack mrkdwn conversion (the engine emits standard Markdown; Slack
speaks a different dialect, so raw Markdown renders its syntax literally)."""

from __future__ import annotations

from slack_mrkdwn import to_mrkdwn


def test_bold_double_to_single_asterisk():
    assert to_mrkdwn("the **root cause** here") == "the *root cause* here"


def test_double_underscore_identifiers_are_not_bolded():
    # __ is deliberately NOT bold: Python dunders / snake paths must survive.
    assert to_mrkdwn("the __init__ method") == "the __init__ method"
    assert to_mrkdwn("see __main__ and __str__") == "see __main__ and __str__"


def test_italic_to_underscore():
    assert to_mrkdwn("it was *OOMKilled* again") == "it was _OOMKilled_ again"


def test_bold_italic_triple():
    assert to_mrkdwn("that is ***critical*** now") == "that is *_critical_* now"


def test_bold_heading_is_not_double_wrapped():
    # `### **Next Steps**` must become one clean bold line, not literal `**...**`.
    assert to_mrkdwn("### **Next Steps**") == "*Next Steps*"
    assert to_mrkdwn("## **Deployment logs**") == "*Deployment logs*"


def test_titled_link_drops_the_title():
    assert to_mrkdwn('[docs](https://x "the title")') == "<https://x|docs>"


def test_control_chars_in_input_do_not_crash_or_spoof():
    # Sentinels leaking in from input are stripped, so restore can't IndexError.
    assert to_mrkdwn("literal \x00 5 \x01 here") == "literal  5  here"


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
