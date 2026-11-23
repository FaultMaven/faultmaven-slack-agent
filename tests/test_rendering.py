"""Block Kit rendering — chunking, actions, and the §7.3 soundness behavior."""

from __future__ import annotations

import json

from faultmaven.client import TurnResult
from rendering import _button_priority, build_turn_blocks, clean_mention


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


# -- displayed turn: the investigation count, not the message clock (#64) -----
def _turn_labels(result: TurnResult) -> list[str]:
    return [t for t in _context_texts(build_turn_blocks(result)) if t.startswith("Turn")]


def test_out_of_band_aside_does_not_advance_the_displayed_turn():
    # Seven investigation turns, then a haiku: the message clock moved to 8,
    # the investigation count did not. "Turn 8" after a haiku was the symptom.
    aside = TurnResult(
        agent_response="Pods restart at dawn...",
        case_state="investigating",
        turn_number=8,
        investigation_turn=7,
    )
    assert _turn_labels(aside) == ["Turn 7"]


def test_investigation_turn_advances_the_displayed_turn():
    after_aside = TurnResult(
        agent_response="The pool is exhausted.",
        case_state="investigating",
        turn_number=9,
        investigation_turn=8,
    )
    assert _turn_labels(after_aside) == ["Turn 8"]


def test_older_server_without_investigation_turn_falls_back_to_turn_number():
    result = TurnResult(agent_response="x", case_state="investigating", turn_number=5)
    assert result.investigation_turn is None
    assert _turn_labels(result) == ["Turn 5"]


def test_aside_before_any_investigation_work_prints_no_turn():
    # investigation_turn 0 is a real answer, so it does NOT fall back to the
    # message clock; and "Turn 0" names a turn the investigation has not
    # reached, so nothing is printed (same rule as the Copilot and Dashboard).
    result = TurnResult(
        agent_response="Hello!", case_state="inquiry", turn_number=1, investigation_turn=0
    )
    ctx = _context_texts(build_turn_blocks(result))
    assert not any(t.startswith("Turn") for t in ctx)
    assert any("inquiry" in t for t in ctx)  # the state line still renders


def test_no_turn_count_at_all_prints_no_turn():
    assert _turn_labels(TurnResult(agent_response="x", case_state="inquiry")) == []


# -- DECIDE suggestions: clickable + legible ---------------------------------
def test_payload_only_decide_becomes_button_carrying_payload():
    # Runbook / regenerate-summary DECIDEs carry a `payload` but NO `intent`
    # (the engine matches those payloads verbatim on terminal turns). They
    # must be clickable (a click submits the payload verbatim), not degrade to
    # a cryptic "Decision: …" text bullet.
    result = TurnResult(
        agent_response="Case closed.",
        case_state="resolved",
        suggested_actions=[
            {"type": "DECIDE", "label": "Regenerate summary",
             "payload": "Regenerate the case summary.",
             "body": "Rebuild the closure summary from the case record."},
            {"type": "DECIDE", "label": "Draft a runbook",
             "payload": "Draft a runbook from this case.",
             "body": "Turn this resolution into a reusable runbook."},
            {"type": "RUN", "payload": "kubectl get pods"},
        ],
    )
    blocks = build_turn_blocks(result)
    buttons = _buttons(blocks)

    labels = [b["text"]["text"] for b in buttons]
    assert "Regenerate summary" in labels and "Draft a runbook" in labels
    for b in buttons:
        value = json.loads(b["value"])
        assert value["q"] in (
            "Regenerate the case summary.",
            "Draft a runbook from this case.",
        )  # payload, not label
        assert "it" not in value  # no intent → query-only submission
        assert b["style"] == "primary"

    # The fuller `body` rides in a description line so the choice reads with its
    # meaning (Copilot renders `label — body`); the old bare-label bullet dropped it.
    joined = "\n".join(_sections(blocks))
    assert "Rebuild the closure summary from the case record." in joined
    assert "Turn this resolution into a reusable runbook." in joined
    assert "Decision: Regenerate summary" not in joined  # confusing prefix is gone

    # RUN stays text, never a button.
    assert "kubectl get pods" in joined
    assert all("kubectl" not in b["text"]["text"] for b in buttons)


