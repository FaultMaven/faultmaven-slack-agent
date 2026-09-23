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
_BOT_USER, _BOT_ID = "UBOT", "B1"


def _ctx():
    """Bolt's context, as the listeners read it."""
    return SimpleNamespace(team_id="T1", bot_user_id=_BOT_USER, bot_id=_BOT_ID)


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
        self.history_reads = 0
        self.fail_history_reads = 0

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return _Response(ts=f"ts{len(self.posts)}")

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return _Response(ts=kwargs.get("ts"))

    def reactions_add(self, **kwargs):
        self.reactions.append(kwargs)

    #: Thread history the probe reads. Default: humans only — a thread the
    #: agent has never been in.
    history: list = [{"ts": "1.0", "text": "the pods are crashlooping", "user": "U9"}]

    def conversations_replies(self, **kwargs):
        self.history_reads += 1
        if self.fail_history_reads:
            self.fail_history_reads -= 1
            raise RuntimeError("slack said no")
        return {"messages": self.history}

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
        context=_ctx(),
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
        context=_ctx(),
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


def _context_notes(update: dict) -> list[str]:
    return [
        e["text"]
        for b in update.get("blocks", [])
        if b.get("type") == "context"
        for e in b.get("elements", [])
    ]


def test_the_restarted_investigation_says_it_is_a_restart(tmp_path):
    """The other half of the reported bug: a new case is the right answer to a
    re-summons, but arriving with a case id the user never saw created, on a
    thread they were mid-conversation in, reads as the agent having quietly
    forgotten everything. Which is what happened — so it says so."""

    store, fm, client, app = _orphaned(tmp_path), _FakeFM(), _FakeClient(), _FakeApp()
    register_events(app, fm, store)

    app.handlers["app_mention"](
        event={"channel": "C1", "ts": "5.0", "thread_ts": "TS1", "text": "<@UBOT> hi"},
        context=_ctx(),
        client=client,
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    notes = _context_notes(client.updates[-1])
    assert any("couldn't find the earlier case" in n for n in notes)
    store.close()


def test_an_ordinary_first_summons_says_nothing_about_a_restart(tmp_path):
    """The note is about something that happened. A thread opening its first
    investigation has nothing to be told."""

    store = CaseStore(str(tmp_path / "cases.db"))
    fm, client, app = _FakeFM(), _FakeClient(), _FakeApp()
    register_events(app, fm, store)

    app.handlers["app_mention"](
        event={"channel": "C1", "ts": "5.0", "thread_ts": "TS1", "text": "<@UBOT> hi"},
        context=_ctx(),
        client=client,
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    notes = _context_notes(client.updates[-1])
    assert not any("earlier case" in n for n in notes)
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


# -- the map itself is gone ----------------------------------------------------
# A tombstone is only written when a turn 404s, so an absent row can also mean
# the map that held it did not survive a restart (an unpersisted volume, which
# docs/HOSTING.md warns about). The thread itself still says which case it was:
# the agent announced the id on the investigation's opening reply.
def _bot_reply(text: str, *, case_id: str | None = None) -> dict:
    message = {
        "ts": "1.1",
        "text": text,
        "user": _BOT_USER,
        "bot_id": _BOT_ID,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }
    if case_id:
        message["blocks"].append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f":card_index_dividers: `{case_id}`"}
                ],
            }
        )
    return message


_HUMAN = {"ts": "1.0", "text": "the pods are crashlooping", "user": "U9"}
_AN_INVESTIGATION = [_HUMAN, _bot_reply("Looking at the restarts", case_id="case_abc")]


def test_a_lost_mapping_is_recovered_from_the_thread_itself(tmp_path):
    """The likelier cause of the reported silence: no 404 ever happened, the
    row simply isn't there any more. The case usually still exists — so the
    thread is re-linked to it and the reply answered, rather than the
    conversation being declared lost on the strength of a missing local file."""

    store = CaseStore(str(tmp_path / "cases.db"))  # empty, as after the loss
    fm, client, app = _FakeFM(), _FakeClient(), _FakeApp()
    client.history = _AN_INVESTIGATION
    register_events(app, fm, store)

    _reply(app, client)

    assert store.get("T1", "C1", "TS1") == "case_abc"
    assert [c for c, _ in fm.turns] == ["case_abc"], "answered on its own case"
    assert not fm.cases, "and no replacement case was invented"
    # Its history is already in the case; re-sending it as evidence would hand
    # the engine its own transcript as a fresh symptom.
    assert store.is_seeded("T1", "C1", "TS1")
    assert fm.turns[0][1].get("pasted_content") is None
    store.close()


def test_a_thread_the_agent_spoke_in_but_never_opened_a_case_for(tmp_path):
    """The placeholder goes up before the case is created, so a summons whose
    create_case failed leaves the agent's own message in a thread that never
    had an investigation. Recovering it would re-link nothing, and telling the
    thread its case 'could not be found' would describe a case that never
    existed. Both are wrong; silence is correct here."""

    store = CaseStore(str(tmp_path / "cases.db"))
    fm, client, app = _FakeFM(), _FakeClient(), _FakeApp()
    client.history = [_HUMAN, _bot_reply(":hourglass_flowing_sand: Working…")]  # no case id
    register_events(app, fm, store)

    _reply(app, client)

    assert client.posts == []
    assert not fm.turns
    assert store.get("T1", "C1", "TS1") is None
    store.close()


