"""Block Kit rendering — chunking, actions, and the §7.3 soundness behavior."""

from __future__ import annotations

import json

from faultmaven.client import TurnResult
from rendering import build_turn_blocks, clean_mention


def _sections(blocks) -> list[str]:
    return [b["text"]["text"] for b in blocks if b["type"] == "section"]


def _buttons(blocks) -> list[dict]:
    out: list[dict] = []
    for b in blocks:
        if b["type"] == "actions":
            out.extend(b["elements"])
    return out


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


def test_case_id_shown_quietly_on_opening_reply():
    # thread = case: the case pointer rides once in the quiet context row so the
    # record is locatable in the backend/dashboard.
    result = TurnResult(agent_response="opened", case_state="inquiry", turn_number=1)
    ctx = _context_texts(build_turn_blocks(result, case_id="case_abc123"))
    assert any("case_abc123" in t for t in ctx)
    # It's a context (gray) element, never a prominent section.
    assert all("case_abc123" not in s for s in _sections(build_turn_blocks(result, case_id="case_abc123")))


def test_case_id_omitted_on_later_turns():
    # Root-only: without case_id (every non-opening turn), nothing is stamped.
    result = TurnResult(agent_response="next", case_state="investigating", turn_number=4)
    ctx = _context_texts(build_turn_blocks(result))
    assert all("case_" not in t for t in ctx)


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


# -- DECIDE suggestions: clickable + legible ---------------------------------
def test_payload_only_decide_becomes_button_carrying_payload():
    # File-classification clarifications carry a `payload` but NO `intent`. They
    # must be clickable (a click submits the payload verbatim), not degrade to a
    # cryptic "Decision: Documentation" text bullet.
    result = TurnResult(
        agent_response="I couldn't confidently classify the file you uploaded.",
        case_state="investigating",
        suggested_actions=[
            {"type": "DECIDE", "label": "Documentation",
             "payload": "Treat the file as documentation or notes and analyze it.",
             "body": "Treat as documentation or notes."},
            {"type": "DECIDE", "label": "Something else",
             "payload": "Treat the file as unstructured text and try to analyze it.",
             "body": "Treat as unstructured text."},
            {"type": "RUN", "payload": "kubectl get pods"},
        ],
    )
    blocks = build_turn_blocks(result)
    buttons = _buttons(blocks)

    labels = [b["text"]["text"] for b in buttons]
    assert "Documentation" in labels and "Something else" in labels
    for b in buttons:
        value = json.loads(b["value"])
        assert value["q"].startswith("Treat the file as")  # payload, not label
        assert "it" not in value  # no intent → query-only submission
        assert b["style"] == "primary"

    # The fuller `body` rides in a description line so the choice reads with its
    # meaning (Copilot renders `label — body`); the old bare-label bullet dropped it.
    joined = "\n".join(_sections(blocks))
    assert "Treat as documentation or notes." in joined
    assert "Treat as unstructured text." in joined
    assert "Decision: Documentation" not in joined  # confusing prefix is gone

    # RUN stays text, never a button.
    assert "kubectl get pods" in joined
    assert all("kubectl" not in b["text"]["text"] for b in buttons)


def test_intent_bearing_decide_button_still_carries_intent():
    # A state-machine gate keeps replaying its intent verbatim (user_confirmed).
    result = TurnResult(
        agent_response="Looks resolved.",
        case_state="investigating",
        suggested_actions=[{
            "type": "DECIDE", "label": "Yes, mark resolved",
            "payload": "Mark this case resolved.",
            "intent": {"type": "status_transition", "to_state": "resolved"},
        }],
    )
    buttons = _buttons(build_turn_blocks(result))
    assert len(buttons) == 1
    value = json.loads(buttons[0]["value"])
    assert value["q"] == "Mark this case resolved."
    assert value["it"] == "status_transition"
    assert value["id"]["to_state"] == "resolved"
    assert value["id"]["user_confirmed"] is True


def test_bodyless_decide_button_has_no_redundant_description():
    # A confirmation gate ("Close case") with no `body` needs no description line
    # — the button label says it all; only body-bearing choices get a line.
    result = TurnResult(
        agent_response="Looks resolved.",
        suggested_actions=[{
            "type": "DECIDE", "label": "Close case",
            "payload": "Close this case.",
            "intent": {"type": "confirmation", "confirmation_value": True},
        }],
    )
    blocks = build_turn_blocks(result)
    assert len(_buttons(blocks)) == 1
    assert "Suggested next steps" not in "\n".join(_sections(blocks))


def test_unsubmittable_decide_stays_text_not_button():
    # A bare label with neither payload nor intent has nothing to submit — it must
    # not become a button (that would send the label as a stray query); it stays
    # a visible text line (preserves test_no_evidence_means_no_needs_section).
    result = TurnResult(
        agent_response="done",
        suggested_actions=[{"type": "DECIDE", "label": "close the case"}],
    )
    blocks = build_turn_blocks(result)
    assert _buttons(blocks) == []
    assert "close the case" in "\n".join(_sections(blocks))


def test_assurance_grade_labeled_in_context(monkeypatch):
    # A held-back grade is surfaced beside the narration so the cause claim in
    # agent_response is never forwarded without its read-time label (#572/INV-28).
    result = TurnResult(
        agent_response="The root cause is connection pool exhaustion.",
        case_state="resolved",
        cause_assurance="mechanistic",
    )
    ctx = _context_texts(build_turn_blocks(result))
    assert any("mechanistic" in t for t in ctx)


def test_confirmed_grade_needs_no_label():
    # The clean top grade carries no qualifier — no assurance context element.
    result = TurnResult(
        agent_response="resolved", case_state="resolved", cause_assurance="confirmed"
    )
    ctx = _context_texts(build_turn_blocks(result))
    assert not any("assurance" in t.lower() for t in ctx)


def test_no_grade_adds_no_assurance_label():
    result = TurnResult(agent_response="still investigating", case_state="investigating")
    ctx = _context_texts(build_turn_blocks(result))
    assert not any("assurance" in t.lower() for t in ctx)


def test_overclaim_adds_caution_marker():
    result = TurnResult(
        agent_response="The root cause is definitely X.",
        case_state="resolved",
        cause_assurance="mechanistic",
        cause_overclaim=True,
    )
    ctx = _context_texts(build_turn_blocks(result))
    assert any("mechanistic" in t and "⚠" in t for t in ctx)
