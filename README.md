# FaultMaven Slack Agent

A backend service that connects a Slack workspace to FaultMaven's core
troubleshooting engine. It turns collaborative thread discussions into
contextual, AI-driven incident investigations.

Built for the **Slack Agent Builder Challenge**.

## Operating model

- **Strict mention-only (Option B).** The bot is passive. It never reads
  background channel traffic. It acts only when:
  - explicitly mentioned (`@FaultMaven …`), or
  - triggered via a message shortcut ("Investigate with FaultMaven").
- **Threads are sessions.** The Slack `thread_ts` is used verbatim as the
  investigation/session id. Every turn rebuilds context from the thread so the
  engine sees the whole collaborative conversation.
- **Async by design.** Slack requires an HTTP 200 within ~3 seconds. Each
  webhook verifies the signature, acks immediately, and offloads the RAG/AI
  work to FastAPI `BackgroundTasks`, replying in-thread when done.

## Layout

```
faultmaven-slack-agent/
├── config.py             # Settings + env validation (fail-fast)
├── main.py               # FastAPI app, routes, background workers
├── requirements.txt
├── services/
│   ├── slack_service.py  # Slack WebClient + Block Kit rendering + thread history
│   └── faultmaven_api.py # Async client for the core engine (with mock mode)
└── utils/
    └── security.py       # Slack request signature verification + replay guard
```

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET
python main.py              # serves on :3000
```

Leave `FAULTMAVEN_API_KEY` empty to run in **mock mode** — the agent returns a
plausible stub investigation so you can demo the full Slack round-trip without
a live backend. With a key set, it calls
`POST {FAULTMAVEN_API_URL}/api/v1/investigations/turn` and gracefully falls
back to mock output if the backend is unreachable.

Expose the port (e.g. `ngrok http 3000`) and point your Slack app at:

| Slack feature        | Request URL                          |
|----------------------|--------------------------------------|
| Event Subscriptions  | `https://<host>/slack/events`        |
| Interactivity        | `https://<host>/slack/interactions`  |

### Required Slack configuration

- **Bot token scopes:** `app_mentions:read`, `chat:write`,
  `channels:history`, `groups:history` (for reading thread replies).
- **Event subscriptions:** `app_mention` only (Option B — do **not** subscribe
  to `message.channels`).
- **Interactivity:** enable, and add a *message* shortcut whose callback id you
  wire to the "Investigate with FaultMaven" action.

## Security

Every inbound request is authenticated against the app signing secret
(HMAC-SHA256) with an explicit timestamp/replay window before any payload is
parsed (`utils/security.py`). Unverified requests get `401`. Duplicate Slack
re-deliveries are deduped on `event_id`.
