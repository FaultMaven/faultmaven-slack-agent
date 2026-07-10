"""Push the local manifest.json to the Slack app config — no copy-paste.

Slack exposes manifest CRUD over its Web API (``apps.manifest.validate`` /
``apps.manifest.update``), so a manifest edit can be applied programmatically
instead of pasting it into the App Manifest tab and clicking Save. This validates
first, then updates, and tells you whether the change ALSO needs a workspace
reinstall (only OAuth-scope changes do — content and event changes don't).

    python scripts/push_manifest.py             # validate + update from manifest.json
    python scripts/push_manifest.py --validate  # validate only (makes no change)
    python scripts/push_manifest.py --diff      # show live-vs-local before updating
    python scripts/push_manifest.py path/to.json

Needs an APP CONFIGURATION TOKEN — separate from the bot/app tokens — in ``.env``:

    SLACK_CONFIG_TOKEN=xoxe.xoxp-...   # api.slack.com/apps → "Your app configuration tokens"
    SLACK_APP_ID=A0XXXXXXX             # the app's ID (Basic Information / App Manifest)

Config tokens expire after ~12 h; when you get a token error, regenerate (or
rotate) them at the same page. This script reads ONLY the config token — never
your bot/app tokens.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

# Run from anywhere: put the repo root on the path so `.env` resolves the same
# way the runtime does.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from pydantic import Field  # noqa: E402
from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402

_API = "https://slack.com/api"
# Auth-shaped errors mean the config token is bad/expired, not the manifest.
_AUTH_ERRORS = {"token_expired", "invalid_auth", "not_authed", "token_revoked"}


class _ConfigTokens(BaseSettings):
    """Ops-only config-token settings (read from .env, like the runtime)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    slack_config_token: str = Field(default="", validation_alias="SLACK_CONFIG_TOKEN")
    slack_app_id: str = Field(default="", validation_alias="SLACK_APP_ID")


def _call(method: str, token: str, **params: str) -> dict:
    """POST a Slack Web API method with the config token; return parsed JSON."""

    resp = httpx.post(
        f"{_API}/{method}", data={"token": token, **params}, timeout=30.0
    )
    resp.raise_for_status()
    return resp.json()


def reinstall_required(update_response: dict) -> bool:
    """True when the update changed OAuth permissions — the installed app must be
    reinstalled to the workspace for the new scopes to take effect. Content and
    event-subscription changes do not set this."""

    return bool(update_response.get("permissions_updated"))


def _fmt_errors(payload: dict) -> str:
    """Render Slack's structured manifest errors (or a bare error) as a list."""

    errors = payload.get("errors") or [{"message": payload.get("error", "unknown error")}]
    lines = []
    for e in errors:
        msg = e.get("message", e) if isinstance(e, dict) else e
        pointer = e.get("pointer") if isinstance(e, dict) else None
        lines.append(f"  - {msg}" + (f"  (at {pointer})" if pointer else ""))
    return "\n".join(lines)


def _print_diff(token: str, app_id: str, local_manifest: str) -> None:
    """Show a unified diff of the live app config vs the local manifest."""

    exported = _call("apps.manifest.export", token, app_id=app_id)
    if not exported.get("ok"):
        print(f"  (couldn't export live manifest for diff: {exported.get('error')})")
        return
    live = json.dumps(exported["manifest"], indent=2, sort_keys=True).splitlines()
    local = json.dumps(json.loads(local_manifest), indent=2, sort_keys=True).splitlines()
    diff = list(
        difflib.unified_diff(live, local, "slack (live)", "local manifest.json", lineterm="")
    )
    print("\n".join(diff) if diff else "  (local manifest matches the live config)")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "manifest", nargs="?", default="manifest.json", help="manifest path (default: manifest.json)"
    )
    parser.add_argument("--validate", action="store_true", help="validate only; make no change")
    parser.add_argument("--diff", action="store_true", help="show live-vs-local before updating")
    args = parser.parse_args()

    cfg = _ConfigTokens()
    if not cfg.slack_config_token:
        print(
            "✗ SLACK_CONFIG_TOKEN is not set. Generate an app configuration token at\n"
            "  api.slack.com/apps → 'Your app configuration tokens', then add it to .env."
        )
        return 2
    if not args.validate and not cfg.slack_app_id:
        print("✗ SLACK_APP_ID is not set (needed to update). It's on the app's Basic Information page.")
        return 2

    manifest_path = Path(args.manifest)
    try:
        manifest_str = manifest_path.read_text(encoding="utf-8")
        json.loads(manifest_str)  # local JSON sanity before hitting the API
    except (OSError, json.JSONDecodeError) as exc:
        print(f"✗ Could not read valid JSON from {manifest_path}: {exc}")
        return 2

    token = cfg.slack_config_token
    app_params = {"app_id": cfg.slack_app_id} if cfg.slack_app_id else {}

    try:
        validated = _call("apps.manifest.validate", token, manifest=manifest_str, **app_params)
        if not validated.get("ok"):
            if validated.get("error") in _AUTH_ERRORS:
                print(
                    f"✗ Config token rejected ({validated.get('error')}). These expire ~12 h —\n"
                    "  regenerate at api.slack.com/apps → 'Your app configuration tokens'."
                )
                return 3
            print("✗ Manifest failed validation:\n" + _fmt_errors(validated))
            return 1
        print(f"✓ {manifest_path} is a valid manifest.")

        if args.validate:
            return 0

        if args.diff:
            _print_diff(token, cfg.slack_app_id, manifest_str)

        updated = _call("apps.manifest.update", token, app_id=cfg.slack_app_id, manifest=manifest_str)
        if not updated.get("ok"):
            print("✗ Update failed:\n" + _fmt_errors(updated))
            return 1
    except httpx.HTTPError as exc:
        print(f"✗ Slack API request failed: {exc}")
        return 4

    print(f"✓ Updated app {updated.get('app_id', cfg.slack_app_id)} from {manifest_path}.")
    if reinstall_required(updated):
        print(
            "⚠  OAuth scopes changed — REINSTALL the app to your workspace for them to take\n"
            "   effect (api.slack.com/apps → your app → Install App → Reinstall)."
        )
    else:
        print(
            "  No scope change → no reinstall needed. Socket Mode picks up event changes on\n"
            "  reconnect; restart the agent to load any code paired with this manifest change."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
