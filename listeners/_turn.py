"""Shared turn pipeline + de-duplication used by every entry point.

All Slack surfaces (assistant panel, mention, …) resolve to the same operation:
find-or-create the case for this thread, submit one turn, return the rendered
result. Keeping it here avoids drift between surfaces.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from faultmaven import FaultMavenClient, TurnResult
from store import CaseStore

logger = logging.getLogger(__name__)


class Dedup:
    """Thread-safe, bounded guard against duplicate event delivery.

    Bolt dispatches listeners across a thread pool, so the check-and-record must
    be atomic; eviction drops the oldest key (LRU) rather than flushing the whole
    set, so a recently-seen retry can't slip through right after an overflow.
    """

    def __init__(self, maxsize: int = 5000) -> None:
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._max = maxsize
        self._lock = threading.Lock()

    def is_duplicate(self, key: str) -> bool:
        with self._lock:
            if key in self._seen:
                self._seen.move_to_end(key)
                return True
            self._seen[key] = None
            if len(self._seen) > self._max:
                self._seen.popitem(last=False)  # drop oldest
            return False


def run_turn(
    fm: FaultMavenClient,
    store: CaseStore,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    text: str,
    prior_context: str | None = None,
) -> TurnResult:
    """Find-or-create the case for this thread and advance it by one turn.

    The user's text is always delivered via the turn's ``query`` (not seeded as
    the case ``initial_message``), so it isn't recorded twice and isn't bound by
    the backend's 4000-char initial-message limit. On the first turn of a thread
    that already had discussion, ``prior_context`` carries that discussion so the
    engine isn't blind to what preceded the summons.
    """

    case_id = store.get(team_id, channel_id, thread_ts)
    if case_id is None:
        case_id = fm.create_case(title=None)
        store.put(team_id, channel_id, thread_ts, case_id)
        logger.info("Opened case %s for thread %s", case_id, thread_ts)
        if prior_context:
            return fm.submit_turn(
                case_id,
                query=text,
                pasted_content=prior_context,
                input_type="paste",
            )

    return fm.submit_turn(case_id, query=text)
