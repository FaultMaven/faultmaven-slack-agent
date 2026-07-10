#!/usr/bin/env python3
"""Render the FaultMaven-for-Slack architecture diagram (Devpost submission asset).

Produces docs/architecture.png (and .svg). Hand-laid with matplotlib so the layout
matches the shipped agent exactly — see docs/design.md §3. Re-run after arch changes:

    python3 docs/architecture_diagram.py
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.patheffects as pe  # noqa: E402
from matplotlib import pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

# ---- palette -------------------------------------------------------------
INK = "#1B1F24"          # primary text
SUB = "#57606A"          # secondary text
PAGE = "#FFFFFF"
SLACK = "#4A154B"        # Slack aubergine
SLACK_FILL = "#F6EEF7"
AGENT = "#0B6E75"        # FaultMaven teal
AGENT_FILL = "#E4F1F2"
CORE = "#4B2E9E"         # engine indigo
CORE_FILL = "#EEEAFA"
STORE = "#475569"        # neutral slate
STORE_FILL = "#F1F4F8"
CHIP = "#FFFFFF"
LINE = "#3A4048"
ACCENT = "#B4531F"       # trust/annotation accent

# Prefer a clean sans if present.
for cand in ("DejaVu Sans", "Arial", "Helvetica"):
    if any(cand == f.name for f in fm.fontManager.ttflist):
        plt.rcParams["font.family"] = cand
        break

fig, ax = plt.subplots(figsize=(16, 10), dpi=200)
fig.patch.set_facecolor(PAGE)
ax.set_facecolor(PAGE)
ax.set_xlim(0, 100)
ax.set_ylim(0, 100)
ax.axis("off")


def band(x0, y0, x1, y1, header, hcolor, fill, header_h=4.4, hsize=12.5):
    """A titled panel: colored header bar + tinted body."""
    ax.add_patch(FancyBboxPatch(
        (x0, y0), x1 - x0, y1 - y0,
        boxstyle="round,pad=0,rounding_size=1.4",
        linewidth=1.4, edgecolor=hcolor, facecolor=fill, zorder=2))
    ax.add_patch(FancyBboxPatch(
        (x0, y1 - header_h), x1 - x0, header_h,
        boxstyle="round,pad=0,rounding_size=1.4",
        linewidth=0, facecolor=hcolor, zorder=3))
    # square off the header's lower corners over the body
    ax.add_patch(plt.Rectangle((x0, y1 - header_h), x1 - x0, header_h * 0.5,
                               linewidth=0, facecolor=hcolor, zorder=3))
    ax.text(x0 + 1.8, y1 - header_h / 2, header, ha="left", va="center",
            color="white", fontsize=hsize, fontweight="bold", zorder=4)


def chip(x0, y0, x1, y1, title, body, tcolor, bcolor=SUB,
         tsize=11, bsize=9.2, edge=None):
    edge = edge or "#D5DBE2"
    ax.add_patch(FancyBboxPatch(
        (x0, y0), x1 - x0, y1 - y0,
        boxstyle="round,pad=0,rounding_size=1.0",
        linewidth=1.1, edgecolor=edge, facecolor=CHIP, zorder=5))
    cy = (y0 + y1) / 2
    ax.text((x0 + x1) / 2, cy + (y1 - y0) * 0.24, title, ha="center", va="center",
            color=tcolor, fontsize=tsize, fontweight="bold", zorder=6)
    ax.text((x0 + x1) / 2, cy - (y1 - y0) * 0.20, body, ha="center", va="center",
            color=bcolor, fontsize=bsize, zorder=6, linespacing=1.35)


def arrow(x0, y0, x1, y1, color=LINE, lw=2.2, style="-|>", ms=16):
    ax.add_patch(FancyArrowPatch(
        (x0, y0), (x1, y1), arrowstyle=style, mutation_scale=ms,
        linewidth=lw, color=color, zorder=7,
        shrinkA=0, shrinkB=0))


# ==== Title ===============================================================
ax.text(50, 97.2, "FaultMaven for Slack — Architecture", ha="center", va="center",
        color=INK, fontsize=21, fontweight="bold")
ax.text(50, 93.4,
        "Slack-native AI troubleshooting copilot  ·  multi-workspace OAuth  ·  "
        "one Slack thread = one FaultMaven investigation",
        ha="center", va="center", color=SUB, fontsize=11.5)

MAIN_L, MAIN_R = 4.0, 72.0

# ==== Band 1 — Slack workspaces ==========================================
b1t, b1b = 90.5, 74.5
band(MAIN_L, b1b, MAIN_R, b1t,
     "SLACK WORKSPACES   ·   per-tenant OAuth install", SLACK, SLACK_FILL)
# three entry surfaces
cw = (MAIN_R - MAIN_L - 2 * 3.0 - 3.0) / 3
cx = MAIN_L + 1.5
for title, body in [
    ("Assistant side panel", "1:1 investigation\nsuggested prompts · live status"),
    ("Channel thread", "@mention → open case\nreply to auto-continue (war room)"),
    ('“Ask FaultMaven” shortcut', "seed a case from ANY\nmessage — alert · error · log"),
]:
    chip(cx, 79.6, cx + cw, 86.0, title, body, SLACK)
    cx += cw + 3.0
ax.text(MAIN_L + 1.8, 77.0,
        "Suggested-action buttons (Decide · Run)  ·  attach logs/configs as evidence on every surface",
        ha="left", va="center", color=INK, fontsize=9.4)
ax.text(MAIN_R - 1.8, 77.0,
        "●  strict summon-only — no channel firehose",
        ha="right", va="center", color=ACCENT, fontsize=9.4, fontweight="bold")

# ==== Connector Slack <-> Agent ==========================================
arrow(30, 74.3, 30, 67.6)   # events in (down)
arrow(46, 67.6, 46, 74.3)   # web API out (up)
ax.text(28.5, 71.0,
        "events + interactivity\nsigned HTTPS → POST /slack/events\n"
        "OAuth: /slack/install · /oauth_redirect",
        ha="right", va="center", color=SUB, fontsize=8.6, linespacing=1.3)
ax.text(47.5, 71.0,
        "Slack Web API (outbound)\nchat.* · assistant.* · files.*\n"
        "conversations.replies (catch-up read only)",
        ha="left", va="center", color=SUB, fontsize=8.6, linespacing=1.3)

# ==== Band 2 — the agent =================================================
b2t, b2b = 67.0, 40.5
band(MAIN_L, b2b, MAIN_R, b2t,
     "FAULTMAVEN SLACK AGENT   ·   Bolt for Python · HTTP transport", AGENT, AGENT_FILL)
# 2x2 internal component grid
gx0, gx1 = MAIN_L + 1.5, MAIN_R - 1.5
gap = 2.4
cwm = (gx1 - gx0 - gap) / 2
rows = [
    [("Listeners", "assistant · events · shortcuts\nactions · home"),
     ("Rendering", "TurnResponse → Block Kit\n+ chat_stream reasoning timeline")],
    [("FaultMaven REST client", "dev-login token bootstrap\n401 re-acquire · typed errors"),
     ("Turn gate", "one turn per thread\ndrop-if-busy · thread ownership")],
]
ry_top = 61.0
rh = 8.2
rgap = 2.0
for r, row in enumerate(rows):
    y0 = ry_top - r * (rh + rgap) - rh
    for c, (t, b) in enumerate(row):
        x0 = gx0 + c * (cwm + gap)
        chip(x0, y0, x0 + cwm, y0 + rh, t, b, AGENT)

# ==== Right column — agent data stores ===================================
STO_L, STO_R = 75.5, 98.0
band(STO_L, 40.5, STO_R, 67.0,
     "AGENT STATE", STORE, STORE_FILL, hsize=11)
chip(STO_L + 1.4, 55.0, STO_R - 1.4, 62.2,
     "Postgres  ·  faultmaven_slack",
     "multi-workspace OAuth\ninstallations + state", STORE, tsize=10)
chip(STO_L + 1.4, 44.2, STO_R - 1.4, 51.4,
     "SQLite  (PersistentVolume)",
     "thread → case map\n+ per-thread busy gate", STORE, tsize=10)
# link agent <-> its stores
arrow(MAIN_R, 53.7, STO_L, 53.7, color=STORE, lw=1.8, style="<|-|>", ms=12)

# ==== Connector Agent -> Core ============================================
arrow(38, 40.3, 38, 31.4)
ax.text(40.0, 35.7,
        "HTTPS + Bearer (FaultMaven service token)\n"
        "POST /api/v1/cases  ·  /cases/{id}/turns (multipart form-data)\n"
        "/cases/{id}/reports  ·  /knowledge  ·  /auth",
        ha="left", va="center", color=SUB, fontsize=9.0, linespacing=1.35)
ax.text(36.0, 35.7,
        "the backend never sees\nSlack tokens or payloads —\nthe agent is the only bridge",
        ha="right", va="center", color=ACCENT, fontsize=8.8, linespacing=1.35,
        fontstyle="italic")

# ==== Band 3 — FaultMaven core (foundation, full width) ==================
b3t, b3b = 31.0, 5.0
band(MAIN_L, b3b, STO_R, b3t,
     "FAULTMAVEN CORE API   ·   api.faultmaven.ai   (the investigation engine)",
     CORE, CORE_FILL)
gx0, gx1 = MAIN_L + 1.5, STO_R - 1.5
ncol = 4
cgap = 2.2
cwc = (gx1 - gx0 - (ncol - 1) * cgap) / ncol
core_chips = [
    ("Investigation engine",
     "milestones\nINQUIRY → INVESTIGATING\n→ RESOLVED / CLOSED\nhypotheses · evidence"),
    ("Knowledge / RAG",
     "ChromaDB · BGE-M3\nhybrid vector + BM25\nrunbooks + past fixes"),
    ("LLM router",
     "9 providers\ncapability routing\n+ fallback chains"),
    ("Cases / Knowledge DB",
     "Postgres\ncase state machine\nevidence · reports"),
]
for i, (t, b) in enumerate(core_chips):
    x0 = gx0 + i * (cwc + cgap)
    chip(x0, 8.6, x0 + cwc, 24.4, t, b, CORE, tsize=10.5, bsize=9.0)

# ==== Footer — soundness =================================================
ax.text(50, 2.4,
        "Soundness guarantees honored in every rendered turn:  "
        "never present an incorrect conclusion  ·  never collapse under pressure — "
        "when data is inadequate, name the missing evidence.",
        ha="center", va="center", color=INK, fontsize=9.2,
        path_effects=[pe.withStroke(linewidth=2, foreground=PAGE)])

fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
fig.savefig("docs/architecture.png", facecolor=PAGE, bbox_inches="tight", pad_inches=0.25)
fig.savefig("docs/architecture.svg", facecolor=PAGE, bbox_inches="tight", pad_inches=0.25)
print("wrote docs/architecture.png and docs/architecture.svg")
