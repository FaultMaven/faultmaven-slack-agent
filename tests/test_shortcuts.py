"""Message-shortcut case-opener: alert extraction + seed-from-message (P3)."""

from __future__ import annotations

import importlib.util
import sys

from faultmaven.client import TurnResult
from slack_text import message_to_text


# -- the make-or-break piece: extract readable text from rich alert messages ---
def test_plain_text_message():
    assert message_to_text({"text": "disk full on web-1"}) == "disk full on web-1"


def test_blocks_section_and_fields_beat_fallback_text():
    msg = {
        "text": "Alert triggered",  # the useless fallback stub
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "🔥 High latency"}},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Service:* checkout-api"},
                "fields": [
                    {"type": "mrkdwn", "text": "*p99:* 2.4s"},
                    {"type": "mrkdwn", "text": "*SLO:* 500ms"},
                ],
            },
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "since 14:02 UTC"}]},
        ],
    }
    text = message_to_text(msg)
    assert "High latency" in text
    assert "checkout-api" in text
    assert "p99:* 2.4s" in text and "SLO:* 500ms" in text
    assert "14:02" in text
    assert text != "Alert triggered"  # we used the blocks, not the stub


def test_legacy_attachments_datadog_style():
    msg = {
        "text": "",
        "attachments": [
            {
                "pretext": "Triggered: error rate",
                "title": "payments-svc error rate > 5%",
                "text": "Current value 7.3%",
                "fields": [
                    {"title": "Host", "value": "pay-3"},
                    {"title": "Window", "value": "5m"},
                ],
                "fallback": "payments-svc error rate alert",
            }
        ],
    }
    text = message_to_text(msg)
    assert "error rate" in text
    assert "payments-svc" in text and "7.3%" in text
    assert "Host: pay-3" in text and "Window: 5m" in text


def test_attachment_fallback_only_when_no_structure():
    msg = {"attachments": [{"fallback": "raw alert text"}]}
    assert message_to_text(msg) == "raw alert text"


# -- the title's destination (#52) --------------------------------------------
# Alertmanager's default Slack template always sets `title` AND `title_link`,
# so `fallback` — the only field carrying the URL — is never reached. The
# engine then asked the reporter for an `<alertmanager-host>:<port>` that was
# in the message it had been handed.
_ALERTMANAGER = {
    "text": "",
    "attachments": [
        {
            "color": "a30200",
            "fallback": (
                "[FIRING:2] FaultMavenSLAAtRisk (faultmaven-api backend warning) | "
                "<http://alertmanager.monitoring:9093/#/alerts?receiver=warning-receiver>"
            ),
            "title": "[FIRING:2] FaultMavenSLAAtRisk (faultmaven-api backend warning)",
            "title_link": (
                "http://alertmanager.monitoring:9093/#/alerts?receiver=warning-receiver"
            ),
            "mrkdwn_in": ["fallback", "pretext", "text"],
        }
    ],
}


def test_alertmanager_title_link_reaches_the_engine():
    text = message_to_text(_ALERTMANAGER)
    assert "FaultMavenSLAAtRisk" in text
    assert "alertmanager.monitoring:9093" in text


def test_title_link_follows_the_title_it_annotates():
    lines = message_to_text(_ALERTMANAGER).splitlines()
    assert lines[0].startswith("[FIRING:2]")
    assert lines[1] == (
        "http://alertmanager.monitoring:9093/#/alerts?receiver=warning-receiver"
    )


def test_title_link_is_not_repeated_when_another_field_carried_it():
    """Some senders put the link in `text` too; the alert must not arrive with
    the same URL twice."""

    msg = {
        "attachments": [
            {
                "title": "payments-svc error rate > 5%",
                "title_link": "https://app.datadoghq.com/monitors/123",
                "text": "See https://app.datadoghq.com/monitors/123 for history",
            }
        ]
    }
    assert message_to_text(msg).count("https://app.datadoghq.com/monitors/123") == 1


def test_title_without_a_link_is_unchanged():
    msg = {"attachments": [{"title": "disk full on web-1"}]}
    assert message_to_text(msg) == "disk full on web-1"


def test_malformed_title_link_degrades_to_no_link():
    """External payloads are occasionally malformed; `str()` on a non-string
    would splice a Python repr into the evidence."""

    msg = {"attachments": [{"title": "T", "title_link": {"unexpected": 1}}]}
    assert message_to_text(msg) == "T"


def test_title_link_without_a_title_is_ignored():
    """Slack does not render `title_link` without a `title`, so neither do we —
    a URL with nothing naming it is not something the reader saw."""

    msg = {"attachments": [{"text": "body", "title_link": "https://example.com/x"}]}
    assert message_to_text(msg) == "body"


