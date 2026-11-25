"""A turn says who wrote its query (FaultMaven/faultmaven#2717, contract 15.18.0).

Core searches a turn's query for system output to offer back as data, and
only a query the user TYPED should be searched: a button's words were chosen
from the engine's own offer, and the shortcut's seed was written by this agent.
So the turn carries ``query_source``: ``chosen`` for a click, ``generated`` for
every query this agent writes (the shortcut's seed, the summons, a files-only
message's query, the doctor's smoke turn), and nothing for a typed message
(core reads absent as ``typed``). The field is sent without moving the
contract pin.

The client tests drive the real ``FaultMavenClient`` over ``httpx.MockTransport``;
the listener tests drive the registered handlers through the real pipeline with
the recording core stand-in of ``tests.test_requester``. Nothing here talks to
Slack, the API or an LLM.
"""

from __future__ import annotations

import importlib.util
from urllib.parse import parse_qs

import httpx
import pytest

import listeners._turn as turn_mod
import listeners.assistant as assistant_mod
import listeners.events as events_mod
from faultmaven.client import FaultMavenAPIError, FaultMavenError
from listeners.assistant import build_assistant
from listeners.events import register_events
from listeners.shortcuts import register_shortcuts
from store import CaseStore
from tests.test_requester import (
    _FM,
    _LOG,
    _App,
    _Response,
    _Slack,
    _click,
    _ctx,
    _live_thread,
)
from tests.test_turn_recovery import _Backend, _client, _ok, _read_timeout

_KEY = "query-source-test-key"


def _form(request: httpx.Request) -> dict[str, list[str]]:
    """The url-encoded form a turn without files sends."""

    return parse_qs(request.content.decode())


def _turn_posts(backend: _Backend) -> list[httpx.Request]:
    return [r for r in backend.requests if r.url.path.endswith("/turns")]


# -- the client: the field reaches the HTTP request -----------------------------
#: Core's published values (the turn route's ``query_source`` form field).
@pytest.mark.parametrize("source", ["typed", "chosen", "generated"])
def test_each_source_reaches_the_form(source):
    backend = _Backend(_ok())
    client, _ = _client(backend)

    client.submit_turn(
        "c1", idempotency_key=_KEY, requester=None, query="x", query_source=source
    )

    (post,) = _turn_posts(backend)
    assert _form(post)["query_source"] == [source]


def test_the_source_rides_beside_files_as_a_form_part():
    backend = _Backend(_ok())
    client, _ = _client(backend)

    client.submit_turn(
        "c1",
        idempotency_key=_KEY,
        requester=None,
        query="Please investigate this.",
        files=[("err.log", b"boom", "text/plain")],
        query_source="generated",
    )

    (post,) = _turn_posts(backend)
    assert b'name="query_source"\r\n\r\ngenerated\r\n' in post.content


def test_no_source_sends_no_field():
    """Absent is how a typed message travels: core reads it as typed."""

    backend = _Backend(_ok())
    client, _ = _client(backend)

    client.submit_turn(
        "c1", idempotency_key=_KEY, requester=None, query="x", query_source=None
    )

    (post,) = _turn_posts(backend)
    assert "query_source" not in _form(post)


def test_an_unknown_source_is_refused_before_anything_is_sent():
    backend = _Backend(_ok())
    client, _ = _client(backend)

    with pytest.raises(ValueError, match="query_source"):
        client.submit_turn(
            "c1", idempotency_key=_KEY, requester=None, query="x", query_source="pasted"
        )

    assert _turn_posts(backend) == []


def test_a_source_alone_is_not_a_turn():
    """It describes the query; it is not content, so the empty-turn guard holds."""

    backend = _Backend(_ok())
    client, _ = _client(backend)

    with pytest.raises(FaultMavenError, match="at least one of"):
        client.submit_turn(
            "c1", idempotency_key=_KEY, requester=None, query_source="chosen"
        )

    assert _turn_posts(backend) == []


