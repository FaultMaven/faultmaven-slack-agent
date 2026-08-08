# Hosting the FaultMaven Slack Agent (HTTP/OAuth transport)

This is the **production** transport: a publicly-hosted HTTP server
with multi-workspace OAuth, so the app installs into many workspaces and is
Slack Marketplace-eligible. (Socket Mode — `SLACK_TRANSPORT=socket` — remains the
local-dev path against a **separate** dev app; it can never satisfy the *Agents
for Organizations* track, which needs a live public server + Marketplace
distribution.)

Public host: **`https://slack.faultmaven.ai`** → serves `/slack/events`,
`/slack/install`, `/slack/oauth_redirect`, `/health`.
Backend: the cluster FM API at **`https://api.faultmaven.ai`**.

> **Where the deploy lives.** This repo owns the **app + `Dockerfile`** (the image
> build). The **Kubernetes manifests, DNS/TLS, Postgres provisioning, Secret
> wiring, and ingress** live in **`faultmaven-enterprise-infra`**, alongside how
> `api.faultmaven.ai` and the dashboard are deployed — one GitOps source of truth
> for cluster facts. This doc is the **app-side contract** the infra repo consumes.

## Architecture

```
Slack  ──HTTPS──▶  ingress (slack.faultmaven.ai, TLS)      [infra repo]
                        │
                        ▼
            faultmaven-slack-agent (FastAPI / uvicorn)      [this repo + Dockerfile]
              /slack/events        → Bolt handler (verifies signing secret)
              /slack/install       → OAuth consent
              /slack/oauth_redirect→ code exchange → InstallationStore
              /health              → liveness (dependency-free)
                        │                         │
              per-team bot token          FM turn pipeline
              (Postgres InstallationStore)  (https://api.faultmaven.ai)
```

One `SLACK_DATABASE_URL` (Postgres) backs both the `InstallationStore` (per-team
bot tokens) and the `OAuthStateStore` (CSRF state). Tables self-create on first
boot.

## Environment contract (what the app reads)

Non-secret values belong in a ConfigMap; secrets in a Secret — both authored in
the infra repo.