def test_classification_clarification_button_round_trips_reclassification_intent():
    # File-classification clarifications are intent-bearing (issue #27): the
    # backend attaches a `file_reclassification` intent (file_id + DataType) and
    # a click must replay it so the engine re-labels the file mechanically —
    # never submit the payload as a bare query the LLM could act on literally
    # (the old behavior deep-analyzed the file instead of reclassifying it).
    result = TurnResult(
        agent_response="I couldn't confidently classify the file you uploaded.",
        case_state="investigating",
        suggested_actions=[
            {"type": "DECIDE", "label": "Application logs",
             "payload": 'Treat the previously uploaded file ("x.log") as application logs.',
             "body": "Treat as application logs.",
             "intent": {"type": "file_reclassification",
                        "file_id": "file_de0e86e511be",
                        "data_type": "logs_and_errors"}},
            {"type": "DECIDE", "label": "Something else",
             "payload": 'Treat the previously uploaded file ("x.log") as unstructured text.',
             "body": "Treat as unstructured text.",
             "intent": {"type": "file_reclassification",
                        "file_id": "file_de0e86e511be",
                        "data_type": "unstructured_text"}},
        ],
    )
    buttons = _buttons(build_turn_blocks(result))
    assert len(buttons) == 2
    for b in buttons:
        value = json.loads(b["value"])
        assert value["it"] == "file_reclassification"
        assert value["id"]["file_id"] == "file_de0e86e511be"
        assert value["id"]["data_type"] in ("logs_and_errors", "unstructured_text")
        assert value["id"]["user_confirmed"] is True
        assert value["q"].startswith("Treat the previously uploaded file")


def test_intent_bearing_decide_button_still_carries_intent():
    # A state-machine gate keeps replaying its intent verbatim (user_confirmed).
    result = TurnResult(
        agent_response="Looks resolved.",
        case_state="investigating",
        suggested_actions=[{
            "type": "DECIDE", "label": "Yes, mark resolved",
            "payload": "Mark this case resolved.",
            # See tests/test_actions.py: a resolve pair is a `confirmation`
            # intent from 9.0.0 on; `status_transition` carries only `closed`.
            "intent": {"type": "status_transition", "to_state": "closed"},
        }],
    )
    buttons = _buttons(build_turn_blocks(result))
    assert len(buttons) == 1
    value = json.loads(buttons[0]["value"])
    assert value["q"] == "Mark this case resolved."
    assert value["it"] == "status_transition"
    assert value["id"]["to_state"] == "closed"
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


def test_assurance_grade_labeled_in_context():
    # A held-back grade is surfaced beside the narration so the cause claim in
    # agent_response is never forwarded without its read-time label (#572/INV-28).
    result = TurnResult(
        agent_response="The root cause is connection pool exhaustion.",
        case_state="resolved",
        cause_assurance="mechanistic",
    )
    ctx = _context_texts(build_turn_blocks(result))
    assert any("mechanistic" in t for t in ctx)


def test_assurance_label_suppressed_pre_terminal():
    # An RCC minted mid-investigation must NOT repeat its qualifier on every
    # non-terminal turn — the label rides only the terminal disposition.
    result = TurnResult(
        agent_response="A likely cause is connection pool exhaustion.",
        case_state="investigating",
        cause_assurance="no_root",
    )
    ctx = _context_texts(build_turn_blocks(result))
    assert not any("assurance" in t.lower() for t in ctx)


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


# --- The button cap spends on decision chips first (#2468) -------------------
# Core appends its engine-owned decision chips after the model's follow-ups and
# any clarification choices; on Slack they are the only path to their action.


def _follow_up(n: int) -> dict:
    return {"type": "DECIDE", "label": f"Follow-up {n}",
            "payload": f"Do follow-up {n}.", "body": f"Follow-up {n} body."}


def _reclassify(n: int) -> dict:
    return {"type": "DECIDE", "label": f"Type {n}", "payload": f"Treat as {n}.",
            "intent": {"type": "file_reclassification",
                       "file_id": f"file_{n // 4}", "data_type": f"type_{n}"}}


def _chip(label: str, **intent) -> dict:
    return {"action_type": "DECIDE", "label": label, "payload": f"{label}.",
            "intent": intent}


def _labels(blocks) -> list[str]:
    return [b["text"]["text"] for b in _buttons(blocks)]


def _steps(blocks) -> str:
    return next(s for s in _sections(blocks) if "Suggested next steps" in s)


