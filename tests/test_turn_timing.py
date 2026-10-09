"""Contract 12.4.0: coded 504s, and attempt/recovery timeouts sized from the
backend's published turn bound (``limits.turnResponseBoundSeconds``).

The capabilities endpoint is a scripted double; no network is touched.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

import listeners._turn as _turn
from faultmaven.client import (
    FALLBACK_ATTEMPT_SECONDS,
    FALLBACK_RECOVERY_SECONDS,
    TURN_NETWORK_MARGIN_SECONDS,
    TURN_RECOVERY_ATTEMPTS,
    FaultMavenClient,
    FaultMavenTimeoutError,
)
from tests._clock import FakeClock
from tests.test_turn_recovery import _KEY, _ANSWER, _Backend, _ok, _read_timeout

_CAPS = "/api/v1/meta/capabilities"


def _caps(bound=240.0, **extra) -> httpx.Response:
    limits = {"allowedExtensions": [], "maxFileBytes": 1, "turnCeilingSeconds": 200.0}
    if bound is not None:
        limits["turnResponseBoundSeconds"] = bound
    limits.update(extra)
    return httpx.Response(200, json={"limits": limits})


class _Caps:
    """Routes the capabilities GET to ``steps`` (last repeats); all else goes to
    the turn backend."""

    def __init__(self, backend: _Backend, *steps) -> None:
        self.backend, self.steps, self.reads = backend, list(steps), 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == _CAPS:
            steps = self.steps or [httpx.Response(503)]
            step = steps[min(self.reads, len(steps) - 1)]
            self.reads += 1
            return step(request) if callable(step) else step
        return self.backend(request)


def _client(backend: _Backend, *caps_steps, **kw):
    kw.setdefault("token", "tok")
    client = FaultMavenClient("http://test", **kw)
    caps = _Caps(backend, *caps_steps)
    client._http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(caps))
    return client, FakeClock().install(client), caps


def _timeouts(backend: _Backend) -> list[float]:
    return [r.extensions["timeout"]["read"] for r in backend.requests]


def _llm_timeout(retry_after: str | None = "30") -> httpx.Response:
    headers = {"x-error-code": "LLM_TIMEOUT"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return httpx.Response(504, json={"detail": "provider timeout"}, headers=headers)


# -- LLM_TIMEOUT: nothing committed, ask again after Retry-After --------------------
def test_llm_timeout_is_resent_after_its_retry_after_and_answers():
    backend = _Backend(_llm_timeout("30"), _ok())
    client, clock, _ = _client(backend)
    result = client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert result.agent_response == _ANSWER["agent_response"]
    assert backend.keys == [_KEY, _KEY]
    assert clock.waits == [30.0]  # the API's own Retry-After, not the backoff


def test_llm_timeout_without_retry_after_uses_the_backoff():
    backend = _Backend(_llm_timeout(None), _ok())
    client, clock, _ = _client(backend)
    client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert clock.waits == [1.0]


def test_llm_timeout_retries_end_with_the_bound_not_before():
    """It is a transient failure: asked again until the recovery bound runs out,
    then reported as the timeout class."""

    backend = _Backend(_llm_timeout("30"))
    client, clock, _ = _client(backend, turn_recovery_seconds=100.0)
    with pytest.raises(FaultMavenTimeoutError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 4  # t=0, 30, 60, 90; the next would pass 100
    assert "nothing of it committed" in str(err.value)
    assert _turn.turn_error_text(err.value) == _turn.TURN_TIMEOUT_TEXT


def test_llm_timeout_is_not_read_as_a_client_side_give_up():
    backend = _Backend(_llm_timeout("30"), _ok())
    client, _, _ = _client(backend)
    client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 2  # it entered the loop; it was not final


def test_a_client_side_timeout_is_still_an_unknown_outcome():
    """The genuine give-up stays "may still complete"."""

    backend = _Backend(_read_timeout)
    client, _, _ = _client(backend, turn_recovery_seconds=5.0)
    with pytest.raises(FaultMavenTimeoutError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert "may" in str(err.value) and "still complete" in str(err.value)


# -- REQUEST_TIMEOUT: at most one retry, no Retry-After expected ---------------------
def test_request_timeout_is_resent_at_most_once_and_at_once():
    timed_out = httpx.Response(504, headers={"x-error-code": "REQUEST_TIMEOUT"})
    backend = _Backend(timed_out, timed_out, _ok())
    client, clock, _ = _client(backend)
    with pytest.raises(FaultMavenTimeoutError):
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 2
    assert clock.waits == []  # no Retry-After to honour, no backoff


def test_request_timeout_ignores_a_retry_after_it_should_not_carry():
    timed_out = httpx.Response(
        504, headers={"x-error-code": "REQUEST_TIMEOUT", "Retry-After": "30"}
    )
    backend = _Backend(timed_out, _ok())
    client, clock, _ = _client(backend)
    client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert clock.waits == []


# -- the attempt timeout, from the published bound ----------------------------------
def test_the_attempt_timeout_is_the_published_bound_plus_the_margin():
    backend = _Backend(_ok())
    client, _, _ = _client(backend, _caps(240.0), derive_attempt_timeout=True)
    client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert _timeouts(backend) == [240.0 + TURN_NETWORK_MARGIN_SECONDS]


def test_the_recovery_bound_is_attempts_times_the_attempt_in_force():
    backend = _Backend()
    client, clock, _ = _client(
        backend, _caps(100.0), derive_attempt_timeout=True, derive_recovery_seconds=True
    )
    attempt = 100.0 + TURN_NETWORK_MARGIN_SECONDS

    def slow(request):
        clock.now += attempt  # each attempt takes its whole timeout
        raise httpx.ReadTimeout("slow", request=request)

    backend.script = [slow]
    with pytest.raises(FaultMavenTimeoutError):
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    # 115 s, 1 s backoff, 115 s, 2 s backoff, then what is left of 3 x 115 = 345.
    assert _timeouts(backend) == [attempt, attempt, 345.0 - (2 * attempt + 3.0)]


def test_recovery_derives_from_a_pinned_attempt_too():
    client, _, _ = _client(
        _Backend(_ok()), _caps(100.0), timeout=40.0, derive_recovery_seconds=True
    )
    assert client.turn_timing() == (40.0, TURN_RECOVERY_ATTEMPTS * 40.0)


def test_a_pinned_attempt_and_recovery_never_read_capabilities():
    backend = _Backend(_ok())
    client, _, caps = _client(
        backend, _caps(240.0), timeout=77.0, turn_recovery_seconds=300.0
    )
    client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert caps.reads == 0
    assert _timeouts(backend) == [77.0]


def test_the_bound_is_cached_then_reread_after_the_ttl():
    backend = _Backend(_ok())
    client, clock, caps = _client(
        backend, _caps(100.0), _caps(300.0), derive_attempt_timeout=True
    )
    assert client.turn_timing()[0] == 100.0 + TURN_NETWORK_MARGIN_SECONDS
    assert client.turn_timing()[0] == 100.0 + TURN_NETWORK_MARGIN_SECONDS
    assert caps.reads == 1
    clock.now += 301.0  # a provider switch since: re-read
    assert client.turn_timing()[0] == 300.0 + TURN_NETWORK_MARGIN_SECONDS
    assert caps.reads == 2


def test_the_longest_attempt_in_use_sizes_the_shutdown_drain():
    client, clock, _ = _client(
        _Backend(_ok()), _caps(300.0), _caps(30.0), derive_attempt_timeout=True
    )
    assert client.longest_attempt_seconds == FALLBACK_ATTEMPT_SECONDS
    client.turn_timing()
    assert client.longest_attempt_seconds == 300.0 + TURN_NETWORK_MARGIN_SECONDS
    clock.now += 301.0
    client.turn_timing()  # the bound shrank; a turn at the old size may be live
    assert client.longest_attempt_seconds == 300.0 + TURN_NETWORK_MARGIN_SECONDS


# -- fallback -------------------------------------------------------------------------
def _unreachable(request):
    raise httpx.ConnectError("no route", request=request)


@pytest.mark.parametrize(
    "response",
    [
        _unreachable,
        httpx.Response(503),
        httpx.Response(200, text="<html>SPA</html>"),
        httpx.Response(200, json={"limits": {}}),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"limits": {"turnResponseBoundSeconds": "soon"}}),
        httpx.Response(200, json={"limits": {"turnResponseBoundSeconds": 0}}),
        httpx.Response(200, json={"limits": {"turnResponseBoundSeconds": -5}}),
        httpx.Response(200, json={"limits": {"turnResponseBoundSeconds": True}}),
        httpx.Response(200, json={"limits": {"turnResponseBoundSeconds": 1e9}}),
    ],
    ids=["unreachable", "503", "html", "no-field", "no-limits", "string", "zero",
         "negative", "bool", "absurd"],
)
def test_an_unusable_capabilities_answer_falls_back_and_says_so(response, caplog):
    backend = _Backend(_ok())
    client, _, _ = _client(
        backend, response, derive_attempt_timeout=True, derive_recovery_seconds=True
    )
    with caplog.at_level(logging.WARNING, logger="faultmaven.client"):
        attempt, recovery = client.turn_timing()
    assert (attempt, recovery) == (FALLBACK_ATTEMPT_SECONDS, FALLBACK_RECOVERY_SECONDS)
    assert any("published turn bound" in r.message for r in caplog.records)
    client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert _timeouts(backend) == [FALLBACK_ATTEMPT_SECONDS]


def test_a_failed_read_is_retried_after_a_short_interval_not_every_turn():
    client, clock, caps = _client(
        _Backend(_ok()), httpx.Response(503), _caps(100.0), derive_attempt_timeout=True
    )
    assert client.turn_timing()[0] == FALLBACK_ATTEMPT_SECONDS
    assert client.turn_timing()[0] == FALLBACK_ATTEMPT_SECONDS
    assert caps.reads == 1
    clock.now += 31.0
    assert client.turn_timing()[0] == 100.0 + TURN_NETWORK_MARGIN_SECONDS


# -- the app wires the knobs ----------------------------------------------------------
@pytest.mark.parametrize(
    ("req", "rec", "derive_attempt", "derive_recovery", "timeout", "recovery"),
    [
        (None, None, True, True, 150.0, 660.0),
        (90.0, None, False, True, 90.0, 660.0),
        (None, 400.0, True, False, 150.0, 400.0),
    ],
)
def test_make_fault_client_derives_only_the_unset_knobs(
    tmp_path, req, rec, derive_attempt, derive_recovery, timeout, recovery
):
    import app

    settings = SimpleNamespace(
        credential_store_path=str(tmp_path / "none.db"),
        faultmaven_refresh_token="",
        faultmaven_api_token="tok",
        faultmaven_api_url="http://test",
        faultmaven_dev_login_username="",
        faultmaven_request_timeout=req,
        faultmaven_turn_recovery_seconds=rec,
        faultmaven_oauth_client_id="x",
        faultmaven_require_workspace_binding=False,
    )
    client = app.make_fault_client(settings)
    assert client._derive_attempt is derive_attempt
    assert client._derive_recovery is derive_recovery
    assert client._timeout == timeout
    assert client._turn_recovery_seconds == recovery
