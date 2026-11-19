"""A turn names who asked, as an opaque id, so one account's people take turns
(FaultMaven/faultmaven#2852, contract 15.6.0).

Every Slack user of a workspace reaches core as that workspace's one service
account, so core's worker admission saw one principal: one person's batch made
everyone else in the workspace wait behind it. The turn now carries
``X-FaultMaven-Requester``, an HMAC of the Slack user under a secret this agent
already holds (``listeners._turn.requester_for``), which core uses to order that
account's own jobs and for nothing else. The raw Slack id never reaches core,
and the core client holds neither it nor the key.

The client tests drive the real ``FaultMavenClient`` over ``httpx.MockTransport``
on a fake clock; the listener tests drive each registered handler through the
real pipeline with a recording core stand-in. Nothing here talks to Slack, the
API or an LLM.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
from types import SimpleNamespace

import httpx
import pytest

import listeners._turn as turn_mod
from faultmaven.client import REQUESTER_HEADER, FaultMavenClient, TurnResult
from listeners import register_listeners
from listeners._turn import requester_for
from listeners.actions import apply_action, register_actions, register_data_box
from listeners.assistant import build_assistant
from listeners.events import register_events
from listeners.shortcuts import register_shortcuts
from rendering import DATA_BOX_BLOCK_ID, DATA_BOX_CALLBACK_ID, DATA_BOX_INPUT_ID
from store import CaseStore
from tests._clock import FakeClock

_LOG = logging.getLogger("test")
_KEY = "xapp-1-requester-test-key"
_USER = "U0ALICE42"
#: Core's published grammar for the header (the turn route's parameter
#: description, contract 15.6.0); the schema itself carries no pattern.
_PUBLISHED = re.compile(r"[A-Za-z0-9_-]{16,64}")


@pytest.fixture(autouse=True)
def _keyed(monkeypatch):
    """Every test runs under ``_KEY``; monkeypatch restores the module's key."""

    monkeypatch.setattr(turn_mod, "_requester_key", _KEY)


def _ctx(user_id: str | None = _USER, *, team_id: str | None = "T1", enterprise_id=None):
    return SimpleNamespace(
        team_id=team_id,
        enterprise_id=enterprise_id,
        user_id=user_id,
        bot_user_id="UBOT",
        bot_id="B1",
    )


def _oracle(key: str, namespace: str, user: str) -> str:
    """The rule, written out independently of the code under test."""

    message = b"faultmaven-requester\0" + f"{namespace}:{user}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()[:32]


def _encodings(raw: str) -> list[str]:
    data = raw.encode()
    return [
        raw,
        data.hex(),
        base64.b64encode(data).decode(),
        base64.urlsafe_b64encode(data).decode().rstrip("="),
    ]


def _assert_opaque(value: str, *raws: str) -> None:
    """``value`` is not, and holds no, plain/hex/base64 encoding of any of
    ``raws``, nor is it a prefix of one; its hex decodes to none of them."""

    decoded = bytes.fromhex(value)
    for raw in raws:
        for encoded in _encodings(raw):
            assert encoded not in value, f"{raw!r} is readable in {value!r}"
            assert value not in encoded, f"{value!r} is a cut of an encoding of {raw!r}"
        assert raw.encode() not in decoded, f"{raw!r} decodes out of {value!r}"


# -- the helper ----------------------------------------------------------------
def test_the_id_is_the_keyed_domain_separated_hmac():
    assert requester_for(_ctx()) == _oracle(_KEY, "T1", _USER)


def test_the_id_is_opaque_and_inside_the_published_grammar():
    value = requester_for(_ctx())

    assert _PUBLISHED.fullmatch(value) and len(value) == 32
    _assert_opaque(value, "T1", _USER, f"T1:{_USER}", f"T1:{_USER}"[:16])


def test_the_id_depends_on_the_key(monkeypatch):
    """A keyless encoding of the identity would be the same under any key."""

    first = requester_for(_ctx())
    monkeypatch.setattr(turn_mod, "_requester_key", "another-key")

    assert requester_for(_ctx()) != first


def test_the_id_is_stable_for_one_key_and_distinct_per_person():
    assert requester_for(_ctx()) == requester_for(_ctx())
    assert requester_for(_ctx()) != requester_for(_ctx("U0BOB"))
    # The same Slack id in another workspace is another person.
    assert requester_for(_ctx()) != requester_for(_ctx(team_id="T2"))


def test_an_enterprise_grid_install_gives_one_id_per_person_on_every_surface():
    """Org-wide, an event arrives with no team while a click carries the
    workspace's; the enterprise is the namespace for both."""

    event = requester_for(_ctx(team_id=None, enterprise_id="E1"))
    click = requester_for(_ctx(team_id="T1", enterprise_id="E1"))

    assert event == click == _oracle(_KEY, "E1", _USER)
    assert requester_for(_ctx(team_id="T1")) == _oracle(_KEY, "T1", _USER)


