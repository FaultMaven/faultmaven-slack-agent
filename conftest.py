# Root conftest so pytest puts the repo root on sys.path (prepend import mode),
# letting tests import the top-level modules (faultmaven, store, rendering,
# listeners, config) without an installed package.

import pytest


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    """Reset the process-global ``_shutting_down`` event around every test.

    It is a module-global ``threading.Event`` (``listeners._turn``); a test that
    exercises a shutdown path (e.g. test_transport's TestClient lifespan →
    ``shutdown_runtime`` → ``begin_shutdown``) sets it process-wide and nothing
    clears it, which would otherwise flip ``turn_error_text``'s branch selection
    for any later test order-dependently. Owned here so it covers all files.
    """

    from listeners import _turn

    _turn._shutting_down.clear()
    yield
    _turn._shutting_down.clear()


@pytest.fixture
def no_poll_sleep(monkeypatch):
    """Collapse ``_poll``'s inter-poll backoff so 202/poll tests run instantly.

    The first sleep is 1.5s of real wall clock; a handful of poll-path tests
    would add seconds to every suite run for timing nobody is asserting. Patches
    only the client module's ``time.sleep``, so the deadline arithmetic (real
    ``time.monotonic``) is untouched and a genuine runaway loop still terminates.
    """

    from faultmaven import client as _client

    monkeypatch.setattr(_client.time, "sleep", lambda _s: None)
