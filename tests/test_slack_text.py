"""Transport transparency: what reaches the engine is what the user sees.

A Slack-forwarded alert and the same alert pasted into the Copilot must arrive
at the backend as identical text. Slack's wire form is not identical — it wraps
links/mentions in entity markup and escapes ``&`` ``<`` ``>`` — so
``message_to_text`` has to undo exactly that encoding, and nothing else.
"""

from __future__ import annotations

from slack_text import message_to_text


# -- the regression this module exists for -----------------------------------
# Alertmanager's Slack receiver makes the whole title a link, which puts the
# firing/resolved marker INSIDE the entity label. Left as markup, the one field
# that says whether the alert is live never reaches the engine in readable form.
_ALERTMANAGER_TITLE = (
    "<http://faultmaven-monitoring-alertmanager.monitoring:9093/#/alerts"
    "?receiver=critical-receiver|[FIRING:1] etcdInsufficientMembers kube-system "
    "(onprem http-metrics kube-etcd critical)>"
)
_ALERTMANAGER_RENDERED = (
    "[FIRING:1] etcdInsufficientMembers kube-system "
    "(onprem http-metrics kube-etcd critical)"
)


def test_alertmanager_link_title_renders_as_displayed_text():
    msg = {"attachments": [{"title": _ALERTMANAGER_TITLE}]}
    assert message_to_text(msg) == _ALERTMANAGER_RENDERED


def test_resolved_marker_survives_too():
    msg = {"text": "<http://am:9093/#/alerts|[RESOLVED] etcdInsufficientMembers>"}
    assert message_to_text(msg) == "[RESOLVED] etcdInsufficientMembers"


def test_same_alert_renders_identically_across_block_shapes():
    """Senders choose blocks, attachments, or plain text; the engine must not
    be able to tell which — otherwise identical alerts investigate differently."""

    via_attachment = message_to_text({"attachments": [{"title": _ALERTMANAGER_TITLE}]})
    via_section = message_to_text(
        {
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": _ALERTMANAGER_TITLE}}
            ]
        }
    )
    via_text = message_to_text({"text": _ALERTMANAGER_TITLE})
    via_rich_text = message_to_text(
        {
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [
                                {
                                    "type": "link",
                                    "url": "http://am:9093/#/alerts",
                                    "text": _ALERTMANAGER_RENDERED,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )
    assert (
        via_attachment == via_section == via_text == via_rich_text
        == _ALERTMANAGER_RENDERED
    )


# -- entity forms -------------------------------------------------------------
def test_unlabelled_link_keeps_the_url():
    assert message_to_text({"text": "see <https://grafana/d/abc>"}) == (
        "see https://grafana/d/abc"
    )


def test_user_and_channel_refs():
    assert message_to_text({"text": "<@U123|alice> in <#C456|incidents>"}) == (
        "@alice in #incidents"
    )


def test_unlabelled_refs_keep_the_raw_id_rather_than_guessing():
    assert message_to_text({"text": "<@U123> in <#C456>"}) == "@U123 in #C456"


def test_broadcasts_and_subteams():
    assert message_to_text({"text": "<!here> <!subteam^S1|@oncall>"}) == (
        "@here @oncall"
    )


# -- escaping -----------------------------------------------------------------
def test_escapes_are_undone():
    msg = {"text": "curl 'a&amp;b' &lt;stdin&gt;"}
    assert message_to_text(msg) == "curl 'a&b' <stdin>"


def test_escaped_angle_brackets_are_not_mistaken_for_an_entity():
    """A log line reading ``<stdin>`` arrives as ``&lt;stdin&gt;``. If escapes
    were undone BEFORE entities were unwrapped, it would be eaten as markup."""

    assert message_to_text({"text": "read error on &lt;stdin&gt;"}) == (
        "read error on <stdin>"
    )


def test_literal_ampersand_entity_is_not_double_unescaped():
    """The user typed ``&lt;`` and wants it back verbatim; Slack sends
    ``&amp;lt;``. Unescaping ``&amp;`` first would collapse it to ``<``."""

    assert message_to_text({"text": "&amp;lt;"}) == "&lt;"


def test_plain_text_composition_is_not_unescaped():
    """``plain_text`` is literal by contract — an ``&amp;`` there is the
    intended characters, not an encoding of ``&``."""

    msg = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "a &amp; b"}}
        ]
    }
    assert message_to_text(msg) == "a &amp; b"


def test_rich_text_content_is_not_unescaped():
    """rich_text carries literal characters already; rendering it would corrupt
    a genuine ``&amp;`` in a pasted log."""

    msg = {
        "blocks": [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": "grep 'a&amp;b'"}],
                    }
                ],
            }
        ]
    }
    assert message_to_text(msg) == "grep 'a&amp;b'"


# -- no framing added ---------------------------------------------------------
def test_nothing_is_added_to_the_message():
    """The engine must not be able to tell Slack from a Copilot paste, so the
    renderer only ever REMOVES transport encoding."""

    plain = "etcd quorum lost on kmaster-2 at 19:36 UTC"
    assert message_to_text({"text": plain}) == plain


def test_degraded_path_still_renders():
    """A payload that blows up mid-parse falls back to ``text`` — still Slack
    wire form, so it needs rendering too."""

    class Exploding(dict):
        def get(self, key, default=None):
            if key == "blocks":
                raise RuntimeError("malformed payload")
            return super().get(key, default)

    msg = Exploding(text=_ALERTMANAGER_TITLE)
    assert message_to_text(msg) == _ALERTMANAGER_RENDERED


# -- review findings ----------------------------------------------------------
def test_plain_text_is_left_entirely_alone():
    """``plain_text`` is LITERAL by Slack's contract. Entity-unwrapping it
    stripped the brackets from ordinary strings — Go's ``<no value>``, ``<nil>``,
    a shell's ``<stdin>`` — and broke the cross-shape invariant besides."""

    msg = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Parse error at <no value> on <stdin>",
                },
            }
        ]
    }
    assert message_to_text(msg) == "Parse error at <no value> on <stdin>"


def test_the_cross_shape_invariant_holds_for_angle_brackets():
    """mrkdwn ``&lt;stdin&gt;`` and plain_text ``<stdin>`` are the same content
    on screen, so they must reach the engine identically."""

    as_mrkdwn = message_to_text(
        {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "on &lt;stdin&gt;"}}]}
    )
    as_plain = message_to_text(
        {"blocks": [{"type": "header", "text": {"type": "plain_text", "text": "on <stdin>"}}]}
    )
    assert as_mrkdwn == as_plain == "on <stdin>"


def test_an_entity_never_spans_a_line_break():
    """Slack's parser does not span lines. Allowing it turned an unrelated ``<``
    and a later ``>`` into a pseudo-entity; with a ``|`` between them everything
    before it — INCLUDING the newline — was deleted, merging two log lines and
    dropping the text between. Data loss, not a rendering nit."""

    raw = "ERROR at index <5 in pool\nWARN retry | giving up>\nnext line"
    assert message_to_text({"text": raw}) == raw


def test_a_multiline_paste_keeps_every_line():
    raw = "panic: index out of range <5>\nwith length 3\n| col | val |\ntail"
    out = message_to_text({"text": raw})
    assert out.count("\n") == raw.count("\n")
    assert "with length 3" in out and "tail" in out