@pytest.mark.parametrize("user", [None, ""], ids=["none", "empty"])
def test_no_user_gives_no_id(user):
    assert requester_for(_ctx(user)) is None


def test_no_key_gives_no_id(monkeypatch):
    monkeypatch.setattr(turn_mod, "_requester_key", "")

    assert requester_for(_ctx()) is None


def _settings(monkeypatch, tmp_path, transport: str):
    """One environment holding both secrets; the transport picks the key."""

    from config import Settings

    env = {
        "SLACK_TRANSPORT": transport,
        "SLACK_BOT_TOKEN": "xoxb-x",
        "SLACK_APP_TOKEN": "xapp-socket-secret",
        "SLACK_SIGNING_SECRET": "http-signing-secret",
        "SLACK_CLIENT_ID": "123.456",
        "SLACK_CLIENT_SECRET": "client-secret",
        "SLACK_DATABASE_URL": f"sqlite:///{tmp_path / 'oauth.db'}",
        "CREDENTIAL_STORE_PATH": str(tmp_path / "credentials.db"),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_the_key_is_the_signing_secret_under_http_and_the_app_token_under_socket(
    monkeypatch, tmp_path
):
    http = _settings(monkeypatch, tmp_path, "http")
    socket = _settings(monkeypatch, tmp_path, "socket")

    assert http.requester_key == "http-signing-secret"
    assert socket.requester_key == "xapp-socket-secret"
    assert _oracle(http.requester_key, "T1", _USER) != _oracle(socket.requester_key, "T1", _USER)


@pytest.mark.parametrize("transport", ["http", "socket"])
def test_registering_the_listeners_installs_the_transport_key(monkeypatch, tmp_path, transport):
    from unittest.mock import MagicMock

    settings = _settings(monkeypatch, tmp_path, transport)
    monkeypatch.setattr(turn_mod, "_requester_key", "")

    register_listeners(
        MagicMock(), MagicMock(), MagicMock(), None, requester_key=settings.requester_key
    )

    assert requester_for(_ctx()) == _oracle(settings.requester_key, "T1", _USER)


# -- submit_turn -------------------------------------------------------------
_ANSWER = {"agent_response": "the disk filled at 02:14", "turn_number": 3}
_OPAQUE = "0123456789abcdef0123456789abcdef"


def _ok(*, replayed: bool = False) -> httpx.Response:
    headers = {"X-Idempotency-Replayed": "true"} if replayed else {}
    return httpx.Response(200, json=_ANSWER, headers=headers)


def _read_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow turn", request=request)


class _Backend:
    """Plays ``script`` one step per turn POST and records every request."""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/dev-login"):
            return httpx.Response(200, json={"access_token": f"tok{len(self.requests)}"})
        if request.url.path == "/api/v1/cases":
            return httpx.Response(201, json={"case_id": "case_new"})
        self.requests.append(request)
        assert len(self.requests) <= 50, "the recovery loop did not stop"
        step = self.script[min(len(self.requests), len(self.script)) - 1]
        return step(request) if callable(step) else step

    @property
    def requesters(self) -> list[str | None]:
        """The header on each turn POST (a capabilities probe is not a turn)."""

        return [
            r.headers.get(REQUESTER_HEADER)
            for r in self.requests
            if r.url.path.endswith("/turns")
        ]

    def assert_never_raw(self) -> None:
        for request in self.requests:
            sent = " ".join(request.headers.values()) + request.content.decode(errors="replace")
            assert _USER not in sent, "the raw Slack id reached core"
            assert _KEY not in sent, "the requester key reached core"


def _client(backend: _Backend, **kw) -> FaultMavenClient:
    client = FaultMavenClient("http://test", token=kw.pop("token", "tok"), **kw)
    client._http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(backend))
    FakeClock().install(client)
    return client


def test_the_first_post_carries_the_id():
    backend = _Backend(_ok())

    _client(backend).submit_turn("c1", idempotency_key="k" * 16, requester=_OPAQUE, query="x")

    assert backend.requesters == [_OPAQUE]


def test_a_keyed_recovery_attempt_carries_the_same_id():
    """The re-send is the same turn: same key, same bytes, same requester."""

    backend = _Backend(_read_timeout, _ok(replayed=True))

    _client(backend).submit_turn("c1", idempotency_key="k" * 16, requester=_OPAQUE, query="x")

    assert backend.requesters == [_OPAQUE, _OPAQUE]
    assert {r.headers["idempotency-key"] for r in backend.requests} == {"k" * 16}


