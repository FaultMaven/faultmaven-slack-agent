# Hosting the FaultMaven Slack Agent (HTTP/OAuth transport)

This is the **submission / production** transport: a publicly-hosted HTTP server
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
| `FAULTMAVEN_API_TOKEN` | secret | cloud FM service bearer (the beta identity all workspaces run under) |
| `CASE_STORE_PATH` | config | thread→case SQLite path — **must be on a persistent volume** (see below) |

Missing http-mode credentials fail fast at boot with a named error
(`config.Settings._validate_transport_requirements`), never as an opaque runtime
error on the first Slack event.

## State that must persist (a deploy requirement for the infra repo)

- **OAuth installs + state** → Postgres (`SLACK_DATABASE_URL`). Replica-safe.
- **thread→case map** → SQLite at `CASE_STORE_PATH`. This is **local disk**, so the
  Deployment **must** mount a PersistentVolume for it (or the infra repo may
  externalize it onto the same Postgres). Without persistence, a restart wipes the
  map and every in-progress investigation is orphaned into a fresh empty case.

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
   `scripts/push_manifest.py`.
6. **Install** at `https://slack.faultmaven.ai/slack/install` per workspace (the
   Orgs track needs 5+); confirm a row in the Postgres `slack_installations` table.

## Deferred (documented, not silently dropped)

- **Per-user FaultMaven account linking (PKCE) + workspace→org binding**
  (design.md §10.2/10.3). Blocked on open backend asks (§15.2/15.3): no
  workspace→org binding API and no first-class service-identity token type exist
  yet. For the beta, every workspace's turns run under one cloud FM service token;
  the case always lives in that one org. No fabricated tenant isolation.
- **Multi-replica / HA** — gated on externalizing the case store.

## Local development (Socket Mode)

Use a **separate** dev Slack app created from `manifest.dev.json` (Socket Mode
enabled). Set `SLACK_TRANSPORT=socket` + `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`. See
`docs/LIVE_TEST.md`.
