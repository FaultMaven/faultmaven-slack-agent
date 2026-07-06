# FaultMaven Slack Agent — Design

> Authoritative design for the FaultMaven Slack AI agent. Replaces an earlier
> "Option B raw-webhook" proposal (`docs/PRODUCT_BLUEPRINT.md`, removed — see git
> history) that predated the decision to rebuild on Bolt's Assistant container.

---

## 0. TL;DR — what we are building and why

A **Slack-native AI troubleshooting copilot** that brings FaultMaven's
investigation engine into the place incidents actually happen — Slack threads and
the AI side panel — with full feature parity to the
[FaultMaven Copilot browser extension](https://github.com/FaultMaven/faultmaven-copilot),
backed by the existing **FaultMaven core API** (`faultmaven`).

Two requirements drive every decision, and they are **complementary, not in
tension**:

1. **Business value** — deliver Copilot-grade troubleshooting (turn-based
   investigations, evidence capture, hypotheses, suggested actions, knowledge
   grounding, reports) using the FaultMaven API server as the backend.
2. **Challenge fit** — meet the
   [Slack Agent Builder Challenge](https://slackhack.devpost.com/) (deadline
   **2026-07-13**), judged on Technological Implementation, Design/UX, Impact,
   and Idea quality.

### Decisions locked

| Decision | Choice | Rationale |
|---|---|---|
| Framework | **Rebuild on Bolt for Python** (replace the raw-FastAPI skeleton) | Unlocks the Slack AI-App primitives the challenge rewards; the skeleton uses none of them. |
| Primary UX | **Assistant / AI-App container** (side panel) + channel `app_mention` + message shortcuts + slash commands | The side panel is the natural home for a 1:1 investigation; channels are the war room. We serve both. |
| Interaction model | **Deliberately-invoked tool, not an ambient participant** — respond only to a *ping* (mention / shortcut / slash / panel); a turn's context is the **pinged message only**, not the surrounding thread (§5) | Matches the backend's serialized, single-author model *and* Slack's own AI-apps guidance *and* every incident bot in the market; curated input protects the soundness guarantee. |
| Case scoping & lifecycle | **Thread-scoped collaborative cases under the org**, with offer-to-close / auto-close-on-inactivity / fresh-on-revival (§6) | A Slack thread never "closes" on its own; the agent must supply the lifecycle drivers a Copilot user did by hand, so cases stay bounded and the knowledge flywheel keeps turning. |
| Required Slack tech | **Slack AI capabilities only** | Assistant container, suggested prompts, streaming, `set_status`, feedback. Directly serves the business need. MCP / Real-Time Search deferred (§14). |
| Deploy / auth | **Cloud OAuth, multi-workspace** → "Agent for Organizations" track | Slack workspace ↔ FaultMaven organization; per-user FaultMaven account linking. |
| Privacy posture | **Keep strict mention-only** — no `message.channels` firehose | An enterprise-trust strength that is fully compatible with the AI-App UX. |
| Backend integration | **FaultMaven REST** (`/cases`, `/cases/{id}/turns`, `/reports`, `/knowledge`) | Carries the case state machine, evidence pipeline, and milestones. |

### The one bug to fix no matter what

The original skeleton posts to **`POST /api/v1/investigations/turn`, which does
not exist.** The real contract is: **create a case**
(`POST /api/v1/cases`) → **submit turns**
(`POST /api/v1/cases/{case_id}/turns`, *multipart form-data*). See §8.

---

## 1. Goals and non-goals

### Flagship scenario — "investigate this → resolved + runbook"

One capability done excellently beats three done ordinarily. The flagship is the
universal **"investigate this"** flow:

1. From **any** message — an alert, a pasted error/log, a problem description — a
   user fires the **"Ask FaultMaven" message shortcut** (§4.3): a
   case opens, seeded with that message (+ optional context/file via the modal).
2. FaultMaven drives a **structured investigation** in the thread — triage →
   hypotheses → *specific* evidence requests → root cause → verified fix —
   collaborating as the user supplies evidence.
3. On resolution it posts a **resolution summary + auto-generated runbook** (the
   knowledge flywheel) and the case closes (§6).

It exercises every FaultMaven strength (milestones, hypotheses, evidence,
RAG-against-past-runbooks, reports) in one loop, and it is the cleanest demo. The
war-room variant is the *same flow* reached by `@mention` with a catch-up read on
entry (§5.2). Ad-hoc Q&A, thread summaries, and ambient chatter are deliberately
**not** headlines — they are commodity and dilute the one thing FaultMaven is
uniquely great at.

**Goals**

- Parity with the Copilot's user-facing capabilities, re-expressed in Slack's
  primitives (§7).
- A first-class AI-App experience: suggested prompts, live status, streamed
  reasoning, hypotheses, and actionable next steps.
- Multi-workspace cloud deployment with clean tenant isolation
  (workspace ↔ org).
- Honor FaultMaven's two soundness guarantees in every rendered turn (§9.3):
  **never present an incorrect conclusion; never collapse under pressure** —
  when data is inadequate, keep engaging and *name the missing data*.

**Non-goals (v1)**

- **Ambient conversational participation.** FaultMaven does *not* read background
  channel traffic, does *not* decide on its own when to speak, and does *not*
  follow a flowing multi-party discussion. It acts only when explicitly pinged
  (§5). This is deliberate — autonomous floor-management is an unsolved,
  high-cost problem with little payoff, and ambient input would poison a sound
  investigation. Every comparable agent makes the same choice.
- Replacing the Dashboard — deep heavy artifacts (full reports, KB editing) link
  out to the Dashboard rather than reimplementing them in Slack.
- MCP server and Real-Time Search integrations (§14 — reconsidered later).

---

## 2. Why re-create the skeleton

The existing skeleton is a competent raw-FastAPI webhook service, but it was
built against a model that the challenge and the business requirement have since
moved past:

| Current skeleton | This design |
|---|---|
| Raw FastAPI webhooks, manual signature verify | **Bolt for Python** (signature, retries, OAuth, Assistant routing handled by the framework) |
| Posts plain Block Kit messages | **Assistant container**: suggested prompts, `set_status`, `chat_stream` reasoning timeline, feedback buttons |
| Calls non-existent `/investigations/turn` | Correct **case → turns** lifecycle (§8) |
| Single bot token, no install flow | **Multi-workspace OAuth** install store + per-user account linking (§10) |
| Mention + shortcut only | Mention + shortcut + **Assistant panel** + **slash commands** + **App Home** |

What we **keep** from the skeleton: the privacy posture (mention-only, no
firehose), the "thread = session" insight, `event_id` de-duplication, the
3-second-ack discipline, and graceful degradation. These are good instincts and
carry forward — Bolt just gives us better machinery to express them.

We keep the repo and its history; we replace the runtime under `main.py` /
`services/` with a Bolt application (§12), and reuse `utils/` ideas where Bolt
doesn't already cover them.

---

## 3. Architecture

```text
                         Slack Workspace (per-tenant install)
   ┌──────────────────────────────────────────────────────────────────────┐
   │  Assistant side panel │ Channel threads │ Message shortcuts │ /faultmaven │
   └───────────────┬──────────────┬───────────────┬─────────────────┬───────┘
                   │ events / interactivity / commands (HTTPS, signed)
                   ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │                 FaultMaven Slack Agent  (Bolt for Python)                │
   │                                                                          │
   │  listeners/      assistant · events · shortcuts · commands · actions     │
   │  rendering/      TurnResponse → Block Kit + chat_stream (task/plan)       │
   │  faultmaven/     REST client · account-linking OAuth · token refresh      │
   │  store/          InstallationStore · OAuthStateStore · thread→case map     │
   │                  · per-thread turn lock · user→FaultMaven-token  (Postgres)│
   │  lifecycle/      offer-to-close · auto-close-on-inactivity (background)    │
   └───────────────┬───────────────────────────────────────┬──────────────────┘
                   │ Slack Web API (chat.*, assistant.*,     │ HTTPS + Bearer (per-user
                   │ files.*; conversations.replies ONLY     │ FaultMaven OAuth token)
                   │ for the @mention catch-up read, §5.2)   ▼
                   ▼                          FaultMaven Core API (faultmaven)
            api.slack.com                     /cases · /cases/{id}/turns
                                              /cases/{id}/reports · /knowledge
                                              /auth (OAuth, multi-tenant orgs)
                                                          │
                                          Investigation engine · RAG (ChromaDB/BGE-M3)
                                          · LLM router (9 providers) · Cases/Knowledge DB
```

Deployment shape (Cloud OAuth): the agent runs as an **HTTP service** exposing
`/slack/events`, `/slack/interactions`, `/slack/commands`, `/slack/install`,
`/slack/oauth_redirect`, plus a `/health` endpoint. We use Bolt's **FastAPI
adapter** (`slack_bolt.adapter.fastapi`) so we keep async I/O and the existing
FastAPI operational surface while getting Bolt's listener ergonomics. Bolt owns
signature verification, the 3-second ack, and retry de-duplication; slow
FaultMaven calls run **inside the listener** (the Assistant `user_message`
handler streams as it works) or via Bolt **lazy listeners** for non-assistant
events — not hand-rolled Starlette background tasks.

---

## 4. Slack surface design — four entry points, one engine

All four funnel into the same `run_turn(case, input)` core. They differ only in
how the user **pings** the agent and where replies land. *How* a turn is scoped,
triggered, and serialized is the **interaction model** (§5).

### 4.1 Assistant container (the AI side panel) — primary 1:1 surface

The flagship experience, and the heart of the "Slack AI capabilities" story.
It is 1:1 and serialized by construction, so it maps **exactly** onto the
backend's single-author turn model — Slack's own recommended home for an AI
conversation.

- **`assistant_thread_started`** → greet + **FaultMaven-aware suggested prompts**:
  - "Investigate an error or stack trace" → opens an evidence-paste flow
  - "Summarize an incident thread" → catch-me-up
  - "What changed recently?" → recent-deploy correlation prompt
  - "Search our runbooks" → knowledge Q&A
- **`user_message`** → one turn against the case bound to this assistant thread:
  1. `set_status("Investigating…", loading_messages=[milestone hints])`
  2. Call FaultMaven (`/cases/{id}/turns`).
  3. Stream the result via `chat_stream` as a **reasoning timeline** (§9).
  4. Close with **suggested-action buttons** and a **feedback** block.
- Each assistant thread `thread_ts` maps 1:1 to a FaultMaven case.

### 4.2 Channel `@FaultMaven` mention — the war-room surface

For collaborative incident channels. Same engine, replies land **in-thread** so
the parent channel stays quiet. **A mention is a ping: the turn's input is the
mention message only** — FaultMaven does *not* replay the surrounding thread (see
§5.2 for why, and the one explicit exception). Strictly gesture-driven — we
subscribe to `app_mention` only, never `message.channels`.

### 4.3 Message shortcut — the universal case-opener

"Ask FaultMaven" is a registered **message shortcut** (in the ⋮
*More actions* menu of any message — *not* a slash command). It is the
**universal entry**: it works on *any* message (an alert's Block Kit payload, a
pasted stack trace, a teammate's description), and Slack hands our agent the
**full selected message** (`message.blocks` included) in the `message_action`
payload — no copy-paste, no thread read.

On invocation the agent may open a lightweight **modal** (via the payload's
`trigger_id`, within ~3 s) carrying an optional **context box** and a Block Kit
**`file_input`** — so "open the case + add what only the human knows + attach the
first evidence" is one step. It then `POST /cases` + `POST /cases/{id}/turns`
(seed = the message + modal inputs) and posts FaultMaven's first reply **threaded
under the selected message** (`thread_ts = message.ts`), starting the
investigation in place. (The shortcut itself opens nothing; *our handler* creates
the thread.)

> **Make-or-break caveat:** alert messages are rich Block Kit / attachments —
> extract readable text from `message.blocks`/`attachments`, **not** just
> `message.text` (usually a stub). See §5.4.
>
> The shortcut is the precise *opener* but is buried in the ⋮ menu; `@mention` /
> `/faultmaven` / the side panel and onboarding carry *discoverability* (§5.2).

### 4.4 Slash command — one registered command

We register **one** slash command, `/faultmaven` (one command string, one request
URL). Slack delivers everything after it as a **single `text` argument** — there
are **no sub-commands**; our handler parses the text. It is discoverable via the
`/`-autocomplete (its **usage hint** is the surface) once registered:

- `/faultmaven <describe the problem>` → start/continue an investigation
- `/faultmaven status` → the current investigation board
- `/faultmaven cases` → cases in this channel
- `/faultmaven connect` / `help` → utility (account-linking §10.2, help)

### 4.5 App Home — the "case list" equivalent

The Home tab renders cases scoped to the **channels the user is in** (and, for
admins, the whole workspace) — title, status pill, last activity, turn count,
deep links. Because Slack cases are *collaborative team artifacts*, not an
individual's private cases (§6.1), the view is **per-channel / per-workspace**,
not a global "my cases" pile.

---

## 5. Interaction model — a deliberately-invoked tool

This section defines *when* FaultMaven responds, *what* it treats as a turn, and
*how* concurrent input is handled. It is the load-bearing adaptation from the
backend's world (a serialized, single-author 1:1 conversation) to Slack's
(an asynchronous, multi-author, unlocked message stream).

### 5.1 Stance: respond to a ping, never ambient

FaultMaven is a **tool you invoke**, not a participant that decides when to speak.
A response is triggered only by an explicit **ping**: an `@mention`, a message
shortcut, a slash command, or a message in the Assistant side panel. Between
pings it is silent.

This is not a compromise — it is the convergent design of the whole ecosystem:

- **Slack's own AI-apps guidance** puts the AI conversation in the 1:1 assistant
  container "to separate it from channel noise," treats channel `@mention` as a
  *light, optional* touch, and even suggests redirecting users to the container
  to actually converse.
- **The reference AI Slackbots** respond to `app_mention` / assistant-thread /
  DM events only — *never every message*.
- **Incident-response leaders** (Rootly, incident.io) are ChatOps tools — slash
  commands, buttons, reactions, and a *scoped, explicitly-invoked* "Ask AI" — not
  ambient participants.

The cost of ambient participation (autonomously deciding when to take the floor
in a multi-party chat) is high and the payoff low: humans tolerate imperfect
turn-taking, so a deliberately-invoked agent that's occasionally out of order is
*fine*, while feeding the agent the raw cross-talk would actively harm it (§5.2).

### 5.2 Entry & context — seeded by the summon, then ping-scoped

FaultMaven gets its context from **how it was summoned** — not by reading the
channel. The opening turn is seeded by the entry gesture:

| Entry | Seed (the opening turn) | Discoverability |
|---|---|---|
| **Message shortcut** (§4.3) — the universal opener | *that specific message* (Slack delivers it) + optional modal context/file | buried in ⋮; the precise *opener* |
| **`@mention` into a thread with prior discussion** | a **one-time catch-up read** of the thread → FaultMaven synthesizes *"here's what I understand … correct?"* then leads (the lone use of `conversations.replies`; the invite authorizes it) | natural (`@`) |
| **`@mention`/message in a fresh thread or side panel; `/faultmaven`** | the mention/command text | natural |

After the opening, **every subsequent turn is ping-scoped**: the input is the
pinged message only, and FaultMaven's memory of the conversation is its **own
case history** (held server-side), not the raw thread. It never reads un-pinged
messages — no `message.channels` subscription.

Why ping-scoped (and why the catch-up read is the *only* thread read):

- **Soundness.** Feeding raw multi-party cross-talk — contradictions, tangents,
  half-formed guesses — into a *stateful* investigation pollutes it and threatens
  the "no incorrect conclusion" guarantee. The catch-up read is bounded and
  *synthesizes + confirms* rather than ingesting as truth, which is itself the
  soundness behavior.
- **Privacy.** The shortcut/slash flavors read nothing beyond what they're handed
  (no history scope); only the `@mention`-into-a-thread flavor reads that one
  thread, on explicit invitation.
- **Simplicity & concurrency.** Smaller blast radius, each turn self-contained.

> Curation is the trade-off — and a feature: point at a message (shortcut), or
> include the context in the ping; the human controls what FaultMaven sees.
> Copy-paste works everywhere but is manual — a fallback, not a designed path.

### 5.3 Concurrency — serialize turns per case

The backend processes turns **serialized and single-author** (Copilot's locked
1:1 UI guarantees this). A Slack thread is **concurrent and multi-author** — two
people can ping the same thread at once. So the agent **serializes turns per
case**: a per-`(team, channel, thread_ts)` lock ensures at most one turn is in
flight per case; concurrent pings queue and run in order.

This is justified specifically by **statefulness**. The stateless reference
Slackbots skip serialization (each response is independent), but FaultMaven
*mutates a persistent case* (turn_history, hypotheses, current_turn), so
concurrent turns would corrupt shared state. Serialization presents the backend
the one-at-a-time stream it is designed for. (The backend's turn-sequence
resilience fix makes any slip non-catastrophic; serialization keeps slips from
happening.)

**Multi-author** is handled as metadata, not a turn-model change: a turn is
attributed to the pinging Slack user; the *case* is a team artifact under the org
(§6.1). Content drives the diagnosis, not authorship, so flattening "who said it"
does not corrupt the reasoning — only the audit trail, which the metadata keeps.

### 5.4 Evidence input — consume Slack's native inputs, don't build an uploader

The Copilot had to ship a file picker, a page-capture engine, and a text-pad
because a web page provides none. Slack provides file upload, paste, and image
sharing **natively**, so our job shrinks to *consuming* them and forwarding to the
turn (§8.1):

| Evidence | Slack mechanism | → backend |
|---|---|---|
| **File(s)** | native composer (drag/drop, paperclip) **or** the opening modal's `file_input`; **multiple per turn** | download via `url_private` (`files:read`) → multipart `files` |
| **Pasted text / logs** | type/paste into the ping, or the modal text box | `pasted_content` (a *large* paste auto-becomes a Slack **snippet file** → handled on the file path) |
| **Screenshots** (dashboards, errors) | image upload | multipart `files` → backend **multimodal** (`MULTIMODAL_PROVIDER`) |
| **Existing Slack content** (an alert, a teammate's log) | the **message shortcut** on it (§4.3) | `pasted_content` — the *page-capture analog*: capture what's already in the conversation |

There is **no DOM page-capture** in Slack (no browser); its intent is covered by
shortcutting the monitoring message or a screenshot. Evidence rides on a **ping**:
attach/paste *with* an `@mention`, or in the side panel where every message is a
turn, or via the opening modal. So a single turn can carry several files at once
— richer than the Copilot's one-at-a-time. We extract readable text from a
message's `blocks`/`attachments`, not just `.text` (§4.3 caveat).

---

## 6. Case scoping & lifecycle

FaultMaven's data model is **case-centric and bounded**: a case has a lifecycle
(INQUIRY → INVESTIGATING → RESOLVED/CLOSED) and was designed for an individual
who *deliberately* resolves and closes it. A Slack thread is multi-author and
never "closes" on its own. A naïve thread→case mapping therefore produces
**eternal, ownerless, topic-mixed cases** — unbounded context, lost focus, no
runbook flywheel, growing cost. This section is how we keep every case a bounded
problem-solving unit.

### 6.1 Ownership — collaborative, not individual

Stop mapping Slack onto "an individual's private cases." In a war room a case is
a **team artifact**:

- The isolation boundary is the **workspace → FaultMaven org** (the multi-tenant
  model, §10). That is the real tenancy line.
- A case is **scoped to its thread/channel**, attributed to its participants
  (initiator + pingers) via metadata / account-linking — not owned by one person.
- App Home views are **per-channel / per-workspace** (§4.5), not a meaningless
  global "my cases."

So "all Slack users collapse to one user" stops being a bug once the meaningful
owner is the *team/channel under the org*; per-user identity is attribution
metadata, not the ownership axis.

### 6.2 The thread is the scope; the agent supplies the lifecycle drivers

**One thread = one case = one problem.** A thread is already a good topical
boundary; the gap is only that the case doesn't *terminate* with it. The backend
already has the machinery — terminal states, `closure_reason`, closure summaries,
report-on-resolution, org tenancy. What's missing are the **drivers** a Copilot
user supplied by hand. The agent supplies them:

- **Offer-to-close when resolved.** When the backend signals resolution, surface
  a proactive **DECIDE** action ("Looks resolved — close this case?"). Drives
  terminal states *and* the runbook flywheel. (Reuses the button machinery.)
- **Auto-close on inactivity.** A background job closes a case whose thread has
  gone quiet (`closure_reason="inactive"`). **Defaults (configurable):** an
  offer-to-close nudge at **~48 h** of quiet; a hard auto-close at **~7 days**.
  (Conservative, so a slow-moving incident isn't closed out from under the team.)
- **Fresh case on revival.** Pinging a closed thread starts a **new** case (the
  old one stays archived), so each case stays bounded to one active problem.
  **Default:** reopen the same case if revived within a **~48 h** grace window of
  closure; after that, a fresh case.

### 6.3 Why this is achievable, not a rewrite

The backend's bounded, lifecycle-managed case model is *correct* — we are not
forcing it to be something it isn't. The Slack agent simply owns the **lifecycle
policy** (case-per-thread, the auto-close job, offer-to-close actions,
fresh-on-revival) that the individual Copilot user provided manually. The payoff:
bounded focus, a flywheel that actually turns (cases reach RESOLVED → runbooks),
and bounded storage/cost.

---

## 7. Feature parity matrix — Copilot → Slack

| Copilot capability | Slack realization | Backend |
|---|---|---|
| Chat investigation session | Assistant thread **or** channel thread; `thread_ts` ↔ `case_id` | `POST /cases`, `POST /cases/{id}/turns` |
| Turn-based conversation | ping → one turn (scoped to the pinged message, §5.2); response streamed | `/cases/{id}/turns` |
| Suggested prompts | `assistant_thread_started` suggested prompts | static + FaultMaven hints |
| File upload (logs/configs/metrics) | native composer or modal `file_input`; **multiple per turn**; `url_private` download (§5.4) | `/cases/{id}/turns` multipart `files[]` |
| Page/DOM capture | *no DOM in Slack* → **message shortcut** captures existing content; **screenshots** → multimodal (§5.4) | `pasted_content` / multipart `files` |
| Pasted content | message text, modal text box, or shortcut body | `pasted_content` |
| Hypothesis tracker | Block Kit section + lifecycle rendered as `TaskUpdateChunk`s (pending→testing→validated/refuted) | `TurnResponse` hypotheses / `progress_transparency` |
| Suggested actions (DECIDE/RUN/EVIDENCE/FREE_SPEECH) | Interactive Block Kit buttons / prompt chips (§9.2) | `suggested_actions[]` |
| Knowledge/runbook lookup | Rendered inline with source links; deep-link to Dashboard KB | `/knowledge/...`, returned in turns |
| Evidence requests ("paste logs from X") | Prominent **EVIDENCE** call-to-action block | `suggested_actions[type=EVIDENCE]` |
| Case status lifecycle | Status pill in thread + App Home; offer-to-close / auto-close (§6.2) | `case_state`, `closure_reason` |
| Reports (resolution/closure/runbook) | On terminal state: summary + "Generate runbook" / "Download" buttons; markdown posted or uploaded as a snippet | `/cases/{id}/report-recommendations`, `/cases/{id}/reports` |
| Post-closure Q&A | Re-pinging a closed thread starts a fresh case (§6.2); the old case stays archived | `/cases/{id}/turns` |
| Case list sidebar | **App Home** (per-channel/workspace) + `/faultmaven cases` | `GET /cases` |
| Auth / login | OAuth account-linking (§10) | `/auth/...` |

---

## 8. Backend integration contract (corrected)

> Verified against the live backend on 2026-06-22:
> `faultmaven/modules/case/api/routes.py` (`create_case` @624, `submit_turn`
> @2194) and `faultmaven/models/api_models.py` (`IntentType` @321). Still confirm
> response-model field names against `api_models.py` before coding the client.

### 8.1 Case + turn lifecycle

1. **First ping in a Slack thread → create a case.**
   ```
   POST /api/v1/cases                          # JSON body (CaseCreateRequest), NOT query params
   { "title": null,                            # null → backend auto-titles (Case-MMDD-N)
     "initial_message": "<first user text>" }  # session_id intentionally omitted — see note
   → CaseSummary { case_id, title, state, ... }
   ```
   Persist `(team_id, channel_id, thread_ts) → case_id` (+ last-activity for the
   lifecycle job, §6.2) in our `thread→case` store; that local map is the source
   of truth for "which case is this thread."
   > **Do not pass the Slack `thread_ts` as `session_id`.** `create_case`
   > *validates* `session_id` against FaultMaven's session service
   > (`routes.py:647`) and rejects anything that isn't a live FaultMaven session.
   > We omit it and rely on our own thread→case map.

2. **Every turn (incl. the first) → submit a turn.**
   ```
   POST /api/v1/cases/{case_id}/turns          # multipart/form-data
     query=<text>                              # at least one of query / files /
     pasted_content=<text>                     #   pasted_content is REQUIRED (else 400)
     files=<file>...                           # optional, repeated
     input_type=file|page_capture|paste        # optional
     source_url=<url>                           # optional (shortcut/page capture)
     intent_type=conversation|status_transition|hypothesis_action|evidence_need|confirmation|greeting
     intent_data=<json>                         # for DECIDE/confirmation/hypothesis buttons
   → 200 TurnResponse   (or 202 + Location header → poll until ready)
   ```
   > Large `query` / `pasted_content` are size-guarded server-side against
   > `MAX_UPLOAD_SIZE_MB`; truncate or route very large pastes through `files`.
   > `intent_type=evidence_need` is **NOT_IMPLEMENTED** server-side today — do not
   > submit it (see §8.3).

3. **`TurnResponse` (render target):**
   ```
   { agent_response, turn_number,
     case_state: inquiry|investigating|resolved|closed,
     progress_made, milestones_completed[],
     attachments_processed[ {evidence_id, filename, data_type, processing_status} ],
     suggested_actions[ {label, type: DECIDE|RUN|EVIDENCE|FREE_SPEECH, payload?, intent?, ...} ],
     progress_transparency? { active, pending_milestone, milestone_description, repair_type } }
   ```

4. **Async turns.** A turn may return `202 Accepted` with a `Location` URL; poll
   it (exponential backoff, ~1.5×, cap ~10 s, ~5 min ceiling) until the
   `TurnResponse` is ready. This lives *behind* the Slack ack, so it is
   comfortable — we keep the user informed with `set_status` / streamed status.

### 8.2 Reports / knowledge

- `GET /api/v1/cases/{id}/report-recommendations` → which artifacts are
  available + runbook reuse/generate recommendation.
- `POST /api/v1/cases/{id}/reports` `{ report_types: [...] }` → generate
  `resolution_summary` / `closure_summary` / `runbook`.
- `GET /api/v1/cases/{id}/reports/{report_id}/download` → blob; in Slack we
  either post the markdown inline or `files.upload` it as a snippet, and link to
  the Dashboard for the canonical copy.
- Knowledge document deep-links resolve via the Dashboard URL.

### 8.3 Mapping Slack intents → FaultMaven intents

Button clicks become typed turns:

| Slack interaction | Turn intent |
|---|---|
| DECIDE button (e.g. "Transition to resolved" / "Close case") | `intent_type=status_transition`, `intent_data={to_state}` |
| Confirmation ("Yes, run it") | `intent_type=confirmation` |
| Hypothesis action (validate/refute) | `intent_type=hypothesis_action`, `intent_data={hypothesis_id, action}` |
| EVIDENCE button → user uploads/pastes | a normal `intent_type=conversation` turn with `files`/`pasted_content` — **not** `evidence_need` (NOT_IMPLEMENTED server-side) |
| Plain message / FREE_SPEECH chip | `intent_type=conversation` |

---

## 9. The reasoning timeline — our signature UX

This is where "Slack AI capabilities" and FaultMaven's depth meet, and where we
win the Design and Technological-Implementation criteria.

### 9.1 Milestones & hypotheses → `chat_stream` chunks

FaultMaven's investigation is milestone-based with a hypothesis lifecycle.
That maps directly onto the template's streaming primitives:

- `set_status("Investigating…", loading_messages=[...])` while the turn runs.
- Render `milestones_completed` + `progress_transparency.pending_milestone` as
  **`TaskUpdateChunk`s** on a `task_display_mode="timeline"` stream:
  `Triaging symptoms ✓ · Forming hypotheses ⟳ · Requesting evidence …`.
- Render each hypothesis as a task with status `pending|in_progress|complete`
  (testing → validated/refuted), so the user literally watches the diagnosis
  take shape.
- Stream `agent_response` markdown as **`MarkdownTextChunk`s**.
- Close the stream with the **suggested-action** and **feedback** blocks.

**v1 (pragmatic):** `/cases/{id}/turns` is request/response, so we synthesize the
timeline from `milestones_completed` + `progress_transparency` after the turn
returns, plus live `set_status` during it.
**v2 (true streaming, optional backend ask):** consume the backend's SSE agent
execution (`POST /cases/{id}/sessions/{sid}/execute`, events
`started/thinking/tool_call/response/completed`) and forward `tool_call` events
(`global_kb_qa`, `case_evidence_qa`, …) as live `TaskUpdateChunk`s. See §15.

### 9.2 Suggested actions → interactive Block Kit

| `type` | Slack rendering | On click |
|---|---|---|
| `DECIDE` | Primary button with the pre-composed label | Submit a typed turn (status_transition/confirmation) |
| `RUN` | Fenced code block + "Mark as run / paste output" affordance | Prompt the user for command output as the next evidence turn |
| `EVIDENCE` | Highlighted "FaultMaven needs…" block with acquisition hints | Open a file/paste flow for that evidence |
| `FREE_SPEECH` | Suggested-prompt chip | Pre-fill the next message |

> Slack bots cannot copy to a user's clipboard, so `RUN` renders the command as
> a selectable code block rather than a clipboard button — the honest Slack
> equivalent.

### 9.3 Honoring the soundness guarantees

FaultMaven guarantees **no incorrect conclusion** and **no collapse under
pressure**. The rendering layer enforces this:

- While `case_state == investigating` and `suggested_actions` include `EVIDENCE`,
  the agent surfaces the **missing data** prominently and does **not** render a
  resolution — even if a user pushes for one. "Name what's missing" is a feature,
  not a failure.
- Confidence and case state are always shown, so users never mistake an
  in-progress hypothesis for a verdict.
- Terminal summaries (resolution/closure) are only offered when the backend
  reports a terminal `case_state` — the Slack layer never fabricates closure.
- Ping-scoped context (§5.2) is itself a soundness measure: it keeps contradictory
  cross-talk out of the engine's input.

---

## 10. Auth & multi-tenancy (Cloud OAuth, multi-workspace)

Two OAuth flows, each doing a distinct job. Mapping: **Slack workspace ↔
FaultMaven organization** (the tenant boundary); **Slack user ↔ FaultMaven user**
(attribution metadata, per §6.1 — not the case-ownership axis).

### 10.1 Slack app installation (workspace-level)

- Standard Slack OAuth (`/slack/install` → `/slack/oauth_redirect`) using Bolt's
  `OAuthSettings` with a **Postgres-backed `InstallationStore`** and
  `OAuthStateStore` (the template's `FileInstallationStore` is dev-only).
- Yields the per-team bot token (`xoxb`), `team_id`, installer identity.
- During install, the installing **org admin links the workspace to a FaultMaven
  organization** (a one-time FaultMaven OAuth/admin step), establishing
  `team_id → faultmaven_org_id`.

### 10.2 FaultMaven account linking (per user)

Reuses the Copilot's proven PKCE flow (`client_id=faultmaven-copilot`, scopes
`openid profile email cases:read cases:write knowledge:read evidence:read`):

1. First time a Slack user invokes the agent without a linked token, the agent
   posts an **ephemeral "Connect your FaultMaven account"** button
   (also `/faultmaven connect`).
2. Button opens the FaultMaven authorize URL → user logs in/consents →
   redirect to our public callback → exchange `code + code_verifier` for
   access/refresh tokens.
3. Store tokens **per `(team_id, slack_user_id)`**; refresh ahead of expiry
   (mirror the Copilot's `TokenManager`, ~5-min early refresh).
4. Turns made by that user attach **their** FaultMaven bearer token, for
   attribution and per-user KB.

**War-room fallback (avoid collapse-under-pressure for UX):** in a shared
incident thread, requiring every participant to link before the agent responds
would stall the room. So unlinked users' turns run under a **workspace service
identity** (the team→org binding), attributed to the Slack user in metadata. The
*case* always lives in the bound org — never cross-tenant.

> **Gate destructive/global actions on a personal token.** The service identity
> is fine for read + investigate turns, but mutations with blast radius beyond
> the case — modifying the global/team knowledge base, deleting reports,
> reclassifying evidence — **require an authenticated personal user token**.

### 10.3 Tenant isolation

- Every FaultMaven call carries a token scoped to the bound org; `org_id` is
  derived from the token server-side (never passed by us).
- Our stores (installations, tokens, thread→case map) are keyed by `team_id`;
  no cross-team reads.
- This rides FaultMaven's existing multi-tenant RLS model
  (workspace = org = tenant boundary).

---

## 11. Privacy & security posture

- **Strict mention/ping-only.** Subscribe to `app_mention`, `assistant_thread_*`,
  `message.im` (the 1:1 assistant DM), shortcuts, and commands — **never**
  `message.channels`. No background ingestion; every read is a direct response
  to an explicit ping, scoped to the pinged message (§5.2).
- **Least-privilege scopes (core):** `app_mentions:read`, `assistant:write`,
  `chat:write`, `im:history`, `commands`, `files:read` (download attached
  evidence). The shortcut/slash flavors need **no** thread-read; the
  `@mention`-into-a-thread **catch-up read** (§5.2) is the only path that reads a
  thread, and the *only* reason we request `channels:history`/`groups:history` —
  used solely on that explicit invitation.
- **Request authenticity** handled by Bolt (signing secret + timestamp/replay).
- **Secrets** via env/secret store; never logged. Evidence is forwarded to
  FaultMaven (which already runs Presidio PII redaction + the preprocessing
  pipeline) — the Slack agent does not persist evidence content itself.
- **The catch-up read** (the one place we call `conversations.replies`, a
  Tier-3 method) is cached keyed by
  `(channel, thread_ts, latest_ts)` and fetches only messages since the last
  cached `ts`, to stay under rate limits during a busy outage.
- **`not_in_channel`** → reply with a clear `/invite @FaultMaven` prompt instead
  of failing silently.

---

## 12. Project structure (rebuilt on Bolt)

```text
faultmaven-slack-agent/
├── app.py                      # Bolt App + FastAPI adapter; OAuthSettings; register listeners
├── manifest.json               # scopes, events, assistant_view, slash commands, shortcuts
├── config.py                   # settings (kept, extended): lifecycle windows, etc.
├── listeners/
│   ├── assistant/              # thread_started (suggested prompts) · user_message (turn)
│   ├── events/                 # app_mention
│   ├── shortcuts/              # "Ask FaultMaven" message shortcut
│   ├── commands/               # one /faultmaven command; handler parses the text arg
│   ├── actions/                # suggested-action buttons (incl. close) · feedback
│   └── views/                  # App Home (per-channel case list) · modals
├── rendering/
│   ├── timeline.py             # TurnResponse → chat_stream task/plan chunks
│   └── blocks.py               # hypotheses, suggested actions, status, reports → Block Kit
├── faultmaven/
│   ├── client.py               # async REST: cases, turns (multipart), reports, knowledge
│   ├── oauth.py                # FaultMaven PKCE account-linking + token refresh
│   └── mapping.py              # thread → case lookup/create
├── store/
│   ├── installations.py        # Postgres InstallationStore + OAuthStateStore
│   ├── tokens.py               # per-(team,user) FaultMaven token store
│   ├── cases.py                # (team,channel,thread_ts) → case_id + last_activity
│   └── locks.py                # per-thread turn serialization (§5.3)
├── lifecycle/
│   └── reaper.py               # background auto-close-on-inactivity job (§6.2)
├── requirements.txt            # slack-bolt, slack-sdk, fastapi, httpx, sqlalchemy, redis
└── docs/design.md              # this file
```

`utils/security.py` from the skeleton is largely **subsumed by Bolt**; we retain
only any replay/observability extras Bolt doesn't already give us.

---

## 13. Challenge submission alignment

| Judging criterion | How we score |
|---|---|
| **Technological Implementation** | Full Slack AI-App stack (Assistant container, streaming `chat_stream` task/plan timeline, `set_status`, feedback) over a real multi-provider RAG investigation engine; multi-workspace OAuth with clean tenant isolation; (optional) live SSE tool-use streaming. |
| **Design / UX** | The reasoning timeline turns opaque AI into a watchable diagnosis; suggested-action buttons make next steps one click; per-channel App Home; in-thread hygiene + ping-scoped focus keep channels quiet and answers on-point. |
| **Potential Impact** | Faster MTTR where incidents already live; the knowledge flywheel — every resolved case becomes a reusable runbook (the lifecycle drivers in §6.2 are what make it actually turn); honest "name the missing data" behavior builds trust. |
| **Quality of Idea** | A human-in-the-loop, deliberately-invoked copilot (not an autopilot, not an ambient chatbot) with explicit soundness guarantees and a privacy-first posture — differentiated from alerting bots. |

**Track:** *Slack Agent for Organizations* (multi-workspace OAuth).
**Deliverables checklist:** ~3-min demo video, this architecture diagram (§3),
a developer **sandbox URL granting `slackhack@salesforce.com` and
`testing@devpost.com`**, and the Slack App ID if pursuing Marketplace.

**Demo script (3 min):** (1) `@FaultMaven` a failing-service thread with a pasted
stack trace → watch the reasoning timeline form hypotheses and request a log;
(2) drag in a log file → hypothesis validates, root cause identified;
(3) click **Generate runbook** → resolution summary + reusable runbook posted;
(4) open the Assistant side panel and `/faultmaven cases` to show the portfolio.

---

## 14. Optional Slack tech — considered and deferred

- **MCP server.** FaultMaven already exposes agent tools internally
  (`global_kb_qa`, `case_evidence_qa`, …). Wrapping them as MCP is attractive for
  reuse, **but the Slack agent's business value rides the REST `/turns` path**,
  which carries the case state machine, evidence pipeline, and milestone
  semantics that a raw MCP tool call would bypass. **Deferred.**
- **Real-Time Search API.** Searching prior incidents across the workspace is
  appealing for "catch-me-up," but it **widens the privacy surface** beyond the
  ping-only posture and overlaps FaultMaven's own RAG over its curated KB.
  **Deferred** until the privacy trade-off is explicitly reviewed.

Anchoring on **Slack AI capabilities** alone satisfies the challenge's
"at least one technology" rule while keeping the design optimal for the business
requirement.

---

## 15. Open questions / backend asks

1. **Streaming turn endpoint.** A token-streaming variant of `/cases/{id}/turns`
   (or a documented way to drive the SSE `…/execute` path through the case state
   machine) would let us stream real tokens + tool calls into `chat_stream`
   instead of synthesizing the timeline post-hoc. Confirms §9.1 v2.
2. **Workspace→org binding API.** What is the cleanest FaultMaven call to bind a
   Slack `team_id` to an org at install time (admin OAuth vs. service token vs.
   provisioning endpoint)?
3. **Service identity for the war-room fallback.** Is there a first-class
   service-account token type (§10.2), or do we mint a per-workspace technical
   user?
4. **Collaborative case ownership.** The case model is single-`user_id`-owned;
   confirm the cleanest way to represent a *team/thread-owned* case (initiator as
   owner + participant metadata?) so attribution is faithful without per-user
   isolation getting in the way (§6.1).
5. **Exact `TurnResponse` / `CaseCreate` schemas.** Confirm field names against
   `faultmaven/models/api_models.py` before coding the client.

---

## 16. Phased roadmap

1. **P0 — Foundation.** Bolt app on FastAPI adapter; manifest; Postgres
   installation/state stores; `/health`; correct `faultmaven/client.py` (case +
   multipart turns); thread→case map. *Exit: `@FaultMaven` in a thread runs one
   real turn end-to-end.*
2. **P1 — Assistant container.** `assistant_thread_started` prompts;
   `user_message` turn; `set_status`; result blocks + feedback. *Exit: side-panel
   investigation works.*
3. **P2 — Reasoning timeline + actions.** `chat_stream` task/plan rendering from
   milestones/hypotheses; suggested-action buttons → typed intents; EVIDENCE/RUN
   flows. *Exit: signature UX demoable.*
4. **P3 — Flagship entry + evidence.** The **"Ask FaultMaven"
   message shortcut** as the universal case-opener (+ context/`file_input`
   modal); seed-by-summon + ping-scoped turns; **per-thread serialization**
   (§5.3); evidence consumption — files (`url_private`), paste, screenshots
   (multimodal), shortcut-existing-content (§5.4).
5. **P4 — Case lifecycle + reports + App Home.** Offer-to-close / auto-close-on-
   inactivity / fresh-on-revival (§6.2); terminal-state report generation;
   per-channel App Home; `/faultmaven cases`.
6. **P5 — Multi-tenant OAuth hardening + war-room entry.** Per-user account
   linking + refresh; workspace→org binding; war-room fallback; `not_in_channel`;
   the `@mention` catch-up read (the one history-scope feature, §5.2).
7. **P6 — Submission polish.** Demo video, sandbox grants, architecture diagram,
   README/setup.
```
