"""Regression tests for the Slack-integration hardening audit fixes.

Each test pins one confirmed audit finding: untrusted-content escaping,
Block Kit limit handling, typed backend errors (404 eviction / preset-token
401 / timeout), turn-vs-post failure separation, and event edge cases.
"""

from __future__ import annotations

import json

import httpx
import pytest

from faultmaven.client import (
    CaseNotFoundError,
    FaultMavenAPIError,
    FaultMavenClient,
    FaultMavenTimeoutError,
)
from faultmaven.client import TurnResult
from listeners import _turn
from listeners.events import is_thread_followup_candidate
from rendering import _chunk, build_turn_blocks
from slack_mrkdwn import escape_mrkdwn, to_mrkdwn
from store import CaseStore


# -- untrusted-content escaping (mrkdwn injection) -----------------------------
def test_channel_broadcast_in_llm_output_is_neutralized():
    """Evidence echoed by the LLM must not mass-ping: <!channel> etc. render
    as literal text, never as a live Slack entity."""

    out = to_mrkdwn("per the log: <!channel> URGENT rotate tokens")
    assert "<!channel>" not in out
    assert "&lt;!channel>" in out


def test_raw_slack_link_entity_is_neutralized():
    out = to_mrkdwn("see <https://evil.example|the official runbook>")
    assert "<https://evil.example" not in out


def test_markdown_links_still_convert_to_live_entities():
    """The converter's OWN links (built after the escape) must stay live."""

    assert to_mrkdwn("[docs](https://ok.example)") == "<https://ok.example|docs>"


def test_ampersand_escaped_and_blockquote_preserved():
    assert to_mrkdwn("a & b") == "a &amp; b"
    assert to_mrkdwn("> a quote") == "> a quote"


def test_unterminated_fence_contents_are_protected():
    """A token-cap-truncated fence must not have its contents rewritten by the
    bullet/emphasis passes (the user may copy-run what's displayed)."""

    out = to_mrkdwn("```yaml\n- name: web\n- name: db")
    assert "- name: web" in out
    assert "• name:" not in out


def test_single_line_fence_pair_is_untouched():
    text = "Run ```kubectl get pods -n prod``` now"
    assert to_mrkdwn(text) == text


# -- Block Kit limits -----------------------------------------------------------
def test_chunk_never_exceeds_slack_section_limit_and_balances_fences():
    text = "intro\n\n```\n" + ("x" * 80 + "\n") * 120 + "```\ntail"
    chunks = _chunk(to_mrkdwn(text))
    for chunk in chunks:
        assert len(chunk) <= 3000
        # Every chunk is fence-self-contained: an odd count would render half
        # the code block as plain mrkdwn in that section.
        assert chunk.count("```") % 2 == 0


def test_oversized_next_steps_section_is_chunked():
    actions = [
        {"type": "RUN", "payload": "grep " + ("a" * 500) + f" file{i}"}
        for i in range(12)
    ]
    blocks = build_turn_blocks(
        TurnResult(agent_response="ok", suggested_actions=actions)
    )
    for block in blocks:
        if block.get("type") == "section":
            assert len(block["text"]["text"]) <= 3000


def test_run_command_backticks_cannot_break_out_of_code_span():
    actions = [{"type": "RUN", "payload": "echo `date` <!here>"}]
    blocks = build_turn_blocks(
        TurnResult(agent_response="ok", suggested_actions=actions)
    )
    steps = [
        b["text"]["text"]
        for b in blocks
        if b.get("type") == "section" and "Run:" in b["text"]["text"]
    ]
    assert steps and "<!here>" not in steps[0]
    # The payload's own backticks were neutralized (span can't be closed early).
    assert steps[0].count("`") == 2


def test_evidence_hints_are_escaped():
    actions = [{"type": "EVIDENCE", "label": "logs", "hints": ["<!here> ping"]}]
    blocks = build_turn_blocks(
        TurnResult(agent_response="ok", suggested_actions=actions)
    )
    text = "".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section"
    )
    assert "<!here>" not in text


# -- typed backend errors --------------------------------------------------------
def _client(handler, *, token: str = "", dev: str = "") -> FaultMavenClient:
    client = FaultMavenClient("http://test", token=token, dev_login_username=dev)
    client._http = httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )
    return client


def test_submit_turn_404_raises_case_not_found_with_detail():
    handler = lambda req: httpx.Response(404, json={"detail": "Case not found"})
    client = _client(handler, token="tok")
    with pytest.raises(CaseNotFoundError) as err:
        client.submit_turn("dead", query="hi")
    assert err.value.status_code == 404
    assert "Case not found" in err.value.detail


