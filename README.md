# FaultMaven Slack Agent

Brings FaultMaven's AI troubleshooting copilot into Slack — investigate incidents
right in the thread (or the AI side panel), grounded in the logs, metrics, and
configs you share, correlated with your runbooks and past fixes. Built on
**Bolt for Python**'s Assistant container, backed by the FaultMaven core API.

> Full architecture, feature design, backend contract, and roadmap:
> [docs/design.md](docs/design.md).

## Getting it

**Try it right now, with no account and nothing installed.** FaultMaven is
already in the [FaultMaven Community
Slack](https://join.slack.com/t/faultmaven-community/shared_invite/zt-493fv3w3o-mPBBI2v3mMYQKS4649mY1A)
— join and `@FaultMaven` in a channel. That workspace is shared and public, so
it is the right place for a real but unremarkable problem and the wrong place
for production secrets.

**In your own workspace:** ask us in the community workspace and we will set it
up. During beta we connect each workspace **by hand** rather than advertising a
self-serve install, and that is deliberate: `fm_workspace_credentials` is not
yet populated per workspace, so a workspace that installs without being bound
is answered on a shared service account and its cases are filed in another
tenant ([fm#1457](https://github.com/FaultMaven/faultmaven/issues/1457)).

‼ **Not advertising the install is not the same as disabling it.**
`GET /slack/install` is a live route (`web.py`), so anyone holding the URL —
from a bookmark, a cached page, `manifest.json`, or this repository's history —
can still install today. The control that actually closes it is
**`FAULTMAVEN_REQUIRE_WORKSPACE_BINDING=true`**, which defaults to `false` so
that existing unbound workspaces keep working; set it on any multi-tenant
backend and an unbound workspace is refused instead of misfiled. Operators
deploying this against Cloud should treat that as required, not optional.

**Not on the Slack Marketplace.** The listing is not live; do not expect to
find it there yet. Note that `manifest.json` still declares
`faultmaven.ai/slack` as the App Directory *installation landing page* while
that page now offers only the community workspace — a resubmission needs one
or the other changed, and because `app_directory` is write-only the drift
cannot be caught programmatically.

## Operating model

- **Assistant container** — a 1:1 AI side-panel session with suggested prompts
  and live status, one FaultMaven case per assistant thread.
- **Summon to create, then auto-continue** — `@FaultMaven` (or the **Ask
  FaultMaven** message shortcut) in a channel *creates* an investigation and
  replies in-thread; after that, plain replies in that thread continue it with no
  re-mention. The bot acts **only on threads it already owns** (a `store`
  lookup gates every message) — never ambient channel chatter.
- **One turn per thread, drop-if-busy** — a Slack thread is N:1 (many people, one
  case) but the backend is linear, so the agent answers the first message and
  **skips** any that arrive before its reply (marked ⏭️, resend after), and
  `@mention`s the person it's answering. See [design.md](docs/design.md) §5.3.
- **Files in-thread** — attached logs/configs/screenshots are downloaded and
  forwarded as multipart file data on any surface (§5.4).
- **Thread = case.** Each Slack thread maps to one FaultMaven case; the mapping
  is tracked locally (we do *not* pass `thread_ts` to the backend as a session
  id — it validates those server-side).

## Layout

```text
faultmaven-slack-agent/
├── app.py                # Bolt app builder (dual transport: http | socket)
├── web.py                # FastAPI host for the HTTP/OAuth transport (+ /health)
├── oauth_store.py        # multi-workspace OAuth Installation + state stores (Postgres)
├── workspace_credentials.py  # Slack workspace → FaultMaven enterprise/team credential binding
├── config.py             # Settings + per-transport env validation (fail-fast)
├── store.py              # thread→case map (SQLite)
├── rendering.py          # TurnResult → Block Kit
├── slack_text.py         # Slack message (blocks/attachments) → readable text
├── slack_mrkdwn.py       # Markdown → Slack mrkdwn conversion
├── slack_files.py        # download a message's attached files → evidence bytes
├── faultmaven/
│   └── client.py         # FaultMaven API client (create case, multipart turns, health)
├── listeners/
│   ├── assistant.py      # Assistant container: thread_started + user_message
│   ├── events.py         # app_mention + thread-reply auto-continue (war-room)
│   ├── shortcuts.py      # "Ask FaultMaven" message-shortcut opener
│   ├── actions.py        # suggested-action button clicks
│   ├── home.py           # App Home tab
│   └── _turn.py          # shared pipeline: gate (drop-if-busy) → turn → post
├── scripts/
│   ├── preflight.py      # preflight doctor (env + Slack + backend checks)
│   └── push_manifest.py  # push manifest.json to a Slack app via the App Manifest API
├── manifest.json         # Slack app manifest — hosted/OAuth (scopes, events, assistant_view, shortcut)
├── manifest.dev.json     # Slack app manifest — local dev (Socket Mode)
├── PRIVACY.md            # pointer to the hosted privacy policy (the canonical copy)
├── docs/design.md        # authoritative design
└── docs/LIVE_TEST.md     # install + smoke runbook (real workspace)
```

## Run locally

Requires a running FaultMaven backend (default `http://localhost:8090`).

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in SLACK_BOT_TOKEN + SLACK_APP_TOKEN
python scripts/preflight.py     # verify env + Slack tokens + backend before connecting
python app.py                   # connects via Socket Mode — no public URL needed
```

How the agent authenticates to the FaultMaven backend, in precedence order:

| Credential | When |
|---|---|
| `FAULTMAVEN_REFRESH_TOKEN` | Backend runs `AUTH_MODE=oauth` (cloud), where dev-login is not served. Provisioned on the backend with `fm-provision-service-account` (pass `-o <enterprise-id>` against a multi-tenant backend); the agent renews and rotates it automatically. See [docs/HOSTING.md](docs/HOSTING.md#service-account-credentials-oauth-mode-backends). |
| `FAULTMAVEN_API_TOKEN` | A static bearer you supply. Cannot be renewed. |
| `FAULTMAVEN_DEV_LOGIN_USERNAME` | Neither of the above set — bootstraps via `/api/v1/auth/dev-login` (local `AUTH_MODE` only). |

**Multi-workspace (hosted) deployments** authenticate each turn as the FaultMaven
`slack` service account bound to *that Slack workspace* (ADR-017 §D6), so its
cases are owned in the right **enterprise** — the isolation boundary — and
auto-share to the right team. Those per-workspace credentials live beside the
installations in `SLACK_DATABASE_URL`; the table above then describes only the
fallback used by workspaces that have not been bound yet. Set
**`FAULTMAVEN_REQUIRE_WORKSPACE_BINDING=true`** against a multi-tenant backend to
refuse an unbound workspace instead: the fallback account acts for one particular
enterprise, so answering on it would file another customer's incident inside that
tenant. See [docs/design.md](docs/design.md) §10.1.

‼ **As deployed today, no workspace is bound** — `fm_workspace_credentials` is
empty, so every installed workspace is on the fallback described above, and the
setting defaults to `false`. Read this section as the design, not the current
state; see [Getting it](#getting-it) and
[fm#1457](https://github.com/FaultMaven/faultmaven/issues/1457).

**Testing in a real workspace?** Follow the step-by-step runbook in
[docs/LIVE_TEST.md](docs/LIVE_TEST.md) — install from the manifest, run preflight,
then smoke each surface (Assistant panel, @mention, message shortcut, buttons).

## Slack app setup

Two manifests, two transports:

- **Hosted / production** — [`manifest.json`](manifest.json): HTTP/Events +
  multi-workspace OAuth (`socket_mode_enabled: false`, request/redirect URLs on
  `slack.faultmaven.ai`). This is the Marketplace-ready transport; deploy it
  yourself following [docs/HOSTING.md](docs/HOSTING.md).
- **Local dev** — [`manifest.dev.json`](manifest.dev.json): Socket Mode on, no
  public URL. Fastest path to a real test. Walkthrough:
  [docs/LIVE_TEST.md](docs/LIVE_TEST.md).

Both request the same least-privilege scopes — `assistant:write`, `chat:write`,
`app_mentions:read`, `commands` (the shortcut), `reactions:write` (the ⏭️ skip
mark), `files:read` (attached files), and `*:history` (thread catch-up + the
reply events that drive continuity) — and register the **Ask** message shortcut
(shown as *Ask FaultMaven*).

## Status

**Working:** Assistant container + `@mention`, **thread-reply auto-continue**, the
**Ask FaultMaven** message shortcut (open a case seeded from any message),
**file ingestion** on all surfaces (attached logs/screenshots → multipart
file data), **one-turn-per-thread drop-if-busy** with ⏭️ skip marks and replier
`@mention`s, the corrected case/turn backend contract, thread→case mapping, Block
Kit rendering, **interactive suggested-action buttons**, the Home tab, and
**HTTP/Events transport + multi-workspace OAuth** with a Postgres
`InstallationStore` (`SLACK_TRANSPORT=http`; Socket Mode remains the local-dev
transport). A **preflight doctor** (`scripts/preflight.py`) verifies the wiring
before a live test.

**Not yet:** **per-workspace isolation** — every installed workspace currently
shares one service account, so workspaces are provisioned by hand during beta
rather than self-serve ([fm#1457](https://github.com/FaultMaven/faultmaven/issues/1457)).
Until each is bound, set `FAULTMAVEN_REQUIRE_WORKSPACE_BINDING=true` on a
multi-tenant backend so an unbound workspace is refused rather than misfiled.
Then per-user FaultMaven account linking (workspace→team binding), a
token-streaming reasoning timeline, and terminal-state reports — see the roadmap
in [docs/design.md](docs/design.md) §16.

## Privacy

What the agent reads, forwards, and stores is documented in the privacy policy at
[www.faultmaven.ai/privacy/slack](https://www.faultmaven.ai/privacy/slack) — the
canonical copy, and the URL the Marketplace listing points at. See
[PRIVACY.md](PRIVACY.md).

## Marketplace listing URLs

Slack requires the listing's landing page, privacy policy, and support URLs to
live on a domain FaultMaven owns. All three are served by `faultmaven-website`:

| Listing field | Value |
| --- | --- |
| Installation landing page | [faultmaven.ai/slack](https://faultmaven.ai/slack) |
| Privacy policy | [faultmaven.ai/privacy/slack](https://faultmaven.ai/privacy/slack) |
| Support | [faultmaven.ai/support](https://faultmaven.ai/support) |
| Support email | <support@faultmaven.ai> |
| Categories | Developer Tools, Productivity |
| Pricing | `freemium` |
| Languages | `en-US` |

These are declared in `manifest.json` under `app_directory`, matching what is set
in the App Directory form, so the manifest completely describes the app.

⚠️ `app_directory` is **write-only** — `apps.manifest.export` does not return it,
so a preview always shows the block as an addition and it cannot be verified
programmatically. Only ever copy these values from the form; never invent one.
See [docs/HOSTING.md](docs/HOSTING.md#marketplace-listing-urls).

## Pushing manifest.json

```bash
python scripts/push_manifest.py --validate   # schema check only; reads nothing live
python scripts/push_manifest.py              # live-vs-local diff, then STOPS
python scripts/push_manifest.py --apply      # applies it (the only mutating form)
```

⚠️ The push is a **full-manifest replace**: anything set in the App Config UI but
absent from `manifest.json` is reset. Nothing mutates without `--apply`, the diff
is always shown first, and the run aborts rather than replacing an app it could
not read.

`manifest.json` therefore tracks what the live app *actually* holds — including
the unused `incoming-webhook` scope, which is recorded only because removing a
granted scope forces every workspace to reinstall. Details in
[docs/HOSTING.md](docs/HOSTING.md#the-manifest-must-match-the-live-app).

## License

Licensed under the Apache License 2.0 — see [LICENSE](LICENSE).
