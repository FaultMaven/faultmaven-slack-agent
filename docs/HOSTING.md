# Hosting the FaultMaven Slack Agent (HTTP/OAuth transport)

This is the **submission / production** transport: a publicly-hosted HTTP server
with multi-workspace OAuth, so the app installs into many workspaces and is
Slack Marketplace-eligible. (Socket Mode — `SLACK_TRANSPORT=socket` — remains the
local-dev path against a single dev app; it can never satisfy the *Agents for
Organizations* track, which requires a live public server + Marketplace
distribution.)

Public host: **`https://slack.faultmaven.ai`** → serves `/slack/events`,
`/slack/install`, `/slack/oauth_redirect`, `/health`.
Backend: the cluster FM API at **`https://api.faultmaven.ai`**.

## Architecture

```
Slack  ──HTTPS──▶  ingress (slack.faultmaven.ai, TLS)
                        │
                        ▼
            faultmaven-slack-agent (FastAPI / uvicorn, 1 replica)
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

## Configuration

Non-secret config in `deploy/k8s/configmap.yaml`; secrets via a Secret you create
out-of-band (`deploy/k8s/secret.example.yaml` is the key list, not real values):

| Var | Where | Notes |
|---|---|---|
| `SLACK_TRANSPORT=http` | ConfigMap | selects the HTTP/OAuth runtime |
| `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` | Secret | Basic Information → App Credentials |
| `SLACK_SIGNING_SECRET` | Secret | verifies inbound requests |
| `SLACK_DATABASE_URL` | Secret | `postgresql://…` — dedicated Slack DB |
| `SLACK_OAUTH_REDIRECT_URI` | ConfigMap | pinned to the public redirect URL |
| `FAULTMAVEN_API_URL=https://api.faultmaven.ai` | ConfigMap | cluster backend |
| `FAULTMAVEN_API_TOKEN` | Secret | cloud FM service bearer (beta identity) |

Missing credentials fail fast at boot with a named error (see
`config.Settings._validate_transport_requirements`), never as an opaque runtime
error on the first Slack event.

## Deploy

1. **DNS + TLS.** Point `slack.faultmaven.ai` at the ingress; cert-manager issues
   the cert (`deploy/k8s/ingress.yaml`). Verify `https://slack.faultmaven.ai/health`
   returns `{"status":"ok"}` before touching Slack.
2. **Postgres.** Create a dedicated database (e.g. `faultmaven_slack`) + user; put
   its URL in the Secret. Same server as the core DB is fine — separate database.
3. **Secret + ConfigMap.** Create the Secret out-of-band; apply the ConfigMap.
4. **Apply.** `kubectl apply -f deploy/k8s/` (configmap, secret, deployment,
   service, ingress). Image: `ghcr.io/faultmaven/faultmaven-slack-agent` — pin a
   tag in prod, don't run `:latest`.
5. **Point Slack at the host.** `manifest.json` already carries the
   `slack.faultmaven.ai` request/redirect URLs and `socket_mode_enabled: false`.
   Apply it (`scripts/push_manifest.py`) or paste it into the App Manifest tab.
6. **Install.** Visit `https://slack.faultmaven.ai/slack/install`, consent, and
   confirm a row lands in the Postgres `slack_installations` table. Repeat per
   workspace (the Orgs track needs 5+).

## Why single-replica (for now)

The Postgres OAuth store is replica-safe, but the **thread→case map (SQLite)** and
the **in-process drop-if-busy gate + event dedup** are per-process. Two replicas
would split them and break one-turn-per-thread and duplicate suppression. The
Deployment pins `replicas: 1` with `strategy: Recreate`. Horizontal scale is gated
on externalizing the case store (e.g. moving it onto the same Postgres) — a
follow-up, not required for the beta's scale.

## Deferred (documented, not silently dropped)

- **Per-user FaultMaven account linking (PKCE) + workspace→org binding**
  (design.md §10.2/10.3). Blocked on open backend asks (§15.2/15.3): no
  workspace→org binding API and no first-class service-identity token type exist
  yet. For the beta, every workspace's turns run under one cloud FM service
  token; the case always lives in that one org. No fabricated tenant isolation.
- **Multi-replica / HA** — see above.
