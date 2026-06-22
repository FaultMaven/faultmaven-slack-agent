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
| Required Slack tech | **Slack AI capabilities only** | Assistant container, suggested prompts, streaming, `set_status`, feedback. Directly serves the business need. MCP / Real-Time Search deferred (§12). |
| Deploy / auth | **Cloud OAuth, multi-workspace** → "Agent for Organizations" track | Slack workspace ↔ FaultMaven organization; per-user FaultMaven account linking. |
| Privacy posture | **Keep strict mention-only** — no `message.channels` firehose | An enterprise-trust strength that is fully compatible with the AI-App UX. |
| Backend integration | **FaultMaven REST** (`/cases`, `/cases/{id}/turns`, `/reports`, `/knowledge`) | Carries the case state machine, evidence pipeline, and milestones. |

### The one bug to fix no matter what

The current skeleton posts to **`POST /api/v1/investigations/turn`, which does
not exist.** The real contract is: **create a case**
(`POST /api/v1/cases`) → **submit turns**
(`POST /api/v1/cases/{case_id}/turns`, *multipart form-data*). See §6.

---

## 1. Goals and non-goals

**Goals**

- Parity with the Copilot's user-facing capabilities, re-expressed in Slack's
  primitives (§5).
- A first-class AI-App experience: suggested prompts, live status, streamed
  reasoning, hypotheses, and actionable next steps.
- Multi-workspace cloud deployment with clean tenant isolation
  (workspace ↔ org).
- Honor FaultMaven's two soundness guarantees in every rendered turn (§7.3):
  **never present an incorrect conclusion; never collapse under pressure** —
  when data is inadequate, keep engaging and *name the missing data*.

**Non-goals (v1)**

- Reading background channel traffic / proactive interjection (privacy posture).
- Replacing the Dashboard — deep heavy artifacts (full reports, KB editing) link
  out to the Dashboard rather than reimplementing them in Slack.
- MCP server and Real-Time Search integrations (§12 — reconsidered later).

---

## 2. Why re-create the skeleton

The existing skeleton is a competent raw-FastAPI webhook service, but it was
built against a model that the challenge and the business requirement have since
moved past:

| Current skeleton | This design |
|---|---|
| Raw FastAPI webhooks, manual signature verify | **Bolt for Python** (signature, retries, OAuth, Assistant routing handled by the framework) |
| Posts plain Block Kit messages | **Assistant container**: suggested prompts, `set_status`, `chat_stream` reasoning timeline, feedback buttons |
| Calls non-existent `/investigations/turn` | Correct **case → turns** lifecycle (§6) |
| Single bot token, no install flow | **Multi-workspace OAuth** install store + per-user account linking (§8) |
| Mention + shortcut only | Mention + shortcut + **Assistant panel** + **slash commands** + **App Home** |

What we **keep** from the skeleton: the privacy posture (mention-only, no
firehose), the "thread = session" insight, `event_id` de-duplication, the
3-second-ack discipline, and graceful degradation. These are good instincts and
carry forward — Bolt just gives us better machinery to express them.

We keep the repo and its history; we replace the runtime under `main.py` /
`services/` with a Bolt application (§10), and reuse `utils/` ideas where Bolt
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
   │                  · user→FaultMaven-token store        (Postgres)          │
   └───────────────┬───────────────────────────────────────┬──────────────────┘
                   │ Slack Web API (chat.*, assistant.*,     │ HTTPS + Bearer (per-user
                   │ files.*, conversations.replies)         │ FaultMaven OAuth token)
                   ▼                                          ▼
            api.slack.com                         FaultMaven Core API (faultmaven)
                                                  /cases · /cases/{id}/turns
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
how the user summons the agent and where replies land.

### 4.1 Assistant container (the AI side panel) — primary 1:1 surface

The flagship experience, and the heart of the "Slack AI capabilities" story.

