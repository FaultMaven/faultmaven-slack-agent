"""Interactive suggested-action buttons — encoding + submission."""

from __future__ import annotations

import json

from faultmaven.client import TurnResult
from listeners.actions import _disable_actions, _plain, apply_action
from rendering import build_turn_blocks


class _CaptureClient:
    """Minimal WebClient stand-in that records the last chat_update call."""

    def __init__(self) -> None:
        self.updated: dict | None = None

    def chat_update(self, **kwargs) -> None:
        self.updated = kwargs


def _sections_of(blocks) -> list[str]:
    return [b["text"]["text"] for b in blocks if b["type"] == "section"]


def test_disable_actions_keeps_the_question_section():
    # Clicking a choice strips the buttons but must leave the question visible.
    client = _CaptureClient()
    body = {
        "channel": {"id": "C1"},
        "message": {
            "ts": "111.222",
            "text": "Would you like to investigate?",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Would you like to investigate?"}},
                {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Yes"}}]},
            ],
        },
    }
    _disable_actions(client, body)
    kept = client.updated["blocks"]
    assert not any(b["type"] == "actions" for b in kept)  # buttons gone
    assert "Would you like to investigate?" in _sections_of(kept)  # question stays


def test_disable_actions_rebuilds_question_when_payload_has_no_section():
    # Defensive: a thinner payload (blocks are only the actions) must not blank
    # the message — the question is rebuilt from the fallback text.
    client = _CaptureClient()
    body = {
        "channel": {"id": "C1"},
        "message": {
            "ts": "111.222",
            "text": "Shall I proceed?",
            "blocks": [
                {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Yes"}}]},
            ],
        },
    }
    _disable_actions(client, body)
    kept = client.updated["blocks"]
    assert not any(b["type"] == "actions" for b in kept)
    assert _sections_of(kept) == ["Shall I proceed?"]  # reconstructed, not blank


def test_plain_neutralizes_mrkdwn_in_echoed_label():
    # The choice echo wraps the label in *...*; an active char must not break it.
    assert _plain("Yes, let's investigate") == "Yes, let's investigate"
    assert _plain("run *now* & <cmd>") == "run now &amp; &lt;cmd&gt;"
    assert "*" not in _plain("a*b_c~d`e") and "_" not in _plain("a*b_c~d`e")


class FakeFM:
    def __init__(self) -> None:
        self.turns: list[tuple] = []

    def submit_turn(self, case_id, **kwargs) -> TurnResult:
        self.turns.append((case_id, kwargs))
        return TurnResult(agent_response="next")


def _buttons(blocks) -> list[dict]:
    out: list[dict] = []
    for b in blocks:
        if b["type"] == "actions":
            out.extend(b["elements"])
    return out


# -- rendering: which actions become buttons ----------------------------------
def test_decide_becomes_primary_button_carrying_intent():
    result = TurnResult(
        agent_response="?",
        case_state="investigating",
        suggested_actions=[
            {
                "type": "DECIDE",
                "label": "Mark resolved",
                "payload": "The issue is fixed.",
                "intent": {"type": "status_transition", "to_state": "resolved"},
            }
        ],
    )
    buttons = _buttons(build_turn_blocks(result))
    assert len(buttons) == 1
    assert buttons[0]["style"] == "primary"
    assert buttons[0]["action_id"].startswith("fm_suggested_action:")
    value = json.loads(buttons[0]["value"])
    assert value["it"] == "status_transition"
    assert value["q"] == "The issue is fixed."
    assert value["id"]["to_state"] == "resolved"
    assert value["id"]["user_confirmed"] is True


def test_free_speech_is_not_clickable():
    # FREE_SPEECH is a prompt to answer in your own words — NOT a button that
    # submits fixed text (that would send text the engine can't act on).
    result = TurnResult(
        agent_response="?",
        suggested_actions=[
            {"type": "FREE_SPEECH", "label": "Tell me about the deploy",
             "payload": "Tell me about the 2pm deploy"}
        ],
    )
    blocks = build_turn_blocks(result)
    assert _buttons(blocks) == []  # no button
    sections = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert any("Tell me about the deploy" in s for s in sections)  # rendered as a hint


def test_run_action_is_not_a_button():
    result = TurnResult(
        agent_response="?",
        suggested_actions=[{"type": "RUN", "payload": "kubectl get pods"}],
    )
    blocks = build_turn_blocks(result)
    assert _buttons(blocks) == []
    sections = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert any("kubectl get pods" in s for s in sections)


def test_evidence_need_decide_is_not_submittable():
    """evidence_need is NOT_IMPLEMENTED server-side; must never become a button."""

    result = TurnResult(
        agent_response="?",
        suggested_actions=[
            {"type": "DECIDE", "label": "provide it",
             "intent": {"type": "evidence_need", "evidence_need_id": "eneed_1"}}
        ],
    )
    assert _buttons(build_turn_blocks(result)) == []


def test_multiple_buttons_get_unique_action_ids():
    """Regression: Slack rejects a message with duplicate action_ids."""

    from rendering import SUGGESTED_ACTION_PATTERN

    result = TurnResult(
        agent_response="?",
        suggested_actions=[
            {"type": "DECIDE", "label": "A", "payload": "a",
             "intent": {"type": "status_transition", "to_state": "resolved"}},
            {"type": "DECIDE", "label": "B", "payload": "b",
             "intent": {"type": "status_transition", "to_state": "closed"}},
            {"type": "DECIDE", "label": "C", "payload": "c",
             "intent": {"type": "confirmation", "confirmation_value": True}},
        ],
    )
    ids = [b["action_id"] for b in _buttons(build_turn_blocks(result))]
    assert len(ids) == 3
    assert len(set(ids)) == 3  # all unique
    assert all(SUGGESTED_ACTION_PATTERN.match(i) for i in ids)  # handler matches


def test_oversized_value_falls_back_to_text_not_button():
    # A DECIDE whose encoded value exceeds Slack's button-value cap renders as
    # text instead of a truncated (broken) button.
    result = TurnResult(
        agent_response="?",
        suggested_actions=[
            {"type": "DECIDE", "label": "huge", "payload": "x" * 3000,
             "intent": {"type": "confirmation", "confirmation_value": True}}
        ],
    )
    assert _buttons(build_turn_blocks(result)) == []


# -- handler core: button value -> submitted turn -----------------------------
def test_apply_action_submits_decide_intent():
    fm = FakeFM()
    value = json.dumps(
        {
            "q": "fixed",
            "it": "status_transition",
            "id": {"type": "status_transition", "to_state": "resolved",
                   "user_confirmed": True},
        }
    )
    result = apply_action(fm, "c1", value)
    assert result.agent_response == "next"
    case_id, kwargs = fm.turns[0]
    assert case_id == "c1"
    assert kwargs["query"] == "fixed"
    assert kwargs["intent_type"] == "status_transition"
    assert kwargs["intent_data"]["to_state"] == "resolved"


def test_apply_action_submits_free_speech_without_intent_data():
    fm = FakeFM()
    apply_action(fm, "c1", json.dumps({"q": "tell me more", "it": "conversation"}))
    _, kwargs = fm.turns[0]
    assert kwargs["intent_type"] == "conversation"
    assert kwargs.get("intent_data") is None
