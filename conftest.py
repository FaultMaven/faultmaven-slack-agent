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