def test_rich_text_block():
    msg = {
        "blocks": [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "OOMKilled on "},
                            {"type": "link", "url": "https://k8s/pod", "text": "pod web-2"},
                        ],
                    }
                ],
            }
        ],
    }
    text = message_to_text(msg)
    assert "OOMKilled on" in text and "pod web-2" in text


def test_empty_message_is_empty_string():
    assert message_to_text({}) == ""


# -- defensive: malformed payloads degrade, they don't crash -------------------
def test_block_text_as_bare_string_does_not_crash():
    # Some bot-relayed messages put a string where a {type,text} object is expected.
    msg = {"blocks": [{"type": "section", "text": "plain string here"}]}
    assert "plain string here" in message_to_text(msg)


def test_malformed_blocks_degrade_to_plain_text():
    msg = {"text": "fallback", "blocks": ["not a dict", {"type": "section"}, 42]}
    assert message_to_text(msg) == "fallback"


# -- shortcut opener reuses run_turn: seed = the message as pasted_content ------
def test_shortcut_core_seeds_case_with_extracted_message():
    # Load _turn.py directly (its package __init__ pulls in slack_bolt).
    spec = importlib.util.spec_from_file_location("_turn", "listeners/_turn.py")
    _turn = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = _turn  # let @dataclass resolve annotations
    spec.loader.exec_module(_turn)

    calls: dict = {}

    class FakeFM:
        def create_case(self, *, title=None, initial_message=None, team_id=None):
            calls["create"] = (title, initial_message)
            return "case_1"

        def submit_turn(self, case_id, **kwargs):
            calls.setdefault("turns", []).append((case_id, kwargs))
            return TurnResult(agent_response="on it")

    class FakeStore:
        def __init__(self):
            self.m = {}

        def get(self, t, c, th):
            return self.m.get((t, c, th))

        def put(self, t, c, th, cid):
            self.m[(t, c, th)] = cid

        def mark_seeded(self, t, c, th):
            pass

        def is_seeded(self, t, c, th):
            return (t, c, th) in self.m

    fm, store = FakeFM(), FakeStore()
    alert = message_to_text(
        {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "disk 98%"}}]}
    )
    # The shortcut sends the alert as pasted_content (this-turn evidence).
    _turn.run_turn(
        fm, store, team_id="T", channel_id="C", thread_ts="msg_ts",
        text="Please investigate this.", pasted_content=alert, source_url="https://slack/p1",
    )
    assert calls["create"] == (None, None)  # no initial_message seed
    case_id, kw = calls["turns"][0]
    assert case_id == "case_1"
    assert kw["query"] == "Please investigate this."
    assert kw["pasted_content"] == "disk 98%"  # the alert seeds as evidence
    assert kw["input_type"] == "paste"
    assert kw["source_url"] == "https://slack/p1"  # provenance back to the alert


def test_pasted_content_is_sent_on_an_existing_case_too():
    # The #3 fix: re-investigating a message whose thread already has a case must
    # still deliver the alert (run_turn used to drop it on existing cases).
    spec = importlib.util.spec_from_file_location("_turn2", "listeners/_turn.py")
    _turn = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = _turn  # let @dataclass resolve annotations
    spec.loader.exec_module(_turn)

    turns: list = []

    class FakeFM:
        def __init__(self):
            self.creates = 0

        def create_case(self, *, title=None, initial_message=None, team_id=None):
            self.creates += 1
            return "case_1"

        def submit_turn(self, case_id, **kwargs):
            turns.append(kwargs)
            return TurnResult(agent_response="ok")

    class FakeStore:
        def __init__(self):
            self.m = {}

        def get(self, t, c, th):
            return self.m.get((t, c, th))

        def put(self, t, c, th, cid):
            self.m[(t, c, th)] = cid

        def mark_seeded(self, t, c, th):
            pass

        def is_seeded(self, t, c, th):
            return (t, c, th) in self.m

    fm, store = FakeFM(), FakeStore()
    kw = dict(team_id="T", channel_id="C", thread_ts="t1")
    _turn.run_turn(fm, store, text="q1", pasted_content="alert A", **kw)  # creates
    _turn.run_turn(fm, store, text="q2", pasted_content="alert B", **kw)  # existing
    assert fm.creates == 1
    assert turns[0]["pasted_content"] == "alert A"
    assert turns[1]["pasted_content"] == "alert B"  # NOT dropped on existing case


