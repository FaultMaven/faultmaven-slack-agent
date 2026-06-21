# Product Blueprint — FaultMaven Slack Agent

> The strategy behind porting the [FaultMaven Copilot browser extension](https://github.com/FaultMaven/faultmaven-copilot)
> into a production-grade Slack agent, and how that strategy maps onto the code
> in this repository.
>
> This document describes the **current design and its roadmap**. Where a
> capability is aspirational rather than shipped, it is marked. See
> [§7 Implementation Status](#7-implementation-status) for the ground truth of
> what runs today.

---

## 1. Core Philosophy: Copilot, Not Autopilot

FaultMaven Slack Edition is a **human-in-the-loop debugging partner**, not an
automated incident responder. Autopilot tools that act on raw signal tend to
generate unvetted noise and alert fatigue; FaultMaven deliberately does the
opposite.

- **Driven by human intent.** The agent never proactively interjects and never
  scrapes background chatter. It stays silent until an engineer explicitly
  pulls it into the flow.
- **The "on-call specialist" persona.** Think of an expert sitting in the
  corner of the incident room — responsive, data-hungry, and context-aware,
  but entirely passive until summoned.

This is the **strict mention-only ("Option B")** posture, and it is enforced in
code: the service subscribes only to `app_mention` events and message
shortcuts. It holds no subscription to channel message streams.

---

## 2. Data Ingestion & Workspace Privacy

To clear enterprise compliance review (SOC 2 / GDPR) and keep infrastructure
cost bounded, FaultMaven rejects the "firehose." It does **not** stream or store
continuous channel data. Data is read only in direct response to a user
gesture, and only the slice needed for that turn.

```text
[ Human trigger ]
      │
      ├─ @FaultMaven mention ───────────┐
      │                                 ├─► Slack webhook ─► verify signature ─► ACK <3s
      └─ "Investigate with FaultMaven" ─┘                                          │
         message shortcut                                                          ▼
                                                            BackgroundTask: read thread → RAG → reply
```

**Data-on-demand:** the backend touches Slack data only when a user authorizes
it by mentioning the bot or invoking the shortcut. Nothing is ingested
speculatively.

### Slack scopes

| Scope | Purpose | Status |
|-------|---------|--------|
| `app_mentions:read` | Receive explicit `@FaultMaven` mentions | **Shipped** |
| `chat:write` | Post replies and Block Kit results in-thread | **Shipped** |
| `channels:history`, `groups:history` | Read the *summoned* thread's replies for context | **Shipped** |
| `commands` | Custom slash commands (e.g. `/faultmaven runbook`) | Planned |
| `reactions:write` | Acknowledge with an 👀 reaction on receipt | Planned |

> **Least privilege.** We request `history` scopes because reconstructing a
> thread's context requires `conversations.replies`. We do **not** request
> them to read channels the bot was never invited into — reads are always
> scoped to the thread the user pointed us at.

---

## 3. The Core Feature Toolkit

### A. Multi-turn thread sessions — `thread_ts` as session ID  *(Shipped)*

Troubleshooting is a dialogue: detect, request logs, hypothesize, confirm.
FaultMaven treats the Slack `thread_ts` as a **stateful session ID** mapped
one-to-one onto a FaultMaven investigation.

- Every reply lands **inside the originating thread**, keeping the parent
  channel quiet.
- Before each turn, the backend reads prior thread replies (`conversations.replies`)
  and replays them as conversation history so the engine has full memory.
- This is the spine of the agent — see [`services/slack_service.py`](../services/slack_service.py)
  (`fetch_thread_history`) and [`main.py`](../main.py) (`_run_investigation`).

### B. Message-action ingestion — the DOM-scrape replacement  *(Shipped)*

The browser extension scrapes stack traces from the page DOM. Slack has no DOM,
so we use **Message Shortcuts** (the message's *More actions* menu). When a
monitoring bot or a teammate drops a messy traceback into a channel, anyone can
click **Investigate with FaultMaven** to isolate *that one payload* and hand it
straight to the agent — no channel-wide reading required. Handled by the
`message_action` branch in [`main.py`](../main.py) (`_handle_shortcut`).

### C. "Catch-Me-Up" contextualizer  *(Planned)*

Engineers joining a war room mid-incident lose minutes scrolling to reconstruct
events. The planned flow: `@FaultMaven summarize this thread` reads the summoned
thread, strips chatter, runs it through the RAG pipeline, and returns a clean,
bulleted diagnostic timeline.

> **Scope note:** consistent with the privacy posture, the summary is bounded to
> the **thread the bot was summoned into**, not arbitrary channel history. A
> broader `summarize this room` variant would require a `commands` scope and an
> explicit, auditable user action, and is deferred until that trade-off is
> reviewed.

---

## 4. Extension → Slack UX Mapping

The port is largely a re-rendering problem: the same investigation data,
expressed in Slack's primitives instead of the extension's side panel.

| Extension UX component | Slack equivalent | Status |
|------------------------|------------------|--------|
| Side-panel chat session | **Slack threads**, keyed by `thread_ts` | **Shipped** |
| DOM-scraped logs / tracebacks | **Message shortcut** capture → Block Kit cards | **Shipped** |
| Runbook markdown & code blocks | **Block Kit** `mrkdwn` sections with fenced code | **Shipped** (text); rich snippets planned |
| Interactive "Next Steps" links | **Block Kit buttons** (`[View Runbook]`, `[Try Script]`) | Planned (rendered as text today) |

Result rendering lives in `SlackService.build_result_blocks`
([`services/slack_service.py`](../services/slack_service.py)): summary,
hypotheses, suggested next steps, and a context line for confidence / case link.

---

## 5. Engineering Guardrails

1. **The 3-second ack is non-negotiable.** Slack retries any webhook it doesn't
   see a `200` for within ~3s. Every handler verifies the signature, does cheap
   triage, schedules the real work on FastAPI `BackgroundTasks`, and returns
   immediately. Retries are de-duplicated on `event_id` so a slow first attempt
   can't double-fire an investigation. *(Shipped.)*

2. **History reads are a scarce resource — cache them.** `conversations.history`
   / `conversations.replies` are Tier 3 methods, and Slack **tightened limits
   for non-Marketplace apps in 2025**, so a busy outage can exhaust the budget
   fast. Treat fetched context as expensive: a Redis/in-memory cache keyed by
   `(channel, thread_ts, latest_ts)` should wrap history reads before this goes
   to real traffic. *(Planned — not yet implemented.)*

3. **Handle `not_in_channel` gracefully.** For private war rooms the bot may not
   be a member. The backend should catch `not_in_channel` from the Slack API and
   reply with a clear prompt to `/invite @FaultMaven` rather than failing
   silently. *(Planned.)*

4. **Give immediate progress feedback.** RAG + LLM can take 2–5s, well past the
   ack window. Today the agent posts a `:mag: investigating…` placeholder
   message and **updates it in place** (`chat.update`) once the result is ready
   — a reliable, bot-friendly progress signal. *(Shipped.)*

   > **Correction vs. the original note:** Slack has no public "typing
   > indicator" API for bots posting into a channel. The two viable patterns are
   > (a) a placeholder message updated in place — what we do — or (b) an 👀
   > reaction via `reactions.add` (requires `reactions:write`). A literal
   > `typing` event is not available to apps here.

---

## 6. Architectural References

When designing block layouts and handler behavior, benchmark against these
established models:

- **Sentry** — for dense traceback payloads rendered as scannable, compact UI
  cards in a chat feed.
- **Glean / Atlassian Rovo** — for on-demand RAG grounding that surfaces
  internal technical docs cleanly.
- **Rootly** — for threading hygiene that keeps high-level incident channels
  quiet and organized.

---

## 7. Implementation Status

What actually runs in this repository today, so the blueprint never drifts ahead
of the code.

| Capability | Status | Where |
|------------|:------:|-------|
| Strict mention-only posture (Option B) | ✅ | `main.py` (`/slack/events`) |
| Slack request signature verification + replay guard | ✅ | `utils/security.py` |
| 3-second ack + `BackgroundTasks` offload | ✅ | `main.py` |
| `event_id` de-duplication of retries | ✅ | `main.py` (`_remember_event`) |
| Thread = session; history replay per turn | ✅ | `slack_service.py`, `main.py` |
| Message-shortcut ingestion (`message_action`) | ✅ | `main.py` (`_handle_shortcut`) |
| Block Kit result rendering | ✅ | `slack_service.py` (`build_result_blocks`) |
| In-place progress placeholder (`chat.update`) | ✅ | `slack_service.py` |
| FaultMaven core client + mock-mode fallback | ✅ | `services/faultmaven_api.py` |
| "Catch-Me-Up" thread summary | ⬜ | planned |
| Slash commands (`/faultmaven …`) | ⬜ | planned (`commands` scope) |
| History-read caching (rate-limit defense) | ⬜ | planned |
| `not_in_channel` → invite prompt | ⬜ | planned |
| Interactive Block Kit buttons | ⬜ | planned |

---

## 8. Near-Term Roadmap

1. **History-read cache** — required before any real-traffic deployment given
   the 2025 Slack rate-limit changes (Guardrail #2).
2. **`not_in_channel` handling** — needed for private war rooms (Guardrail #3).
3. **Interactive buttons** — promote runbook/script "next steps" from text to
   real Block Kit actions, with an interaction handler on `/slack/interactions`.
4. **Catch-Me-Up summary** — thread-scoped diagnostic timeline (Feature C).
5. **Slash commands** — `/faultmaven runbook <query>` once the `commands` scope
   and its UX are reviewed.
