"""run_turn_and_post: post placeholder → run one turn → update it, addressing the
replier (@mention) and carrying a one-time etiquette note on a thread's first
reply. Runs synchronously (the caller holds the drop-if-busy gate)."""

from __future__ import annotations

import importlib.util
import sys

from slack_sdk.errors import SlackApiError

from faultmaven.client import TurnResult

_seq = 0


def _load_turn():
    global _seq
    _seq += 1
    name = f"_turn_rp{_seq}"
    spec = importlib.util.spec_from_file_location(name, "listeners/_turn.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeClient:
    def __init__(self, *, fail_post: bool = False, replies: list | None = None) -> None:
        self.fail_post = fail_post
        self.replies = replies or []
        self.posts: list = []
        self.updates: list = []

    def chat_postMessage(self, **kw):
        if self.fail_post:
            raise SlackApiError("cannot_post", {"error": "not_in_channel"})
        self.posts.append(kw)
        return {"ts": "PH1"}

    def chat_update(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    def conversations_replies(self, **kw):
        # Slack always returns the thread parent first and honors ``limit``, so
        # a limit with no room left for the reply comes back as the parent
        # alone — the case a limit=1 lookup silently loses.
        messages = [{"ts": kw.get("ts"), "text": "parent", "blocks": []}]
        messages.extend(self.replies)
        return {"messages": messages[: kw.get("limit", 100)]}


class FakeFM:
    def __init__(self, result: TurnResult | None = None) -> None:
        self.turns: list = []
        self.result = result or TurnResult(agent_response="on it")

    def create_case(self, *, title=None, initial_message=None, team_id=None):
        return "case_1"

    def submit_turn(self, case_id, **kwargs):
        self.turns.append((case_id, kwargs))
        return self.result


class FakeStore:
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


_COMMON = dict(channel="C", thread_ts="TS", team_id="T")


def _first_section_text(update) -> str:
    for b in update["blocks"]:
        if b.get("type") == "section":
            return b["text"]["text"]
    return ""


def _context_texts(update) -> list[str]:
    return [
        e.get("text", "")
        for b in update["blocks"]
        if b.get("type") == "context"
        for e in b.get("elements", [])
    ]


def test_posts_a_placeholder_then_updates_it():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    _turn.run_turn_and_post(client, fm, FakeStore(), text="hi", **_COMMON)
    assert len(client.posts) == 1
    assert client.updates[0]["ts"] == "PH1"


def test_reuses_an_existing_placeholder():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    _turn.run_turn_and_post(
        client, fm, FakeStore(), text="hi", placeholder_ts="PH_PRE", **_COMMON
    )
    assert client.posts == []
    assert client.updates[0]["ts"] == "PH_PRE"


def test_bails_without_running_when_it_cannot_post():
    _turn = _load_turn()
    client, fm = FakeClient(fail_post=True), FakeFM()
    _turn.run_turn_and_post(client, fm, FakeStore(), text="hi", **_COMMON)
    assert fm.turns == []
    assert client.updates == []


def test_addresses_the_replier_and_warns_on_first_turn():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    _turn.run_turn_and_post(
        client, fm, FakeStore(), text="hi", mention_user="U42", **_COMMON
    )
    update = client.updates[0]
    assert _first_section_text(update).startswith("<@U42> ")  # addressed
    # First reply carries the one-time "one at a time" etiquette note.
    assert _turn._INTRO_WARNING in _context_texts(update)


def test_no_warning_on_later_turns():
    _turn = _load_turn()
    client, fm, store = FakeClient(), FakeFM(), FakeStore()
    store.put("T", "C", "TS", "case_1")  # case already exists → not the first turn
    _turn.run_turn_and_post(
        client, fm, store, text="again", mention_user="U42", **_COMMON
    )
    assert _turn._INTRO_WARNING not in _context_texts(client.updates[0])


def test_forwards_files_to_submit_turn():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    files = [("app.log", b"boom", "text/plain")]
    _turn.run_turn_and_post(
        client, fm, FakeStore(), text="hi", files=files, **_COMMON
    )
    _, kw = fm.turns[0]
    assert kw["files"] == files


def _prev_choice_message(blocks="buttons") -> dict:
    """A previous turn's message, still carrying its choice buttons."""

    return {
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
        ]
        if blocks == "buttons"
        else blocks,
    }


def _with_buttons(store) -> None:
    store.put("T", "C", "TS", "case_1")
    store.record_turn(
        "T", "C", "TS", turn_ts="PREV_ACTION_TS", action_ts="PREV_ACTION_TS"
    )


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


def test_disables_previous_turn_actions_when_new_turn_runs():
    _turn = _load_turn()
    client = FakeClient(replies=[_prev_choice_message()])
    store = FakeStore()
    _with_buttons(store)

    _turn.run_turn_and_post(client, FakeFM(), store, text="next turn query", **_COMMON)

    assert len(client.updates) >= 2
    prev_update = client.updates[0]
    assert prev_update["ts"] == "PREV_ACTION_TS"
    assert not any(b["type"] == "actions" for b in prev_update["blocks"])
    assert any(
        "Choose an option:" in b["text"]["text"]
        for b in prev_update["blocks"]
        if b["type"] == "section"
    )


def test_records_the_posted_turn_when_it_has_decide_buttons():
    _turn = _load_turn()
    client = FakeClient()
    store = FakeStore()

    _turn.run_turn_and_post(
        client,
        FakeFM(result=_DECIDE_RESULT),
        store,
        text="hello",
        placeholder_ts="PH_ACTION",
        **_COMMON,
    )

    assert store.get_last_action_ts("T", "C", "TS") == "PH_ACTION"
    assert store.get_last_turn_ts("T", "C", "TS") == "PH_ACTION"


def test_records_the_turn_even_when_it_carries_no_buttons():
    """The turn marker is the stale-click guard's only anchor, so a plain reply
    has to move it too — erasing it would let a click on the previous turn's
    buttons (a stale client view, an offline queue) sail through as live."""

    _turn = _load_turn()
    client = FakeClient(replies=[_prev_choice_message()])
    store = FakeStore()
    _with_buttons(store)

    _turn.run_turn_and_post(
        client, FakeFM(), store, text="hello", placeholder_ts="PH_PLAIN", **_COMMON
    )

    assert store.get_last_turn_ts("T", "C", "TS") == "PH_PLAIN"
    assert store.get_last_action_ts("T", "C", "TS") is None


def test_a_failed_turn_leaves_the_previous_choice_buttons_alone():
    """The conversation never advanced, so the question the user can still
    answer must stay answerable."""

    _turn = _load_turn()

    class _FailingFM:
        def create_case(self, *, title=None, initial_message=None, team_id=None):
            return "case_1"

        def submit_turn(self, case_id, **kwargs):
            raise RuntimeError("backend down")

    client = FakeClient(replies=[_prev_choice_message()])
    store = FakeStore()
    _with_buttons(store)

    _turn.run_turn_and_post(client, _FailingFM(), store, text="next", **_COMMON)

    assert all(u["ts"] != "PREV_ACTION_TS" for u in client.updates)
    assert store.get_last_action_ts("T", "C", "TS") == "PREV_ACTION_TS"


def test_null_blocks_on_the_tracked_message_do_not_break_the_strip():
    """Slack sends ``"blocks": null`` on some messages, so ``.get("blocks", [])``
    hands back None rather than the default."""

    _turn = _load_turn()
    client = FakeClient(replies=[_prev_choice_message(blocks=None)])
    store = FakeStore()
    _with_buttons(store)

    _turn.run_turn_and_post(
        client, FakeFM(), store, text="next", placeholder_ts="PH", **_COMMON
    )

    assert all(u["ts"] != "PREV_ACTION_TS" for u in client.updates)
    assert store.get_last_action_ts("T", "C", "TS") is None
