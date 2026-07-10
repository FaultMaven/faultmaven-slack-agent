# Live test — install, smoke, and verify in a real workspace

This is the operator runbook for taking the agent from merged code to a working
loop in a real Slack workspace, then confirming each surface end to end. It uses
**Socket Mode**, so no public URL or OAuth round-trip is required — the fastest
path to a genuine test. (Multi-workspace HTTP/OAuth is a later deployment step;
see [design.md](design.md) §16, P5–P6.)

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

> This guide is for **local Socket Mode** testing. Use the **dev** manifest
> ([`manifest.dev.json`](../manifest.dev.json), Socket Mode **on**) — NOT the
> hosted `manifest.json`, which has Socket Mode off and points at
> `slack.faultmaven.ai`. Create a **separate** app for the hosted transport.

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Pick the workspace, paste [`manifest.dev.json`](../manifest.dev.json), create.
3. **Set the app icon** (the manifest can't carry it): *Basic Information* →
   *Display Information* → **App icon** → upload
   [`assets/slack-app-icon.png`](../assets/slack-app-icon.png) — the FaultMaven
   "FM" mark. Otherwise the bot shows a generic avatar.

**Updating the manifest later — no copy-paste.** Once the app exists, apply
`manifest.json` edits with [`scripts/push_manifest.py`](../scripts/push_manifest.py)
instead of re-pasting into the App Manifest tab:

```bash
# one-time: generate an APP CONFIGURATION TOKEN (separate from the bot/app
# tokens) at api.slack.com/apps → "Your app configuration tokens", then add to .env:
#   SLACK_CONFIG_TOKEN=xoxe.xoxp-...
#   SLACK_APP_ID=A0XXXXXXX
python scripts/push_manifest.py --diff   # preview live-vs-local, then update
```

It validates, updates, and tells you whether the change also needs a **reinstall**
(only OAuth-scope changes do). Config tokens expire ~12 h — regenerate on a token
error. It reads only the config token, never your bot/app tokens.

The manifest pre-wires everything this agent uses:

| Manifest section | Enables |
|---|---|
| `assistant_view` + `assistant_thread_started`/`message.im` events | the **Assistant** side-panel surface |
| `app_mention` event + `app_mentions:read` | the **@mention** summon |
| `message.channels` / `message.groups` events | **thread-reply auto-continue** (gated to owned threads) |
| `shortcuts: [fm_investigate_message]` + `interactivity` + `commands` | the **Ask** message shortcut (shown as *Ask FaultMaven*; shortcuts need `commands`) |
| `chat:write` | posting replies + the investigating placeholder |
| `reactions:write` | marking a skipped message ⏭️ (drop-if-busy) |
| `files:read` | downloading attached logs/screenshots as evidence |
| `channels:history` / `groups:history` / `im:history` | thread catch-up on first summons + receiving reply events |
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

**Also — the plain DM composer** (the app's direct-message box, *not* the
Assistant rail — messages here carry no `thread_ts`): type a first message with
no thread. **Expect** exactly *one* investigation opens — a single placeholder,
one case, one reply (the Assistant middleware must *not* also claim it). Then
reply in that thread and confirm it continues the *same* case.

### B. @mention war-room + auto-continue (channels)

1. In a channel thread (or a fresh message), post an incident note, then:
   `@FaultMaven can you investigate this?`
2. When it replies, **reply again in that thread — no @mention** — and attach a
   log if you have one.

**Expect:** the bot replies **in that thread** (channel stays quiet); on the
*first* mention it replays the prior thread as catch-up. The follow-up reply
**continues the investigation without a re-mention** (auto-continue), ingesting
the attached file. A reply in a thread it was *not* summoned into is ignored.

- **Drop-if-busy:** have two people reply at once (or fire a second reply before
  it answers). **Expect:** FaultMaven answers the first (`@mention`ing that
  person) and marks the others ⏭️ — resend after it replies.

### C. "Ask FaultMaven" message shortcut (the flagship)

1. Find any message — ideally a monitoring alert (Datadog/PagerDuty/Grafana Block
   Kit), better yet with a **log file attached**. Hover → **⋮ More actions** →
   **Ask FaultMaven**.

**Expect:** a case opens **seeded with that message as evidence** (rich alerts
are extracted from blocks/attachments, not a bare "Alert triggered" stub), the
attached file is **downloaded and forwarded**, and the first reply threads under
the selected message.

- **Edge — unreadable file-only message:** a message with no readable text and a
  file the bot can't read (too large / no access) does *not* open a blank case;
  it posts a short note telling you to paste the key text and `@mention` it.

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
| Preflight: `! status='degraded'` (yellow) | non-critical component down / `ALLOW_TOOLLESS_INVESTIGATION` | **not a failure** — the agent still works; check `GET /health` for which component |
| Preflight: `token rejected (401)` | stale/wrong `FAULTMAVEN_API_TOKEN` | re-issue the token, or use a local-`AUTH_MODE` backend so dev-login works |
| Preflight: `could not obtain a bearer token` | backend not in local auth mode | set `FAULTMAVEN_API_TOKEN` for this backend |
| App starts, but nothing happens on events | bot not in the channel | `/invite @FaultMaven` |
| Log: `Cannot post in channel … not_in_channel` | same | `/invite @FaultMaven` |
| Shortcut missing from ⋮ menu | manifest not applied / app not reinstalled, or `commands` scope missing | recreate from manifest (needs `commands`) or reinstall |
| Mention catch-up empty | missing history scope | confirm `channels:history`/`groups:history` are granted; reinstall |
| Thread replies ignored (no auto-continue) | `message.channels`/`message.groups` not subscribed, or the thread isn't one FaultMaven owns | reinstall with those events; remember only summoned threads continue |
| Skipped messages get no ⏭️ mark | `reactions:write` not granted (re-consent needed after adding it) | grant `reactions:write` and reinstall; the log warns `Could not mark a skipped message` |
| Attached file ignored / "couldn't read that file" | missing `files:read`, file >8 MiB, or no access to that file's channel | grant `files:read` + reinstall; keep files ≤8 MiB |

---

## 8. Notes

- **State lives in `data/cases.db`** (the thread→case map). Delete it to start
  every thread fresh; it's safe to remove between test runs.
- **One case per thread.** Re-mentioning or re-running the shortcut in a thread
  that already has a case continues that case — it does not open a second one.
- **Backend stays Slack-agnostic.** The agent is the only bridge: it reads Slack
  and posts to Slack; the FaultMaven API never sees Slack tokens or payloads.
- This run does **not** exercise multi-workspace OAuth or the HTTP transport —
  those are later roadmap phases ([design.md](design.md) §16, P5–P6).