def test_submit_turn_4xx_carries_backend_detail():
    handler = lambda req: httpx.Response(
        400, json={"detail": "file type not allowed"}
    )
    client = _client(handler, token="tok")
    with pytest.raises(FaultMavenAPIError) as err:
        client.submit_turn("c1", query="hi")
    assert err.value.status_code == 400
    assert "file type not allowed" in err.value.detail


def test_submit_turn_timeout_is_typed():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow turn")

    client = _client(handler, token="tok")
    with pytest.raises(FaultMavenTimeoutError):
        client.submit_turn("c1", query="hi")


def test_preset_token_is_never_wiped_on_401():
    """A transient 401 against a preset FAULTMAVEN_API_TOKEN must surface as a
    401 error — not wipe the token and degrade every later request into a
    misleading dev-login failure."""

    calls = {"posts": 0, "dev_logins": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/dev-login"):
            calls["dev_logins"] += 1
            return httpx.Response(404, json={})
        calls["posts"] += 1
        return httpx.Response(401, json={"detail": "expired"})

    client = _client(handler, token="preset", dev="admin")
    with pytest.raises(FaultMavenAPIError) as err:
        client.create_case(title=None)
    assert err.value.status_code == 401
    assert calls["dev_logins"] == 0  # never tried to replace a preset token
    assert client._token == "preset"  # kept: a transient 401 self-heals


# -- turn_error_text mapping ------------------------------------------------------
def test_error_text_for_case_gone_names_the_fresh_start():
    exc = CaseNotFoundError("gone", status_code=404, detail="")
    assert _turn.turn_error_text(exc) == _turn.CASE_GONE_TEXT


def test_error_text_for_timeout_warns_against_resend():
    assert (
        _turn.turn_error_text(FaultMavenTimeoutError("slow"))
        == _turn.TURN_TIMEOUT_TEXT
    )
    assert "try again" not in _turn.TURN_TIMEOUT_TEXT.lower()


def test_error_text_for_4xx_says_retry_wont_help_and_escapes_detail():
    exc = FaultMavenAPIError(
        "submit_turn failed", status_code=400, detail="bad <!channel> file"
    )
    text = _turn.turn_error_text(exc)
    assert "won't help" in text
    assert "<!channel>" not in text


def test_error_text_for_429_and_unknown_stays_generic():
    exc = FaultMavenAPIError("busy", status_code=429, detail="slow down")
    assert _turn.turn_error_text(exc) == _turn.TURN_ERROR_TEXT
    assert _turn.turn_error_text(RuntimeError("boom")) == _turn.TURN_ERROR_TEXT


# -- run_turn: mapping lifecycle ---------------------------------------------------
class _Store:
    def __init__(self) -> None:
        self.m: dict = {}
        self.deleted: list = []

    def get(self, t, c, th):
        return self.m.get((t, c, th))

    def put(self, t, c, th, cid):
        self.m[(t, c, th)] = cid

    def delete(self, t, c, th):
        self.deleted.append((t, c, th))
        self.m.pop((t, c, th), None)


class _FM:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.turns: list = []

    def create_case(self, *, title=None, initial_message=None):
        return "case_1"

    def submit_turn(self, case_id, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.turns.append((case_id, kwargs))
        return TurnResult(agent_response="on it")


def test_mapping_committed_only_after_first_turn_succeeds():
    """A transient failure on turn 1 must NOT leave a mapping behind: the
    retry would find the case 'existing' and never re-deliver the one-time
    seed context."""

    store = _Store()
    fm = _FM(fail=FaultMavenAPIError("boom", status_code=502, detail=""))
    with pytest.raises(FaultMavenAPIError):
        _turn.run_turn(
            fm, store, team_id="T", channel_id="C", thread_ts="TS", text="hi"
        )
    assert store.m == {}  # no mapping → the retry re-seeds prior_context

    fm_ok = _FM()
    _turn.run_turn(
        fm_ok, store, team_id="T", channel_id="C", thread_ts="TS", text="hi"
    )
    assert store.get("T", "C", "TS") == "case_1"


def test_stale_mapping_evicted_on_server_side_404():
    store = _Store()
    store.put("T", "C", "TS", "dead_case")
    fm = _FM(fail=CaseNotFoundError("gone", status_code=404, detail=""))
    with pytest.raises(CaseNotFoundError):
        _turn.run_turn(
            fm, store, team_id="T", channel_id="C", thread_ts="TS", text="hi"
        )
    assert store.deleted == [("T", "C", "TS")]
    assert store.get("T", "C", "TS") is None  # next message starts fresh


# -- run_turn_and_post: turn-vs-post failure separation ------------------------------
class _SlackClient:
    def __init__(self, *, fail_updates: int = 0) -> None:
        self.fail_updates = fail_updates
        self.posts: list = []
        self.updates: list = []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": "PH1"}

    def chat_update(self, **kw):
        if self.fail_updates > 0:
            self.fail_updates -= 1
            raise RuntimeError("invalid_blocks")
        self.updates.append(kw)
        return {"ok": True}


_COMMON = dict(channel="C", thread_ts="TS", team_id="T")


def test_turn_failure_renders_typed_error_text():
    client = _SlackClient()
    fm = _FM(fail=FaultMavenAPIError("no", status_code=400, detail="too big"))
    _turn.run_turn_and_post(client, fm, _Store(), text="hi", **_COMMON)
    assert "won't help" in client.updates[0]["text"]


def test_post_failure_after_committed_turn_degrades_to_plain_text():
    """The blocks update fails (e.g. invalid_blocks) AFTER the backend
    committed the turn: the reply must degrade to plain text — never claim
    the turn errored and invite a duplicate."""

    client = _SlackClient(fail_updates=1)
    fm = _FM()
    _turn.run_turn_and_post(client, fm, _Store(), text="hi", **_COMMON)
    assert len(fm.turns) == 1
    fallback = client.updates[0]
    assert "blocks" not in fallback
    assert "on it" in fallback["text"]
    assert "try again" not in fallback["text"].lower()


def test_every_update_failing_never_raises():
    client = _SlackClient(fail_updates=10)
    fm = _FM()
    _turn.run_turn_and_post(client, fm, _Store(), text="hi", **_COMMON)
    assert len(fm.turns) == 1  # turn ran; nothing propagated to the runner


def test_dm_intro_note_is_attached_on_first_turn():
    client = _SlackClient()
    fm = _FM()
    _turn.run_turn_and_post(
        client, fm, _Store(), text="hi", intro_note="reply in thread", **_COMMON
    )
    contexts = [
        e["text"]
        for b in client.updates[0]["blocks"]
        if b.get("type") == "context"
        for e in b.get("elements", [])
    ]
    assert "reply in thread" in contexts


# -- event edge cases -----------------------------------------------------------------
def test_thread_broadcast_reply_is_a_followup_candidate():
    """'Also send to #channel' replies carry new user input and must not be
    silently dropped."""

    event = {
        "subtype": "thread_broadcast",
        "channel_type": "channel",
        "thread_ts": "111.0",
        "text": "restart fixed it",
    }
    assert is_thread_followup_candidate(event, bot_user_id="B1")


def test_reply_during_opening_turn_gets_skip_reaction():
    """While the case-opening turn holds the gate (mapping not yet committed),
    a thread reply must get the ⏭️ signal, not a silent drop."""

    key = _turn._thread_key("T", "C", "TS")
    assert _turn._gate.try_enter(key)
    try:
        assert _turn.is_thread_busy("T", "C", "TS")
    finally:
        _turn._gate.release(key)
    assert not _turn.is_thread_busy("T", "C", "TS")


# -- store ---------------------------------------------------------------------------
def test_store_delete_evicts_mapping(tmp_path):
    store = CaseStore(str(tmp_path / "cases.db"))
    store.put("T", "C", "TS", "case_1")
    store.delete("T", "C", "TS")
    assert store.get("T", "C", "TS") is None
    store.close()


# -- config ----------------------------------------------------------------------------
def _settings(monkeypatch, **env):
    from config import Settings

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_log_level_is_case_insensitive(monkeypatch):
    assert _settings(monkeypatch, LOG_LEVEL="debug").log_level == "DEBUG"


def test_invalid_log_level_names_the_setting(monkeypatch):
    with pytest.raises(Exception, match="LOG_LEVEL"):
        _settings(monkeypatch, LOG_LEVEL="chatty")


def test_relative_store_path_is_anchored_to_repo_not_cwd(monkeypatch):
    from pathlib import Path

    import config as config_mod

    settings = _settings(monkeypatch, CASE_STORE_PATH="data/x.db")
    resolved = Path(settings.case_store_path)
    assert resolved.is_absolute()
    assert resolved.parent.parent == Path(config_mod.__file__).resolve().parent


def test_absolute_store_path_is_kept(monkeypatch):
    settings = _settings(monkeypatch, CASE_STORE_PATH="/var/lib/fm.db")
    assert settings.case_store_path == "/var/lib/fm.db"
