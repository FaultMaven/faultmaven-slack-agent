"""App Home view — a valid, on-message Block Kit home."""

from __future__ import annotations

from listeners.home import build_home_view


def _mrkdwn_text(view) -> str:
    return "\n".join(
        b["text"]["text"] for b in view["blocks"] if b.get("type") == "section"
    )


def test_home_view_is_a_valid_home_view():
    view = build_home_view()
    assert view["type"] == "home"
    assert view["blocks"], "home view must have blocks"
    # Every section carries non-empty mrkdwn (Slack rejects empty section text).
    for b in view["blocks"]:
        if b.get("type") == "section":
            assert b["text"]["type"] == "mrkdwn"
            assert b["text"]["text"].strip()


def test_home_view_carries_the_positioning():
    body = _mrkdwn_text(build_home_view())
    # The four pillars and the "start here" pointer are present.
    for phrase in ("goal-driven", "methodical", "evidence-based", "self-learning"):
        assert phrase in body
    # Home is the orientation surface — it teaches all three entry points.
    assert "Chat" in body
    assert "@mention" in body
    assert "Ask FaultMaven" in body


def test_home_view_says_where_data_goes():
    """Message text — code blocks included — goes to the engine as a claim; only
    a snippet or a file is data. Home is where that is said up front (#100)."""

    body = _mrkdwn_text(build_home_view())
    assert "Where data goes" in body
    assert "Create a text snippet" in body
    assert "code blocks included" in body
    # Nothing on Home may tell the user to paste data into the message.
    assert "paste" not in body.lower()
