# FaultMaven Slack Agent

Brings FaultMaven's AI troubleshooting engine into Slack — investigate incidents
right in the thread (or the AI side panel), grounded in your runbooks, telemetry,
and past fixes. Built on **Bolt for Python**'s Assistant container, backed by the
FaultMaven core API.

Built for the **Slack Agent Builder Challenge**.

> Full architecture, feature design, backend contract, and roadmap:
> [docs/design.md](docs/design.md).

## Operating model

- **Assistant container** — a 1:1 AI side-panel session with suggested prompts
  and live status, one FaultMaven case per assistant thread.
- **Mention-driven in channels** — `@FaultMaven` in an incident thread runs an
  investigation and replies in-thread, keeping the channel quiet. The bot never
  reads background channel traffic; it acts only when summoned.
- **Thread = case.** Each Slack thread maps to one FaultMaven case; the mapping
  is tracked locally (we do *not* pass `thread_ts` to the backend as a session
  id — it validates those server-side).

## Layout

```text
faultmaven-slack-agent/
├── app.py                # Bolt app entrypoint (Socket Mode)
├── config.py             # Settings + env validation (fail-fast)
├── store.py              # thread→case map (SQLite)
├── rendering.py          # TurnResult → Block Kit
├── faultmaven/
│   └── client.py         # FaultMaven API client (create case, multipart turns, 202-poll)
├── listeners/
│   ├── assistant.py      # Assistant container: thread_started + user_message
│   ├── events.py         # app_mention (war-room)
│   └── _turn.py          # shared find-or-create-case → submit-turn pipeline
├── manifest.json         # Slack app manifest (scopes, events, assistant_view)
└── docs/design.md        # authoritative design
```

## Run locally

Requires a running FaultMaven backend (default `http://localhost:8090`).

```bash
pip install -r requirements.txt
cp .env.example .env     # fill in SLACK_BOT_TOKEN + SLACK_APP_TOKEN
python app.py            # connects via Socket Mode — no public URL needed
```

If `FAULTMAVEN_API_TOKEN` is empty, the agent bootstraps a token via
`/auth/dev-login` (local `AUTH_MODE` only) using `FAULTMAVEN_DEV_LOGIN_USERNAME`.

## Slack app setup

Create the app from [`manifest.json`](manifest.json) (api.slack.com/apps → *From
a manifest*). It enables Socket Mode and requests least-privilege scopes
(`assistant:write`, `chat:write`, `app_mentions:read`, plus `*:history` to replay
a summoned thread). Then create an app-level token with `connections:write`
(→ `SLACK_APP_TOKEN`) and install to your workspace.

## Status

Working: Assistant container + `@mention`, the corrected case/turn backend
contract, thread→case mapping, Block Kit rendering, and **interactive
suggested-action buttons** (DECIDE/FREE_SPEECH clicks submit typed turns to
advance the investigation). Token-streaming reasoning timeline, evidence upload,
reports, App Home, and multi-workspace OAuth follow next — see the roadmap in
[docs/design.md](docs/design.md) §14.