- **`assistant_thread_started`** → greet + **FaultMaven-aware suggested prompts**:
  - "Investigate an error or stack trace" → opens an evidence-paste flow
  - "Summarize an incident thread" → catch-me-up
  - "What changed recently?" → recent-deploy correlation prompt
  - "Search our runbooks" → knowledge Q&A
- **`user_message`** → one turn against the case bound to this assistant thread:
  1. `set_status("Investigating…", loading_messages=[milestone hints])`
  2. Call FaultMaven (`/cases/{id}/turns`).
  3. Stream the result via `chat_stream` as a **reasoning timeline** (§7).
  4. Close with **suggested-action buttons** and a **feedback** block.
- Each assistant thread `thread_ts` maps 1:1 to a FaultMaven case.

### 4.2 Channel `@FaultMaven` mention — the war-room surface

For collaborative incident channels. Same engine, replies land **in-thread** so
the parent channel stays quiet. The summoned thread's prior replies are replayed
as conversation context (`conversations.replies`). Strictly gesture-driven — we
subscribe to `app_mention` only, never `message.channels`.

### 4.3 Message shortcut — "Investigate with FaultMaven"

The DOM-scrape replacement. When a monitoring bot or teammate drops a traceback,
anyone clicks **More actions → Investigate with FaultMaven** to hand *that one
message* (text + any file attachments) to the engine as evidence — no
channel-wide reading. Captured as `input_type=paste`/`page_capture`.

### 4.4 Slash commands — quick control

- `/faultmaven <question>` — start/continue an investigation from anywhere.
- `/faultmaven cases` — ephemeral list of my open cases (deep links to threads /
  Dashboard).
- `/faultmaven connect` — link my FaultMaven account (§8.2).
- `/faultmaven help` — capabilities + privacy summary.

### 4.5 App Home — the "case list sidebar" equivalent

The Home tab renders the signed-in user's cases (title, status pill, last
activity, turn count) with deep links — the Slack analogue of the Copilot's case
sidebar.

---

## 5. Feature parity matrix — Copilot → Slack

| Copilot capability | Slack realization | Backend |
|---|---|---|
| Chat investigation session | Assistant thread **or** channel thread; `thread_ts` ↔ `case_id` | `POST /cases`, `POST /cases/{id}/turns` |
| Turn-based conversation | message → one turn; response streamed | `/cases/{id}/turns` |
| Suggested prompts | `assistant_thread_started` suggested prompts | static + FaultMaven hints |
| File upload (logs/configs/metrics) | Slack file in message/shortcut → `files.info` + `url_private` download → multipart `files[]` | `/cases/{id}/turns` (multipart) |
| Page/DOM capture | **Message shortcut** captures the target message as evidence | `input_type=page_capture`, `source_url` |
| Pasted content | message text or shortcut body | `pasted_content` |
| Hypothesis tracker | Block Kit section + lifecycle rendered as `TaskUpdateChunk`s (pending→testing→validated/refuted) | `TurnResponse` hypotheses / `progress_transparency` |
| Suggested actions (DECIDE/RUN/EVIDENCE/FREE_SPEECH) | Interactive Block Kit buttons / prompt chips (§7.2) | `suggested_actions[]` |
| Knowledge/runbook lookup | Rendered inline with source links; deep-link to Dashboard KB | `/knowledge/...`, returned in turns |
| Evidence requests ("paste logs from X") | Prominent **EVIDENCE** call-to-action block | `suggested_actions[type=EVIDENCE]` |
| Case status lifecycle | Status pill in thread header + App Home | `case_state` |
| Reports (resolution/closure/runbook) | On terminal state: summary + "Generate runbook" / "Download" buttons; markdown posted or uploaded as a snippet | `/cases/{id}/report-recommendations`, `/cases/{id}/reports` |
| Post-closure Q&A | Thread stays open; turns still allowed | `/cases/{id}/turns` |
| Case list sidebar | **App Home** tab + `/faultmaven cases` | `GET /cases` |
| Auth / login | OAuth account-linking (§8) | `/auth/...` |

