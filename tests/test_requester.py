"""A turn names who asked, as an opaque id, so one workspace's people take turns
(FaultMaven/faultmaven#2852, contract 15.6.0).

Every Slack user of a workspace reaches core as that workspace's one service
account, so core's worker admission saw one principal: one person's batch made
everyone else in the workspace wait behind it. The turn now carries
``X-FaultMaven-Requester``, an HMAC of the Slack user under a secret this agent
already holds, which core uses to order that account's own jobs and for nothing
else. The raw Slack id never leaves the process.

The client tests drive the real ``FaultMavenClient`` over ``httpx.MockTransport``
on a fake clock; the listener tests drive each registered handler through the
real pipeline with a recording core stand-in. Nothing here talks to Slack, the
API or an LLM.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
from types import SimpleNamespace

import httpx
import pytest

import listeners._turn as turn_mod
from faultmaven.client import (
    REQUESTER_HEADER,
    FaultMavenClient,
    TurnResult,
    requester_id,
)
from listeners.actions import register_actions, register_data_box
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


# -- the helper ----------------------------------------------------------------
def test_the_id_is_opaque_and_inside_the_published_grammar():
    value = requester_id(_KEY, "T1", _USER)

    assert value is not None
    assert _PUBLISHED.fullmatch(value)
    assert len(value) == 32
    assert _USER not in value and "T1" not in value


def test_the_id_is_stable_for_one_key_and_distinct_per_user_and_workspace():
    assert requester_id(_KEY, "T1", _USER) == requester_id(_KEY, "T1", _USER)
    assert requester_id(_KEY, "T1", _USER) != requester_id(_KEY, "T1", "U0BOB")
    # The same Slack id in another workspace is another person.
    assert requester_id(_KEY, "T1", _USER) != requester_id(_KEY, "T2", _USER)


def test_the_id_is_the_domain_separated_hmac():
    import hashlib
    import hmac

    expected = hmac.new(
        _KEY.encode(), b"faultmaven-requester\0" + b"T1:" + _USER.encode(), hashlib.sha256
    ).hexdigest()[:32]
    assert requester_id(_KEY, "T1", _USER) == expected
    # Not the bare HMAC of the identity: the key is a Slack secret with a use
    # of its own, so the id must never be a value computed for that use.
    bare = hmac.new(_KEY.encode(), f"T1:{_USER}".encode(), hashlib.sha256).hexdigest()
    assert not bare.startswith(expected)


@pytest.mark.parametrize(
    ("key", "user"), [("", _USER), (_KEY, None), (_KEY, "")], ids=["no-key", "no-user", "empty"]
)
def test_no_user_or_no_key_gives_no_id(key, user):
    assert requester_id(key, "T1", user) is None


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
        "FAULTMAVEN_API_TOKEN": "tok",
        "FAULTMAVEN_REFRESH_TOKEN": "",
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
    assert requester_id(http.requester_key, "T1", _USER) != requester_id(
        socket.requester_key, "T1", _USER
    )


@pytest.mark.parametrize("transport", ["http", "socket"])
def test_the_agent_client_sends_the_id_under_its_transport_key(
    monkeypatch, tmp_path, transport
):
    """``make_fault_client`` hands the client the transport's key, end to end."""

    from app import make_fault_client

    settings = _settings(monkeypatch, tmp_path, transport)
    backend = _Backend(_ok())
    client = make_fault_client(settings)
    _install(client, backend)

    client.submit_turn("c1", idempotency_key="k" * 16, query="x", team_id="T1", requester=_USER)

    assert backend.requesters == [requester_id(settings.requester_key, "T1", _USER)]


# -- submit_turn -------------------------------------------------------------
_ANSWER = {"agent_response": "the disk filled at 02:14", "turn_number": 3}


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


def _install(client: FaultMavenClient, backend: _Backend) -> FakeClock:
    client._http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(backend))
    return FakeClock().install(client)


def _client(backend: _Backend, *, requester_key: str = _KEY, **kw) -> FaultMavenClient:
    client = FaultMavenClient(
        "http://test", token=kw.pop("token", "tok"), requester_key=requester_key, **kw
    )
    _install(client, backend)
    return client


def _assert_never_raw(backend: _Backend) -> None:
    for request in backend.requests:
        for name, value in request.headers.items():
            assert _USER not in value, f"the raw Slack id travelled in {name}"


def test_the_first_post_carries_the_id():
    backend = _Backend(_ok())

    _client(backend).submit_turn(
        "c1", idempotency_key="k" * 16, query="x", team_id="T1", requester=_USER
    )

    assert backend.requesters == [requester_id(_KEY, "T1", _USER)]
    _assert_never_raw(backend)


