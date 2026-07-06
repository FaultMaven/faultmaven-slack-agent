"""app_mention helpers — the catch-up read must never propagate.

Since the handler now posts the placeholder *before* the catch-up read, a raise
from that read would strand the placeholder; _fetch_thread_context must degrade
to None on any error, not just SlackApiError.
"""

from __future__ import annotations

import httpx
from slack_sdk.errors import SlackApiError

from listeners.events import _fetch_thread_context


class _Client:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def conversations_replies(self, **kwargs):
        raise self._exc


def test_degrades_to_none_on_slack_api_error():
    client = _Client(SlackApiError("boom", {"error": "not_in_channel"}))
    assert _fetch_thread_context(client, "C", "TS", exclude_ts=None) is None


def test_degrades_to_none_on_transport_error():
    # A connection reset/timeout is NOT a SlackApiError; it must still be caught
    # so the already-posted placeholder isn't stranded.
    client = _Client(httpx.ConnectError("connection reset"))
    assert _fetch_thread_context(client, "C", "TS", exclude_ts=None) is None


def test_returns_prior_human_messages_joined():
    class Ok:
        def conversations_replies(self, **kwargs):
            return {
                "messages": [
                    {"ts": "1", "text": "web-1 is 500ing"},
                    {"ts": "2", "text": "bot reply", "bot_id": "B1"},  # excluded
                    {"ts": "3", "text": "since the deploy"},
                ]
            }

    out = _fetch_thread_context(Ok(), "C", "TS", exclude_ts="9")
    assert out == "web-1 is 500ing\nsince the deploy"