---

## 6. Backend integration contract (corrected)

> Verified against the live backend on 2026-06-22:
> `faultmaven/modules/case/api/routes.py` (`create_case` @624, `submit_turn`
> @2194) and `faultmaven/models/api_models.py` (`IntentType` @321). Still confirm
> response-model field names against `api_models.py` before coding the client.

### 6.1 Case + turn lifecycle

1. **First turn in a Slack thread → create a case.**
   ```
   POST /api/v1/cases                          # JSON body (CaseCreateRequest), NOT query params
   { "title": null,                            # null → backend auto-titles (Case-MMDD-N)
     "initial_message": "<first user text>" }  # session_id intentionally omitted — see note
   → CaseSummary { case_id, title, state, ... }
   ```
   Persist `(team_id, channel_id, thread_ts) → case_id` in our `thread→case`
   store; that local map is the source of truth for "which case is this thread."
   > **Do not pass the Slack `thread_ts` as `session_id`.** `create_case`
   > *validates* `session_id` against FaultMaven's session service
   > (`routes.py:647`) and rejects anything that isn't a live FaultMaven session.
   > We omit it and rely on our own thread→case map. If a flow later needs a
   > FaultMaven session, create one per `(team, user)` and reuse it.

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
   > submit it (see §6.3).

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

### 6.2 Reports / knowledge

- `GET /api/v1/cases/{id}/report-recommendations` → which artifacts are
  available + runbook reuse/generate recommendation.
- `POST /api/v1/cases/{id}/reports` `{ report_types: [...] }` → generate
  `resolution_summary` / `closure_summary` / `runbook`.
- `GET /api/v1/cases/{id}/reports/{report_id}/download` → blob; in Slack we
  either post the markdown inline or `files.upload` it as a snippet, and link to
  the Dashboard for the canonical copy.
- Knowledge document deep-links resolve via the Dashboard URL.

### 6.3 Mapping Slack intents → FaultMaven intents

Button clicks become typed turns:

| Slack interaction | Turn intent |
|---|---|
| DECIDE button (e.g. "Transition to resolved") | `intent_type=status_transition`, `intent_data={to_state}` |
| Confirmation ("Yes, run it") | `intent_type=confirmation` |
| Hypothesis action (validate/refute) | `intent_type=hypothesis_action`, `intent_data={hypothesis_id, action}` |
| EVIDENCE button → user uploads/pastes | a normal `intent_type=conversation` turn with `files`/`pasted_content` — **not** `evidence_need` (NOT_IMPLEMENTED server-side) |
| Plain message / FREE_SPEECH chip | `intent_type=conversation` |

---

## 7. The reasoning timeline — our signature UX

This is where "Slack AI capabilities" and FaultMaven's depth meet, and where we
win the Design and Technological-Implementation criteria.

### 7.1 Milestones & hypotheses → `chat_stream` chunks

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
(`global_kb_qa`, `case_evidence_qa`, …) as live `TaskUpdateChunk`s — showing
FaultMaven's actual tool use inside Slack. See §13.

### 7.2 Suggested actions → interactive Block Kit

| `type` | Slack rendering | On click |
|---|---|---|
| `DECIDE` | Primary button with the pre-composed label | Submit a typed turn (status_transition/confirmation) |
| `RUN` | Fenced code block + "Mark as run / paste output" affordance | Prompt the user for command output as the next evidence turn |
| `EVIDENCE` | Highlighted "FaultMaven needs…" block with acquisition hints | Open a file/paste flow for that evidence |
| `FREE_SPEECH` | Suggested-prompt chip | Pre-fill the next message |

> Slack bots cannot copy to a user's clipboard, so `RUN` renders the command as
> a selectable code block rather than a clipboard button — the honest Slack
> equivalent.

### 7.3 Honoring the soundness guarantees

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

---