def test_the_401_re_auth_re_post_carries_the_id():
    """The re-POST after a re-login is the attempt that runs the turn."""

    backend = _Backend(httpx.Response(401, json={"detail": "expired"}), _ok())

    _client(backend, token="", dev_login_username="admin").submit_turn(
        "c1", idempotency_key="k" * 16, requester=_OPAQUE, query="x"
    )

    assert backend.requesters == [_OPAQUE, _OPAQUE]


def test_no_requester_sends_no_header():
    backend = _Backend(_ok())

    _client(backend).submit_turn("c1", idempotency_key="k" * 16, requester=None, query="x")

    assert backend.requesters == [None]


def test_the_requester_is_required_on_every_turn_path():
    """As the idempotency key is: a new surface that forgets it fails here,
    instead of sending turns that name nobody."""

    client = _client(_Backend(_ok()))
    with pytest.raises(TypeError, match="requester"):
        client.submit_turn("c1", idempotency_key="k" * 16, query="x")
    with pytest.raises(TypeError, match="requester"):
        turn_mod.run_turn(
            client, None, team_id="T", channel_id="C", thread_ts="t", text="x",
            idempotency_key="k" * 16,
        )
    with pytest.raises(TypeError, match="requester"):
        turn_mod.run_turn_and_post(
            None, client, None, channel="C", thread_ts="t", team_id="T", text="x",
            idempotency_key="k" * 16,
        )
    with pytest.raises(TypeError, match="requester"):
        apply_action(client, "c1", "{}", team_id="T", idempotency_key="k" * 16)


def test_preflight_sends_no_requester():
    """The doctor's smoke turn is nobody's."""

    spec = importlib.util.spec_from_file_location("_preflight_requester", "scripts/preflight.py")
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)
    backend = _Backend(_ok())

    assert preflight.check_turn_contract(_client(backend))
    assert backend.requesters == [None]


def test_the_turn_route_declares_the_header():
    """Pins the header name to the contract this repo is pinned to. Runs when
    the spec is at hand (``FM_OPENAPI_SPEC``, as CI sets it)."""

    path = os.environ.get("FM_OPENAPI_SPEC")
    if not path:
        pytest.skip("FM_OPENAPI_SPEC not set to a local copy of the pinned contract")
    with open(path) as handle:
        spec = json.load(handle)
    params = spec["paths"]["/api/v1/cases/{case_id}/turns"]["post"]["parameters"]
    assert REQUESTER_HEADER in [p["name"] for p in params if p["in"] == "header"]


# -- the seven listener sites ---------------------------------------------------
class _FM:
    """Core, recording each turn's requester."""

    def __init__(self) -> None:
        self.turns: list[dict] = []

    def create_case(self, *, title=None, initial_message=None, team_id=None):
        return "case_new"

    def submit_turn(self, case_id, **kwargs):
        self.turns.append(kwargs)
        return TurnResult(agent_response="on it", case_state="investigating")

    @property
    def requesters(self) -> list:
        return [t["requester"] for t in self.turns]


class _Response(dict):
    pass


class _Slack:
    token = "xoxb-test"

    def chat_postMessage(self, **kw):
        return _Response(ts="post1.000")

    def chat_update(self, **kw):
        return _Response(ts=kw.get("ts"))

    def chat_postEphemeral(self, **kw):
        return _Response()

    def reactions_add(self, **kw):
        return _Response()

    def conversations_replies(self, **kw):
        return {"messages": []}

    def chat_getPermalink(self, *, channel, message_ts):
        return {"permalink": f"https://slack/archives/{channel}/p{message_ts}"}


class _App:
    def __init__(self) -> None:
        self.events: dict = {}
        self.shortcuts: dict = {}
        self.actions: list = []
        self.views: dict = {}

    def event(self, name):
        return lambda fn: self.events.setdefault(name, fn)

    def shortcut(self, spec):
        return lambda fn: self.shortcuts.setdefault(spec["callback_id"], fn)

    def action(self, pattern):
        return lambda fn: (self.actions.append(fn), fn)[1]

    def view(self, callback_id):
        return lambda fn: self.views.setdefault(callback_id, fn)


@pytest.fixture
def store(tmp_path):
    s = CaseStore(str(tmp_path / "cases.db"))
    yield s
    s.close()


def _live_thread(store: CaseStore, thread: str = "TS1", team: str = "T1") -> None:
    store.put(team, "C1", thread, "case_1")
    store.mark_seeded(team, "C1", thread)


def _mention(store, fm, context) -> None:
    app = _App()
    register_events(app, fm, store)
    app.events["app_mention"](
        event={"channel": "C1", "ts": "5.0", "thread_ts": "TS1", "user": context.user_id,
               "text": "<@UBOT> disk full"},
        context=context,
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)


