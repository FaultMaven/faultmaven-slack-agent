"""Block Kit rendering — chunking, actions, and the §7.3 soundness behavior."""

from __future__ import annotations

from faultmaven.client import TurnResult
from rendering import build_turn_blocks, clean_mention


def _sections(blocks) -> list[str]:
    return [b["text"]["text"] for b in blocks if b["type"] == "section"]


def _context_texts(blocks) -> list[str]:
    out: list[str] = []
    for b in blocks:
        if b["type"] == "context":
            out.extend(e["text"] for e in b["elements"])
    return out


def test_clean_mention_strips_mentions():
    assert clean_mention("<@U123> help me") == "help me"
    assert clean_mention("") == ""
    assert clean_mention(None) == ""  # type: ignore[arg-type]


def test_long_response_split_within_section_limit():
    result = TurnResult(agent_response="x" * 7000)
    sections = _sections(build_turn_blocks(result))
    assert len(sections) > 1
    assert all(len(s) <= 3000 for s in sections)


def test_short_response_is_single_section():
    result = TurnResult(agent_response="all good")
    assert len(_sections(build_turn_blocks(result))) == 1


def test_reply_has_no_redundant_faultmaven_header():
    # Slack already shows the app name + icon above every message, so the reply
    # is the response text itself — no ":robot_face: *FaultMaven*" prefix.
    sections = _sections(build_turn_blocks(TurnResult(agent_response="root cause found")))
    assert sections[0] == "root cause found"
    assert ":robot_face:" not in sections[0] and "*FaultMaven*" not in sections[0]


def test_response_markdown_is_converted_to_slack_mrkdwn():
    # The engine emits standard Markdown; the reply must be Slack mrkdwn.
    result = TurnResult(agent_response="The **root cause** is a\n- bad deploy")
    body = _sections(build_turn_blocks(result))[0]
    assert "*root cause*" in body and "**" not in body
    assert "• bad deploy" in body


# -- §7.3 soundness behavior --------------------------------------------------
def test_evidence_ask_gets_its_own_prominent_section():
    result = TurnResult(
        agent_response="I need more data.",
        case_state="investigating",
        suggested_actions=[
            {"type": "EVIDENCE", "label": "paste the DB connection logs",
             "hints": "from 14:00-14:10"},
            {"type": "RUN", "payload": "kubectl get pods"},
        ],
    )
    blocks = build_turn_blocks(result)
    joined = "\n".join(_sections(blocks))

    # Evidence ask is surfaced prominently, separate from other suggestions.
    assert "FaultMaven needs" in joined
    assert "paste the DB connection logs" in joined
    assert "from 14:00-14:10" in joined  # hint included
    assert "Suggested next steps" in joined  # RUN still rendered
    # The evidence section must come before the generic suggestions section.
    assert joined.index("FaultMaven needs") < joined.index("Suggested next steps")


def test_open_investigation_with_evidence_shows_in_progress_cue():
    result = TurnResult(
        agent_response="still digging",
        case_state="investigating",
        suggested_actions=[{"type": "EVIDENCE", "label": "share the config"}],
    )
    ctx = _context_texts(build_turn_blocks(result))
    assert any("in progress" in t.lower() for t in ctx)


def test_terminal_state_has_no_in_progress_cue():
    result = TurnResult(
        agent_response="root cause: stale DB host",
        case_state="resolved",
        suggested_actions=[{"type": "EVIDENCE", "label": "leftover ask"}],
    )
    ctx = _context_texts(build_turn_blocks(result))
    assert not any("in progress" in t.lower() for t in ctx)


def test_no_evidence_means_no_needs_section():
    result = TurnResult(
        agent_response="done",
        case_state="resolved",
        suggested_actions=[{"type": "DECIDE", "label": "close the case"}],
    )
    joined = "\n".join(_sections(build_turn_blocks(result)))
    assert "FaultMaven needs" not in joined
    assert "close the case" in joined


def test_context_shows_state_and_turn():
    result = TurnResult(agent_response="x", case_state="inquiry", turn_number=3)
    ctx = _context_texts(build_turn_blocks(result))
    assert any("inquiry" in t for t in ctx)
    assert any("Turn 3" in t for t in ctx)
