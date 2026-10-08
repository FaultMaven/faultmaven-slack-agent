"""A turn the agent stopped waiting on is recovered, not lost (slack-agent#90).

Contract 12.2.0 (faultmaven#1888): a turn sent with an ``Idempotency-Key``
commits a receipt with it, and a retry under the same key is answered with the
committed turn (``X-Idempotency-Replayed: true``) instead of running it again.
So an attempt whose outcome is unknown — a client timeout, a gateway 502/504, a
dropped connection — is re-sent under its own key until the backend answers
with the turn, bounded by ``turn_recovery_seconds``.

The client tests drive the real ``FaultMavenClient`` over ``httpx.MockTransport``
on a fake clock, and assert on the requests the backend would have received.
The pipeline tests drive ``run_turn_and_post`` and the surfaces end to end with
a recording Slack stand-in. Nothing here talks to Slack, the API or an LLM.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

import listeners._turn as _turn
from faultmaven.client import (
    CaseTerminalError,
    CaseVersionConflictError,
    FaultMavenClient,
    FaultMavenError,
    FaultMavenTimeoutError,
    IdempotencyKeyReuseError,
    IdempotencyReplayUnavailableError,
    TurnResult,
)
from listeners.actions import apply_action
from tests._clock import FakeClock

_LOG = logging.getLogger("test")

#: The ``Idempotency-Key`` header's grammar as contract 12.2.0 publishes it
#: (``docs/reference/api/openapi.json`` at the pinned ref, the turn route's
#: header parameter). The generated models do not carry it — a header parameter
#: is not a model — so it is quoted here and checked against the document by
#: :func:`test_the_quoted_grammar_is_the_published_one` whenever the spec is at
#: hand (``FM_OPENAPI_SPEC``, the generator's own override).
_PUBLISHED_KEY_PATTERN = r"^[A-Za-z0-9_-]+$"
_PUBLISHED_KEY_MIN_LENGTH = 8
_PUBLISHED_KEY_MAX_LENGTH = 255


def _admitted(key: str) -> bool:
    """Is ``key`` inside the published grammar? ``fullmatch``, as the server
    does, so a trailing newline cannot slip past ``$``."""

    return (
        _PUBLISHED_KEY_MIN_LENGTH <= len(key) <= _PUBLISHED_KEY_MAX_LENGTH
        and re.fullmatch(_PUBLISHED_KEY_PATTERN, key) is not None
    )


# -- the backend, scripted ------------------------------------------------------
_ANSWER = {"agent_response": "the disk filled at 02:14", "turn_number": 3}


def _ok(*, replayed: bool = False) -> httpx.Response:
    headers = {"X-Idempotency-Replayed": "true"} if replayed else {}
    return httpx.Response(200, json=_ANSWER, headers=headers)


def _conflict(code: str, **headers: str) -> httpx.Response:
    return httpx.Response(
        409, json={"detail": code.lower()}, headers={"x-error-code": code, **headers}
    )


def _read_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow turn", request=request)


class _Backend:
    """A MockTransport handler that plays ``script`` one step per turn POST and
    records every request. A step is a response, or a callable taking the
    request (to raise a transport error). The last step repeats."""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/dev-login"):
            return httpx.Response(200, json={"access_token": f"tok{len(self.requests)}"})
        self.requests.append(request)
        # A runaway loop fails the test instead of hanging it.
        assert len(self.requests) <= 200, "the recovery loop did not stop"
        step = self.script[min(len(self.requests), len(self.script)) - 1]
        return step(request) if callable(step) else step

    @property
    def keys(self) -> list[str | None]:
        return [r.headers.get("idempotency-key") for r in self.requests]


def _client(backend: _Backend, *, recovery: float = 660.0, **kw) -> tuple:
    client = FaultMavenClient(
        "http://test", token=kw.pop("token", "tok"), turn_recovery_seconds=recovery, **kw
    )
    client._http = httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(backend)
    )
    return client, FakeClock().install(client)


_KEY = _turn.turn_key("T1", "C1", "1754336177.123456")


# -- 1. the key -----------------------------------------------------------------
def test_the_key_is_deterministic_admitted_and_distinct_per_turn():
    message = _turn.turn_key("T1", "C1", "1754336177.123456")
    # A redelivery of the same event (even to a restarted process) is the same
    # turn, so it must be the same key.
    assert message == _turn.turn_key("T1", "C1", "1754336177.123456")
    assert _admitted(message)
    assert len(message) == 64  # sha256 hex, not truncated: it fits
    # Two messages, two turns — including in another channel or workspace.
    assert message != _turn.turn_key("T1", "C1", "1754336177.123457")
    assert message != _turn.turn_key("T1", "C2", "1754336177.123456")
    assert message != _turn.turn_key("T2", "C1", "1754336177.123456")

    # A click: the same, on its action_ts. A shortcut: on its trigger_id.
    click = _turn.turn_key("T1", "C1", "1754336199.000100")
    assert _admitted(click) and click == _turn.turn_key("T1", "C1", "1754336199.000100")
    shortcut = _turn.turn_key("T1", "12345.98765.abcd0123")
    assert _admitted(shortcut) and shortcut != click


def test_a_raw_slack_ts_is_outside_the_grammar():
    """Why the key is hashed rather than the identity sent as is: a ts carries a
    dot, which the grammar refuses (a published 422)."""

    assert not _admitted("T1:C1:1754336177.123456")


def test_an_incomplete_identity_gets_a_fresh_key_not_a_shared_one():
    """A missing part must not make every such turn share one key — the second
    would be refused as a reuse of the first."""

    first, second = _turn.turn_key("T1", "C1", None), _turn.turn_key("T1", "C1", None)
    assert first != second
    assert _admitted(first) and _admitted(second)


def test_the_quoted_grammar_is_the_published_one():
    """Pins the quoted grammar to the contract this repo is pinned to.

    Runs when the spec is at hand: ``FM_OPENAPI_SPEC`` names a local copy, as it
    does for ``scripts/generate_api_models.py``. The suite stays hermetic (no
    network) by skipping otherwise.
    """

    path = os.environ.get("FM_OPENAPI_SPEC")
    if not path or not os.path.exists(path):
        pytest.skip("FM_OPENAPI_SPEC not set to a local copy of the pinned contract")
    with open(path) as handle:
        spec = json.load(handle)
    pin_path = os.path.join(os.path.dirname(__file__), "..", "api-contract.pin.json")
    with open(pin_path) as handle:
        assert spec["info"]["version"] == json.load(handle)["contractVersion"]
    params = spec["paths"]["/api/v1/cases/{case_id}/turns"]["post"]["parameters"]
    (header,) = [p for p in params if p["name"] == "Idempotency-Key"]
    (schema,) = [s for s in header["schema"]["anyOf"] if s.get("type") == "string"]
    assert schema["pattern"] == _PUBLISHED_KEY_PATTERN
    assert schema["minLength"] == _PUBLISHED_KEY_MIN_LENGTH
    assert schema["maxLength"] == _PUBLISHED_KEY_MAX_LENGTH


def test_an_unkeyed_turn_is_impossible():
    """A2: no default anywhere on the path. An unkeyed turn cannot be
    recovered, so it must not be expressible."""

    backend = _Backend(_ok())
    client, _ = _client(backend)
    with pytest.raises(TypeError, match="idempotency_key"):
        client.submit_turn("c1", query="x")
    with pytest.raises(TypeError, match="idempotency_key"):
        _turn.run_turn(
            client, None, team_id="T", channel_id="C", thread_ts="t", text="x"
        )
    with pytest.raises(TypeError, match="idempotency_key"):
        _turn.run_turn_and_post(
            None, client, None, channel="C", thread_ts="t", team_id="T", text="x"
        )
    with pytest.raises(TypeError, match="idempotency_key"):
        apply_action(client, "c1", "{}", team_id="T")
    for fn in (FaultMavenClient.submit_turn, _turn.run_turn, _turn.run_turn_and_post, apply_action):
        param = inspect.signature(fn).parameters["idempotency_key"]
        assert param.default is inspect.Parameter.empty, fn.__name__
    assert backend.requests == []


def test_the_key_rides_on_the_401_re_auth_post_too():
    """A1: the re-auth re-POST is the attempt that runs the turn; sent bare, a
    later recovery attempt could not be answered from its receipt."""

    backend = _Backend(httpx.Response(401, json={"detail": "expired"}), _ok())
    client, _ = _client(backend, token="", dev_login_username="admin")
    client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert backend.keys == [_KEY, _KEY]


# -- 2. recovery ----------------------------------------------------------------
def test_a_timed_out_turn_is_recovered_from_its_receipt():
    backend = _Backend(_read_timeout, _ok(replayed=True))
    client, _ = _client(backend)
    waiting: list = []

    result = client.submit_turn(
        "c1", idempotency_key=_KEY, query="why is the disk full",
        files=[("df.txt", b"/dev/sda1 100%", "text/plain")],
        on_waiting=lambda: waiting.append(1),
    )

    assert result.agent_response == _ANSWER["agent_response"]
    assert result.replayed is True
    # The same turn, under the same key, both times — bytes included.
    assert backend.keys == [_KEY, _KEY]
    assert all(b"/dev/sda1 100%" in r.content for r in backend.requests)
    assert all(b"why is the disk full" in r.content for r in backend.requests)
    assert waiting == [1]


@pytest.mark.parametrize(
    "first",
    [
        _read_timeout,
        lambda r: (_ for _ in ()).throw(httpx.RemoteProtocolError("closed", request=r)),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(504, text="gateway timeout"),
    ],
    ids=["read-timeout", "dropped-connection", "gateway-502", "gateway-504"],
)
def test_every_unknown_outcome_enters_recovery(first):
    backend = _Backend(first, _ok(replayed=True))
    client, _ = _client(backend)
    assert client.submit_turn("c1", idempotency_key=_KEY, query="x").replayed
    assert backend.keys == [_KEY, _KEY]


def test_the_answer_is_posted_once_after_recovery():
    """End to end through the channel pipeline: the thread sees the placeholder,
    one "still working" notice, then the answer — never the timeout text."""

    backend = _Backend(_read_timeout, _ok(replayed=True))
    client, _ = _client(backend)
    slack = _Slack()

    _turn.run_turn_and_post(
        slack, client, _Store(case_id="c1"), channel="C1", thread_ts="TS1",
        team_id="T1", text="why", idempotency_key=_KEY,
    )

    texts = [u["text"] for u in slack.updates]
    assert texts[0] == _turn.STILL_WORKING_TEXT
    assert texts[1:] == [_ANSWER["agent_response"]]
    assert _turn.TURN_TIMEOUT_TEXT not in texts


# -- 3. TURN_IN_PROGRESS -----------------------------------------------------------
def test_turn_in_progress_waits_retry_after_then_replays():
    backend = _Backend(
        _read_timeout, _conflict("TURN_IN_PROGRESS", **{"Retry-After": "3"}),
        _ok(replayed=True),
    )
    client, clock = _client(backend)

    assert client.submit_turn("c1", idempotency_key=_KEY, query="x").replayed

    # One backoff after the timeout, then exactly the server's Retry-After.
    assert clock.waits == [1.0, 3.0]
    assert backend.keys == [_KEY] * 3


@pytest.mark.parametrize(
    ("retry_after", "waited"), [("300", 60.0), ("0", 1.0), (None, 1.0), ("soon", 1.0)]
)
def test_retry_after_is_clamped_to_a_poll_interval(retry_after, waited):
    """Retry-After is the claim's remaining TTL — an upper bound that can
    outlast the turn by ~48.5 s — so it is a poll interval, clamped to [1, 60]."""

    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    backend = _Backend(_conflict("TURN_IN_PROGRESS", **headers), _ok(replayed=True))
    client, clock = _client(backend)
    client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert clock.waits == [waited]


# -- 4. the bound ---------------------------------------------------------------
def test_an_exhausted_bound_raises_the_timeout():
    backend = _Backend(_conflict("TURN_IN_PROGRESS", **{"Retry-After": "60"}))
    client, clock = _client(backend, recovery=300.0)
    start = clock.now
    with pytest.raises(FaultMavenTimeoutError):
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert clock.now - start <= 300.0
    assert len(backend.requests) == 5  # at 0, 60, 120, 180, 240 s; none at 300


def test_an_exhausted_bound_tells_the_thread_once():
    backend = _Backend(_conflict("TURN_IN_PROGRESS", **{"Retry-After": "60"}))
    client, _ = _client(backend)
    slack = _Slack()

    _turn.run_turn_and_post(
        slack, client, _Store(case_id="c1"), channel="C1", thread_ts="TS1",
        team_id="T1", text="why", idempotency_key=_KEY,
    )

    texts = [u["text"] for u in slack.updates]
    assert texts == [_turn.STILL_WORKING_TEXT, _turn.TURN_TIMEOUT_TEXT]
    assert len(backend.requests) > 2  # the notice came once across many attempts


def test_the_timeout_text_points_at_the_case_not_a_re_send():
    """A7: a re-send is a new key, so a duplicate turn if the first committed."""

    text = _turn.TURN_TIMEOUT_TEXT
    assert "check the case" in text.lower()
    assert "re-sending the same message" not in text


# -- 5. the 409s inside the loop ----------------------------------------------------
def test_key_reuse_is_not_retried():
    backend = _Backend(_read_timeout, _conflict("IDEMPOTENCY_KEY_REUSE"), _ok())
    client, _ = _client(backend)
    with pytest.raises(IdempotencyKeyReuseError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 2
    assert _turn.turn_error_text(err.value) == _turn.KEY_REUSE_TEXT


def test_replay_unavailable_is_not_retried():
    backend = _Backend(_read_timeout, _conflict("IDEMPOTENCY_REPLAY_UNAVAILABLE"), _ok())
    client, _ = _client(backend)
    with pytest.raises(IdempotencyReplayUnavailableError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 2
    assert _turn.turn_error_text(err.value) == _turn.REPLAY_UNAVAILABLE_TEXT
    # The turn DID commit, so neither the text nor a button invites a re-send.
    assert not _turn.retry_may_help(err.value)


def test_replay_unavailable_still_records_the_turn_as_landed():
    """A7: the case has the turn, so the thread is seeded — or its next turn
    would re-send the catch-up and hand the case its own history again."""

    backend = _Backend(_read_timeout, _conflict("IDEMPOTENCY_REPLAY_UNAVAILABLE"))
    client, _ = _client(backend)
    store = _Store(case_id="c1")
    slack = _Slack()

    _turn.run_turn_and_post(
        slack, client, store, channel="C1", thread_ts="TS1", team_id="T1",
        text="why", idempotency_key=_KEY, prior_context="earlier discussion",
    )

    assert store.seeded == [("T1", "C1", "TS1")]
    assert slack.updates[-1]["text"] == _turn.REPLAY_UNAVAILABLE_TEXT


def test_a_version_conflict_in_the_loop_is_re_sent_once():
    """Without a claim store the re-send runs beside the first attempt and can
    lose the case version to it; the next attempt replays."""

    backend = _Backend(
        _read_timeout, _conflict("CASE_VERSION_CONFLICT"), _ok(replayed=True)
    )
    client, _ = _client(backend)
    assert client.submit_turn("c1", idempotency_key=_KEY, query="x").replayed
    assert backend.keys == [_KEY] * 3


def test_a_second_version_conflict_in_the_loop_is_the_ordinary_conflict():
    backend = _Backend(
        _read_timeout, _conflict("CASE_VERSION_CONFLICT"), _conflict("CASE_VERSION_CONFLICT"),
        _ok(),
    )
    client, _ = _client(backend)
    with pytest.raises(CaseVersionConflictError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 3
    assert _turn.turn_error_text(err.value) == _turn.CASE_BUSY_TEXT


def test_a_version_conflict_on_the_first_attempt_is_not_re_sent():
    """Outside the loop nothing of ours ran before it: another writer advanced
    the case, which is today's busy text, not a recovery."""

    backend = _Backend(_conflict("CASE_VERSION_CONFLICT"), _ok())
    client, _ = _client(backend)
    with pytest.raises(CaseVersionConflictError):
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 1


