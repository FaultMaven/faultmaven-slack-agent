"""A fake clock for the turn recovery loop (``FaultMavenClient.submit_turn``).

The loop waits between attempts for seconds to minutes of wall time, bounded by
``turn_recovery_seconds``. Driven on this clock instead, a test runs the whole
bound in microseconds and can assert exactly how long each wait was.
"""

from __future__ import annotations

import threading


class FakeClock:
    """``monotonic()`` and an interruptible ``wait()`` that advance virtual time.

    ``wait`` mirrors ``threading.Event.wait``: it returns True when ``stop`` is
    set (immediately, without advancing), else advances the clock by the whole
    wait and returns False. ``waits`` records every requested wait, in order.
    """

    #: Reads after which the loop is a runaway. A loop that no longer
    #: advances virtual time (no backoff) or has no end (no bound) would
    #: otherwise spin forever on this clock; this fails it in milliseconds.
    MAX_READS = 10_000

    def __init__(self, stop: threading.Event | None = None) -> None:
        self.now = 1000.0
        self.waits: list[float] = []
        self.reads = 0
        self.stop = stop or threading.Event()

    def monotonic(self) -> float:
        self.reads += 1
        assert self.reads <= self.MAX_READS, "the recovery loop did not stop"
        return self.now

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        if self.stop.is_set():
            return True
        self.now += seconds
        return self.stop.is_set()

    def install(self, client) -> "FakeClock":
        """Drive ``client``'s recovery loop on this clock. Shares the client's
        shutdown event, so ``client.begin_shutdown()`` interrupts a wait."""

        self.stop = client._stopping
        client._clock = self.monotonic
        client._wait = self.wait
        return self
