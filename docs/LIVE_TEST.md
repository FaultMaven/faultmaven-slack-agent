# Live test — install, smoke, and verify in a real workspace

This is the operator runbook for taking the agent from merged code to a working
loop in a real Slack workspace, then confirming each surface end to end. It uses
**Socket Mode**, so no public URL or OAuth round-trip is required — the fastest
path to a genuine test. (Multi-workspace HTTP/OAuth is a later deployment step;
see [design.md](design.md) §14.)

Budget ~15 minutes the first time.

---

## 0. Prerequisites

- A FaultMaven backend reachable from this machine (default `http://localhost:8090`;
  any reachable host works — set `FAULTMAVEN_API_URL`).
- A Slack workspace where you can install an app (you're an admin, or app install
  isn't restricted).
- Python env set up: `pip install -r requirements.txt` (Python 3.12+).

---

## 1. Create the Slack app from the manifest

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Pick the workspace, paste [`manifest.json`](../manifest.json), create.

The manifest pre-wires everything this agent uses:

| Manifest section | Enables |
|---|---|
| `assistant_view` + `assistant_thread_started`/`message.im` events | the **Assistant** side-panel surface |
| `app_mention` event + `app_mentions:read` | the **@mention war-room** surface |
| `shortcuts: [fm_investigate_message]` + `interactivity` | the **"Investigate with FaultMaven"** message shortcut |
| `chat:write` | posting replies + the investigating placeholder |
| `channels:history` / `groups:history` / `im:history` | replaying a thread on first summons (catch-up) |
| `socket_mode_enabled: true` | the dev transport (no public URL) |

## 2. Get the two tokens

- **Bot token** (`xoxb-…`): *OAuth & Permissions* → **Install to Workspace** →
  copy *Bot User OAuth Token* → `SLACK_BOT_TOKEN`.
- **App-level token** (`xapp-…`): *Basic Information* → *App-Level Tokens* →
  **Generate**, scope **`connections:write`** → copy → `SLACK_APP_TOKEN`.

(`SLACK_SIGNING_SECRET` is only needed for the future HTTP transport — leave it
blank for Socket Mode.)

## 3. Configure `.env`

```bash
cp .env.example .env
# Fill in SLACK_BOT_TOKEN and SLACK_APP_TOKEN.
# Point FAULTMAVEN_API_URL at your backend (default http://localhost:8090).
# If the backend runs in local AUTH_MODE, leave FAULTMAVEN_API_TOKEN blank —
# the agent bootstraps a token via dev-login. Otherwise set a bearer token.
```

## 4. Preflight — fail fast before touching Slack

```bash
python scripts/preflight.py          # read-only: config, both Slack tokens, backend, auth
python scripts/preflight.py --full   # also creates a throwaway case + 1 turn (writes data)
```

Every check prints a ✓ or a ✗ with the exact fix. Get a clean **read-only** run
before step 5; run **`--full`** once to confirm the case/turn contract against
*this* backend (it proves the whole pipeline, not just connectivity).

## 5. Run the agent

```bash
python app.py     # connects via Socket Mode; logs "FaultMaven Slack Agent starting"
```

Leave it running. Then invite the bot to a test channel: `/invite @FaultMaven`
(required — the bot can only post in channels it's a member of).

---

## 6. Smoke the four surfaces

Run each and check the **Expect** line. The agent posts a
`:mag: FaultMaven is investigating…` placeholder, then edits it in place with the
result — so a flash-then-update is the healthy signal.

### A. Assistant side panel (1:1)

1. Open the **FaultMaven** app from the right-hand *Assistant* rail (or DM it).
2. Type: `Our checkout API p99 jumped to 2.4s after the 14:00 deploy.`

**Expect:** a threaded reply that engages with the symptom (asks a clarifying
question or names what it needs). A new case is created for this assistant thread.

### B. @mention war-room (channels)

1. In a channel thread (or a fresh message), post an incident note, then:
   `@FaultMaven can you investigate this?`

**Expect:** the bot replies **in that thread** (channel stays quiet). On the
*first* mention in an existing thread, it replays the prior thread messages as
catch-up context, so its reply should reflect what was already discussed.

### C. "Investigate with FaultMaven" message shortcut (the flagship)

1. Find any message — ideally a monitoring alert (Datadog/PagerDuty/Grafana
   Block Kit). Hover → **⋮ More actions** → **Investigate with FaultMaven**.

**Expect:** a case opens **seeded with that message's content as evidence**, and
the first reply threads under the selected message. Rich alerts (blocks, fields,
legacy attachments) are extracted to text — the seed is the alert's substance,
not a bare "Alert triggered" stub.

- **Edge — empty/file-only message:** the shortcut on a message with no readable
  text (e.g. only a file) should *not* open a blank case; it posts a short note
  telling you to paste the key error text and `@mention` it. (File ingestion is
  the next increment.)

### D. Interactive action buttons

1. On any reply that renders **suggested-action buttons** (DECIDE / FREE_SPEECH),
   click one.

**Expect:** the click submits that action as the next turn and the investigation
advances (a new placeholder → updated reply appears in the thread).

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Preflight: `auth.test rejected` | wrong/rotated bot token | re-copy `SLACK_BOT_TOKEN` (the `xoxb-…` one); reinstall if scopes changed |
| Preflight: `apps.connections.open failed` | app token missing `connections:write` | regenerate the app-level token with that scope |
| Preflight: `backend unreachable` | API down or wrong URL | start the backend / fix `FAULTMAVEN_API_URL` |
| Preflight: `could not obtain a bearer token` | backend not in local auth mode | set `FAULTMAVEN_API_TOKEN` for this backend |
| App starts, but nothing happens on events | bot not in the channel | `/invite @FaultMaven` |
| Log: `Cannot post in channel … not_in_channel` | same | `/invite @FaultMaven` |
| Shortcut missing from ⋮ menu | manifest not applied / app not reinstalled | recreate from manifest or reinstall |
| Mention catch-up empty | missing history scope | confirm `channels:history`/`groups:history` are granted; reinstall |

---

## 8. Notes

- **State lives in `data/cases.db`** (the thread→case map). Delete it to start
  every thread fresh; it's safe to remove between test runs.
- **One case per thread.** Re-mentioning or re-running the shortcut in a thread
  that already has a case continues that case — it does not open a second one.
- **Backend stays Slack-agnostic.** The agent is the only bridge: it reads Slack
  and posts to Slack; the FaultMaven API never sees Slack tokens or payloads.
- This run does **not** exercise multi-workspace OAuth or the HTTP transport —
  those are the next deployment increment ([design.md](design.md) §14).