def test_another_bots_case_pointer_is_not_ours_to_adopt(tmp_path):
    """An incident thread is usually full of other bots — one of them is
    generally what started it — so the match is on this app, not on any bot."""

    store = CaseStore(str(tmp_path / "cases.db"))
    fm, client, app = _FakeFM(), _FakeClient(), _FakeApp()
    impostor = _bot_reply("relayed", case_id="case_xyz")
    impostor["user"], impostor["bot_id"] = "UOTHER", "B_ALERTMANAGER"
    client.history = [_HUMAN, impostor]
    register_events(app, fm, store)

    _reply(app, client)

    assert client.posts == []
    assert store.get("T1", "C1", "TS1") is None
    store.close()


def test_the_thread_history_is_read_once_per_thread(tmp_path):
    """The probe sits on the path that sees every threaded message in every
    channel the agent is in. Reading the history per message would put a Slack
    call behind ordinary chatter; a thread already found to be someone else's
    is remembered instead."""

    store = CaseStore(str(tmp_path / "cases.db"))
    fm, client, app = _FakeFM(), _FakeClient(), _FakeApp()  # history: humans only
    register_events(app, fm, store)

    _reply(app, client, ts="2.0")
    _reply(app, client, ts="3.0", text="still broken")
    _reply(app, client, ts="4.0", text="anyone?")

    assert client.history_reads == 1
    assert client.posts == []
    store.close()


def test_a_probe_that_could_not_read_the_thread_tries_again(tmp_path):
    """The one-shot key stands for an answer, not an attempt. Recording it
    before the call would let a single 429 or timeout drop that thread for the
    life of the process — reintroducing the silence, by the failure path."""

    store = CaseStore(str(tmp_path / "cases.db"))
    fm, client, app = _FakeFM(), _FakeClient(), _FakeApp()
    client.history = _AN_INVESTIGATION
    client.fail_history_reads = 1
    register_events(app, fm, store)

    _reply(app, client, ts="2.0")
    assert store.get("T1", "C1", "TS1") is None  # the read failed

    _reply(app, client, ts="3.0", text="hello?")
    assert store.get("T1", "C1", "TS1") == "case_abc"  # retried, and recovered
    store.close()


def test_a_content_free_reply_never_costs_a_history_read(tmp_path):
    store = CaseStore(str(tmp_path / "cases.db"))
    fm, client, app = _FakeFM(), _FakeClient(), _FakeApp()
    client.history = _AN_INVESTIGATION
    register_events(app, fm, store)

    _reply(app, client, ts="2.0", text="   ")

    assert client.history_reads == 0
    assert client.posts == []
    store.close()


def test_a_recovered_case_that_is_also_gone_falls_back_to_the_notice(tmp_path):
    """Recovery removes the guessing, it does not assume. If the case named in
    the thread is gone too, the turn 404s and the ordinary tombstone path takes
    over — with the message it was always going to give."""

    from faultmaven import CaseNotFoundError

    class _GoneFM(_FakeFM):
        def submit_turn(self, case_id, **kwargs):
            raise CaseNotFoundError("gone", status_code=404)

    store = CaseStore(str(tmp_path / "cases.db"))
    fm, client, app = _GoneFM(), _FakeClient(), _FakeApp()
    client.history = _AN_INVESTIGATION
    register_events(app, fm, store)

    _reply(app, client, ts="2.0")

    assert store.is_unlinked("T1", "C1", "TS1")
    assert "couldn't find" in client.updates[-1]["text"]
    store.close()


# -- the 1:1 must not be bricked ----------------------------------------------
def test_a_told_dm_thread_is_not_silenced_for_good(tmp_path):
    """There is no @mention in a 1:1, so a tombstone that outlived the telling
    would answer every later message the same way forever — and the 404 behind
    it is not always a deleted case (a proxy 404 during a deploy arrives as the
    same error). The user is told once; their next message starts fresh."""

    store = _orphaned(tmp_path, channel="D1", thread="TS1")
    fm, client = _FakeFM(), _FakeClient()
    assistant = build_assistant(fm, store)
    handler = assistant._user_message_listeners[0].ack_function
    say = _FakeSay()

    def send(text: str, ts: str) -> None:
        handler(
            payload={"channel": "D1", "thread_ts": "TS1", "ts": ts, "text": text},
            context=_ctx(),
            client=client,
            set_status=lambda _s: None,
            say=say,
            logger=_LOG,
        )
        turn_mod.drain_turns(5.0)

    send("still there?", "1.0")
    assert "couldn't find" in say.posts[0]
    assert not fm.cases

    send("ok, new problem then", "2.0")
    assert fm.cases == ["T1"], "the next message opens a case normally"
    assert "couldn't find" not in (say.posts[-1] or "")
    store.close()
