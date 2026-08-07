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


class _NoNetwork:
    """Any real HTTP call is a test failure, not a mock miss.

    The trace assertions below watch the ``_call`` seam. That alone would go
    vacuous the moment someone reaches Slack without routing through ``_call``
    (an inlined ``httpx.post``, say) — the recorded trace would stay clean while
    the app was replaced for real. This is the hard floor under that.
    """

    def post(self, *a, **k):  # noqa: D102
        raise AssertionError("push_manifest attempted a real HTTP request")


@pytest.fixture
def _recorded_calls(monkeypatch, tmp_path):
    """Run main() against a stubbed Slack API, recording every method called."""

    calls: list[str] = []
    export_ok = {"value": True}

    def fake_call(method: str, token: str, **params: str) -> dict:
        calls.append(method)
        if method == "apps.manifest.export":
            if not export_ok["value"]:
                return {"ok": False, "error": "app_not_found"}
            return {"ok": True, "manifest": {"display_information": {"name": "Live"}}}
        return {"ok": True, "app_id": "A123"}

    monkeypatch.setattr(pm, "_call", fake_call)
    monkeypatch.setattr(pm, "httpx", _NoNetwork())
    monkeypatch.setattr(
        pm,
        "_ConfigTokens",
        lambda: type("C", (), {"slack_config_token": "xoxe-x", "slack_app_id": "A123"})(),
    )

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"display_information": {"name": "Local"}}), encoding="utf-8")
    return calls, str(path), export_ok


def test_default_invocation_previews_and_never_updates(_recorded_calls, monkeypatch):
    """No flag at all must be safe — the mutating path needs --apply."""

    calls, path, _ = _recorded_calls
    monkeypatch.setattr("sys.argv", ["push_manifest.py", path])

    assert pm.main() == 0
    assert "apps.manifest.export" in calls, "the preview must read the live config"
    assert "apps.manifest.update" not in calls, "default MUST NOT mutate the app"


def test_dry_run_reads_live_config_but_never_updates(_recorded_calls, monkeypatch):
    calls, path, _ = _recorded_calls
    monkeypatch.setattr("sys.argv", ["push_manifest.py", path, "--dry-run"])

    assert pm.main() == 0
    assert "apps.manifest.export" in calls
    assert "apps.manifest.update" not in calls, "dry-run MUST NOT mutate the app"


def test_deprecated_diff_no_longer_pushes(_recorded_calls, monkeypatch):
    """--diff used to diff-then-push, which read as a preview and was not one."""

    calls, path, _ = _recorded_calls
    monkeypatch.setattr("sys.argv", ["push_manifest.py", path, "--diff"])

    assert pm.main() == 0
    assert "apps.manifest.update" not in calls, "--diff MUST NOT mutate any more"


def test_apply_is_the_only_form_that_updates(_recorded_calls, monkeypatch):
    calls, path, _ = _recorded_calls
    monkeypatch.setattr("sys.argv", ["push_manifest.py", path, "--apply"])

    assert pm.main() == 0
    assert "apps.manifest.export" in calls, "--apply must still show the diff first"
    assert "apps.manifest.update" in calls


def test_apply_aborts_when_the_live_config_cannot_be_read(_recorded_calls, monkeypatch):
    """A failed export means nothing was compared — that must block the replace."""

    calls, path, export_ok = _recorded_calls
    export_ok["value"] = False
    monkeypatch.setattr("sys.argv", ["push_manifest.py", path, "--apply"])

    assert pm.main() == 5, "unreadable live config must fail closed, not push blind"
    assert "apps.manifest.update" not in calls


def test_preview_also_fails_closed_on_export_error(_recorded_calls, monkeypatch):
    """A preview that inspected nothing must not report success."""

    calls, path, export_ok = _recorded_calls
    export_ok["value"] = False
    monkeypatch.setattr("sys.argv", ["push_manifest.py", path])

    assert pm.main() == 5
    assert "apps.manifest.update" not in calls


def test_validate_only_touches_neither_export_nor_update(_recorded_calls, monkeypatch):
    calls, path, _ = _recorded_calls
    monkeypatch.setattr("sys.argv", ["push_manifest.py", path, "--validate"])

    assert pm.main() == 0
    assert calls == ["apps.manifest.validate"]


def test_env_is_read_from_the_repo_not_the_cwd(tmp_path, monkeypatch):
    """A stray .env in the cwd must not redirect the push at another app.

    pydantic-settings resolves a relative env_file against the working
    directory, so before this was anchored, running the script from a directory
    holding its own .env loaded THAT app's SLACK_APP_ID — and a full-manifest
    replace would land on the wrong Slack app.
    """

    (tmp_path / ".env").write_text(
        "SLACK_CONFIG_TOKEN=xoxe-WRONG\nSLACK_APP_ID=A_WRONG_APP\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    cfg = pm._ConfigTokens()
    assert cfg.slack_app_id != "A_WRONG_APP", "cwd .env must not select the target app"
    assert cfg.slack_config_token != "xoxe-WRONG"
