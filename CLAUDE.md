# FaultMaven Slack Agent

A Slack app (Bolt for Python) that turns a Slack thread into a FaultMaven case:
each message becomes a turn against the FaultMaven core API, and the result is
rendered back as Block Kit. Top-level modules, no installed package; Python 3.12.

## Run, test, lint

```bash
pip install -r requirements-dev.txt        # runtime + pytest, ruff, datamodel-code-generator
python -m pytest -q                        # CI "Test (pytest)"; the root conftest.py puts the repo on sys.path
ruff check .                               # CI "Lint (ruff)"; rule set pinned in ruff.toml
python scripts/generate_api_models.py      # CI "API Contract Drift Check" — must leave no diff
python scripts/preflight.py [--full]       # checks env, Slack tokens, backend; --full writes a case
python app.py                              # SLACK_TRANSPORT=socket (default) or http
```

CI (`.github/workflows/ci.yml`) runs lint, contract drift, and test, then a
no-push Docker build. `publish-docker.yml` publishes to GHCR only after CI
succeeds on `main`. A PR is done when those check conclusions are green, not
when local tests pass.

## Layout

| Path | Role |
|---|---|
| `app.py` | Builds the Bolt `App`; runs the Socket Mode loop, or hands off to `web.run_http` |
| `web.py` | FastAPI host for HTTP transport: `/slack/events`, `/slack/install`, `/slack/oauth_redirect`, `/health` |
| `config.py` | `Settings` (pydantic-settings, reads `.env`) and per-transport fail-fast validation; `DEFAULT_BOT_SCOPES` |
| `listeners/` | `assistant` (side panel), `events` (mention + thread replies), `shortcuts`, `actions` (buttons), `home`, `lifecycle` (uninstall) |
| `listeners/_turn.py` | Shared pipeline: find-or-create case → turn → post; per-thread busy gate |
| `faultmaven/client.py` | Hand-written sync `httpx` client for the core API and its credential handling |
| `faultmaven/api_generated.py` | Generated from the pinned OpenAPI contract — never hand-edit |
| `store.py` | SQLite thread→case map (`thread_cases`), with tombstones for vanished cases |
| `credentials.py` | SQLite store for the rotated process-wide refresh token (`fm_credential`) |
| `oauth_store.py` | Builds the SQLAlchemy stores on `SLACK_DATABASE_URL` (Slack installations, OAuth state) |
| `workspace_credentials.py` | `fm_workspace_credentials`: Slack `team_id` → FaultMaven service-account credential |
| `binding.py`, `pending_binds.py`, `install_pages.py` | Install-time workspace binding (Slack OAuth chained to FaultMaven consent) |
| `rendering.py`, `slack_mrkdwn.py`, `slack_text.py` | TurnResult → Block Kit; Markdown → mrkdwn; Slack message → plain text |
| `slack_files.py` | Downloads attached files (bot-token auth) to forward as multipart evidence |
| `scripts/` | `preflight.py`, `push_manifest.py`, `generate_api_models.py` |
| `manifest.json` / `manifest.dev.json` | Hosted (HTTP/OAuth) / local-dev (Socket Mode) Slack app manifests |

## Talking to Slack

- Two transports, one set of listeners. Listeners use Bolt's per-request
  `client` and `context.team_id`, never a captured global token.
- `socket` needs `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`. `http` needs
  `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_SIGNING_SECRET`,
  `SLACK_DATABASE_URL`; there is no SQLite default for it. `Settings` refuses to
  boot when any are missing.
- The bot acts only on threads the `store` already maps. A thread runs one turn
  at a time; messages that arrive mid-turn are skipped and marked ⏭️, not queued.

## Talking to the FaultMaven core API

- Endpoints used: `POST /api/v1/cases`, `POST /api/v1/cases/{case_id}/turns`
  (multipart), `/api/v1/auth/{oauth/token,oauth/revoke,dev-login,me}`,
  `/api/v1/admin/integrations/slack/workspaces`, and `/health`.
