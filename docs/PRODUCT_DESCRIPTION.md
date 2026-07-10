# FaultMaven for Slack — product description

Canonical product copy for the FaultMaven Slack agent. Reuse these across the
Devpost submission, the App Directory listing, the repo README, and the website —
same facts and positioning everywhere, each surface in its own voice
(per the `brand-messaging` skill: consistent, aligned, corroborative — never the
same sentence twice).

**Voice note for Sterling:** the longer pieces are drafted for *you to voice and
finalize* — judges flag AI boilerplate, so read them aloud and make them sound
like you. Everything here is factually true against the shipped agent; the one
place a real number would land is marked **⟨IMPACT⟩** — see the note at the
bottom before you fill it.

---

## 1. Tagline (one line — Devpost title/subtitle, hero)

> **Run the whole investigation in the thread where the incident already lives.**

Alternates (all approved-tone, pick by surface):
- *Your incident is in Slack. Now your troubleshooting is too.*
- *Troubleshoots like an engineer. Learns like a team. Lives in your thread.*

## 2. Short description (≤140 chars — App Directory, manifest, social)

This is the shipped `manifest.json` `description` — keep it in sync:

> AI troubleshooting copilot that works a problem like a seasoned engineer —
> methodical, evidence-based, and sharper with every case.

## 3. Elevator (2–3 sentences — README opener, "what is this")

> FaultMaven is an AI troubleshooting copilot that runs the investigation right
> inside your Slack thread. `@mention` it on an incident — or run **Ask
> FaultMaven** on any alert — and it triages the symptom, forms hypotheses, asks
> for the specific logs or configs that confirm or rule each one out, and keeps
> going until the root cause is found and the fix is verified. It reads the
> evidence you give it, draws on your runbooks and past fixes, and shows its work
> at every step — you approve and execute, so it never acts on its own.

## 4. Full description (Devpost "About the project" / long App Directory blurb)

*Draft for Sterling to voice. First paragraph leads with the pain point and
answers what / who / why-it-matters, per the organizer's guidance.*

---

When something breaks, the war room is a Slack thread — but the actual
troubleshooting isn't. It's scattered across a dashboard in one tab, a runbook
wiki in another, and the one engineer who remembers the last time this happened.
So the thread fills with "can someone check the logs?" while the real work
happens somewhere else, and the moment the incident closes, everything the team
just learned evaporates. **FaultMaven closes that gap: it brings a methodical,
evidence-based investigation into the thread itself** — for the on-call engineer,
the responder, and everyone watching the channel. ⟨IMPACT⟩

FaultMaven doesn't just answer questions — it **runs the investigation**. Hand it
an error, a log, or a config and it works the problem the way a seasoned engineer
would: triage the symptom, form hypotheses, ask for the *specific* evidence that
confirms or eliminates each one, and keep going until the root cause is found and
the fix is verified. Four things define how it behaves:

- **Goal-driven** — every reply moves the case toward a resolution, not just a
  response.
- **Methodical** — it follows a real diagnostic method and won't jump to a
  conclusion; while a case is still open it names what it needs, rather than
  guessing at an answer.
- **Evidence-based** — it reads the logs, errors, and configs you attach, draws
  on your runbooks and past fixes, and shows the evidence behind every step.
- **Self-learning** — when a case resolves, the fix can be captured as a runbook,
  so the next incident starts from what the last one taught you.

You work with it the way you already work in Slack:

- **`@mention` it in a channel thread** to open an investigation, then just
  **reply** to keep it going — no re-mention needed. It threads its answers, so
  the channel stays quiet.
- **Run "Ask FaultMaven"** from the `⋮` menu on any message — a Datadog or
  PagerDuty alert, a stack trace, a teammate's note — to open a case seeded with
  that message (and any attached log) as evidence.
- **Open the Assistant side panel** for a focused 1:1 session with suggested
  next steps and live status.
- **Attach a log, config, or screenshot** on any surface and it's ingested as
  evidence for the case.
- **Click the suggested-action buttons** on a reply — decide between hypotheses,
  run the next step — instead of retyping.

