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

- Run from the repo root: `.env` is read from the cwd, while relative
  `CASE_STORE_PATH` / `CREDENTIAL_STORE_PATH` resolve against the repo.
- `generate_api_models.py` needs `datamodel-codegen` and `ruff` on `PATH` and
  network access to fetch the pinned spec.
- **Preflight rotates credentials** under the refresh grant: the default run
  renews the default one, `--full` every bound workspace's. Never run it against
  a shared `SLACK_DATABASE_URL` or a copy of a running agent's credential store.

CI (`ci.yml`) runs lint, contract drift and test; the no-push Docker build
`needs: [lint, test]` only. `publish-docker.yml` publishes to GHCR after CI
passes on `main`, and on a `v*.*.*` tag or manual dispatch without CI. A PR is
done when its check conclusions are green, not when local tests pass.

## Layout

| Path | Role |
|---|---|
| `app.py` | Builds the Bolt `App`; runs the Socket Mode loop, or hands off to `web.run_http` |
| `web.py` | FastAPI host for HTTP transport: `/slack/events`, `/slack/install`, `/slack/oauth_redirect`, `/faultmaven/callback` (bind return leg), `/health` |
| `config.py` | `Settings` (pydantic-settings, reads `.env`) and per-transport fail-fast validation; `DEFAULT_BOT_SCOPES` |
| `listeners/` | `assistant` (side panel), `events` (mention, thread replies, first plain DM), `shortcuts`, `actions` (buttons), `home`, `lifecycle` (`app_uninstalled`, `tokens_revoked`) |
| `listeners/_turn.py` | Shared pipeline: find-or-create case → turn → post; per-thread busy gate |
| `faultmaven/client.py` | Hand-written sync `httpx` client for the core API and its credential handling |
| `faultmaven/api_generated.py` | Generated from the pinned OpenAPI contract — never hand-edit |
| `store.py` | SQLite thread→case map (`thread_cases`), with tombstones for vanished cases |
| `credentials.py` | SQLite store for the rotated process-wide refresh token (`fm_credential`) |
| `oauth_store.py` | Builds every store on `SLACK_DATABASE_URL`: Slack installations, OAuth state, workspace credentials, pending binds |
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
- A summons (@mention, the Ask shortcut, the assistant panel, a first plain DM
  per `events.is_dm_summons`) opens a case in an unmapped thread. A plain reply
  continues a thread only if the `store` maps it or `events.recover_lost_case`
  re-links it from the thread's own history; otherwise it is ignored.
- One turn per thread: a message arriving mid-turn is skipped (⏭️), not queued.
  The gate is the in-memory `_busy` set in `listeners/_turn.py` — per process,
  no cross-replica lock.

## Talking to the FaultMaven core API

- Endpoints used: `POST /api/v1/cases`, `POST /api/v1/cases/{case_id}/turns`
  (multipart), `/api/v1/auth/{oauth/token,oauth/revoke,dev-login,me}`,
  `/api/v1/admin/integrations/slack/workspaces`, and `/health`. A 202 turn
  response is polled by GET of its `Location` (`FaultMavenClient._poll`).
- Default credential (`app.make_fault_client`): the refresh grant if
  `FAULTMAVEN_REFRESH_TOKEN` is set, or if a `CREDENTIAL_STORE_PATH` store exists
  and `FAULTMAVEN_API_TOKEN` is unset (a set API token wins, with a warning). A
  stored token beats the env one, which is only a first-boot seed. Otherwise
  `FAULTMAVEN_API_TOKEN`, then `FAULTMAVEN_DEV_LOGIN_USERNAME` (local auth only).
  Auth is lazy; the agent boots with the backend down.
- Under `SLACK_TRANSPORT=http`, a workspace bound in `fm_workspace_credentials`
  acts as its own service account; an unbound workspace falls back to the
  process-wide credential unless `FAULTMAVEN_REQUIRE_WORKSPACE_BINDING=true`
  refuses it. That setting fails boot under Socket Mode, which has no bindings.
- The contract is pinned in `api-contract.pin.json` (`ref` + `contractVersion`,
  which must agree). Adopting a new contract = move the pin and commit the
  regenerated `faultmaven/api_generated.py` in the same PR.

## Hard rules

- **Refresh tokens rotate.** Each renewal revokes the old token, so the new one
  is written (`credentials.py` / `fm_workspace_credentials`) before use. A failed
  write keeps the token, sets `cred.unpersisted` and keepalive retries it; never
  make that arm raise (it would discard the only live token). A missing
  workspace row means unbound, and drops the credential. `CREDENTIAL_STORE_PATH`
  and `SLACK_DATABASE_URL` must be durable; losing either needs re-provisioning or reinstalls.
- **Tenant check.** `FaultMavenClient._assert_expected_enterprise` rejects a
  workspace credential's minted token whose enterprise claim is absent or
  differs from the one recorded at bind. It skips the process-wide default
  principal and opaque (non-JWT) tokens; only
  `FAULTMAVEN_REQUIRE_WORKSPACE_BINDING` constrains the default. In the bind
  tables `enterprise_id` is Slack's Grid id; the tenant is `fm_enterprise_id`.
- **No migration tool.** Every table is created at startup if absent:
  `thread_cases`, `fm_credential`, `fm_workspace_credentials`, `fm_pending_binds`
  and the Slack SDK tables. Only `store.py` evolves a schema (guarded
  `ALTER TABLE ADD COLUMN`); any other shape change needs its own upgrade path
  or a table drop (plus re-binding, for `fm_workspace_credentials`).
- **Scopes stay in lockstep.** `config.DEFAULT_BOT_SCOPES` must match
  `manifest.json` bot scopes, apart from `MANIFEST_ONLY_BOT_SCOPES`
  (`tests/test_config.py` enforces this).
- **Untrusted text is escaped.** Engine output and backend error text are
  external input: `rendering.py` / `slack_mrkdwn.py` neutralize `<!channel>`-style
  broadcasts and raw link entities, and Block Kit chunking respects Slack's
  section limits (`tests/test_hardening.py`).
- **Sanitized filenames.** Slack-supplied filenames pass through
  `slack_files._safe_name` (basename only, no control chars, ≤255 chars, never
  `.`/`..`). Downloads are capped by `MAX_FILE_BYTES` (streamed) and `MAX_FILES`;
  a Slack sign-in page served instead of the file counts as a failed download.
- **Install pages** (`install_pages.py`) render only server-established values,
  HTML-escaped, never request input.
- **Manifest pushes are full replaces.** `scripts/push_manifest.py` mutates the
  live app only with `--apply`, and uses `SLACK_CONFIG_TOKEN` + `SLACK_APP_ID`
  (ops-only; not agent settings).

## Docs

- `docs/design.md`: design intent. §0 lists built vs not built; §12's tree is
  the planned layout (use the Layout table above). Backend contract §8, auth and
  multi-tenancy §10.
- `docs/HOSTING.md`: hosted deployment; `docs/LIVE_TEST.md`: real-workspace smoke test
- `docs/USER_GUIDE.md`: end-user behavior; `PRIVACY.md`: privacy policy pointer
