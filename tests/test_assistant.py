"""Assistant surface (the 1:1 "Chat" tab) — the turn bookkeeping it owes the
stale-click guard, driven through the listener Bolt actually registers.

The channel surfaces get this from ``run_turn_and_post``; this surface posts via
``say()`` and hand-rolls the same sequence, so it needs its own coverage: it is
the one surface that declines (unreadable files, an empty message) *without*
running a turn.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import listeners._turn as turn_mod
from faultmaven.client import TurnResult
from listeners.assistant import build_assistant

_DECIDE_RESULT = TurnResult(
    agent_response="Should I proceed?",
    suggested_actions=[
        {
            "type": "DECIDE",
            "label": "Yes proceed",
            "payload": "yes",
            "intent": {"type": "confirmation", "confirmation_value": True},
        }
    ],
)


class _FakeFM:
    def __init__(self, result: TurnResult | None = None) -> None:
        self.turns: list = []
        self.result = result or TurnResult(agent_response="on it")

    def create_case(self, *, title=None, initial_message=None):
        return "case_1"

    def submit_turn(self, case_id, **kwargs):
        self.turns.append((case_id, kwargs))
        return self.result


class _FakeStore:
    def __init__(self) -> None:
        self.m: dict = {}
        self.seeded: set = set()
        self.last_turn: dict = {}
        self.last_action: dict = {}

    def get(self, t, c, th):
        return self.m.get((t, c, th))

    def put(self, t, c, th, cid):
        self.m[(t, c, th)] = cid

    def mark_seeded(self, t, c, th):
        self.seeded.add((t, c, th))

    def is_seeded(self, t, c, th):
        return (t, c, th) in self.seeded

    def get_last_turn_ts(self, t, c, th):
        return self.last_turn.get((t, c, th))

    def get_last_action_ts(self, t, c, th):
        return self.last_action.get((t, c, th))

    def record_turn(self, t, c, th, *, turn_ts, action_ts):
        if turn_ts is not None:
            self.last_turn[(t, c, th)] = turn_ts
        if action_ts is None:
            self.last_action.pop((t, c, th), None)
        else:
            self.last_action[(t, c, th)] = action_ts

    def clear_last_action_ts(self, t, c, th):
        self.last_action.pop((t, c, th), None)


class _FakeClient:
    token = "xoxb-test"

    def __init__(self, replies: list | None = None) -> None:
        self.replies = replies or []
        self.updates: list[dict] = []

    def chat_update(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    def conversations_replies(self, **kw):
        messages = [{"ts": kw.get("ts"), "text": "parent", "blocks": []}]
        messages.extend(self.replies)
        return {"messages": messages[: kw.get("limit", 100)]}


class _FakeSay:
    """say() stand-in: records the posts and answers with the new message's ts."""

    def __init__(self) -> None:
        self.posts: list[dict] = []

    def __call__(self, text="", blocks=None, **kw):
        self.posts.append({"text": text, "blocks": blocks})
        return {"ok": True, "ts": f"say{len(self.posts)}.000"}


_PREV_CHOICE_MESSAGE = {
    "ts": "PREV_ACTION_TS",
    "text": "Choose an option:",
    "blocks": [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Choose an option:"}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Yes"}}
            ],
        },
    ],
}


def _run_message(fm, store, client, payload: dict) -> _FakeSay:
    """Drive one user message through the registered Assistant listener."""

    assistant = build_assistant(fm, store)
    handler = assistant._user_message_listeners[0].ack_function
    say = _FakeSay()
    handler(
        payload=payload,
        context=SimpleNamespace(team_id="T1"),
        client=client,
        set_status=lambda _status: None,
        say=say,
        logger=logging.getLogger("test"),
    )
    turn_mod.drain_turns(5.0)  # the turn runs on a background daemon
    return say


def _message(text: str | None = "disk is full", **extra) -> dict:
    return {
        "channel": "C1",
        "thread_ts": "TS1",
        "ts": "1.0",
        "text": text,
        **extra,
    }


def test_new_choice_buttons_are_recorded_as_the_current_turn():
    """Regression: say() answers with a SlackResponse, not a dict, so the
    isinstance(resp, dict) gate recorded nothing and this surface never armed
    the stale-click guard at all."""

    fm, store, client = _FakeFM(result=_DECIDE_RESULT), _FakeStore(), _FakeClient()

    say = _run_message(fm, store, client, _message())

    assert fm.turns, "the message must reach the backend"
    reply_ts = f"say{len(say.posts)}.000"
    assert store.get_last_turn_ts("T1", "C1", "TS1") == reply_ts
    assert store.get_last_action_ts("T1", "C1", "TS1") == reply_ts


def test_a_plain_reply_still_advances_the_turn_marker():
    fm, store, client = _FakeFM(), _FakeStore(), _FakeClient()

    say = _run_message(fm, store, client, _message())

    assert store.get_last_turn_ts("T1", "C1", "TS1") == f"say{len(say.posts)}.000"
    assert store.get_last_action_ts("T1", "C1", "TS1") is None


def test_a_new_turn_takes_down_the_previous_turns_buttons():
    fm, store = _FakeFM(), _FakeStore()
    client = _FakeClient(replies=[_PREV_CHOICE_MESSAGE])
    store.put("T1", "C1", "TS1", "case_1")
    store.record_turn(
        "T1", "C1", "TS1", turn_ts="PREV_ACTION_TS", action_ts="PREV_ACTION_TS"
    )

    _run_message(fm, store, client, _message())

    assert [u["ts"] for u in client.updates] == ["PREV_ACTION_TS"]
    assert not any(b["type"] == "actions" for b in client.updates[0]["blocks"])


def test_a_declined_message_leaves_the_previous_buttons_alone():
    """Nothing to investigate — no case, no turn, so the pending question must
    stay answerable rather than being stripped by a message that went nowhere."""

    fm, store = _FakeFM(), _FakeStore()
    client = _FakeClient(replies=[_PREV_CHOICE_MESSAGE])
    store.put("T1", "C1", "TS1", "case_1")
    store.record_turn(
        "T1", "C1", "TS1", turn_ts="PREV_ACTION_TS", action_ts="PREV_ACTION_TS"
    )

    say = _run_message(fm, store, client, _message(text="   "))

    assert fm.turns == [], "an empty message must not open a turn"
    assert client.updates == [], "and must not take the previous buttons down"
    assert store.get_last_action_ts("T1", "C1", "TS1") == "PREV_ACTION_TS"
    assert any("Tell me what's going on" in p["text"] for p in say.posts)