Two design choices make it safe to put in a shared channel:

- **It acts only on threads you summon it into.** No ambient listening, no
  reading channel chatter — it responds where you invite it and nowhere else.
  Every problem gets its own thread, and each thread is its own case, so separate
  investigations stay separate.
- **It won't fabricate a root cause.** While a case is still open, FaultMaven
  tells you what evidence it needs — it does not post a confident, made-up
  resolution to look decisive. You stay in command throughout: it suggests, you
  approve and execute.

And because every resolved case can become a runbook, a team that troubleshoots
with FaultMaven builds a shared, reusable body of troubleshooting knowledge as a
byproduct of the work it was already doing — the next investigation starts
smarter than the last.

---

## 5. Facts sheet (for whoever writes a derivative surface)

Verifiable, on-message facts — pull from these, don't invent:

- **What it is:** the FaultMaven troubleshooting copilot, delivered as a native
  Slack agent. Backed by the FaultMaven API (the investigation engine, knowledge
  base, and AI orchestration); the backend never sees Slack tokens or payloads —
  the agent is the only bridge.
- **Who it's for:** engineers, SREs, and DevOps professionals running incidents
  in Slack (capability-first framing — don't narrow to one role).
- **Surfaces shipped:** Assistant side panel (1:1), `@mention` + reply
  auto-continue (war-room), "Ask FaultMaven" message shortcut, interactive
  suggested-action buttons, file/evidence ingestion on every surface.
- **Model:** one Slack thread = one FaultMaven case; summon-to-create then
  auto-continue; one turn per thread (a second message before it replies is
  marked ⏭️ — resend after).
- **Privacy posture:** strict summon-only; acts only on threads it owns; no
  `message.channels` firehose. An enterprise-trust strength — relevant to the
  "Agent for Organizations" framing.
- **Behavior guarantees:** goal-driven, methodical, evidence-based, self-learning
  (the four traits). Will not render a premature/fabricated resolution while the
  case is still investigating.
- **Engine facts** (if a surface needs backend depth): milestone-based
  investigation (INQUIRY → INVESTIGATING → RESOLVED/CLOSED); hypotheses with
  confidence scoring; evidence grounded in your logs, runbooks, and past fixes;
  9 LLM providers supported by the backend.
- **License / model:** the FaultMaven backend is source-available (fair-source,
  FSL-1.1-ALv2). Do **not** call it "open source." One unified codebase;
  Standalone and Cloud deployments.

## 6. Terminology guardrails (from the `brand-messaging` skill)

Use: investigation, case, evidence, runbook, hypothesis, milestone,
troubleshooting copilot. Avoid: incident/ticket (for *case*), playbook/doc (for
*runbook*), AIOps/observability platform, "incident assistant." No superlatives
without evidence ("best-in-class", "cutting-edge"). Plain verbs (reads, forms,
asks, grounds, captures) over "leverages"/"utilizes". "FaultMaven" is the whole
product; "FaultMaven Copilot" is the *browser extension* only — don't call this
Slack agent "FaultMaven Copilot."

---

## ⟨IMPACT⟩ — the one number to add

The organizer's rubric asks the first paragraph to carry a **specific impact
number**, and judges reward it. I deliberately did **not** invent one — FaultMaven
is built on a no-incorrect-conclusion guarantee, and a made-up "cuts MTTR 40%"
would undercut exactly the credibility the product sells on.

If you have a **real** figure from your own use, it's the single
highest-leverage edit here. Good candidates, in order of impact:
1. A before/after time from a real investigation you ran through the agent
   ("root-caused a p99 regression in one thread in ~8 minutes").
2. A knowledge-flywheel number (runbooks in the KB the agent draws on; cases
   turned into runbooks).
3. A concrete scope number that's true today (surfaces, providers, KB size).

Drop it where the ⟨IMPACT⟩ marker sits and delete the marker. If you don't have
a defensible number yet, the paragraph stands on its own without one — an
honest, specific pain-point beats a fabricated statistic with these judges.