def test_a_keyed_recovery_attempt_carries_the_same_id():
    """The re-send is the same turn: same key, same bytes, same requester."""

    backend = _Backend(_read_timeout, _ok(replayed=True))

    _client(backend).submit_turn(
        "c1", idempotency_key="k" * 16, query="x", team_id="T1", requester=_USER
    )

    expected = requester_id(_KEY, "T1", _USER)
    assert len(backend.requests) == 2
    assert backend.requesters == [expected, expected]
    assert {r.headers["idempotency-key"] for r in backend.requests} == {"k" * 16}
    _assert_never_raw(backend)


def test_the_401_re_auth_re_post_carries_the_id():
    """The re-POST after a re-login is the attempt that runs the turn."""

    backend = _Backend(httpx.Response(401, json={"detail": "expired"}), _ok())

    _client(backend, token="", dev_login_username="admin").submit_turn(
        "c1", idempotency_key="k" * 16, query="x", team_id="T1", requester=_USER
    )

    expected = requester_id(_KEY, "T1", _USER)
    assert len(backend.requests) == 2
    assert backend.requesters == [expected, expected]
    _assert_never_raw(backend)


def test_no_requester_sends_no_header():
    backend = _Backend(_ok())

    _client(backend).submit_turn("c1", idempotency_key="k" * 16, query="x", team_id="T1")

    assert backend.requesters == [None]


def test_no_key_sends_no_header():
    backend = _Backend(_ok())

    _client(backend, requester_key="").submit_turn(
        "c1", idempotency_key="k" * 16, query="x", team_id="T1", requester=_USER
    )

    assert backend.requesters == [None]
    _assert_never_raw(backend)


def test_preflight_sends_no_requester():
    """The doctor's smoke turn is nobody's: it names no requester even from a
    client that holds a key."""

    spec = importlib.util.spec_from_file_location("_preflight_requester", "scripts/preflight.py")
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/cases":
            return httpx.Response(201, json={"case_id": "case_pf"})
        return backend(request)

    backend = _Backend(_ok())
    client = _client(backend)
    client._http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))

    assert preflight.check_turn_contract(client)
    assert backend.requests, "the smoke turn was never sent"
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
        return [t.get("requester") for t in self.turns]


class _Response(dict):
    pass


class _Slack:
    token = "xoxb-test"

    def __init__(self) -> None:
        self.posts: list = []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return _Response(ts=f"post{len(self.posts)}.000")

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


def _ctx(user_id: str | None = None):
    return SimpleNamespace(team_id="T1", user_id=user_id, bot_user_id="UBOT", bot_id="B1")


@pytest.fixture
def store(tmp_path):
    s = CaseStore(str(tmp_path / "cases.db"))
    yield s
    s.close()


def _live_thread(store: CaseStore, channel: str = "C1", thread: str = "TS1") -> None:
    store.put("T1", channel, thread, "case_1")
    store.mark_seeded("T1", channel, thread)


def _events(store, fm):
    app = _App()
    register_events(app, fm, store)
    return app


def _mention(app, event_user: str | None):
    event = {"channel": "C1", "ts": "5.0", "thread_ts": "TS1", "text": "<@UBOT> disk full"}
    if event_user:
        event["user"] = event_user
    app.events["app_mention"](event=event, context=_ctx(), client=_Slack(), logger=_LOG)
    turn_mod.drain_turns(5.0)


def test_a_mention_passes_its_user(store):
    fm = _FM()

    _mention(_events(store, fm), _USER)

    assert fm.requesters == [_USER]


def test_a_user_less_mention_sends_none(store):
    fm = _FM()

    _mention(_events(store, fm), None)

    assert len(fm.turns) == 1
    assert fm.requesters == [None]


def test_a_dm_summons_passes_its_user(store):
    fm = _FM()
    app = _events(store, fm)

    app.events["message"](
        event={"channel": "D1", "channel_type": "im", "ts": "9.0", "user": _USER,
               "text": "the pods are crashlooping"},
        context=_ctx(),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    assert fm.requesters == [_USER]


def test_a_thread_reply_passes_its_user(store):
    fm = _FM()
    _live_thread(store)
    app = _events(store, fm)

    app.events["message"](
        event={"channel": "C1", "channel_type": "channel", "thread_ts": "TS1", "ts": "6.0",
               "user": _USER, "text": "here is the log"},
        context=_ctx(),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    assert fm.requesters == [_USER]


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
        context=_ctx(user_id=_USER),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    assert fm.requesters == [_USER]


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

    assert fm.requesters == [_USER]


def test_a_button_click_passes_its_clicker(store):
    fm = _FM()
    _live_thread(store, thread="111.222")
    store.record_turn("T1", "C1", "111.222", turn_ts="111.222", action_ts="111.222")
    app = _App()
    register_actions(app, fm, store)
    (on_click,) = app.actions

    on_click(
        ack=lambda: None,
        body={
            "channel": {"id": "C1"},
            "user": {"id": _USER},
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
        context=_ctx(),
        client=_Slack(),
        logger=_LOG,
    )
    turn_mod.drain_turns(5.0)

    assert fm.requesters == [_USER]


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

    assert fm.requesters == [_USER]
