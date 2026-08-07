"""push_manifest — the reinstall-required signal, error rendering, dry-run safety."""

from __future__ import annotations

import json

import pytest

import scripts.push_manifest as pm
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


# --- dry-run must never reach the mutating call ------------------------------
#
# ``apps.manifest.update`` is a FULL-manifest replace: it resets anything set in
# the App Config UI but absent from the local file (notably the app_directory
# listing fields). ``--diff`` is deliberately NOT a preview — it diffs and then
# pushes — so the safety of ``--dry-run`` is what operators rely on before
# touching a live, Marketplace-listed app.


@pytest.fixture
def _recorded_calls(monkeypatch, tmp_path):
    """Run main() against a stubbed Slack API, recording every method called."""

    calls: list[str] = []

    def fake_call(method: str, token: str, **params: str) -> dict:
        calls.append(method)
        if method == "apps.manifest.export":
            return {"ok": True, "manifest": {"display_information": {"name": "Live"}}}
        return {"ok": True, "app_id": "A123"}

    monkeypatch.setattr(pm, "_call", fake_call)
    monkeypatch.setattr(
        pm, "_ConfigTokens", lambda: type("C", (), {"slack_config_token": "xoxe-x", "slack_app_id": "A123"})()
    )

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"display_information": {"name": "Local"}}), encoding="utf-8")
    return calls, str(path)


def test_dry_run_reads_live_config_but_never_updates(_recorded_calls, monkeypatch):
    calls, path = _recorded_calls
    monkeypatch.setattr("sys.argv", ["push_manifest.py", path, "--dry-run"])

    assert pm.main() == 0
    assert "apps.manifest.export" in calls, "dry-run should read the live config to diff it"
    assert "apps.manifest.update" not in calls, "dry-run MUST NOT mutate the app"


def test_diff_is_not_a_preview_it_updates(_recorded_calls, monkeypatch):
    """Guards the documented contract: --diff shows the diff AND pushes."""

    calls, path = _recorded_calls
    monkeypatch.setattr("sys.argv", ["push_manifest.py", path, "--diff"])

    assert pm.main() == 0
    assert "apps.manifest.export" in calls
    assert "apps.manifest.update" in calls


def test_validate_only_touches_neither_export_nor_update(_recorded_calls, monkeypatch):
    calls, path = _recorded_calls
    monkeypatch.setattr("sys.argv", ["push_manifest.py", path, "--validate"])

    assert pm.main() == 0
    assert calls == ["apps.manifest.validate"]
