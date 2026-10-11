"""The chat-data offer's data box (#100, contract 13.3.0).

A reply to a message carrying system output leads ``suggested_actions`` with
"Yes, save it as data" (a DECIDE forwarded as received) and "Not exactly, let me
re-submit" (an EVIDENCE with ``prefill``, the blocks); a reported state with no
data gets "Submit the output" (``prefill`` ``""``). The ``prefill`` choices
render as buttons that open a modal, and the modal's submission is a paste turn.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

import listeners._turn as turn_mod
from faultmaven.client import TurnResult
from listeners.actions import (
    DATA_BOX_BUSY_ERROR,
    DATA_BOX_EMPTY_ERROR,
    DATA_BOX_UNAVAILABLE_TEXT,
    register_data_box,
)
from rendering import (
    DATA_BOX_BLOCK_ID,
    DATA_BOX_CALLBACK_ID,
    DATA_BOX_INPUT_ID,
    DATA_BOX_PATTERN,
    SUGGESTED_ACTION_PATTERN,
    build_data_box_view,
    build_turn_blocks,
)
from store import CaseStore

_BLOCKS = "ERROR 502 upstream\n  at gateway.go:88\nretrying in 5s"

_SAVE = {
    "label": "Yes, save it as data",
    "type": "DECIDE",
    "payload": "Save that output as data.",
    "body": "It is output copied from the system, unchanged.",
    "intent": {"type": "save_chat_data", "offer_id": "cdo_abc123"},
}
_RESUBMIT = {
    "label": "Not exactly, let me re-submit",
    "type": "EVIDENCE",
    "body": "Correct it, then send it as data (pasted or attached).",
    "prefill": _BLOCKS,
}
_SUBMIT = {
    "label": "Submit the output",
    "type": "EVIDENCE",
    "body": "Send it as data (pasted or attached): the output of `systemctl status nginx`.",
    "prefill": "",
}


def _buttons(blocks) -> list[dict]:
    return [e for b in blocks if b["type"] == "actions" for e in b["elements"]]


def _sections(blocks) -> str:
    return "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")


# --- rendering ---------------------------------------------------------------


def test_offer_renders_save_and_resubmit_as_buttons_not_as_a_data_request():
    result = TurnResult(
        agent_response="Looks like a gateway error.",
        case_state="investigating",
        suggested_actions=[_SAVE, _RESUBMIT, {"type": "EVIDENCE", "label": "nginx error log"}],
    )

    blocks = build_turn_blocks(result)
    save, resubmit = _buttons(blocks)

    # The save choice is an ordinary DECIDE button: its intent goes back as
    # received (offer_id included), so the server can find the offer.
    assert SUGGESTED_ACTION_PATTERN.match(save["action_id"])
    value = json.loads(save["value"])
    assert value["it"] == "save_chat_data"
    assert value["id"]["offer_id"] == "cdo_abc123"
    assert save["style"] == "primary"
    # The re-submit choice is a data-box button carrying the blocks verbatim.
    assert DATA_BOX_PATTERN.match(resubmit["action_id"])
    assert json.loads(resubmit["value"])["p"] == _BLOCKS
    assert save["action_id"] != resubmit["action_id"]
    # Only the EVIDENCE without prefill is still a data request line.
    text = _sections(blocks)
    request = text.split("needs this data:*")[1].split("*Suggested next steps*")[0]
    assert "nginx error log" in request
    assert "re-submit" not in request
    # Each choice's body describes it in the step list.
    assert "Correct it, then send it as data" in text
    assert "output copied from the system" in text


def test_state_report_renders_submit_the_output_as_an_empty_data_box():
    result = TurnResult(agent_response="Good to hear.", suggested_actions=[_SUBMIT])

    (button,) = _buttons(build_turn_blocks(result))

    assert DATA_BOX_PATTERN.match(button["action_id"])
    value = json.loads(button["value"])
    assert value["p"] == ""
    assert "systemctl status nginx" in value["h"]


def test_prefill_too_long_for_a_button_is_left_out_not_cut():
    """A truncated block saved as data would record output the system never
    produced, so the button carries no prefill and the modal says why."""

    big = {**_RESUBMIT, "prefill": "line of output\n" * 400}

    (button,) = _buttons(build_turn_blocks(TurnResult(agent_response="x", suggested_actions=[big])))

    assert len(button["value"]) <= 2000
    value = json.loads(button["value"])
    assert "p" not in value and value["long"] is True


def test_data_box_view_holds_the_prefill_in_a_multiline_input():
    view = build_data_box_view(
        json.dumps({"p": _BLOCKS, "h": "Correct it."}), private_metadata='{"c":"C1","t":"1.0"}'
    )

    assert view["callback_id"] == DATA_BOX_CALLBACK_ID
    assert view["private_metadata"] == '{"c":"C1","t":"1.0"}'
    note, field = view["blocks"]
    assert note["text"]["type"] == "plain_text"  # server text formats nothing here
    assert note["text"]["text"].startswith("Correct it.")
    assert "text snippet" in note["text"]["text"]
    assert field["block_id"] == DATA_BOX_BLOCK_ID
    element = field["element"]
    assert element["action_id"] == DATA_BOX_INPUT_ID
    assert element["multiline"] is True
    assert element["initial_value"] == _BLOCKS


def test_empty_data_box_view_has_no_initial_value_and_shows_the_hint():
    view = build_data_box_view(
        json.dumps({"p": "", "h": "Send the output of `df -h`."}), private_metadata="{}"
    )

    note, field = view["blocks"]
    assert "initial_value" not in field["element"]
    assert "df -h" in note["text"]["text"]


def test_long_data_box_view_says_why_it_is_empty():
    view = build_data_box_view(json.dumps({"h": "", "long": True}), private_metadata="{}")

    note, field = view["blocks"]
    assert "initial_value" not in field["element"]
    assert "too long" in note["text"]["text"]


# --- listeners ---------------------------------------------------------------


class _App:
    def __init__(self) -> None:
        self.actions: dict = {}
        self.views: dict = {}

    def action(self, pattern):
        return lambda fn: self.actions.setdefault(pattern, fn)

    def view(self, callback_id):
        return lambda fn: self.views.setdefault(callback_id, fn)


class _Slack:
    def __init__(self, open_error: Exception | None = None) -> None:
        self.opened: list[dict] = []
        self.posts: list[dict] = []
        self.ephemerals: list[dict] = []
        self.updates: list[dict] = []
        self.open_error = open_error

    def views_open(self, **kw):
        if self.open_error:
            raise self.open_error
        self.opened.append(kw)

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": f"post{len(self.posts)}.000"}

    def chat_postEphemeral(self, **kw):
        self.ephemerals.append(kw)

    def chat_update(self, **kw):
        self.updates.append(kw)

    def conversations_replies(self, **kw):
        return {"messages": []}


class _FM:
    def __init__(self) -> None:
        self.turns: list = []

    def submit_turn(self, case_id, **kwargs):
        self.turns.append((case_id, kwargs))
        return TurnResult(agent_response="Thanks — reading it.", case_state="investigating")


@pytest.fixture
def store(tmp_path):
    s = CaseStore(str(tmp_path / "cases.db"))
    s.put("T1", "C1", "TS1", "case_1")
    s.mark_seeded("T1", "C1", "TS1")
    s.record_turn("T1", "C1", "TS1", turn_ts="OFFER_TS", action_ts="OFFER_TS")
    return s


def _registered(store, fm=None):
    app = _App()
    register_data_box(app, fm or _FM(), store)
    return app


def _click(app, slack, *, message_ts="OFFER_TS", value=None):
    app.actions[DATA_BOX_PATTERN](
        ack=lambda: None,
        body={
            "trigger_id": "TRIG1",
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "message": {"ts": message_ts, "thread_ts": "TS1", "text": "offer", "blocks": []},
            "actions": [{"value": value or json.dumps({"p": _BLOCKS, "h": "Correct it."})}],
        },
        context=SimpleNamespace(team_id="T1"),
        client=slack,
        logger=logging.getLogger("test"),
    )


def _submit(app, slack, text, *, view_id="V1"):
    acks: list[dict] = []
    app.views[DATA_BOX_CALLBACK_ID](
        ack=lambda **kw: acks.append(kw),
        body={"user": {"id": "U1"}},
        view={
            "id": view_id,
            "private_metadata": json.dumps({"c": "C1", "t": "TS1"}),
            "state": {"values": {DATA_BOX_BLOCK_ID: {DATA_BOX_INPUT_ID: {"value": text}}}},
        },
        context=SimpleNamespace(team_id="T1"),
        client=slack,
        logger=logging.getLogger("test"),
    )
    turn_mod.drain_turns(5.0)
    return acks


def test_click_opens_the_data_box_for_the_thread(store):
    slack = _Slack()

    _click(_registered(store), slack)

    (opened,) = slack.opened
    assert opened["trigger_id"] == "TRIG1"
    view = opened["view"]
    assert json.loads(view["private_metadata"]) == {"c": "C1", "t": "TS1"}
    assert view["blocks"][1]["element"]["initial_value"] == _BLOCKS
    # Opening the box commits nothing, so the offer keeps its buttons.
    assert slack.updates == []


def test_click_on_an_older_turn_opens_nothing(store):
    slack = _Slack()

    _click(_registered(store), slack, message_ts="OLDER_TS")

    assert slack.opened == []
    assert "previous turn" in slack.ephemerals[0]["text"]


def test_a_box_that_cannot_open_points_to_the_snippet(store):
    slack = _Slack(open_error=RuntimeError("expired_trigger_id"))

    _click(_registered(store), slack)

    assert slack.ephemerals[0]["text"] == DATA_BOX_UNAVAILABLE_TEXT
    assert slack.ephemerals[0]["user"] == "U1"


def test_submission_is_a_paste_only_turn_on_the_threads_case(store):
    fm, slack = _FM(), _Slack()

    acks = _submit(_registered(store, fm), slack, _BLOCKS)

    assert acks == [{}]  # the modal closes
    ((case_id, kwargs),) = fm.turns
    assert case_id == "case_1"
    assert kwargs["pasted_content"] == _BLOCKS
    assert kwargs["input_type"] == "paste"
    assert kwargs["query"] is None  # data, with no claim beside it
    assert kwargs["team_id"] == "T1"
    assert kwargs["idempotency_key"] == turn_mod.view_turn_key("T1", "V1")
    # The thread shows who submitted what, then the reply.
    assert slack.posts[0]["text"] == "> <@U1> submitted data (3 lines)"
    assert store.get_last_turn_ts("T1", "C1", "TS1") != "OFFER_TS"


def test_empty_submission_is_refused_in_the_modal(store):
    fm, slack = _FM(), _Slack()

    acks = _submit(_registered(store, fm), slack, "   \n")

    assert acks == [{"response_action": "errors", "errors": {DATA_BOX_BLOCK_ID: DATA_BOX_EMPTY_ERROR}}]
    assert fm.turns == [] and slack.posts == []


def test_submission_mid_turn_keeps_the_modal_open(store):
    fm, slack = _FM(), _Slack()
    assert turn_mod.try_begin_turn(slack, team_id="T1", channel="C1", thread_ts="TS1")
    try:
        acks = _submit(_registered(store, fm), slack, _BLOCKS)
    finally:
        turn_mod.end_turn("T1", "C1", "TS1")

    assert acks == [{"response_action": "errors", "errors": {DATA_BOX_BLOCK_ID: DATA_BOX_BUSY_ERROR}}]
    assert fm.turns == []


def test_submission_to_a_thread_whose_case_is_gone_opens_no_new_case(store):
    fm, slack = _FM(), _Slack()
    store.mark_unlinked("T1", "C1", "TS1")

    _submit(_registered(store, fm), slack, _BLOCKS)

    assert fm.turns == []
    assert slack.posts[0]["text"] == turn_mod.case_gone_text("C1")


def test_view_turn_key_is_its_own_namespace():
    assert turn_mod.view_turn_key("T1", "1.0") != turn_mod.shortcut_turn_key("T1", "1.0")
    assert turn_mod.view_turn_key("T1", "V1") == turn_mod.view_turn_key("T1", "V1")
