# FaultMaven for Slack — install & use

FaultMaven is an AI troubleshooting copilot that runs the investigation right
inside your Slack thread. `@mention` it on an incident — or run **Ask
FaultMaven** on any alert — and it triages the symptom, forms hypotheses, asks
for the specific evidence that confirms or rules each one out, and keeps going
until the root cause is found and the fix is verified. You approve and execute;
it never acts on its own.

This guide covers installing the app and using every surface. For the design and
backend contract, see [design.md](design.md); for self-hosting the server, see
[HOSTING.md](HOSTING.md).

---

## 1. Install

FaultMaven installs into your workspace over standard Slack OAuth — one click,
no configuration.

1. Go to **`https://slack.faultmaven.ai/slack/install`** (or find **FaultMaven**
   in the Slack App Directory) and click **Add to Slack**.
2. Pick your workspace and review the permissions (they're least-privilege —
   see [§6](#6-permissions--why)). Click **Allow**.
3. You're returned to Slack with FaultMaven installed. That's it — there is no
   API key to paste and nothing to configure for the beta.

**Invite it to a channel.** Like any Slack app, FaultMaven can only post in
channels it belongs to. In any channel you'll use it in, run:

```
/invite @FaultMaven
```

---

## 2. The four ways to summon it

FaultMaven is **summon-only** — it acts only in threads you explicitly invite it
into, and never reads background channel chatter. There are four entry points;
all run the same investigation engine.

### A. Assistant side panel — a focused 1:1 session
Open **FaultMaven** from the right-hand **Assistant** rail (or send it a direct
message). Describe the problem and it opens a private investigation with live
status and suggested next steps. Best for working a problem on your own.

### B. `@mention` in a channel thread — the war room
In an incident channel, post the symptom and add `@FaultMaven can you
investigate this?`. It opens a case and replies **in that thread**, so the
channel stays quiet. On the first mention it reads the existing thread for
context.

**Then just reply — no re-mention.** Once FaultMaven owns a thread, plain
replies keep the investigation going. Attach a log to a reply and it's ingested
as evidence. (Replies in threads it was *not* summoned into are ignored.)

### C. "Ask FaultMaven" — open a case from any message
On **any** message — a Datadog/PagerDuty/Grafana alert, a pasted stack trace, a
teammate's note — hover, open the **⋮ More actions** menu, and choose **Ask
FaultMaven**. The case opens seeded with that message (and any attached log) as
the first evidence, and the investigation starts threaded under it. This is the
fastest way to go from "an alert fired" to "an investigation is running."

### D. Direct message — a quick private start
Message FaultMaven directly to open a case without a channel. Same engine as the
Assistant panel.

---

## 3. Working an investigation

Once a case is open, FaultMaven drives a real diagnostic loop and you
collaborate in the thread:

- **It asks for specific evidence.** Rather than guessing, it names the exact
  log, config, or metric that would confirm or eliminate a hypothesis. Attach it
  to a reply (logs, configs, screenshots — up to 8 MiB each) and it's ingested.
- **It shows suggested-action buttons.** When there's a decision to make or a
  next step to run, FaultMaven renders buttons — click one to advance the case
  instead of retyping.
- **It shows live status** while it works ("investigating…"), then edits its
  reply in place with the result — a brief flash-then-update is the healthy
  signal.
- **It grounds every step** in your runbooks and past fixes, and shows the
  evidence behind what it concludes.

When the root cause is found and the fix is verified, FaultMaven posts a
**resolution summary**, and the case can be **captured as a runbook** — so the
next time this happens, the investigation starts from what this one learned.

---

## 4. How it behaves (worth knowing)

- **One Slack thread = one investigation.** Each thread maps to its own case;
  separate threads stay separate. Re-summoning in a thread that already has a
  case continues it — it never opens a duplicate.
- **One turn at a time.** The engine works a case linearly. If several people
  reply at once, FaultMaven answers the first (and `@mention`s that person) and
  marks the others with a ⏭️ reaction — just resend after it replies.
- **It won't fabricate a root cause.** While a case is still open, FaultMaven
  tells you what evidence it needs rather than posting a confident, made-up
  answer to look decisive. When data is inadequate, it keeps engaging and names
  the gap.
- **It only acts where you invite it.** No ambient listening, no reading channel
  history it wasn't summoned into.
- **You stay in command.** It suggests; you approve and execute.

---

## 5. Tips & limits (beta)

- **Seed it with real signal.** The richer the first message or attachment (the
  actual error, the alert payload, the config), the faster it converges.
- **Keep evidence files ≤ 8 MiB.** Larger files are skipped; paste the key
  section instead.
- **Deep artifacts live in the Dashboard.** Full reports, the knowledge base,
  and case browsing are on the FaultMaven Dashboard; Slack owns the in-flow
  investigation and links out for the rest.
- **Beta identity.** During the beta, investigations across all workspaces run
  under a single FaultMaven service identity (per-user account linking is on the
  roadmap). Cases are not fabricated into separate tenants — see
  [HOSTING.md](HOSTING.md) for the honest scope.

---

## 6. Permissions & why

FaultMaven requests least-privilege scopes, each tied to a capability:

| Scope | Why |
|---|---|
| `assistant:write` | the Assistant side-panel surface |
| `chat:write` | post replies and the "investigating…" placeholder |
| `app_mentions:read` | receive `@FaultMaven` summons |
| `commands` | register the **Ask FaultMaven** message shortcut |
| `files:read` | download attached logs/configs/screenshots as evidence |
| `reactions:write` | mark a skipped message ⏭️ when busy |
| `channels:history` / `groups:history` / `im:history` | read a thread for catch-up on first summons, and receive the reply events that drive auto-continue |

It does **not** request a broad message-read scope over your channels — it never
subscribes to a channel firehose. The FaultMaven backend never sees your Slack
tokens or message payloads; the agent is the only bridge.

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| FaultMaven doesn't respond in a channel | Invite it: `/invite @FaultMaven` (it can only post where it's a member). |
| The **Ask FaultMaven** shortcut isn't in the ⋮ menu | The workspace admin may need to reinstall the app; confirm you're in a workspace where it's installed. |
| A reply in a thread is ignored | FaultMaven only continues threads it was summoned into. `@mention` it (or use the shortcut) to open the case first. |
| "Couldn't read that file" | The file is over 8 MiB or in a channel the bot can't access — paste the key text instead. |
| Your message got a ⏭️ reaction | It arrived while FaultMaven was answering someone else. Resend it after its reply lands. |

---

*FaultMaven honors two guarantees in every reply: it never presents an incorrect
conclusion, and it never collapses under pressure — when the data isn't enough,
it names what's missing and keeps working the problem.*
