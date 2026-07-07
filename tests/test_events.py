"""app_mention helpers — the catch-up read must never propagate.

Since the handler now posts the placeholder *before* the catch-up read, a raise
from that read would strand the placeholder; _fetch_thread_context must degrade
to None on any error, not just SlackApiError.
"""

from __future__ import annotations

import httpx
from slack_sdk.errors import SlackApiError

from listeners.events import _fetch_thread_context, is_thread_followup_candidate

_BOT = "UBOT"


def _reply(**over) -> dict:
    """A plain human reply inside a channel thread (the True case)."""
    e = {"channel_type": "channel", "thread_ts": "t1", "text": "here's the log", "ts": "9"}
    e.update(over)
    return e


# -- is_thread_followup_candidate (auto-continue gate) ------------------------
def test_plain_thread_reply_is_a_candidate():
    assert is_thread_followup_candidate(_reply(), bot_user_id=_BOT) is True


def test_file_share_reply_is_a_candidate():
    assert is_thread_followup_candidate(
        _reply(subtype="file_share", text=""), bot_user_id=_BOT
    ) is True


def test_bot_own_message_is_not_a_candidate():
    assert is_thread_followup_candidate(_reply(bot_id="B1"), bot_user_id=_BOT) is False


def test_edit_delete_subtypes_are_not_candidates():
    assert is_thread_followup_candidate(_reply(subtype="message_changed"), bot_user_id=_BOT) is False


def test_dm_is_not_a_candidate():
    # The Assistant surface owns DMs.
    assert is_thread_followup_candidate(_reply(channel_type="im"), bot_user_id=_BOT) is False


def test_top_level_channel_message_is_not_a_candidate():
    # No thread_ts → ambient chatter, never starts an investigation.
    e = _reply()
    e.pop("thread_ts")
    assert is_thread_followup_candidate(e, bot_user_id=_BOT) is False


def test_mention_is_not_a_candidate_app_mention_owns_it():
    assert is_thread_followup_candidate(
        _reply(text=f"<@{_BOT}> take another look"), bot_user_id=_BOT
    ) is False


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