def test_a_recovery_resend_carries_the_same_source():
    """Core fingerprints the field (a key re-sent with another source is a 409
    IDEMPOTENCY_KEY_REUSE), so every attempt must carry it."""

    backend = _Backend(_read_timeout, _ok(replayed=True))
    client, _ = _client(backend)

    assert client.submit_turn(
        "c1", idempotency_key=_KEY, requester=None, query="x", query_source="chosen"
    ).replayed

    posts = _turn_posts(backend)
    assert len(posts) == 2
    assert [_form(p)["query_source"] for p in posts] == [["chosen"], ["chosen"]]


# -- the listeners: each surface says who wrote its query -----------------------
@pytest.fixture
def store(tmp_path):
    s = CaseStore(str(tmp_path / "cases.db"))
    yield s
    s.close()


def _shortcut(store, fm, *, message_ts: str = "1785872177.123456") -> None:
    app = _App()
    register_shortcuts(app, fm, store)
    app.shortcuts["fm_investigate_message"](
        ack=lambda: None,
        shortcut={
            "trigger_id": "trig1",
            "channel": {"id": "C1"},
            "message": {"ts": message_ts, "text": "[FIRING:1] DiskFull"},
            "response_url": None,
        },
        context=_ctx(),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)


def _thread_reply(store, fm, *, thread_ts: str, text: str) -> None:
    app = _App()
    register_events(app, fm, store)
    app.events["message"](
        event={"channel": "C1", "channel_type": "channel", "thread_ts": thread_ts,
               "ts": "1785872200.000001", "user": "U1", "text": text},
        context=_ctx(),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)


def test_a_button_click_sends_chosen(store):
    fm = _FM()

    _click(store, fm, _ctx())

    assert [t["query"] for t in fm.turns] == ["fixed"]
    assert [t["query_source"] for t in fm.turns] == ["chosen"]


def test_the_shortcut_seed_sends_generated(store):
    fm = _FM()

    _shortcut(store, fm)

    assert [t["query"] for t in fm.turns] == ["Please investigate this."]
    assert [t["query_source"] for t in fm.turns] == ["generated"]


def test_a_typed_reply_after_the_shortcut_sends_no_source(store):
    """The source belongs to one turn, not the thread: the reply typed under
    the shortcut's case goes as typed (no field), not as the seed's source."""

    fm = _FM()
    _shortcut(store, fm, message_ts="1785872177.123456")

    _thread_reply(store, fm, thread_ts="1785872177.123456", text="here is the log")

    assert [t["query"] for t in fm.turns] == ["Please investigate this.", "here is the log"]
    assert [t.get("query_source") for t in fm.turns] == ["generated", None]


def test_a_click_puts_chosen_on_the_wire(store):
    """End to end through the real client: the click's form carries the field."""

    backend = _Backend(_ok())
    client, _ = _client(backend)

    _click(store, client, _ctx())

    (post,) = _turn_posts(backend)
    form = _form(post)
    assert form["query"] == ["fixed"]
    assert form["query_source"] == ["chosen"]


# -- the agent's own fallback queries go as generated ---------------------------
_LOG_FILE = [("app.log", b"OOMKilled", "text/plain")]


def _files_only(monkeypatch) -> None:
    """Every surface's download yields one readable file and no snippet."""

    def download(token, event):
        return list(_LOG_FILE), None, []

    monkeypatch.setattr(events_mod, "download_message_content", download)
    monkeypatch.setattr(assistant_mod, "download_message_content", download)


def _with_files(event: dict, files: bool) -> dict:
    return {**event, "files": [{"id": "F1", "name": "app.log"}]} if files else event


def _mention(store, fm, *, text: str, files: bool = False) -> None:
    app = _App()
    register_events(app, fm, store)
    app.events["app_mention"](
        event=_with_files(
            {"channel": "C1", "ts": "5.0", "thread_ts": "TS1", "user": "U1",
             "text": text},
            files,
        ),
        context=_ctx(),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)


