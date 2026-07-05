"""run_turn_and_post + post_placeholder — the shared post/update flow that the
mention and shortcut surfaces rely on (incl. the placeholder_ts reuse that keeps
feedback instant before a slow file download)."""

from __future__ import annotations

import importlib.util

from faultmaven.client import TurnResult
from slack_sdk.errors import SlackApiError


def _load_turn():
    spec = importlib.util.spec_from_file_location("_turn_p", "listeners/_turn.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClient:
    def __init__(self, *, fail_post: bool = False) -> None:
        self.fail_post = fail_post
        self.posts: list = []
        self.updates: list = []
        self.token = "xoxb-test"

    def chat_postMessage(self, **kw):
        if self.fail_post:
            raise SlackApiError("cannot_post", {"error": "not_in_channel"})
        self.posts.append(kw)
        return {"ts": "PH_NEW"}

    def chat_update(self, **kw):
        self.updates.append(kw)
        return {"ok": True}


class FakeFM:
    def __init__(self) -> None:
        self.turns: list = []

    def create_case(self, *, title=None, initial_message=None):
        return "case_1"

    def submit_turn(self, case_id, **kwargs):
        self.turns.append((case_id, kwargs))
        return TurnResult(agent_response="on it")


class FakeStore:
    def __init__(self) -> None:
        self.m: dict = {}

    def get(self, t, c, th):
        return self.m.get((t, c, th))

    def put(self, t, c, th, cid):
        self.m[(t, c, th)] = cid


_COMMON = dict(channel="C", thread_ts="TS", team_id="T", text="hi")


def test_posts_a_placeholder_then_updates_it_when_none_given():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    _turn.run_turn_and_post(client, fm, FakeStore(), **_COMMON)
    assert len(client.posts) == 1  # posted its own placeholder
    assert client.updates[0]["ts"] == "PH_NEW"  # updated that same message


def test_reuses_an_existing_placeholder_and_posts_no_second_one():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    _turn.run_turn_and_post(
        client, fm, FakeStore(), placeholder_ts="PH_PRE", **_COMMON
    )
    assert client.posts == []  # did NOT post a second placeholder
    assert client.updates[0]["ts"] == "PH_PRE"  # updated the caller's placeholder


def test_bails_without_running_the_turn_when_it_cannot_post():
    _turn = _load_turn()
    client, fm = FakeClient(fail_post=True), FakeFM()
    _turn.run_turn_and_post(client, fm, FakeStore(), **_COMMON)
    assert fm.turns == []  # never reached submit_turn
    assert client.updates == []


def test_post_placeholder_returns_none_on_slack_error():
    _turn = _load_turn()
    assert _turn.post_placeholder(FakeClient(fail_post=True), "C", "TS") is None


def test_forwards_files_through_to_submit_turn():
    _turn = _load_turn()
    client, fm = FakeClient(), FakeFM()
    files = [("app.log", b"boom", "text/plain")]
    _turn.run_turn_and_post(
        client, fm, FakeStore(), placeholder_ts="PH", files=files, **_COMMON
    )
    _, kw = fm.turns[0]
    assert kw["files"] == files