def _click(store, fm, context) -> None:
    team = context.team_id or ""
    _live_thread(store, thread="111.222", team=team)
    store.record_turn(team, "C1", "111.222", turn_ts="111.222", action_ts="111.222")
    app = _App()
    register_actions(app, fm, store)
    (on_click,) = app.actions
    on_click(
        ack=lambda: None,
        body={
            "channel": {"id": "C1"},
            "user": {"id": context.user_id},
            "message": {"ts": "111.222", "text": "Mark this resolved?", "blocks": []},
            "actions": [
                {
                    "action_id": "fm_suggested_action:0",
                    "action_ts": "222.333",
                    "text": {"type": "plain_text", "text": "Yes"},
                    "value": '{"q": "fixed", "it": "status_transition"}',
                }
            ],
        },
        context=context,
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)


_EXPECTED = _oracle(_KEY, "T1", _USER)


def test_a_mention_passes_its_user(store):
    fm = _FM()

    _mention(store, fm, _ctx())

    assert fm.requesters == [_EXPECTED]


def test_a_user_less_mention_sends_none(store):
    fm = _FM()

    _mention(store, fm, _ctx(None))

    assert fm.requesters == [None]


def test_a_dm_summons_passes_its_user(store):
    fm = _FM()
    app = _App()
    register_events(app, fm, store)

    app.events["message"](
        event={"channel": "D1", "channel_type": "im", "ts": "9.0", "user": _USER,
               "text": "the pods are crashlooping"},
        context=_ctx(),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    assert fm.requesters == [_EXPECTED]


def test_a_thread_reply_passes_its_user(store):
    fm = _FM()
    _live_thread(store)
    app = _App()
    register_events(app, fm, store)

    app.events["message"](
        event={"channel": "C1", "channel_type": "channel", "thread_ts": "TS1", "ts": "6.0",
               "user": _USER, "text": "here is the log"},
        context=_ctx(),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    assert fm.requesters == [_EXPECTED]


def test_the_shortcut_passes_its_invoker(store):
    fm = _FM()
    app = _App()
    register_shortcuts(app, fm, store)

    app.shortcuts["fm_investigate_message"](
        ack=lambda: None,
        shortcut={
            "trigger_id": "trig1",
            "channel": {"id": "C1"},
            "message": {"ts": "1785872177.123456", "text": "[FIRING:1] DiskFull"},
            "response_url": None,
        },
        context=_ctx(),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    assert fm.requesters == [_EXPECTED]


def test_the_assistant_panel_passes_its_user(store):
    fm = _FM()
    handler = build_assistant(fm, store)._user_message_listeners[0].ack_function

    handler(
        payload={"channel": "D1", "thread_ts": "TS1", "ts": "1.0", "user": _USER,
                 "text": "the pods are crashlooping"},
        context=_ctx(),
        client=_Slack(),
        set_status=lambda _status: None,
        say=lambda *a, **k: _Response(ts="say1.000"),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    assert fm.requesters == [_EXPECTED]


def test_a_button_click_passes_its_clicker(store):
    fm = _FM()

    _click(store, fm, _ctx())

    assert fm.requesters == [_EXPECTED]


def test_a_data_box_submission_passes_its_submitter(store):
    fm = _FM()
    _live_thread(store)
    app = _App()
    register_data_box(app, fm, store)

    app.views[DATA_BOX_CALLBACK_ID](
        ack=lambda **kw: None,
        body={"user": {"id": _USER}},
        view={
            "id": "V1",
            "private_metadata": json.dumps({"c": "C1", "t": "TS1"}),
            "state": {"values": {DATA_BOX_BLOCK_ID: {DATA_BOX_INPUT_ID: {"value": "df -h"}}}},
        },
        context=_ctx(),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    assert fm.requesters == [_EXPECTED]


def test_on_grid_a_mention_and_a_click_name_the_same_person(store):
    """The two payload shapes of an org-wide install: the event has no team,
    the click has the workspace's. One person, one id."""

    fm = _FM()

    _mention(store, fm, _ctx(team_id=None, enterprise_id="E1"))
    _click(store, fm, _ctx(team_id="T1", enterprise_id="E1"))

    assert fm.requesters == [_oracle(_KEY, "E1", _USER)] * 2


def test_a_real_turn_carries_the_id_and_logs_neither_the_raw_id_nor_the_key(store, caplog):
    """End to end through the core client: the header is the helper's value,
    and at DEBUG no log line, no header and no body holds the raw id or the key."""

    backend = _Backend(_ok())
    caplog.set_level(logging.DEBUG)

    _mention(store, _client(backend), _ctx())

    assert backend.requesters == [_EXPECTED]
    backend.assert_never_raw()
    assert caplog.records, "nothing was logged, so the check below proves nothing"
    for record in caplog.records:
        line = record.getMessage()
        assert _USER not in line, f"the raw Slack id was logged: {line!r}"
        assert _KEY not in line, f"the requester key was logged: {line!r}"
