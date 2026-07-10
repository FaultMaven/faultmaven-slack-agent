# 3-minute demo — script & shot-list

The Devpost submission video for the Slack Agent Builder Challenge. Target **≤ 3:00**.
The flagship flow: **an alert fires → FaultMaven investigates in the thread →
root cause + verified fix → the fix becomes a runbook.** One capability shown
excellently.

**Judging constraints baked in** (organizer guidance): lead with the *pain
point* in the first 60 seconds, keep the demo front-and-center, tight and
rehearsed, and publish a link you've verified in an incognito window ≥ 24 h
before the deadline (**2026-07-13 5:00 PM PT**).

---

## Before you record — set the stage

Do a dry run first; the live agent has one turn per thread, so rehearse the
timing.

1. **A clean Slack workspace** with FaultMaven installed and invited to a channel
   like `#incidents`. Bump font size / zoom so text is legible in 1080p.
2. **Seed evidence ready to paste/attach:**
   - A realistic **alert message** already in the channel (a Datadog/Grafana-style
     block, or a plain one): *"🔴 checkout-api p99 latency 2.4s (SLO 800ms) — since 14:02 UTC."*
   - A **log snippet** file to attach when asked (e.g. `checkout-api.log` showing
     DB connection-pool exhaustion / timeouts after a deploy).
   - Optionally a **runbook already in the KB** so the grounding is visible.
3. **Verify the whole flow end-to-end once** so the investigation actually
   reaches a resolution on camera. Have a fallback recording.
4. Close notifications; use a clean Slack theme; hide unrelated channels.

---

## The script (voiceover + on-screen)

Times are targets. Voiceover (VO) is a guide — say it in your own voice; judges
flag AI-boilerplate narration.

### 0:00 – 0:20 · Cold open — the pain
> **VO:** "When production breaks, the war room is a Slack thread. But the actual
> troubleshooting isn't — it's scattered across a dashboard, a runbook wiki, and
> the one person who remembers last time. So the thread fills with 'can someone
> check the logs?' while the real work happens somewhere else."

**On screen:** a busy `#incidents` thread — the alert, a couple of human messages
("looking…", "is this the deploy?"), no real progress. Let it feel familiar.

### 0:20 – 0:35 · The turn
> **VO:** "FaultMaven brings the investigation *into* the thread. Watch — I'll
> open a case straight from the alert."

**On screen:** hover the alert → **⋮ More actions** → click **Ask FaultMaven**.

### 0:35 – 1:15 · It runs a real investigation
> **VO:** "It doesn't just answer — it works the problem like a seasoned
> engineer. It's already read the alert, and instead of guessing it names the
> *specific* evidence it needs."

**On screen:** FaultMaven's first threaded reply — a short triage + a hypothesis,
then a targeted ask ("share the checkout-api logs around 14:02"). Show the live
**investigating…** status, then the reply.

> **VO:** "I give it what it asked for."

**On screen:** reply in the thread with the **log file attached** — no re-mention.
FaultMaven picks it up (auto-continue), ingests the file as evidence.

### 1:15 – 2:05 · Root cause, grounded and verified
> **VO:** "It reads the evidence, connects it to a recent change, and — grounded
> in our own runbooks and past fixes — narrows to the root cause. I decide the
> next step with a click, not by retyping."

**On screen:** FaultMaven correlates the log to the 14:00 deploy, raises a
hypothesis (DB pool too small for the new connection pattern), and renders
**suggested-action buttons**. Click one to advance. It confirms the root cause
and proposes the fix; you confirm the fix is applied and latency recovered.

### 2:05 – 2:35 · The payoff — the fix becomes knowledge
> **VO:** "Case resolved. And here's the part that compounds: the fix is captured
> as a runbook — so the next time this happens, the investigation starts from
> what this one just learned."

**On screen:** the **resolution summary** in the thread, then the **runbook**
captured from it. Brief, but land it — this is the differentiator.

### 2:35 – 3:00 · Close — why it's safe to trust
> **VO:** "It only acts where you invite it — no ambient listening. And while a
> case is open it tells you what evidence it needs; it won't fabricate a
> confident answer to look decisive. You stay in command: it suggests, you
> approve. That's FaultMaven — the whole investigation, in the thread where the
> incident already lives."

**On screen:** pull back to the thread — alert → investigation → resolution →
runbook, all in one place. End card: **FaultMaven for Slack** + the tagline.

---

## Shot-list (quick reference)

| # | Time | Shot | Key action | VO beat |
|---|------|------|-----------|---------|
| 1 | 0:00 | `#incidents` thread, no progress | (idle) | the pain |
| 2 | 0:20 | ⋮ menu on the alert | click **Ask FaultMaven** | the turn |
| 3 | 0:35 | first threaded reply | triage + targeted evidence ask | works it like an engineer |
| 4 | 0:55 | reply with log attached | auto-continue ingests evidence | I give it what it asked |
| 5 | 1:15 | correlation to deploy + hypothesis | click a suggested-action button | grounded root cause, one click |
| 6 | 1:50 | confirmed root cause + fix | confirm fix applied | verified, not guessed |
| 7 | 2:05 | resolution summary | — | case resolved |
| 8 | 2:20 | runbook captured | show the runbook | the knowledge flywheel |
| 9 | 2:35 | pull back to full thread + end card | — | summon-only, sound, you're in command |

---

## Delivery checklist

- [ ] Runtime **≤ 3:00**; the pain point lands in the **first 60 s**.
- [ ] The demo is front-and-center (minimal slides; show the product).
- [ ] 1080p, legible text, captions/subtitles on (many judges watch muted).
- [ ] Uploaded to a **public** link; **verified in an incognito window**.
- [ ] Published **≥ 24 h before** 2026-07-13 5:00 PM PT.
- [ ] Title avoids generic AI naming; leads with **FaultMaven for Slack**.