def _dm(store, fm, *, text: str, files: bool = False) -> None:
    app = _App()
    register_events(app, fm, store)
    app.events["message"](
        event=_with_files(
            {"channel": "D1", "channel_type": "im", "ts": "9.0", "user": "U1",
             "text": text},
            files,
        ),
        context=_ctx(),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)


def _panel(store, fm, *, text: str, files: bool = False) -> None:
    build_assistant(fm, store)._user_message_listeners[0].ack_function(
        payload=_with_files(
            {"channel": "D1", "thread_ts": "TS9", "ts": "1.0", "user": "U1",
             "text": text},
            files,
        ),
        context=_ctx(),
        client=_Slack(),
        set_status=lambda _status: None,
        say=lambda *a, **k: _Response(ts="say1.000"),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)


def _reply(store, fm, *, text: str, files: bool = False) -> None:
    _live_thread(store)
    app = _App()
    register_events(app, fm, store)
    app.events["message"](
        event=_with_files(
            {"channel": "C1", "channel_type": "channel", "thread_ts": "TS1",
             "ts": "6.0", "user": "U1", "text": text},
            files,
        ),
        context=_ctx(),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)


def _sent(fm) -> list[tuple]:
    return [(t["query"], t.get("query_source")) for t in fm.turns]


def test_a_bare_mention_s_summons_sends_generated(store):
    fm = _FM()

    _mention(store, fm, text="<@UBOT>")

    assert _sent(fm) == [(turn_mod.SUMMONS_TEXT, "generated")]


class _Pre28FM(_FM):
    """A core older than contract 2.8.0: it refuses the empty turn with a 400."""

    def submit_turn(self, case_id, **kwargs):
        if kwargs.get("query") == "":
            self.turns.append(kwargs)
            raise FaultMavenAPIError("rejected", status_code=400)
        return super().submit_turn(case_id, **kwargs)


def test_the_summons_re_sent_for_a_refused_empty_turn_sends_generated(store):
    """The empty turn carries no source; its fallback is the agent's text."""

    fm = _Pre28FM()
    _live_thread(store)

    _mention(store, fm, text="<@UBOT>")

    assert _sent(fm) == [("", None), (turn_mod.SUMMONS_TEXT, "generated")]


def test_a_files_only_dm_summons_sends_generated(store, monkeypatch):
    _files_only(monkeypatch)
    fm = _FM()

    _dm(store, fm, text="", files=True)

    assert _sent(fm) == [(turn_mod.FILES_ONLY_QUERY, "generated")]


def test_a_files_only_panel_message_sends_generated(store, monkeypatch):
    _files_only(monkeypatch)
    fm = _FM()

    _panel(store, fm, text="", files=True)

    assert _sent(fm) == [(turn_mod.FILES_ONLY_QUERY, "generated")]


def test_a_files_only_thread_reply_sends_generated(store, monkeypatch):
    _files_only(monkeypatch)
    fm = _FM()

    _reply(store, fm, text="", files=True)

    assert _sent(fm) == [(events_mod.FILES_ONLY_REPLY_QUERY, "generated")]


@pytest.mark.parametrize("surface", [_mention, _dm, _panel, _reply])
@pytest.mark.parametrize("files", [False, True])
def test_a_typed_message_sends_no_source(store, monkeypatch, surface, files):
    """With or without a file, the user's own words go as typed (no field)."""

    _files_only(monkeypatch)
    fm = _FM()
    prefix = "<@UBOT> " if surface is _mention else ""

    surface(store, fm, text=f"{prefix}the pods are crashlooping", files=files)

    assert _sent(fm) == [("the pods are crashlooping", None)]


def test_the_doctor_s_smoke_turn_sends_generated():
    spec = importlib.util.spec_from_file_location(
        "_preflight_query_source", "scripts/preflight.py"
    )
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)
    # One step per request: the case it opens, then its turn.
    backend = _Backend(httpx.Response(201, json={"case_id": "c1"}), _ok())
    client, _ = _client(backend)

    assert preflight.check_turn_contract(client)

    (post,) = _turn_posts(backend)
    assert _form(post)["query_source"] == ["generated"]
