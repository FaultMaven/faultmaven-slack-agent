"""Message-shortcut case-opener: alert extraction + seed-from-message (P3)."""

from __future__ import annotations

import importlib.util

from faultmaven.client import TurnResult
from slack_text import message_to_text


# -- the make-or-break piece: extract readable text from rich alert messages ---
def test_plain_text_message():
    assert message_to_text({"text": "disk full on web-1"}) == "disk full on web-1"


def test_blocks_section_and_fields_beat_fallback_text():
    msg = {
        "text": "Alert triggered",  # the useless fallback stub
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "🔥 High latency"}},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Service:* checkout-api"},
                "fields": [
                    {"type": "mrkdwn", "text": "*p99:* 2.4s"},
                    {"type": "mrkdwn", "text": "*SLO:* 500ms"},
                ],
            },
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "since 14:02 UTC"}]},
        ],
    }
    text = message_to_text(msg)
    assert "High latency" in text
    assert "checkout-api" in text
    assert "p99:* 2.4s" in text and "SLO:* 500ms" in text
    assert "14:02" in text
    assert text != "Alert triggered"  # we used the blocks, not the stub


def test_legacy_attachments_datadog_style():
    msg = {
        "text": "",
        "attachments": [
            {
                "pretext": "Triggered: error rate",
                "title": "payments-svc error rate > 5%",
                "text": "Current value 7.3%",
                "fields": [
                    {"title": "Host", "value": "pay-3"},
                    {"title": "Window", "value": "5m"},
                ],
                "fallback": "payments-svc error rate alert",
            }
        ],
    }
    text = message_to_text(msg)
    assert "error rate" in text
    assert "payments-svc" in text and "7.3%" in text
    assert "Host: pay-3" in text and "Window: 5m" in text


def test_attachment_fallback_only_when_no_structure():
    msg = {"attachments": [{"fallback": "raw alert text"}]}
    assert message_to_text(msg) == "raw alert text"


def test_rich_text_block():
    msg = {
        "blocks": [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "OOMKilled on "},
                            {"type": "link", "url": "https://k8s/pod", "text": "pod web-2"},
                        ],
                    }
                ],
            }
        ],
    }
    text = message_to_text(msg)
    assert "OOMKilled on" in text and "pod web-2" in text


def test_empty_message_is_empty_string():
    assert message_to_text({}) == ""


# -- shortcut opener reuses run_turn: seed = the message as pasted_content ------
def test_shortcut_core_seeds_case_with_extracted_message():
    # Load _turn.py directly (its package __init__ pulls in slack_bolt).
    spec = importlib.util.spec_from_file_location("_turn", "listeners/_turn.py")
    _turn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_turn)

    calls: dict = {}

    class FakeFM:
        def create_case(self, *, title=None, initial_message=None):
            calls["create"] = (title, initial_message)
            return "case_1"

        def submit_turn(self, case_id, **kwargs):
            calls.setdefault("turns", []).append((case_id, kwargs))
            return TurnResult(agent_response="on it")

    class FakeStore:
        def __init__(self):
            self.m = {}

        def get(self, t, c, th):
            return self.m.get((t, c, th))

        def put(self, t, c, th, cid):
            self.m[(t, c, th)] = cid

    fm, store = FakeFM(), FakeStore()
    alert = message_to_text(
        {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "disk 98%"}}]}
    )
    _turn.run_turn(
        fm, store, team_id="T", channel_id="C", thread_ts="msg_ts",
        text="Please investigate this.", prior_context=alert,
    )
    assert calls["create"] == (None, None)  # no initial_message seed
    case_id, kw = calls["turns"][0]
    assert case_id == "case_1"
    assert kw["query"] == "Please investigate this."
    assert kw["pasted_content"] == "disk 98%"  # the alert seeds as evidence
    assert kw["input_type"] == "paste"
