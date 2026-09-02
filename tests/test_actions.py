"""Interactive suggested-action buttons — encoding + submission."""

from __future__ import annotations

import json

from faultmaven.client import TurnResult
from listeners.actions import (
    _disable_actions,
    _plain,
    _restore_actions,
    _show_working,
    apply_action,
)
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


def _body(blocks, text="Would you like to investigate?"):
    return {
        "channel": {"id": "C1"},
        "message": {"ts": "111.222", "text": text, "blocks": blocks},
    }


def test_show_working_swaps_buttons_for_a_working_note():
    # The instant a click lands the buttons must go away (Slack can't disable
    # them) and a visible in-progress cue must take their place.
    client = _CaptureClient()
    body = _body(
        [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Would you like to investigate?"}},
            {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Yes"}}]},
        ]
    )
    _show_working(client, body, "Yes")
    kept = client.updated["blocks"]
    assert not any(b["type"] == "actions" for b in kept)  # unclickable now
    assert "Would you like to investigate?" in _sections_of(kept)  # question stays
    contexts = [b for b in kept if b["type"] == "context"]
    assert len(contexts) == 1
    assert "Working on *Yes*" in contexts[0]["elements"][0]["text"]


def test_show_working_strips_every_actions_block():
    # rendering chunks >5 buttons into MULTIPLE actions blocks (Slack's per-block
    # element cap) — one click must take down all of them, not just the first.
    client = _CaptureClient()
    button = {"type": "button", "text": {"type": "plain_text", "text": "x"}}
    body = _body(
        [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Would you like to investigate?"}},
            {"type": "actions", "elements": [button] * 5},
            {"type": "actions", "elements": [button] * 2},
        ]
    )
    _show_working(client, body, "x")
    assert not any(b["type"] == "actions" for b in client.updated["blocks"])


def test_show_working_neutralizes_label_and_survives_missing_label():
    # The label lands inside *...* mrkdwn — active chars must be stripped, and
    # a payload without a label still gets a generic in-progress note.
    client = _CaptureClient()
    body = _body(
        [{"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "x"}}]}],
        text="Shall I proceed?",
    )
    _show_working(client, body, "run *now* & <cmd>")
    note = [b for b in client.updated["blocks"] if b["type"] == "context"][0]
    assert "run now &amp; &lt;cmd&gt;" in note["elements"][0]["text"]
    assert _sections_of(client.updated["blocks"]) == ["Shall I proceed?"]  # rebuilt

    _show_working(client, body, "")
    note = [b for b in client.updated["blocks"] if b["type"] == "context"][0]
    assert "Working on it" in note["elements"][0]["text"]


def test_restore_actions_puts_the_original_message_back():
    # A failed submit must return the message to its delivered state — buttons
    # live again, working note gone — so retry stays one click away.
    client = _CaptureClient()
    original_blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Would you like to investigate?"}},
        {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Yes"}}]},
    ]
    body = _body(original_blocks)
    _restore_actions(client, body)
    assert client.updated["blocks"] == original_blocks
    assert client.updated["text"] == "Would you like to investigate?"


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
    result = apply_action(fm, "c1", value, team_id="T1")
    assert result.agent_response == "next"
    case_id, kwargs = fm.turns[0]
    assert case_id == "c1"
    assert kwargs["query"] == "fixed"
    assert kwargs["intent_type"] == "status_transition"
    assert kwargs["intent_data"]["to_state"] == "resolved"
    # A button click is a turn like any other: it must authenticate as its own
    # workspace's service account, not the process-wide default (ADR-013 D3).
    assert kwargs["team_id"] == "T1"


def test_apply_action_submits_free_speech_without_intent_data():
    fm = FakeFM()
    apply_action(
        fm,
        "c1",
        json.dumps({"q": "tell me more", "it": "conversation"}),
        team_id="T1",
    )
    _, kwargs = fm.turns[0]
    assert kwargs["intent_type"] == "conversation"
    assert kwargs.get("intent_data") is None
    assert kwargs["team_id"] == "T1"


# -- the click lifecycle: hide → submit → settle -------------------------------
# The buttons come down the instant a click lands and come back only where a
# re-click can genuinely succeed, so these drive the real handler end to end.
class _LifecycleClient:
    """WebClient stand-in recording the FULL sequence of message updates."""

    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.posts: list[dict] = []
        self.post_ts: list[str] = []

    def chat_update(self, **kwargs) -> None:
        self.updates.append(kwargs)

    def chat_postMessage(self, **kwargs) -> dict:
        self.posts.append(kwargs)
        # A real WebClient answers with a SlackResponse carrying the new
        # message's ts — that is how the handler learns which message it just
        # posted, so the stand-in has to hand one back too.
        self.post_ts.append(f"post{len(self.post_ts) + 1}.000")
        return {"ok": True, "ts": self.post_ts[-1]}

    def chat_postEphemeral(self, **kwargs) -> None:
        self.posts.append(kwargs)


class _FakeApp:
    """Captures the handler that register_actions decorates."""

    def __init__(self) -> None:
        self.handler = None

    def action(self, pattern):
        def decorate(fn):
            self.handler = fn
            return fn

        return decorate


class _LifecycleStore:
    def __init__(
        self,
        case_id="c1",
        get_error=None,
        last_turn_ts="111.222",
        turn_ts_error=None,
    ) -> None:
        self.case_id = case_id
        self.get_error = get_error
        self.turn_ts_error = turn_ts_error
        self.last_turn_ts = last_turn_ts
        self.last_action_ts = last_turn_ts
        self.deleted: list[tuple] = []

    def get(self, team, channel, thread):
        if self.get_error:
            raise self.get_error
        return self.case_id

    def delete(self, team, channel, thread):
        self.deleted.append((team, channel, thread))

    def get_last_turn_ts(self, team, channel, thread):
        if self.turn_ts_error:
            raise self.turn_ts_error
        return self.last_turn_ts

    def get_last_action_ts(self, team, channel, thread):
        return self.last_action_ts

    def record_turn(self, team, channel, thread, *, turn_ts, action_ts):
        if turn_ts is not None:
            self.last_turn_ts = turn_ts
        self.last_action_ts = action_ts

    def clear_last_action_ts(self, team, channel, thread):
        self.last_action_ts = None


class _FailingFM:
    def __init__(self, error) -> None:
        self.error = error

    def submit_turn(self, case_id, **kwargs):
        raise self.error


_ORIGINAL_BLOCKS = [
    {"type": "section", "text": {"type": "mrkdwn", "text": "Mark this resolved?"}},
    {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": "fm_suggested_action:0",
                "text": {"type": "plain_text", "text": "Yes"},
                "value": '{"q": "fixed", "it": "status_transition"}',
            }
        ],
    },
]


def _run_click(fm, store):
    """Drive one real button click through the registered handler."""

    import logging
    from types import SimpleNamespace

    import listeners._turn as turn_mod
    from listeners.actions import register_actions

    app = _FakeApp()
    register_actions(app, fm, store)
    client = _LifecycleClient()
    body = {
        "channel": {"id": "C1"},
        "user": {"id": "U1"},
        "message": {
            "ts": "111.222",
            "text": "Mark this resolved?",
            "blocks": [dict(b) for b in _ORIGINAL_BLOCKS],
        },
        "actions": [
            {
                "action_id": "fm_suggested_action:0",
                "text": {"type": "plain_text", "text": "Yes"},
                "value": '{"q": "fixed", "it": "status_transition"}',
            }
        ],
    }
    app.handler(
        ack=lambda: None,
        body=body,
        context=SimpleNamespace(team_id="T1"),
        client=client,
        logger=logging.getLogger("test"),
    )
    turn_mod.drain_turns(5.0)  # the turn runs on a background daemon
    return client


def _has_buttons(update) -> bool:
    return any(b.get("type") == "actions" for b in update["blocks"])


def _working_note(update) -> bool:
    return any(
        "Working on" in e.get("text", "")
        for b in update["blocks"]
        if b.get("type") == "context"
        for e in b.get("elements", [])
    )


def test_click_hides_buttons_before_the_turn_runs():
    """The FIRST update must land before the submit — that window is the whole
    point: a slow turn used to leave the buttons live for its full duration."""

    client = _run_click(FakeFM(), _LifecycleStore())
    assert not _has_buttons(client.updates[0])  # unclickable immediately
    assert _working_note(client.updates[0])  # and visibly in progress


def test_committed_click_settles_with_buttons_gone_for_good():
    client = _run_click(FakeFM(), _LifecycleStore())
    final = client.updates[-1]
    assert not _has_buttons(final)  # never re-armed after a committed decision
    assert not _working_note(final)  # transient note cleared
    assert "Mark this resolved?" in _sections_of(final["blocks"])  # question kept


def test_transient_failure_re_arms_the_buttons():
    """A conflict/5xx never committed and its message says to send it again, so
    the one-click path must be there to take."""

    from faultmaven import CaseVersionConflictError

    client = _run_click(
        _FailingFM(CaseVersionConflictError("stale", status_code=409)),
        _LifecycleStore(),
    )
    assert _has_buttons(client.updates[-1])  # retry stays one click away
    assert not _working_note(client.updates[-1])


def test_closed_case_leaves_the_buttons_down():
    """A concluded case refuses this forever — a re-armed button would offer a
    retry that can only fail again, contradicting the reply's own wording."""

    from faultmaven import CaseTerminalError

    client = _run_click(
        _FailingFM(CaseTerminalError("closed", status_code=409)), _LifecycleStore()
    )
    assert not _has_buttons(client.updates[-1])


def test_deleted_case_leaves_the_buttons_down_after_unlinking():
    """The sharp one: the thread is unlinked here, so its next message opens a
    FRESH case. A re-armed button still carries the old decision, which would
    then land on that unrelated case."""

    from faultmaven import CaseNotFoundError

    store = _LifecycleStore()
    client = _run_click(
        _FailingFM(CaseNotFoundError("gone", status_code=404)), store
    )
    assert store.deleted == [("T1", "C1", "111.222")]  # mapping evicted
    assert not _has_buttons(client.updates[-1])  # and no stale decision left armed


def test_lost_mapping_leaves_the_buttons_down():
    # Same stale-decision hazard: no case to submit against, and the next
    # @mention opens a fresh one.
    client = _run_click(FakeFM(), _LifecycleStore(case_id=None))
    assert not _has_buttons(client.updates[-1])
    assert any("lost track" in p.get("text", "") for p in client.posts)


def test_store_failure_still_settles_the_message():
    """Regression: the store read sits inside the pending window. An unguarded
    raise there stranded the message at "working…" — buttons gone, no reply."""

    client = _run_click(FakeFM(), _LifecycleStore(get_error=RuntimeError("db gone")))
    assert client.posts, "the failure must be reported in-thread"
    assert len(client.updates) >= 2, "the working note must be settled, not stranded"
    assert _has_buttons(client.updates[-1])  # a transient failure re-arms
    assert not _working_note(client.updates[-1])


def test_stale_button_click_is_rejected_and_disabled():
    """Clicking a button from an older turn must strip buttons and notify user ephemerally."""
    fm = FakeFM()
    # The thread has moved on to a newer turn (ts=999.999); the click is on the
    # older 111.222 message.
    store = _LifecycleStore(last_turn_ts="999.999")
    client = _run_click(fm, store)

    assert fm.turns == [], "Must not submit turn to backend"
    assert len(client.updates) == 1
    assert not _has_buttons(client.updates[0]), "Must disable stale buttons"
    assert any("previous turn and is no longer available" in p.get("text", "") for p in client.posts)


def test_the_reply_s_new_buttons_become_the_current_turn():
    """Regression: the ts came back as a SlackResponse, not a dict, so the
    isinstance(resp, dict) gate recorded nothing — every fresh set of buttons
    was left untracked and the stale-click guard never armed."""

    class _DecidingFM:
        """Answers the click with a turn that asks a new question."""

        def __init__(self) -> None:
            self.turns: list[tuple] = []

        def submit_turn(self, case_id, **kwargs) -> TurnResult:
            self.turns.append((case_id, kwargs))
            return TurnResult(
                agent_response="Should I proceed?",
                suggested_actions=[
                    {
                        "type": "DECIDE",
                        "label": "Yes",
                        "payload": "yes",
                        "intent": {
                            "type": "confirmation",
                            "confirmation_value": True,
                        },
                    }
                ],
            )

    store = _LifecycleStore()
    client = _run_click(_DecidingFM(), store)

    reply_ts = client.post_ts[-1]
    assert store.last_turn_ts == reply_ts, "the reply is now the current turn"
    assert store.last_action_ts == reply_ts, "its buttons are the live ones"


def test_a_plain_reply_still_becomes_the_current_turn():
    """No buttons on the reply, but the turn marker still has to advance — it is
    what rejects a click on the turn just superseded."""

    store = _LifecycleStore()
    client = _run_click(FakeFM(), store)

    assert store.last_turn_ts == client.post_ts[-1]
    assert store.last_action_ts is None


def test_a_store_failure_in_the_stale_check_still_runs_the_click():
    """The check lands after ack(), so an unguarded raise would drop the click
    with nothing on screen: no reply, no notice, buttons still armed."""

    fm = FakeFM()
    store = _LifecycleStore(turn_ts_error=RuntimeError("closed database"))
    client = _run_click(fm, store)

    assert fm.turns, "the click must still reach the backend"
    assert client.posts, "and its reply must still land in the thread"