def test_run_turn_forwards_files_even_without_text_evidence():
    # A file-only message: no pasted_content, but the downloaded files must still
    # reach submit_turn (the file-ingestion increment).
    spec = importlib.util.spec_from_file_location("_turn3", "listeners/_turn.py")
    _turn = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = _turn  # let @dataclass resolve annotations
    spec.loader.exec_module(_turn)

    turns: list = []

    class FakeFM:
        def create_case(self, *, title=None, initial_message=None, team_id=None):
            return "case_1"

        def submit_turn(self, case_id, **kwargs):
            turns.append(kwargs)
            return TurnResult(agent_response="ok")

    class FakeStore:
        def __init__(self):
            self.m = {}

        def get(self, t, c, th):
            return self.m.get((t, c, th))

        def put(self, t, c, th, cid):
            self.m[(t, c, th)] = cid

        def mark_seeded(self, t, c, th):
            pass

        def is_seeded(self, t, c, th):
            return (t, c, th) in self.m

    files = [("app.log", b"boom", "text/plain")]
    _turn.run_turn(
        FakeFM(), FakeStore(), team_id="T", channel_id="C", thread_ts="t1",
        text="Please investigate this.", pasted_content=None, files=files,
    )
    assert turns[0]["files"] == files
    assert turns[0]["pasted_content"] is None  # no text, files carry the evidence