def test_a_refused_connection_on_the_first_attempt_stays_retryable():
    """Nothing reached the backend, so it is not an unknown outcome."""

    backend = _Backend(lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused", request=r)))
    client, _ = _client(backend)
    with pytest.raises(FaultMavenError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert not isinstance(err.value, FaultMavenTimeoutError)
    assert len(backend.requests) == 1


def test_a_refused_connection_while_recovering_keeps_recovering():
    """An EARLIER attempt may have committed, so a backend mid-restart is
    waited out rather than reported as a safe-to-retry failure."""

    refused = lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused", request=r))  # noqa: E731
    backend = _Backend(_read_timeout, refused, refused, _ok(replayed=True))
    client, _ = _client(backend)
    assert client.submit_turn("c1", idempotency_key=_KEY, query="x").replayed
    assert backend.keys == [_KEY] * 4


# -- A3: the app's own 504 ----------------------------------------------------------
def test_two_request_timeouts_are_two_posts_then_the_timeout():
    """REQUEST_TIMEOUT means nothing committed: the turn exhausted the ceiling
    on this input. One re-send, then today's timeout text."""

    timed_out = httpx.Response(
        504, json={"detail": "timeout"},
        headers={"x-error-code": "REQUEST_TIMEOUT", "Retry-After": "30"},
    )
    backend = _Backend(timed_out, timed_out, _ok())
    client, _ = _client(backend)
    with pytest.raises(FaultMavenTimeoutError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 2
    assert _turn.turn_error_text(err.value) == _turn.TURN_TIMEOUT_TEXT


def test_a_request_timeout_then_an_answer_is_the_answer():
    timed_out = httpx.Response(504, headers={"x-error-code": "REQUEST_TIMEOUT"})
    backend = _Backend(timed_out, _ok())
    client, _ = _client(backend)
    assert client.submit_turn("c1", idempotency_key=_KEY, query="x").agent_response
    assert backend.keys == [_KEY, _KEY]


# -- 6. CASE_TERMINAL ----------------------------------------------------------------
@pytest.mark.parametrize("labelled", [True, False], ids=["labelled", "unlabelled"])
def test_a_terminal_case_is_closed_labelled_or_not(labelled):
    """S4: the core will label its terminal 409s CASE_TERMINAL after this ships;
    both must read as a closed case, so the label can land without a window."""

    headers = {"x-error-code": "CASE_TERMINAL"} if labelled else {}
    backend = _Backend(
        httpx.Response(409, json={"detail": "Cannot submit new data"}, headers=headers)
    )
    client, _ = _client(backend)
    with pytest.raises(CaseTerminalError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x", pasted_content="log")
    assert _turn.turn_error_text(err.value) == _turn.CASE_CLOSED_TEXT
    assert len(backend.requests) == 1


def test_the_terminal_label_is_honored_on_the_poll_path(no_poll_sleep):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, headers={"Location": "/status/1"})
        return httpx.Response(409, headers={"x-error-code": "CASE_TERMINAL"})

    client = FaultMavenClient("http://test", token="tok")
    client._http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    FakeClock().install(client)
    with pytest.raises(CaseTerminalError):
        client.submit_turn("c1", idempotency_key=_KEY, query="x")


# -- A4: shutdown ----------------------------------------------------------------
def test_shutdown_during_a_wait_ends_the_turn_with_the_timeout_text():
    """The loop stops between attempts at shutdown. Real clock and a real wait:
    a 60 s Retry-After must end the moment the drain begins, not run out."""

    backend = _Backend(_conflict("TURN_IN_PROGRESS", **{"Retry-After": "60"}))
    client = FaultMavenClient("http://test", token="tok")
    client._http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(backend))
    slack = _Slack()

    worker = threading.Thread(
        target=_turn.run_turn_and_post,
        args=(slack, client, _Store(case_id="c1")),
        kwargs=dict(
            channel="C1", thread_ts="TS1", team_id="T1", text="why",
            idempotency_key=_KEY,
        ),
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + 5
    while not slack.updates and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [u["text"] for u in slack.updates] == [_turn.STILL_WORKING_TEXT]

    client.begin_shutdown()
    worker.join(5)

    assert not worker.is_alive()
    texts = [u["text"] for u in slack.updates]
    assert texts == [_turn.STILL_WORKING_TEXT, _turn.TURN_TIMEOUT_TEXT]
    assert len(backend.requests) == 1  # nothing re-sent after the drain began


def test_shutdown_stops_the_loop_before_the_drain(monkeypatch):
    """The client is told first, then the drain waits out the turn's bound."""

    import app

    seen: dict = {}

    class _FM:
        stopping = False

        def begin_shutdown(self):
            self.stopping = True

        def close(self):
            seen["closed"] = True

    fm = _FM()

    def drain(timeout):
        seen["timeout"], seen["stopping"] = timeout, fm.stopping

    monkeypatch.setattr(app, "drain_turns", drain)
    monkeypatch.setattr(
        app,
        "get_settings",
        lambda: SimpleNamespace(
            faultmaven_request_timeout=150.0, faultmaven_turn_recovery_seconds=660.0
        ),
    )
    app.shutdown_runtime(SimpleNamespace(close=lambda: None), fm)

    assert seen["stopping"] is True
    assert seen["timeout"] == 660.0 + app._SHUTDOWN_DRAIN_HEADROOM_SECONDS
    assert seen["closed"]


# -- A6: each surface's own notice ---------------------------------------------------
class _WaitingFM:
    """Stands in for a client whose first attempt went unanswered: it calls the
    surface's on_waiting (as the real loop does, at most once), then answers."""

    def __init__(self) -> None:
        self.turns: list = []

    def create_case(self, *, title=None, initial_message=None, team_id=None):
        return "case_1"

    def submit_turn(self, case_id, *, idempotency_key, on_waiting=None, **kwargs):
        self.turns.append((case_id, idempotency_key, kwargs))
        if on_waiting is not None:
            on_waiting()
        return TurnResult(agent_response="on it")


def test_the_assistant_notice_is_its_status_line():
    from listeners.assistant import build_assistant

    fm = _WaitingFM()
    statuses: list = []
    handler = build_assistant(fm, _Store())._user_message_listeners[0].ack_function
    handler(
        payload={"channel": "D1", "thread_ts": "TS1", "ts": "1.5", "text": "disk full"},
        context=SimpleNamespace(team_id="T1"),
        client=_Slack(),
        set_status=statuses.append,
        say=lambda *a, **k: {"ts": "say1"},
        logger=_LOG,
    )
    _turn.drain_turns(5.0)

    assert len(statuses) == 2 and "still working" in statuses[1]
    assert fm.turns[0][1] == _turn.turn_key("T1", "D1", "1.5")


def test_the_click_notice_rewrites_the_clicked_message():
    from listeners.actions import register_actions

    fm, slack = _WaitingFM(), _Slack()
    app = SimpleNamespace(handler=None)
    app.action = lambda pattern: lambda fn: setattr(app, "handler", fn) or fn
    register_actions(app, fm, _Store(case_id="c1"))
    app.handler(
        ack=lambda: None,
        body={
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "message": {"ts": "111.222", "text": "Resolve?", "blocks": []},
            "actions": [
                {
                    "action_id": "fm_suggested_action:0",
                    "action_ts": "1754336199.000100",
                    "text": {"type": "plain_text", "text": "Yes"},
                    "value": '{"q": "fixed", "it": "status_transition"}',
                }
            ],
        },
        context=SimpleNamespace(team_id="T1"),
        client=slack,
        logger=_LOG,
    )
    _turn.drain_turns(5.0)

    notes = [
        e["text"]
        for u in slack.updates
        for b in u.get("blocks") or []
        if b.get("type") == "context"
        for e in b["elements"]
    ]
    assert sum("Still working on *Yes*" in n for n in notes) == 1
    assert fm.turns[0][1] == _turn.turn_key("T1", "C1", "1754336199.000100")


def test_a_redelivered_mention_is_the_same_turn_to_the_backend():
    """Event redelivery is deduped on channel:ts in-process; across a restart it
    is the key that makes it the same turn."""

    from listeners.events import register_events

    keys = []
    for _process in range(2):  # two processes: no shared in-memory dedup
        fm, slack = _WaitingFM(), _Slack()
        app = SimpleNamespace(handlers={})
        app.event = lambda name, app=app: lambda fn: app.handlers.setdefault(name, fn)
        register_events(app, fm, _Store())
        app.handlers["app_mention"](
            event={"channel": "C1", "ts": "1754336177.123456", "text": "<@UBOT> disk"},
            context=SimpleNamespace(team_id="T1", bot_user_id="UBOT", bot_id="B1"),
            client=slack,
            logger=_LOG,
        )
        _turn.drain_turns(5.0)
        keys.append(fm.turns[0][1])
    assert keys[0] == keys[1] == _turn.turn_key("T1", "C1", "1754336177.123456")


def test_a_shortcut_turn_is_keyed_on_its_trigger_id():
    from listeners.shortcuts import register_shortcuts

    fm, slack = _WaitingFM(), _Slack()
    app = SimpleNamespace(handler=None)
    app.shortcut = lambda spec: lambda fn: setattr(app, "handler", fn) or fn
    register_shortcuts(app, fm, _Store())
    app.handler(
        ack=lambda: None,
        shortcut={
            "trigger_id": "12345.98765.abcd0123",
            "channel": {"id": "C1"},
            "message": {"ts": "1754336100.000001", "text": "disk 98% on kmaster-2"},
        },
        context=SimpleNamespace(team_id="T1", user_id="U1"),
        client=slack,
        logger=_LOG,
    )
    _turn.drain_turns(5.0)

    assert fm.turns[0][1] == _turn.turn_key("T1", "12345.98765.abcd0123")


def test_the_preflight_turn_is_keyed():
    """``preflight --full`` drives the real client: unkeyed, it would now be a
    TypeError rather than a round-trip check."""

    import importlib.util

    spec = importlib.util.spec_from_file_location("_preflight", "scripts/preflight.py")
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)

    backend = _Backend(httpx.Response(201, json={"case_id": "c9"}), _ok())
    client, _ = _client(backend)
    assert preflight.check_turn_contract(client) is True
    (key,) = backend.keys[1:]
    assert key and _admitted(key)


# -- Slack and the store, recorded --------------------------------------------------
class _Slack:
    token = "xoxb-test"

    def __init__(self) -> None:
        self.posts: list = []
        self.updates: list = []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"PH{len(self.posts)}"}

    def chat_update(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    def chat_getPermalink(self, **kw):
        return {"permalink": "https://slack/p1"}

    def conversations_replies(self, **kw):
        return {"messages": [{"ts": kw.get("ts"), "text": "parent", "blocks": []}]}

    def reactions_add(self, **kw):
        pass


class _Store:
    def __init__(self, case_id: str | None = None) -> None:
        self.case_id = case_id
        self.seeded: list = []

    def get(self, *key):
        return self.case_id

    def put(self, team, channel, thread, case_id):
        self.case_id = case_id

    def mark_seeded(self, *key):
        self.seeded.append(key)

    def is_seeded(self, *key):
        return bool(self.seeded)

    def is_unlinked(self, *key):
        return False

    def restart_pending(self, *key):
        return False

    def get_last_turn_ts(self, *key):
        return None

    def get_last_action_ts(self, *key):
        return None

    def record_turn(self, *key, turn_ts, action_ts):
        pass

    def clear_last_action_ts(self, *key):
        pass