- Credential precedence: `FAULTMAVEN_REFRESH_TOKEN` (refresh grant) →
  `FAULTMAVEN_API_TOKEN` (static bearer) → `FAULTMAVEN_DEV_LOGIN_USERNAME`
  (local auth mode only). Auth is lazy; the agent boots with the backend down.
- Under `SLACK_TRANSPORT=http`, a workspace bound in `fm_workspace_credentials`
  acts as its own service account; an unbound workspace falls back to the
  process-wide credential unless `FAULTMAVEN_REQUIRE_WORKSPACE_BINDING=true`
  refuses it. That setting fails boot under Socket Mode, which has no bindings.
- The Slack `thread_ts` is never sent as a session id; `store.py` owns the
  thread→case mapping.
- The contract is pinned in `api-contract.pin.json` (`ref` + `contractVersion`,
  which must agree). Adopting a new contract = move the pin and commit the
  regenerated `faultmaven/api_generated.py` in the same PR.

## Hard rules

- **Refresh tokens rotate.** Each renewal revokes the old token, so the new one
  is committed (`credentials.py` / `fm_workspace_credentials`) before use.
  `CREDENTIAL_STORE_PATH` must be durable: losing it locks the agent out until an
  operator re-provisions. `SLACK_DATABASE_URL` holds every workspace's bot token
  and binding, so it must be durable too.
- **Tenant check.** `FaultMavenClient._assert_expected_enterprise` rejects a
  minted token whose enterprise claim differs from the one recorded at bind.
  In the bind tables, `enterprise_id` is Slack's Enterprise Grid id and the
  FaultMaven tenant is `fm_enterprise_id`. Never shorten one to the other.
- **Schemas without a migration tool.** Tables are created on startup
  (`CREATE TABLE IF NOT EXISTS` / SQLAlchemy `create_all`). `store.py` adds new
  `thread_cases` columns with guarded `ALTER TABLE`; add the same for any new
  column there. `fm_workspace_credentials` has no migration by design: a shape
  change means dropping the table and re-binding.
- **Relative store paths** (`CASE_STORE_PATH`, `CREDENTIAL_STORE_PATH`) resolve
  against the repo directory, not the cwd.
- **Scopes stay in lockstep.** `config.DEFAULT_BOT_SCOPES` must match
  `manifest.json` bot scopes, apart from `MANIFEST_ONLY_BOT_SCOPES`
  (`tests/test_config.py` enforces this).
- **Untrusted text is escaped.** Engine output and backend error text are
  external input: `rendering.py` / `slack_mrkdwn.py` neutralize `<!channel>`-style
  broadcasts and raw link entities, and Block Kit chunking respects Slack's
  section limits (`tests/test_hardening.py`).
- **Sanitized filenames.** Slack-supplied filenames pass through
  `slack_files._safe_name` (basename only, no control chars, ≤255 chars, never
  `.`/`..`). Downloads are streamed with a byte cap (`MAX_FILE_BYTES`) and a
  count cap (`MAX_FILES`). A Slack sign-in page served in place of the file is
  treated as a failed download.
- **Install pages** (`install_pages.py`) never interpolate request input; only
  server-established values, HTML-escaped.
- **Manifest pushes are full replaces.** `scripts/push_manifest.py` mutates the
  live app only with `--apply`, and uses `SLACK_CONFIG_TOKEN` + `SLACK_APP_ID`
  (ops-only; not agent settings).
- Never commit `.env`; `.env.example` is the template for every setting.

## Docs

- `docs/design.md`: architecture, backend contract (§8), auth and multi-tenancy (§10)
- `docs/HOSTING.md`: hosted HTTP/OAuth deployment, service-account credentials
- `docs/LIVE_TEST.md`: install and smoke test in a real workspace
- `docs/USER_GUIDE.md`: end-user behavior; `PRIVACY.md`: privacy policy pointer
