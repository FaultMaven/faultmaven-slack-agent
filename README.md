# FaultMaven Slack Agent

Brings FaultMaven's AI troubleshooting engine into Slack — investigate incidents
right in the thread (or the AI side panel), grounded in your runbooks, telemetry,
and past fixes. Built on **Bolt for Python**'s Assistant container, backed by the
FaultMaven core API.

> Full architecture, feature design, backend contract, and roadmap:
> [docs/design.md](docs/design.md).

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
- **Evidence in-thread** — attached logs/configs/screenshots are downloaded and
  forwarded as multipart evidence on any surface (§5.4).
- **Thread = case.** Each Slack thread maps to one FaultMaven case; the mapping
  is tracked locally (we do *not* pass `thread_ts` to the backend as a session
  id — it validates those server-side).

## Layout

```text
faultmaven-slack-agent/
├── app.py                # Bolt app builder (dual transport: http | socket)
├── web.py                # FastAPI host for the HTTP/OAuth transport (+ /health)
├── oauth_store.py        # multi-workspace OAuth Installation + state stores (Postgres)
├── config.py             # Settings + per-transport env validation (fail-fast)
├── store.py              # thread→case map (SQLite)
├── rendering.py          # TurnResult → Block Kit
├── slack_text.py         # Slack message (blocks/attachments) → readable text
├── slack_files.py        # download a message's attached files → evidence bytes
├── faultmaven/
│   └── client.py         # FaultMaven API client (create case, multipart turns, health)
├── listeners/
│   ├── assistant.py      # Assistant container: thread_started + user_message
│   ├── events.py         # app_mention + thread-reply auto-continue (war-room)
│   ├── shortcuts.py      # "Ask FaultMaven" message-shortcut opener
│   ├── actions.py        # suggested-action button clicks
│   └── _turn.py          # shared pipeline: gate (drop-if-busy) → turn → post
├── scripts/preflight.py  # preflight doctor (env + Slack + backend checks)
├── manifest.json         # Slack app manifest (scopes, events, assistant_view, shortcut)
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

If `FAULTMAVEN_API_TOKEN` is empty, the agent bootstraps a token via
`/auth/dev-login` (local `AUTH_MODE` only) using `FAULTMAVEN_DEV_LOGIN_USERNAME`.

**Testing in a real workspace?** Follow the step-by-step runbook in
[docs/LIVE_TEST.md](docs/LIVE_TEST.md) — install from the manifest, run preflight,
then smoke each surface (Assistant panel, @mention, message shortcut, buttons).

## Slack app setup

Two manifests, two transports:

- **Hosted / production** — [`manifest.json`](manifest.json): HTTP/Events +
  multi-workspace OAuth (`socket_mode_enabled: false`, request/redirect URLs on
  `slack.faultmaven.ai`). This is the Marketplace-eligible production app. Deploy
  guide: [docs/HOSTING.md](docs/HOSTING.md).
- **Local dev** — [`manifest.dev.json`](manifest.dev.json): Socket Mode on, no
  public URL. Fastest path to a real test. Walkthrough:
  [docs/LIVE_TEST.md](docs/LIVE_TEST.md).

Both request the same least-privilege scopes — `assistant:write`, `chat:write`,
`app_mentions:read`, `commands` (the shortcut), `reactions:write` (the ⏭️ skip
mark), `files:read` (attached evidence), and `*:history` (thread catch-up + the
reply events that drive continuity) — and register the **Ask** message shortcut
(shown as *Ask FaultMaven*).

## Status

**Working:** Assistant container + `@mention`, **thread-reply auto-continue**, the
**Ask FaultMaven** message shortcut (open a case seeded from any message),
**file ingestion** on all surfaces (attached logs/screenshots → multipart
evidence), **one-turn-per-thread drop-if-busy** with ⏭️ skip marks and replier
`@mention`s, the corrected case/turn backend contract, thread→case mapping, Block
Kit rendering, **interactive suggested-action buttons**, the Home tab, and
**HTTP/Events transport + multi-workspace OAuth** with a Postgres
`InstallationStore` (`SLACK_TRANSPORT=http`; Socket Mode remains the local-dev
transport). A **preflight doctor** (`scripts/preflight.py`) verifies the wiring
before a live test.

**Next:** per-user FaultMaven account linking (workspace→Team binding), a
token-streaming reasoning timeline, and terminal-state reports — see the roadmap
in [docs/design.md](docs/design.md) §16.