def test_decision_chips_after_twelve_choices_and_follow_ups_keep_their_buttons():
    resolve = _chip("Mark it resolved", type="status_transition", to_state="resolved")
    close = _chip("Close without resolving", type="status_transition", to_state="closed")
    actions = [*(_reclassify(n) for n in range(12)),
               *(_follow_up(n) for n in range(3)), resolve, close]
    blocks = build_turn_blocks(TurnResult(agent_response="?", suggested_actions=actions))

    # Priority picks which actions get a button; the row keeps core's order.
    assert _labels(blocks) == [
        *(f"Type {n}" for n in range(8)), "Mark it resolved", "Close without resolving"]
    # The overflow is listed in core's order; the decision chips are not listed.
    steps = _steps(blocks)
    overflow = [f"*Type {n}*" for n in range(8, 12)] + [
        f"*Follow-up {n}*" for n in range(3)]
    positions = [steps.index(label) for label in overflow]
    assert positions == sorted(positions)
    assert "Mark it resolved" not in steps and "Close without resolving" not in steps


def test_declined_close_card_after_ten_follow_ups_is_a_button():
    card = {**_chip("Close the case", type="status_transition", to_state="closed"),
            "body": "Close it as a false alarm."}
    actions = [*(_follow_up(n) for n in range(10)), card]
    blocks = build_turn_blocks(TurnResult(agent_response="?", suggested_actions=actions))

    assert _labels(blocks) == [*(f"Follow-up {n}" for n in range(9)), "Close the case"]
    # Body lines stay in core's order, the overflowed follow-up among them.
    steps = _steps(blocks)
    assert steps.index("Follow-up 9") < steps.index("Close it as a false alarm.")


def test_under_the_cap_buttons_keep_core_order():
    actions = [
        {"type": "EVIDENCE", "label": "paste the DB logs"},
        _follow_up(0),
        _reclassify(0),
        {"type": "EVIDENCE", "label": "Submit the output", "prefill": ""},
        _chip("Yes, close it", type="confirmation", confirmation_value=True),
        _follow_up(1),
    ]
    blocks = build_turn_blocks(TurnResult(agent_response="?", suggested_actions=actions))

    assert _labels(blocks) == [
        "Follow-up 0", "Type 0", "Submit the output", "Yes, close it", "Follow-up 1"]
    assert _buttons(blocks)[2]["action_id"] == "fm_data_box:2"
    # The EVIDENCE ask keeps its own section, never a button.
    assert any("paste the DB logs" in s and "FaultMaven needs" in s
               for s in _sections(blocks))


def test_over_the_cap_a_data_box_and_decision_chips_keep_buttons_gate1_ranks_as_engine():
    data_box = {"type": "EVIDENCE", "label": "Submit the output", "prefill": ""}
    gate1 = [_chip(label, type="confirmation", confirmation_value=value,
                   proposal_id="gate1:0123456789abcdef")
             for label, value in (("Yes, that's it", True), ("Not quite", False))]
    resolve = _chip("Mark it resolved", type="status_transition", to_state="resolved")
    close = _chip("Close without resolving", type="status_transition", to_state="closed")
    actions = [data_box, *(_reclassify(n) for n in range(12)), *gate1, resolve, close]
    blocks = build_turn_blocks(TurnResult(agent_response="?", suggested_actions=actions))

    # The decision chips take 2 buttons; the other 8 go to the engine choices in
    # core's order, so Gate 1's pair, ranked among them after 12 choices, has none.
    assert _labels(blocks) == [
        "Submit the output", *(f"Type {n}" for n in range(7)),
        "Mark it resolved", "Close without resolving"]
    assert _buttons(blocks)[0]["action_id"] == "fm_data_box:0"
    assert {_button_priority(a) for a in (data_box, *gate1, _reclassify(0))} == {1}
    assert _button_priority(resolve) == _button_priority(close) == 0
    assert _button_priority(_follow_up(0)) == 2


def test_without_decision_chips_buttons_keep_core_order():
    # Core's order (turn_response.py): save-chat-data choices, the data box,
    # clarification choices, then the follow-ups. This is what main renders.
    save = {"type": "DECIDE", "label": "Yes, save it as data",
            "payload": "Save that output as data.",
            "intent": {"type": "save_chat_data", "offer_id": "offer_1"}}
    resubmit = {"type": "EVIDENCE", "label": "Not exactly, let me re-submit",
                "prefill": "ERROR 42"}
    actions = [save, resubmit, *(_reclassify(n) for n in range(12)),
               *(_follow_up(n) for n in range(3))]
    blocks = build_turn_blocks(TurnResult(agent_response="?", suggested_actions=actions))

    assert [(b["text"]["text"], b["action_id"]) for b in _buttons(blocks)] == [
        ("Yes, save it as data", "fm_suggested_action:0"),
        ("Not exactly, let me re-submit", "fm_data_box:1"),
        *((f"Type {n}", f"fm_suggested_action:{n + 2}") for n in range(8)),
    ]
