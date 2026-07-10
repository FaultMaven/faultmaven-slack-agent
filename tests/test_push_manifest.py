"""push_manifest — the reinstall-required signal + error rendering."""

from __future__ import annotations

from scripts.push_manifest import _fmt_errors, reinstall_required


def test_reinstall_required_true_when_permissions_updated():
    # Slack sets permissions_updated when the update changed OAuth scopes.
    assert reinstall_required({"ok": True, "permissions_updated": True}) is True


def test_reinstall_not_required_without_permission_change():
    assert reinstall_required({"ok": True, "permissions_updated": False}) is False
    assert reinstall_required({"ok": True}) is False  # field absent → no reinstall


def test_fmt_errors_renders_structured_validation_errors():
    msg = _fmt_errors(
        {
            "ok": False,
            "errors": [
                {"message": "invalid scope", "pointer": "/oauth_config/scopes/bot/0"}
            ],
        }
    )
    assert "invalid scope" in msg
    assert "/oauth_config/scopes/bot/0" in msg


def test_fmt_errors_falls_back_to_bare_error():
    assert "token_expired" in _fmt_errors({"ok": False, "error": "token_expired"})
