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
    FaultMavenNothingCommittedError,
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


def test_llm_timeout_is_resent_twice_at_most_then_nothing_committed():
    """R6: each re-send is a fresh charged run, so the count caps it, well
    before the recovery bound does."""

    backend = _Backend(_llm_timeout("30"))
    client, clock, _ = _client(backend, turn_recovery_seconds=10_000.0, timeout=10.0)
    with pytest.raises(FaultMavenNothingCommittedError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 3  # the run and two re-sends
    assert err.value.kind == "llm_timeout"
    assert not isinstance(err.value, FaultMavenTimeoutError)
    assert _turn.turn_error_text(err.value) == _turn.NOTHING_COMMITTED_TEXT
    assert _turn.retry_may_help(err.value)


def test_a_request_timeout_ends_a_llm_timeout_sequence_without_a_resend():
    rto = httpx.Response(504, headers={"x-error-code": "REQUEST_TIMEOUT"})
    backend = _Backend(_llm_timeout("30"), rto, _ok())
    client, _, _ = _client(backend, turn_recovery_seconds=10_000.0, timeout=10.0)
    with pytest.raises(FaultMavenNothingCommittedError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 2  # the LLM_TIMEOUT re-send, then final
    assert err.value.kind == "request_timeout"


def test_a_resend_that_cannot_fit_a_whole_attempt_is_not_launched():
    """R2: never start a charged fresh run the bound will cut off."""

    backend = _Backend(_llm_timeout("30"), _ok())
    # After the 30 s wait 70 s remain; an 80 s attempt would be cut to 70.
    client, clock, _ = _client(backend, turn_recovery_seconds=100.0, timeout=80.0)
    with pytest.raises(FaultMavenNothingCommittedError):
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 1
    # ...and one that fits is launched.
    backend = _Backend(_llm_timeout("30"), _ok())
    client, _, _ = _client(backend, turn_recovery_seconds=100.0, timeout=70.0)
    assert client.submit_turn("c1", idempotency_key=_KEY, query="x").agent_response
    assert len(backend.requests) == 2


def test_an_unmodelled_coded_504_is_final_and_nothing_committed():
    """R4: the contract says no 504 commits; a label we do not model is final."""

    backend = _Backend(httpx.Response(504, headers={"x-error-code": "SOMETHING_NEW"}), _ok())
    client, _, _ = _client(backend)
    with pytest.raises(FaultMavenNothingCommittedError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 1
    assert err.value.kind == "coded_504"


def test_an_unmodelled_coded_504_after_a_client_timeout_is_still_final():
    backend = _Backend(_read_timeout, httpx.Response(504, headers={"x-error-code": "NEW"}), _ok())
    client, _, _ = _client(backend)
    with pytest.raises(FaultMavenNothingCommittedError):
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 2


# -- R3: a refusal is the real answer when no earlier attempt can have committed ------
@pytest.mark.parametrize(
    ("refusal", "expected"),
    [
        (httpx.Response(402, json={"detail": "q"}, headers={"x-error-code": "QUOTA_EXHAUSTED"}), "FaultMavenAPIError"),
        (httpx.Response(400, json={"detail": "bad"}), "FaultMavenAPIError"),
        (httpx.Response(429, json={"detail": "cap"}, headers={"x-error-code": "TENANT_TURN_CAP_EXCEEDED", "Retry-After": "40000"}), "FaultMavenRateLimitError"),
        (httpx.Response(503, json={"detail": "down"}), "FaultMavenAPIError"),
    ],
    ids=["402", "400", "429-cap", "503"],
)
def test_after_a_labelled_504_a_refusal_surfaces_as_itself(refusal, expected):
    import faultmaven.client as fc

    backend = _Backend(_llm_timeout("30"), refusal)
    client, _, _ = _client(backend, timeout=10.0, turn_recovery_seconds=10_000.0)
    with pytest.raises(getattr(fc, expected)) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert not isinstance(err.value, (FaultMavenTimeoutError, FaultMavenNothingCommittedError))
    assert len(backend.requests) == 2


def test_a_refusal_after_a_client_timeout_is_still_masked_as_unknown():
    """The doubt is real when an earlier attempt may have committed."""

    backend = _Backend(_read_timeout, httpx.Response(400, json={"detail": "bad"}))
    client, _, _ = _client(backend)
    with pytest.raises(FaultMavenTimeoutError):
        client.submit_turn("c1", idempotency_key=_KEY, query="x")


@pytest.mark.parametrize("gateway", [502, 504])
def test_an_unlabelled_gateway_error_sets_the_doubt(gateway):
    backend = _Backend(httpx.Response(gateway), httpx.Response(400, json={"detail": "bad"}))
    client, _, _ = _client(backend)
    with pytest.raises(FaultMavenTimeoutError):
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 2


def test_a_labelled_504_clears_the_doubt_raised_by_a_client_timeout():
    backend = _Backend(_read_timeout, _llm_timeout("30"), httpx.Response(400, json={"detail": "bad"}))
    client, _, _ = _client(backend, timeout=10.0, turn_recovery_seconds=10_000.0)
    import faultmaven.client as fc

    with pytest.raises(fc.FaultMavenAPIError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert not isinstance(err.value, FaultMavenTimeoutError)


def test_a_server_error_before_any_doubt_is_not_waited_out():
    import faultmaven.client as fc

    backend = _Backend(_llm_timeout("30"), httpx.Response(503, json={"detail": "x"}), _ok())
    client, _, _ = _client(backend, timeout=10.0, turn_recovery_seconds=10_000.0)
    with pytest.raises(fc.FaultMavenAPIError):
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 2


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
def test_request_timeout_is_never_resent_automatically():
    timed_out = httpx.Response(504, headers={"x-error-code": "REQUEST_TIMEOUT"})
    backend = _Backend(timed_out, _ok())
    client, clock, _ = _client(backend)
    with pytest.raises(FaultMavenNothingCommittedError) as err:
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 1
    assert clock.waits == []
    assert err.value.kind == "request_timeout"
    assert _turn.turn_error_text(err.value) == _turn.NOTHING_COMMITTED_NARROW_TEXT


def test_request_timeout_after_a_client_timeout_is_final_too():
    timed_out = httpx.Response(504, headers={"x-error-code": "REQUEST_TIMEOUT"})
    backend = _Backend(_read_timeout, timed_out, _ok())
    client, _, _ = _client(backend)
    with pytest.raises(FaultMavenNothingCommittedError):
        client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert len(backend.requests) == 2


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


def test_a_short_attempt_pin_does_not_shrink_recovery_below_the_backends_bound():
    """R1: the Cloud configmap pins 120 s against a 258.5 s bound."""

    client, _, _ = _client(
        _Backend(_ok()), _caps(258.5 - TURN_NETWORK_MARGIN_SECONDS),
        timeout=120.0, derive_recovery_seconds=True,
    )
    attempt, recovery = client.turn_timing()
    assert attempt == 120.0
    assert recovery == TURN_RECOVERY_ATTEMPTS * 258.5


def test_a_long_attempt_pin_still_scales_recovery():
    client, _, _ = _client(
        _Backend(_ok()), _caps(100.0), timeout=400.0, derive_recovery_seconds=True
    )
    assert client.turn_timing() == (400.0, TURN_RECOVERY_ATTEMPTS * 400.0)


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
        httpx.Response(200, json={"limits": {"turnResponseBoundSeconds": 29.9}}),
        httpx.Response(200, json={"limits": {"turnResponseBoundSeconds": 1200.1}}),
        httpx.Response(200, content=b'{"limits":{"turnResponseBoundSeconds":NaN}}'),
        httpx.Response(200, content=b'{"limits":{"turnResponseBoundSeconds":Infinity}}'),
    ],
    ids=["unreachable", "503", "html", "no-field", "no-limits", "string", "zero",
         "negative", "bool", "absurd", "below-min", "above-max", "nan", "inf"],
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


def test_the_published_range_edges_are_accepted():
    for edge in (30.0, 1200.0):
        client, _, _ = _client(_Backend(_ok()), _caps(edge), derive_attempt_timeout=True)
        assert client.turn_timing()[0] == edge + TURN_NETWORK_MARGIN_SECONDS


def test_the_retry_after_of_an_llm_timeout_is_clamped_to_a_poll_interval():
    backend = _Backend(_llm_timeout("40000"), _ok())
    client, clock, _ = _client(backend, timeout=10.0, turn_recovery_seconds=10_000.0)
    client.submit_turn("c1", idempotency_key=_KEY, query="x")
    assert clock.waits == [60.0]


def test_concurrent_turns_share_one_capabilities_read():
    import threading
    import time

    reads = []

    def slow(request):
        reads.append(1)
        time.sleep(0.2)
        return _caps(100.0)

    client = FaultMavenClient("http://test", token="tok", derive_attempt_timeout=True)
    client._http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(slow))
    results: list = []
    threads = [
        threading.Thread(target=lambda: results.append(client.turn_timing()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(reads) == 1
    assert set(results) == {(100.0 + TURN_NETWORK_MARGIN_SECONDS, FALLBACK_RECOVERY_SECONDS)}


# -- R5: the pins in Settings ----------------------------------------------------------
def _slack_env(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")


@pytest.mark.parametrize("name", ["FAULTMAVEN_REQUEST_TIMEOUT", "FAULTMAVEN_TURN_RECOVERY_SECONDS"])
def test_a_blank_pin_means_unset(monkeypatch, name):
    from config import Settings

    _slack_env(monkeypatch)
    monkeypatch.setenv(name, "")
    s = Settings(_env_file=None)
    assert s.faultmaven_request_timeout is None
    assert s.faultmaven_turn_recovery_seconds is None


@pytest.mark.parametrize("name", ["FAULTMAVEN_REQUEST_TIMEOUT", "FAULTMAVEN_TURN_RECOVERY_SECONDS"])
@pytest.mark.parametrize("bad", ["inf", "nan", "-inf", "0", "-5"])
def test_a_non_finite_or_non_positive_pin_is_refused(monkeypatch, name, bad):
    from config import Settings

    _slack_env(monkeypatch)
    from pydantic import ValidationError

    monkeypatch.setenv(name, bad)
    with pytest.raises(ValidationError, match=name):
        Settings(_env_file=None)
