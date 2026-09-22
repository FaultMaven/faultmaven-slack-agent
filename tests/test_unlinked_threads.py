"""What a thread is told once its case can't be found.

One story across three surfaces, so it lives in one file. A case can go missing
under a thread that is still very much alive — deleted from the dashboard, lost
to a DB reset, absent from a restore — and the thread only finds out when
someone next writes into it. Before this, all three surfaces got that moment
wrong in a different way: a channel reply was dropped without a word, a DM
quietly opened a fresh case and answered as though nothing had been said
before it, and a button click blamed the agent for losing track.

The rule they now share: the message can't be answered, because the
investigation it was written into is gone, and saying so is not optional.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import listeners._turn as turn_mod
from faultmaven.client import TurnResult
from listeners.assistant import build_assistant
from listeners.events import register_events
from store import CaseStore

_LOG = logging.getLogger("test")


class _FakeFM:
    def __init__(self) -> None:
        self.cases: list = []
        self.turns: list = []

    def create_case(self, *, title=None, initial_message=None, team_id=None):
        self.cases.append(team_id)
        return f"case_{len(self.cases)}"

    def submit_turn(self, case_id, **kwargs):
        self.turns.append((case_id, kwargs))
        return TurnResult(agent_response="on it")


class _Response(dict):
    """Slack's SlackResponse supports both ``resp["ts"]`` and ``resp.get("ts")``
    — and the codebase uses each in different places, so the fake must too."""


class _FakeClient:
    """Enough Slack for the paths under test; every call is recorded."""

    token = "xoxb-test"

    def __init__(self) -> None:
        self.posts: list = []
        self.updates: list = []
        self.reactions: list = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return _Response(ts=f"ts{len(self.posts)}")

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return _Response(ts=kwargs.get("ts"))

    def reactions_add(self, **kwargs):
        self.reactions.append(kwargs)

    def conversations_replies(self, **kwargs):
        return {"messages": [{"ts": "1.0", "text": "the pods are crashlooping"}]}

    @property
    def texts(self) -> list[str]:
        return [p.get("text", "") for p in self.posts]


class _FakeApp:
    def __init__(self) -> None:
        self.handlers: dict = {}

    def event(self, name):
        def register(fn):
            self.handlers[name] = fn
            return fn

        return register


def _orphaned(tmp_path, channel="C1", thread="TS1") -> CaseStore:
    """A store holding one thread whose case has since gone missing."""

    store = CaseStore(str(tmp_path / "cases.db"))
    store.put("T1", channel, thread, "dead_case")
    store.mark_seeded("T1", channel, thread)
    store.mark_unlinked("T1", channel, thread)
    return store


def _reply(app, client, *, ts="2.0", text="can we continue the conversation"):
    app.handlers["message"](
        event={
            "channel": "C1",
            "channel_type": "channel",
            "thread_ts": "TS1",
            "ts": ts,
            "text": text,
        },
        context=SimpleNamespace(team_id="T1", bot_user_id="UBOT"),
        client=client,
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)


# -- the channel thread that used to answer -----------------------------------
def test_a_reply_in_an_orphaned_thread_is_answered_not_dropped(tmp_path):
    """The reported bug. The thread is unmapped, which is also what a stranger's
    thread looks like — and a stranger's thread must be ignored. The tombstone
    is what tells the two apart."""

    store, fm, client, app = _orphaned(tmp_path), _FakeFM(), _FakeClient(), _FakeApp()
    register_events(app, fm, store)

    _reply(app, client)

    assert client.texts, "the reply must not vanish into silence"
    assert "couldn't find" in client.texts[0]
    assert not fm.turns, "and it must not be answered on some other case"
    store.close()


def test_a_thread_we_never_owned_is_still_ignored(tmp_path):
    """The other half of the same rule: answering here would make the bot
    respond to any threaded message in any channel it sits in."""

    store = CaseStore(str(tmp_path / "cases.db"))
    fm, client, app = _FakeFM(), _FakeClient(), _FakeApp()
    register_events(app, fm, store)

    _reply(app, client)

    assert client.posts == []
    store.close()


def test_the_notice_is_posted_once_not_on_every_reply(tmp_path):
    """A war-room thread keeps talking after FaultMaven drops out of it. Saying
    this on every message would turn one piece of bad news into a heckle."""

    store, fm, client, app = _orphaned(tmp_path), _FakeFM(), _FakeClient(), _FakeApp()
    register_events(app, fm, store)

    _reply(app, client, ts="2.0")
    _reply(app, client, ts="3.0", text="anyone?")
    _reply(app, client, ts="4.0", text="ok, doing it by hand")

    assert len(client.posts) == 1
    store.close()


def test_a_content_free_reply_does_not_spend_the_notice(tmp_path):
    """The notice is owed to a person who wrote something. Spending it on a
    lone emoji or a mention of someone else would mean the next real message —
    the one worth explaining to — gets the silence this whole change is about."""

    store, fm, client, app = _orphaned(tmp_path), _FakeFM(), _FakeClient(), _FakeApp()
    register_events(app, fm, store)

    _reply(app, client, ts="2.0", text="   ")
    assert client.posts == []
    assert store.unlink_notice_pending("T1", "C1", "TS1")

    _reply(app, client, ts="3.0", text="so what now?")
    assert len(client.posts) == 1
    store.close()


def test_a_mention_is_the_way_back(tmp_path):
    """The notice asks for an @mention, so an @mention has to work: a new case,
    and the thread behaving like any other live thread afterwards."""

    store, fm, client, app = _orphaned(tmp_path), _FakeFM(), _FakeClient(), _FakeApp()
    register_events(app, fm, store)

    app.handlers["app_mention"](
        event={
            "channel": "C1",
            "ts": "5.0",
            "thread_ts": "TS1",
            "text": "<@UBOT> let's pick this back up",
        },
        context=SimpleNamespace(team_id="T1", bot_user_id="UBOT"),
        client=client,
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    assert fm.cases == ["T1"], "a fresh case, opened deliberately"
    assert store.get("T1", "C1", "TS1") == "case_1"
    assert not store.is_unlinked("T1", "C1", "TS1")
    # The new case knows nothing, so the thread's history is replayed into it
    # exactly as it would be for a thread FaultMaven had never seen.
    assert "crashlooping" in fm.turns[0][1]["pasted_content"]
    store.close()


# -- the 1:1 that quietly started over ----------------------------------------
class _FakeSay:
    def __init__(self) -> None:
        self.posts: list = []

    def __call__(self, text=None, blocks=None, **kwargs):
        self.posts.append(text)
        return _Response(ts=f"say{len(self.posts)}.000")


def test_a_dm_says_the_case_is_gone_instead_of_starting_over_silently(tmp_path):
    """The DM surface's version of the bug: it never consulted the map as a
    gate, so it opened a fresh case and answered — presenting a reply built on
    nothing as the continuation of a conversation the user could still scroll
    up and read."""

    store = _orphaned(tmp_path, channel="D1", thread="TS1")
    fm, client = _FakeFM(), _FakeClient()
    assistant = build_assistant(fm, store)
    handler = assistant._user_message_listeners[0].ack_function
    say = _FakeSay()

    handler(
        payload={"channel": "D1", "thread_ts": "TS1", "ts": "1.0", "text": "still there?"},
        context=SimpleNamespace(team_id="T1"),
        client=client,
        set_status=lambda _status: None,
        say=say,
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    assert not fm.cases, "no case may be opened behind the user's back"
    assert not fm.turns
    assert "couldn't find" in say.posts[0]
    # A DM has no @mention to offer, so it must not ask for one.
    assert "@mention" not in say.posts[0]
    store.close()