# -- observation time: when the alert was POSTED, not when it was forwarded ----
def _load_turn(name: str):
    """Load listeners/_turn.py directly (its package __init__ pulls slack_bolt)."""
    spec = importlib.util.spec_from_file_location(name, "listeners/_turn.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # let @dataclass resolve annotations
    spec.loader.exec_module(module)
    return module


class _RecordingFM:
    def __init__(self):
        self.turns: list = []

    def create_case(self, *, title=None, initial_message=None, team_id=None):
        return "case_1"

    def submit_turn(self, case_id, **kwargs):
        self.turns.append(kwargs)
        return TurnResult(agent_response="ok")


class _MemStore:
    def __init__(self):
        self.m = {}

    def get(self, t, c, th):
        return self.m.get((t, c, th))

    def put(self, t, c, th, cid):
        self.m[(t, c, th)] = cid

    def mark_seeded(self, t, c, th):
        pass

    def is_seeded(self, t, c, th):
        return (t, c, th) in self.m


def test_slack_ts_converts_to_an_iso_instant():
    _turn = _load_turn("_turn_ts")
    # 1785872177 == 2026-08-04T19:36:17Z — the alert's post time in this case.
    assert _turn.slack_ts_to_iso("1785872177.123456") == "2026-08-04T19:36:17.123456+00:00"


def test_malformed_slack_ts_yields_no_observation_time():
    """Unknown is honest; a fabricated instant would be worse than none."""
    _turn = _load_turn("_turn_ts_bad")
    assert _turn.slack_ts_to_iso(None) is None
    assert _turn.slack_ts_to_iso("") is None
    assert _turn.slack_ts_to_iso("not-a-ts") is None


def test_run_turn_forwards_observation_time():
    _turn = _load_turn("_turn_obs")
    fm = _RecordingFM()
    _turn.run_turn(
        fm, _MemStore(), team_id="T", channel_id="C", thread_ts="t1",
        text="Please investigate this.",
        pasted_content="[FIRING:1] etcdInsufficientMembers",
        observed_at="2026-08-04T19:36:17+00:00",
    )
    assert fm.turns[0]["observed_at"] == "2026-08-04T19:36:17+00:00"


def test_observation_time_is_withheld_when_a_thread_replay_is_merged_in():
    """``prior_context`` splices many messages from many times into one blob;
    no single instant describes it, so the stamp must be dropped rather than
    mis-applied to the whole thing."""

    _turn = _load_turn("_turn_obs_prior")
    fm = _RecordingFM()
    _turn.run_turn(
        fm, _MemStore(), team_id="T", channel_id="C", thread_ts="t1",
        text="Please investigate this.",
        pasted_content="[FIRING:1] etcdInsufficientMembers",
        observed_at="2026-08-04T19:36:17+00:00",
        prior_context="earlier: we restarted kmaster-2",
    )
    assert fm.turns[0]["observed_at"] is None
    assert "restarted kmaster-2" in fm.turns[0]["pasted_content"]


def test_observation_time_is_not_prepended_to_the_message_text():
    """Transport transparency: the engine must not be able to tell this alert
    came via Slack rather than a Copilot paste, so the timestamp travels as
    structured metadata and NEVER as framing inside the content."""

    _turn = _load_turn("_turn_obs_clean")
    fm = _RecordingFM()
    alert = "[FIRING:1] etcdInsufficientMembers kube-system"
    _turn.run_turn(
        fm, _MemStore(), team_id="T", channel_id="C", thread_ts="t1",
        text="Please investigate this.", pasted_content=alert,
        observed_at="2026-08-04T19:36:17+00:00",
    )
    assert fm.turns[0]["pasted_content"] == alert


# -- the shortcut HANDLER itself: message.ts must reach the turn ---------------
# The helpers above cover run_turn in isolation; this drives the registered
# handler so the wiring from the Slack payload to the turn is covered too.
class _CapturingApp:
    """Stands in for slack_bolt.App: captures the decorated handler."""

    def __init__(self):
        self.handlers: dict = {}

    def shortcut(self, spec):
        def register(fn):
            self.handlers[spec["callback_id"]] = fn
            return fn

        return register


class _Ctx:
    team_id = "T1"
    user_id = "U1"


class _FakeClient:
    token = "xoxb-test"

    def chat_getPermalink(self, *, channel, message_ts):
        return {"permalink": f"https://slack/archives/{channel}/p{message_ts}"}


def _drive_shortcut(monkeypatch, message: dict) -> dict:
    """Invoke the registered shortcut handler and return run_turn_and_post's kwargs."""
    from listeners import shortcuts as sc

    captured: dict = {}
    monkeypatch.setattr(sc, "post_placeholder", lambda *a, **k: "ph_ts")
    monkeypatch.setattr(sc, "run_gated", lambda *a, **k: (k["work"](), True)[1])
    monkeypatch.setattr(
        sc, "run_turn_and_post", lambda *a, **k: captured.update(k)
    )

    app = _CapturingApp()
    sc.register_shortcuts(app, object(), object())
    app.handlers["fm_investigate_message"](
        ack=lambda: None,
        shortcut={
            "trigger_id": "trig1",
            "channel": {"id": "C1"},
            "message": message,
            "response_url": None,
        },
        context=_Ctx(),
        client=_FakeClient(),
        logger=__import__("logging").getLogger("test"),
    )
    return captured


def test_shortcut_sends_the_selected_messages_post_time_as_observed_at(monkeypatch):
    """The defect this fixes: the alert's own post time was read for threading
    and then thrown away, so the backend stamped ingestion time and a two-hour-
    old alert reached the engine looking live."""

    captured = _drive_shortcut(
        monkeypatch,
        {"ts": "1785872177.123456", "text": "[FIRING:1] etcdInsufficientMembers"},
    )
    # Assert the turn ran at all first, so "handler never fired" can't be
    # mistaken for "handler fired but dropped the timestamp".
    assert captured, "the shortcut handler did not reach run_turn_and_post"
    assert captured.get("observed_at") == "2026-08-04T19:36:17.123456+00:00"
    # ...and it is NOT the moment of forwarding.
    assert captured["pasted_content"] == "[FIRING:1] etcdInsufficientMembers"


def test_shortcut_observed_at_is_independent_of_the_thread_root(monkeypatch):
    """A shortcut on a REPLY threads under the parent but must timestamp the
    reply that was actually selected — threading and observation are different
    questions about different messages."""

    captured = _drive_shortcut(
        monkeypatch,
        {
            "ts": "1785872177.000000",
            "thread_ts": "1785000000.000000",  # older parent
            "text": "etcd quorum lost",
        },
    )
    assert captured, "the shortcut handler did not reach run_turn_and_post"
    # The parent's ts would render 2026-07-26T…; reading the wrong field is the
    # most plausible mis-wiring, so pin the selected message's own instant.
    assert captured.get("observed_at") == "2026-08-04T19:36:17+00:00"


def test_out_of_range_slack_ts_does_not_escape():
    """`datetime.fromtimestamp` raises OverflowError for inf/1e20 and OSError on
    some platforms — neither is a ValueError. The only call site runs inside
    work() AFTER the placeholder is posted and outside any try, so an escaping
    exception is not "a bad timestamp is ignored", it is ":mag: Investigating…"
    left in the channel forever with no reply."""

    _turn = _load_turn("_turn_ts_overflow")
    for bad in ("inf", "-inf", "1e20", "nan"):
        assert _turn.slack_ts_to_iso(bad) is None


def test_mention_path_renders_the_wire_form_too(monkeypatch):
    """The rendering fix originally reached only the shortcut path, so the SAME
    alert arrived in two different shapes depending on how it was forwarded."""

    from rendering import clean_mention

    alertmanager = (
        "<@UBOT> <http://am:9093/#/alerts|[FIRING:1] etcdInsufficientMembers> "
        "a &amp; b"
    )
    assert clean_mention(alertmanager) == (
        "[FIRING:1] etcdInsufficientMembers a & b"
    )


def test_bot_mention_is_stripped_before_rendering():
    """Order is load-bearing: rendering first would rewrite `<@UBOT>` to `@UBOT`
    before the mention strip could match it, leaving the bot's own handle in the
    query."""

    from rendering import clean_mention

    assert clean_mention("<@UBOT> why is etcd down?") == "why is etcd down?"
    assert "UBOT" not in clean_mention("<@UBOT> why is etcd down?")