| Var | Kind | Notes |
|---|---|---|
| `SLACK_TRANSPORT=http` | config | selects the HTTP/OAuth runtime |
| `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` | secret | Basic Information → App Credentials |
| `SLACK_SIGNING_SECRET` | secret | verifies inbound requests |
| `SLACK_DATABASE_URL` | secret | **required in http mode** — `postgresql://…` (a dedicated Slack DB). Boot fails fast if unset, so installs can never land in ephemeral storage. |
| `SLACK_OAUTH_REDIRECT_URI` | config | pinned to `https://slack.faultmaven.ai/slack/oauth_redirect` |
| `FAULTMAVEN_API_URL=https://api.faultmaven.ai` | config | cluster backend |
| `FAULTMAVEN_API_TOKEN` | secret | static FM bearer; cannot be renewed. Superseded by the refresh credential below wherever the backend runs `AUTH_MODE=oauth` |
| `FAULTMAVEN_REFRESH_TOKEN` | secret | **required against an `oauth`-mode backend** — provisioned refresh credential (ADR-012 D10). A one-time seed: the grant rotates and the live token then lives in `CREDENTIAL_STORE_PATH`. See [Service account credentials](#service-account-credentials-oauth-mode-backends) |
| `FAULTMAVEN_OAUTH_CLIENT_ID` | config | client id presented on the refresh grant (default `faultmaven-slack-agent`) |
| `CASE_STORE_PATH` | config | thread→case SQLite path — **must be on a persistent volume** (see below) |
| `CREDENTIAL_STORE_PATH` | config | rotated refresh credential SQLite path — **must be on a persistent volume** |

Missing http-mode credentials fail fast at boot with a named error
(`config.Settings._validate_transport_requirements`), never as an opaque runtime
error on the first Slack event.

## State that must persist (a deploy requirement for the infra repo)

- **OAuth installs + state** → Postgres (`SLACK_DATABASE_URL`). Replica-safe.
- **thread→case map** → SQLite at `CASE_STORE_PATH`. This is **local disk**, so the
  Deployment **must** mount a PersistentVolume for it (or the infra repo may
  externalize it onto the same Postgres). Without persistence, a restart wipes the
  map and every in-progress investigation is orphaned into a fresh empty case.
- **rotated FaultMaven credential** → SQLite at `CREDENTIAL_STORE_PATH`, on the
  same PersistentVolume. Each renewal revokes the token it presented, so a
  restart that cannot read this file cannot authenticate at all — recovery
  needs an operator (below).

## Service account credentials (oauth-mode backends)

Against a backend running `AUTH_MODE=oauth` (cloud), dev-login is not served —
it returns 404. The agent instead holds a provisioned **refresh token** and mints
its own access tokens (ADR-012 D10).

**Bootstrap.** On the backend, mint a credential and put it in the agent's
Secret as `FAULTMAVEN_REFRESH_TOKEN`:

```bash
kubectl exec -it deploy/faultmaven-api -- \
    python scripts/auth/provision_service_account.py -u slack-agent --token-only
```

**Renewal is automatic and rotating.** Every renewal returns a new refresh token
and revokes the presented one. The agent persists the new token before using it,
serializes renewals so two never race, and — because the window is wall-clock —
also renews on an idle timer so a quiet workspace can't age its credential out.

**Lockout and recovery.** If the credential is rejected (expired past the
server's window, or revoked because a rotation was lost), the agent logs a
credential error naming the fix and stops trying. Recovery: re-run the
provisioning command above and update the Secret. Re-provisioning does not
revoke a credential still in use, so it is safe to run against a healthy agent.

**Blast radius.** A failure here degrades Slack only; the dashboard and Copilot
authenticate through WorkOS/PKCE.

## Single-replica (for now)

The Postgres OAuth store is replica-safe, but the **thread→case map** and the
**in-process drop-if-busy gate + event dedup** are per-process. The infra
Deployment must pin **one replica** (`strategy: Recreate`) until the case store is
externalized. Horizontal scale is a follow-up, not required for the beta.

## Deploy sequence (executed from the infra repo)

1. **DNS + TLS** for `slack.faultmaven.ai`; verify `/health` → `{"status":"ok"}`
   before touching Slack.
2. **Postgres** — dedicated database + user; URL into the Secret.
3. **Image** — `ghcr.io/faultmaven/faultmaven-slack-agent`, **pinned tag** (never
   `:latest`). Built from this repo's `Dockerfile`.
4. **Apply** the manifests (ConfigMap, Secret, Deployment+PVC, Service, Ingress).
5. **Point Slack at the host** — apply `manifest.json` (already carries the
   `slack.faultmaven.ai` URLs + `socket_mode_enabled: false`) via
   `scripts/push_manifest.py`. The manifest also carries the `app_directory`
   listing URLs (see below), and the push is a **full-manifest** update — run
   it bare first (which previews and stops) so it cannot silently overwrite
   listing fields set in the App Directory form, then `--apply`.
6. **Install** at `https://slack.faultmaven.ai/slack/install` per workspace (the
   Orgs track needs 5+); confirm a row in the Postgres `slack_installations` table.

## Marketplace listing URLs

Slack requires the listing's landing page, privacy policy, and support URLs to be
hosted on a domain FaultMaven owns — a GitHub-hosted policy or repo README is
rejected. All three are served by `faultmaven-website` (Vercel, deploys from
`main`):

| Listing field | URL | Served by |
| --- | --- | --- |
| Installation landing page | `https://www.faultmaven.ai/slack` | `src/app/slack/page.tsx` |
| Privacy policy | `https://www.faultmaven.ai/privacy/slack` | `src/app/privacy/slack/page.tsx` |
| Support | `https://www.faultmaven.ai/support` | `src/app/support/page.tsx` |
| Support email | `support@faultmaven.ai` | — |
| Direct install | `https://slack.faultmaven.ai/slack/install` | this service, `web.py` |

These pages must be live on `www.faultmaven.ai` **before** the listing is
submitted — Slack fetches each URL during review.

**Set these in the App Directory form, not in `manifest.json`.** The manifest has
an `app_directory` block and `apps.manifest.validate` accepts it, but
`apps.manifest.export` does **not** return it — verified against the live API on
2026-08-07. That makes it write-only: you cannot preview what a push would do to
the listing, and you cannot reconcile local values against the ones already
chosen in the form. A block of guessed values in the file that the push applies
blind is a loaded gun, so it was removed. If Slack later starts exporting it, it
can come back — the requirement is that a value be *checkable* before it ships.

The privacy policy is maintained only in `faultmaven-website`. This repo's
`PRIVACY.md` is a pointer to it, deliberately: two copies of a legal document
drift, and only the hosted one satisfies the requirement.

## The manifest must match the live app

The push is a **full-manifest replace**: anything present in the live App Config
but absent from `manifest.json` is reset. `push_manifest.py` therefore previews
by default and mutates only with `--apply`, and aborts rather than applying when
the live config cannot be read.

Because of that, `manifest.json` has to record what the app *actually* holds, not
only what this repo intended. Two entries exist for that reason alone:

- **`incoming-webhook`** (bot scope) — granted on the live app, added by hand in
  **OAuth & Permissions**, never through this file. Nothing here uses it: the
  shortcut handler replies via `response_url`, which is a per-interaction
  callback needing no scope, and `settings.incoming_webhooks` is absent. It is
  recorded because *removing* a granted scope sets `permissions_updated`, which
  forces every installed workspace to reinstall. Dropping it is a deliberate
  least-privilege cleanup with a reinstall cost — not a side effect of an
  unrelated push.
- **`oauth_config.pkce_enabled`** and **`settings.is_mcp_enabled`** — defaults
  Slack populates on the live app. Declared so a preview shows only real drift.

Scopes are listed in Slack's canonical (sorted) order for the same reason: a
reordered list produces a diff that looks like a change and isn't.

⚠️ This drift was invisible until 2026-08-07. `--validate` never read the live
config and `--diff` pushed, so there was no safe way to compare — the
`incoming-webhook` divergence sat undetected for the life of the repo. Run a bare
`push_manifest.py` periodically, not just before a push.

## Deferred (documented, not silently dropped)

- **Per-user FaultMaven account linking (PKCE) + workspace→Team binding**
  (design.md §10.2/10.3). Blocked on open backend asks (§15.2/15.3): no
  workspace→Team binding API and no first-class service-identity token type exist
  yet. For the beta, every workspace's turns run under one cloud FM service token;
  the case always lives in that one Org. No fabricated tenant isolation.
- **Multi-replica / HA** — gated on externalizing the case store.

## Local development (Socket Mode)

Use a **separate** dev Slack app created from `manifest.dev.json` (Socket Mode
enabled). Set `SLACK_TRANSPORT=socket` + `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`. See
`docs/LIVE_TEST.md`.