## 8. Auth & multi-tenancy (Cloud OAuth, multi-workspace)

Two OAuth flows, each doing a distinct job. Mapping: **Slack workspace ↔
FaultMaven organization**; **Slack user ↔ FaultMaven user** (for attribution and
per-user knowledge).

### 8.1 Slack app installation (workspace-level)

- Standard Slack OAuth (`/slack/install` → `/slack/oauth_redirect`) using Bolt's
  `OAuthSettings` with a **Postgres-backed `InstallationStore`** and
  `OAuthStateStore` (the template's `FileInstallationStore` is dev-only).
- Yields the per-team bot token (`xoxb`), `team_id`, installer identity.
- During install, the installing **org admin links the workspace to a FaultMaven
  organization** (a one-time FaultMaven OAuth/admin step), establishing
  `team_id → faultmaven_org_id`.

### 8.2 FaultMaven account linking (per user)

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
4. Turns made by that user attach **their** FaultMaven bearer token, so cases are
   correctly attributed and per-user KB applies.

**War-room fallback (avoid collapse-under-pressure for UX):** in a shared
incident thread, requiring every participant to link before the agent responds
would stall the room. So unlinked users' turns run under a **workspace service
identity** (the team→org binding), attributed to the Slack user in metadata, and
the agent gently nudges them to link for full personalization. The *case* always
lives in the bound org — never cross-tenant.

> **Gate destructive/global actions on a personal token.** The service identity
> is fine for read + investigate turns, but mutations with blast radius beyond
> the case — modifying the global/team knowledge base, deleting reports,
> reclassifying evidence — **require an authenticated personal user token**. An
> unlinked war-room participant can investigate; they cannot quietly alter shared
> state under the service identity.

### 8.3 Tenant isolation

- Every FaultMaven call carries a token scoped to the bound org; `org_id` is
  derived from the token server-side (never passed by us).
- Our stores (installations, tokens, thread→case map) are keyed by `team_id`;
  no cross-team reads.
- This rides FaultMaven's existing multi-tenant RLS model
  (workspace = org = tenant boundary).

---

## 9. Privacy & security posture

- **Strict mention-only.** Subscribe to `app_mention`, `assistant_thread_*`,
  `message.im` (the 1:1 assistant DM), shortcuts, and commands — **never**
  `message.channels`. No background ingestion; every read is a direct response
  to an explicit user gesture, scoped to the summoned thread/message.
- **Least-privilege scopes:** `assistant:write`, `chat:write`,
  `app_mentions:read`, `im:history`, `channels:history`/`groups:history` (only to
  replay a *summoned* thread), `files:read` (download attached evidence),
  `commands`. No `users:read.email` unless account-matching requires it.
- **Request authenticity** handled by Bolt (signing secret + timestamp/replay).
- **Secrets** via env/secret store; never logged. Evidence is forwarded to
  FaultMaven (which already runs Presidio PII redaction + the preprocessing
  pipeline) — the Slack agent does not persist evidence content itself.
- **History reads are Tier-3 / rate-limited** — cache replayed thread context
  keyed by `(channel, thread_ts, latest_ts)` (Redis) before real traffic, and on
  a cache hit fetch only messages *since* the last cached `ts` rather than
  re-reading the whole thread. A single outage can spawn many concurrent threads;
  this keeps us under quota.
- **`not_in_channel`** → reply with a clear `/invite @FaultMaven` prompt instead
  of failing silently.

---

## 10. Project structure (rebuilt on Bolt)

```text
faultmaven-slack-agent/
├── app.py                      # Bolt App + FastAPI adapter; OAuthSettings; register listeners
├── manifest.json               # scopes, events, assistant_view, slash commands, shortcuts
├── config.py                   # settings (kept, extended)
├── listeners/
│   ├── assistant/              # thread_started (suggested prompts) · user_message (turn)
│   ├── events/                 # app_mention
│   ├── shortcuts/              # "Investigate with FaultMaven" message shortcut
│   ├── commands/               # /faultmaven {question|cases|connect|help}
│   ├── actions/                # suggested-action buttons · feedback
│   └── views/                  # App Home (case list) · modals
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
│   └── cases.py                # (team,channel,thread_ts) → case_id
├── requirements.txt            # slack-bolt, slack-sdk, fastapi, httpx, sqlalchemy, redis
└── docs/design.md              # this file
```

`utils/security.py` from the skeleton is largely **subsumed by Bolt**; we retain
only any replay/observability extras Bolt doesn't already give us.

---

## 11. Challenge submission alignment

| Judging criterion | How we score |
|---|---|
| **Technological Implementation** | Full Slack AI-App stack (Assistant container, streaming `chat_stream` task/plan timeline, `set_status`, feedback) over a real multi-provider RAG investigation engine; multi-workspace OAuth with clean tenant isolation; (optional) live SSE tool-use streaming. |
| **Design / UX** | The reasoning timeline turns opaque AI into a watchable diagnosis; suggested-action buttons make next steps one click; App Home case list; in-thread hygiene keeps channels quiet. |
| **Potential Impact** | Faster MTTR where incidents already live; the knowledge flywheel — every resolved case becomes a reusable runbook; honest "name the missing data" behavior builds trust. |
| **Quality of Idea** | A human-in-the-loop copilot (not an autopilot) with explicit soundness guarantees and a privacy-first posture — differentiated from alerting bots. |

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

## 12. Optional Slack tech — considered and deferred

- **MCP server.** FaultMaven already exposes agent tools internally
  (`global_kb_qa`, `case_evidence_qa`, …). Wrapping them as MCP is attractive for
  reuse, **but the Slack agent's business value rides the REST `/turns` path**,
  which carries the case state machine, evidence pipeline, and milestone
  semantics that a raw MCP tool call would bypass. MCP would be a *separate*
  FaultMaven offering, not the right integration here. **Deferred.**
- **Real-Time Search API.** Searching prior incidents across the workspace is
  appealing for "catch-me-up," but it **widens the privacy surface** beyond the
  strict mention-only posture and overlaps FaultMaven's own RAG over its curated
  KB. **Deferred** until the privacy trade-off is explicitly reviewed.

Anchoring on **Slack AI capabilities** alone satisfies the challenge's
"at least one technology" rule while keeping the design optimal for the business
requirement.

---

## 13. Open questions / backend asks

1. **Streaming turn endpoint.** A token-streaming variant of `/cases/{id}/turns`
   (or a documented way to drive the SSE `…/execute` path through the case state
   machine) would let us stream real tokens + tool calls into `chat_stream`
   instead of synthesizing the timeline post-hoc. Confirms §7.1 v2.
2. **Workspace→org binding API.** What is the cleanest FaultMaven call to bind a
   Slack `team_id` to an org at install time (admin OAuth vs. service token vs.
   provisioning endpoint)?
3. **Exact `TurnResponse` / `CaseCreate` schemas.** Confirm field names
   (`session_id` support on create, `suggested_actions` shape, hypotheses
   location) against `faultmaven/models/api_models.py` before coding the client.
4. **Bot/service identity in FaultMaven.** Is there a first-class service-account
   token type for the war-room fallback (§8.2), or do we mint a per-workspace
   technical user?

---

## 14. Phased roadmap

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
4. **P3 — Evidence + shortcuts.** File download → multipart; "Investigate with
   FaultMaven" shortcut; page-capture inputs.
5. **P4 — Reports + App Home.** Terminal-state report generation/download;
   App Home case list; `/faultmaven cases`.
6. **P5 — Multi-tenant OAuth hardening.** Per-user account linking + refresh;
   workspace→org binding; war-room fallback; history caching; `not_in_channel`.
7. **P6 — Submission polish.** Demo video, sandbox grants, architecture diagram,
   README/setup.
```
