"""Preflight doctor — verify the agent's wiring before it connects to Slack.

A live test fails in confusing ways when one credential is wrong or the backend
is down: Bolt connects, then the *first* Slack event 500s deep inside a handler.
This script front-loads those failures into clear, actionable checks so you know
the agent is ready before you click anything.

    python scripts/preflight.py          # read-only checks (safe to run anytime)
    python scripts/preflight.py --full   # also create a case + 1 turn (writes data)

Checks, in order (each independent; we run them all and summarize at the end):

  1. Config loads          — env present + token formats valid (fail-fast rules)
  2. Slack bot token       — auth.test → workspace + bot identity
  3. Slack app token       — apps.connections.open → Socket Mode + connections:write
  4. FaultMaven backend    — /health reachable (degraded is a warning, not a fail)
  5. FaultMaven auth        — a bearer token is obtained AND accepted (/auth/me)
  6. Turn contract (--full)— create a case, submit one turn, render the reply

Exit code is non-zero if any check fails, so it doubles as a CI/start gate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Run from anywhere: put the repo root (this file's parent's parent) on the path
# so `config`/`faultmaven` import whether invoked as `scripts/preflight.py` or
# `python -m scripts.preflight`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slack_sdk import WebClient  # noqa: E402
from slack_sdk.errors import SlackApiError  # noqa: E402

# config/client pull no Slack handlers, so importing them here is cheap.
from app import make_fault_client  # noqa: E402 — shared client factory
from config import Settings, get_settings  # noqa: E402
from faultmaven import FaultMavenClient, FaultMavenError  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[0m",
)


def _ok(msg: str, detail: str = "") -> bool:
    print(f"  {GREEN}✓{RESET} {msg}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    return True


def _fail(msg: str, fix: str) -> bool:
    print(f"  {RED}✗{RESET} {msg}\n    {YELLOW}→ {fix}{RESET}")
    return False


def _warn(msg: str, detail: str = "") -> bool:
    # A non-fatal caveat: the agent can still run, so this does NOT fail the gate.
    print(f"  {YELLOW}!{RESET} {msg}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    return True


def check_config() -> tuple[bool, Settings | None]:
    print("\nConfig")
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 — surface the validation message
        # pydantic-settings raises with all field errors at once. Guard against
        # an empty message (splitlines() on "" is [], which would IndexError).
        lines = str(exc).strip().splitlines()
        first = lines[0] if lines else type(exc).__name__
        _fail(
            f"settings failed to load: {first}",
            "copy .env.example to .env and fill in the required values "
            "(see the validation error above).",
        )
        return False, None
    _ok("settings loaded", f"backend={settings.faultmaven_api_url}")
    # Per-transport credentials are already enforced by Settings validation (a
    # missing one would have raised above), so here we just report the mode.
    _ok(f"transport = {settings.slack_transport}")
    return True, settings


def check_slack_bot(settings: Settings) -> bool:
    print("\nSlack bot token")
    try:
        resp = WebClient(token=settings.slack_bot_token).auth_test()
    except SlackApiError as exc:
        return _fail(
            f"auth.test rejected the bot token: {exc.response.get('error')}",
            "re-copy SLACK_BOT_TOKEN from 'OAuth & Permissions' (it must be "
            "the bot token, xoxb-…) and reinstall the app if scopes changed.",
        )
    except Exception as exc:  # noqa: BLE001 — network, DNS, etc.
        return _fail(f"could not reach Slack: {exc}", "check your network/proxy.")
    return _ok(
        "auth.test passed",
        f"team={resp.get('team')} bot={resp.get('user')} ({resp.get('bot_id')})",
    )


def check_slack_app(settings: Settings) -> bool:
    print("\nSlack app token")
    if not settings.slack_app_token.startswith("xapp-"):
        return _fail(
            "SLACK_APP_TOKEN is not an app-level token",
            "it must start with 'xapp-' (not the bot token).",
        )
    try:
        # Opens (but does not hold) a Socket Mode WSS URL — proves the token is
        # valid and carries connections:write.
        WebClient().apps_connections_open(app_token=settings.slack_app_token)
    except SlackApiError as exc:
        error = exc.response.get("error")
        # apps.connections.open is Tier-1 rate-limited (~1/min); a rate-limit on
        # a repeated run doesn't mean the token is bad, so don't fail the gate.
        if error == "ratelimited":
            return _warn(
                "apps.connections.open was rate-limited — token not verified",
                "rerun in ~1 min if you need to confirm it",
            )
        return _fail(
            f"apps.connections.open failed: {error}",
            "regenerate the app-level token with the connections:write scope "
            "and confirm Socket Mode is enabled in the app manifest.",
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(f"could not reach Slack: {exc}", "check your network/proxy.")
    return _ok("apps.connections.open passed", "Socket Mode reachable")


def check_oauth_db(settings: Settings) -> bool:
    print("\nOAuth store (HTTP transport)")
    try:
        # Building the stores opens the engine and creates the tables, so this
        # proves SLACK_DATABASE_URL is reachable AND the DB driver is installed
        # (a missing psycopg2 for a postgresql:// URL fails right here, not at
        # the first install in production).
        from oauth_store import build_oauth_stores

        stores = build_oauth_stores(
            database_url=settings.slack_database_url,
            client_id=settings.slack_client_id,
        )
        stores.engine.dispose()
    except Exception as exc:  # noqa: BLE001 — driver missing / DB unreachable
        return _fail(
            f"could not open the OAuth store: {exc}",
            "check SLACK_DATABASE_URL is reachable and its driver is installed "
            "(postgresql:// needs psycopg2, in requirements.txt).",
        )
    return _ok("OAuth store reachable", "installation + state tables ready")


def check_backend(fm: FaultMavenClient) -> bool:
    print("\nFaultMaven backend")
    try:
        health = fm.health()
    except FaultMavenError as exc:
        return _fail(
            f"backend unreachable: {exc}",
            "start the FaultMaven API (default :8090) or fix FAULTMAVEN_API_URL.",
        )
    status = health.get("status", "unknown")
    if status == "healthy":
        return _ok("/health is healthy")
    if status == "degraded":
        # The case/turn API still serves in 'degraded' (e.g. a non-critical
        # component warming up, ALLOW_TOOLLESS_INVESTIGATION, RLS-owner-role).
        # Warn so the operator notices, but don't block the live test.
        return _warn(
            "backend reports status='degraded'",
            "serviceable, but check GET /health for which component is down",
        )
    # 'unhealthy' / 'unknown' (missing field) → treat as a real failure.
    return _fail(
        f"backend reports status={status!r}",
        "the backend is not serviceable; check the API logs / GET /health.",
    )


def check_backend_auth(fm: FaultMavenClient, settings: Settings) -> bool:
    print("\nFaultMaven auth")
    try:
        # verify_auth() obtains a token (dev-login or preset) AND confirms the
        # backend accepts it — so a stale/wrong preset token fails here rather
        # than 401-ing on the first real Slack turn.
        fm.verify_auth()
    except FaultMavenError as exc:
        if "401" in str(exc):
            return _fail(
                f"token rejected: {exc}",
                "the bearer token is invalid — re-issue FAULTMAVEN_API_TOKEN, "
                "or use a local-AUTH_MODE backend so dev-login works.",
            )
        if "inconclusive" in str(exc):
            # e.g. /auth/me absent on an older backend — token may still be fine.
            return _warn(f"could not confirm the token: {exc}")
        return _fail(
            f"could not obtain a bearer token: {exc}",
            "set FAULTMAVEN_API_TOKEN for this backend, or run a backend in "
            "local AUTH_MODE so dev-login works.",
        )
    how = "preset token" if settings.faultmaven_api_token else "dev-login bootstrap"
    return _ok("bearer token accepted", f"via {how}")


def check_turn_contract(fm: FaultMavenClient) -> bool:
    print("\nTurn contract (--full)")
    try:
        case_id = fm.create_case(title="preflight smoke test")
        result = fm.submit_turn(case_id, query="Preflight ping — please ack.")
    except FaultMavenError as exc:
        return _fail(
            f"case/turn round-trip failed: {exc}",
            "the agent's core contract is broken against this backend; check "
            "the API logs and the version of faultmaven/modules/case.",
        )
    snippet = (result.agent_response or "").strip().replace("\n", " ")[:60]
    ok = _ok(
        f"created {case_id} + 1 turn",
        f"state={result.case_state} reply={snippet!r}…",
    )
    # This case persists in the backend — there's no delete in the contract yet.
    print(f"    {DIM}note: '{case_id}' is left in the backend (titled "
          f"'preflight smoke test'){RESET}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="FaultMaven Slack agent preflight")
    parser.add_argument(
        "--full",
        action="store_true",
        help="also create a throwaway case and submit one turn (writes data)",
    )
    args = parser.parse_args()

    print(f"{DIM}FaultMaven Slack Agent — preflight{RESET}")
    results: list[bool] = []

    config_ok, settings = check_config()
    results.append(config_ok)
    if settings is None:
        return _summarize(results)

    # Slack checks don't need the backend, and vice-versa — run all, report all.
    # The Slack-side checks differ by transport: Socket Mode verifies the static
    # bot + app tokens; HTTP verifies the OAuth store (no static bot token exists).
    if settings.slack_transport == "socket":
        results.append(check_slack_bot(settings))
        results.append(check_slack_app(settings))
    else:  # http
        results.append(check_oauth_db(settings))

    fm = make_fault_client(settings)
    try:
        results.append(check_backend(fm))
        results.append(check_backend_auth(fm, settings))
        if args.full:
            results.append(check_turn_contract(fm))
    finally:
        fm.close()

    return _summarize(results)


def _summarize(results: list[bool]) -> int:
    passed, total = sum(results), len(results)
    print()
    if all(results):
        print(f"{GREEN}All {total} checks passed — ready to `python app.py`.{RESET}")
        return 0
    print(f"{RED}{passed}/{total} checks passed — fix the ✗ above before testing.{RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
