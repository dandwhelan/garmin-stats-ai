# Garmin Stats AI — Codex / OpenAI Agent Guide

This file mirrors [CLAUDE.md](CLAUDE.md) for OpenAI Codex / Codex CLI users. The
architecture and runtime are model-agnostic — only the *default* AI model used by
the agent layer differs (Anthropic Claude). When working in this repo with an
OpenAI-based coding assistant, the same file map, env vars, and commands apply.

## Project Overview

Two-module Python monorepo:

- **`garmin-grafana/`** — Data ingestion: fetches Garmin Connect metrics → SQLite
- **`garmin-insights/`** — AI analysis agent: FastAPI web server + CLI. The
  *user-facing* AI (the chat / scan / dashboard insights) is powered by Anthropic
  Claude (`claude-sonnet-4-6` by default, opt into `claude-opus-4-7` via
  `CLAUDE_MODEL`). Your Codex assistant is a separate thing — it edits the code
  in this repo; it does not run inside the deployed product.
- **`users/`** — Per-user `.env` files for multi-user mode. Real `.env`s are
  git-ignored; `*.env.example` templates are checked in.
- **`scripts/`** — Launchers (`run-user.sh`, `run-dan.sh`, `run-helen.sh`) suitable
  for `cron @reboot`. Honour `START_WEB=false` so only one user's launcher owns
  the shared dashboard.

## Commands (copy / paste verbatim)

```bash
# Setup
pip install -e garmin-grafana
pip install -e garmin-insights

# Fetch Garmin data (single-user)
python -m garmin_grafana.garmin_fetch

# Run the web interface (single-user)
garmin-insights web          # equivalent to `garmin-web`
# Multi-user: the consolidated dashboard listens on WEB_PORT of the user
# whose users/<id>.env has START_WEB=true (e.g. http://localhost:8081)

# CLI alternatives
garmin-insights chat           # interactive terminal chat
garmin-insights scan           # one-off AI health scan
garmin-insights scan --weekly  # weekly summary
garmin-insights status         # check DB + API connectivity
```

## Multi-user mode (one web, N fetchers)

```bash
# Each users/<name>.env declares the user's own SQLITE_DB_PATH, TOKEN_DIR,
# DISPLAY_NAME, BIOLOGICAL_SEX, and START_WEB. Exactly ONE user sets
# START_WEB=true and owns WEB_PORT for the shared dashboard; all others run
# the fetcher only and share that dashboard via the header dropdown.

bash scripts/run-dan.sh        # dan.env: START_WEB=false → fetcher only
bash scripts/run-helen.sh      # helen.env: START_WEB=true, WEB_PORT=8081 → fetcher + web

# Cron — @reboot starts every user, */10min self-heals if anything died.
# @reboot      sleep 20 && bash /home/dan/garmin-data/scripts/run-dan.sh
# @reboot      sleep 25 && bash /home/dan/garmin-data/scripts/run-helen.sh
# */10 * * * * bash /home/dan/garmin-data/scripts/run-dan.sh
# */10 * * * * bash /home/dan/garmin-data/scripts/run-helen.sh
```

`run-user.sh` is idempotent — it matches `SQLITE_DB_PATH` in
`/proc/<pid>/environ` and skips relaunching what's already alive.

## Key Files

| File | Purpose |
|------|---------|
| `garmin-insights/src/garmin_insights/agent.py` | Core AI agent — tool-calling loop, prompt caching, streaming. `_identity_block()` injects the active user's name + biological sex; `_cycle_context_block()` adds today's menstrual phase + day for female users. |
| `garmin-insights/src/garmin_insights/tools/query_tools.py` | 17 tool definitions (JSON schema) + handler methods — the surface the AI calls to read the DB. |
| `garmin-insights/src/garmin_insights/web/app.py` | FastAPI server. Routes: SSE chat, dashboard (auto-cache-refresh + date params + cycle-field enrichment), scans (`/api/scan`), `/api/visualizations`, `/api/lifestyle`, `/api/intraday/heatmap`, `/api/menstrual`, `/api/users`, `/api/health`. |
| `garmin-insights/src/garmin_insights/web/user_context.py` | Per-user agent pool — one `HealthAgent` / `VisualizationService` / `LifestyleService` per `users/<id>.env`, lazily constructed and cached for the server's lifetime. |
| `garmin-insights/src/garmin_insights/web/visualizations.py` | `VisualizationService` — intraday heatmap, sleep timeline, anomaly z-score calendar, correlation matrix, 90-day behavior impact. |
| `garmin-insights/src/garmin_insights/web/lifestyle_viz.py` | `LifestyleService` — 15 research-backed lifestyle analytics (SRI, social jet lag, illness radar, recovery debt, etc.) plus the cycle analytics (phase-stratified vitals, cycle-day curve, calendar, sleep by phase, stress by phase). |
| `garmin-insights/src/garmin_insights/web/static/` | Frontend: `index.html`, `style.css`, `app.js`. Date-range toolbar, customize panel, info-icon tooltips, user/sync badges, Entities tab, ~25 chart renderers. |
| `garmin-insights/src/garmin_insights/db/sqlite_repo.py` | SQLite query layer returning pandas DataFrames. |
| `garmin-insights/src/garmin_insights/db/memory.py` | Per-user memory store — baselines, insights, session history, user profile. |
| `garmin-insights/src/garmin_insights/db/cache.py` | Daily-summary + baseline cache builder. |
| `garmin-insights/src/garmin_insights/config.py` | Pydantic-settings + `.env`. `settings_for_user(user_id)` overlays per-user `DISPLAY_NAME`, `GARMINCONNECT_EMAIL`, `BIOLOGICAL_SEX` from `users/<id>.env`. |
| `garmin-grafana/src/garmin_grafana/garmin_fetch.py` | Garmin Connect poller — daily stats, intraday, activities, etc. |
| `garmin-grafana/src/garmin_grafana/sqlite_manager.py` | SQLite write layer for the fetcher. |
| `users/*.env.example` | Per-user env templates for multi-user mode. |
| `scripts/run-user.sh` | Generic launcher (sources `users/<name>.env`, starts fetcher; starts web if `START_WEB=true`). |

## Environment Variables (`.env` and `users/<name>.env`)

```bash
# Garmin fetcher
GARMINCONNECT_EMAIL=your@email.com
GARMINCONNECT_PASSWORD=your_password
SQLITE_DB_PATH=/path/to/garmin.db
TOKEN_DIR=/home/you/.garminconnect   # separate per-user in multi-user mode

# Insights agent (same db as fetcher)
ANTHROPIC_API_KEY=sk-ant-...
SQLITE_DB_PATH=/path/to/garmin.db    # must match fetcher

# UI identity
DISPLAY_NAME=Alice                   # shown in header badge
BIOLOGICAL_SEX=Female                # Male / Female — applied to AI prompt for sex-specific
                                     # reference ranges and (for Female) menstrual-phase context

# Multi-user web mode (per-user env files only)
START_WEB=true                       # exactly ONE user sets this true; others run fetcher only

# Model selection
CLAUDE_MODEL=claude-sonnet-4-6       # default; set claude-opus-4-7 for Opus

# Web server
WEB_HOST=0.0.0.0
WEB_PORT=8080                        # only honoured for the user whose env has START_WEB=true

# Repo-root .env only — multi-user registry
USERS=alice:/path/alice.db,bob:/path/bob.db
```

## Architecture

```
Browser
  │  HTTP + SSE
  ▼
FastAPI (web/app.py)
  │
  ├─ /api/users           → user_context.UserContext.user_ids
  ├─ /api/health?user=…   → resolved identity (name + email) + last sync
  ├─ /api/dashboard       → daily_summaries + baselines (+ cycle-field enrichment)
  ├─ /api/visualizations  → VisualizationService
  ├─ /api/lifestyle       → LifestyleService (15 analytics + 5 cycle analytics)
  ├─ /api/intraday/heatmap
  ├─ /api/menstrual       → raw menstrual_cycle rows
  └─ /api/chat (SSE)      → HealthAgent.chat_stream()
       │
       ▼
     HealthAgent (one per user)
       │  manual tool loop
       ▼
     QueryToolHandler
       │
       ├─ SqliteRepo   → garmin.db (raw measurements)
       └─ MemoryStore  → garmin.db (summaries, baselines, sessions)
```

## Database Schema (SQLite)

All data for one user lives in one `garmin.db`. Multi-user mode = one DB per user.
Key tables:

- `daily_stats` — RHR, steps, stress, body battery (one row per day)
- `sleep_summary` — sleep score, HRV, deep/REM/light/awake
- `activity_summary` — workouts with HR, distance, calories
- `lifestyle_journal` — user-logged behaviours (alcohol, caffeine, etc.)
- `body_composition` — weight, body fat, BMI
- `training_readiness` — Garmin training readiness score + factors
- `menstrual_cycle` — phase, day-of-cycle, predicted/observed length, flow,
  symptoms (only for users who track cycles in Garmin Connect)
- `daily_summaries` — pre-computed cache used by the LLM (faster than raw queries)
- `baselines` — 7-day and 30-day rolling averages per metric
- `sessions` — conversation summaries for cross-session continuity
- `user_profile` — user notes/preferences saved by the agent

## Adding a New Tool

1. Add a method to `QueryToolHandler` in `tools/query_tools.py`.
2. Add its JSON schema to `get_all_tools_anthropic()` in the same file.
3. The method is automatically callable by the agent — no other registration needed.

## Web UI Features

- **User picker** — when `USERS` is set, a header dropdown switches between
  configured users. Switching clears the chat session, refetches `/api/health`,
  reloads the dashboard against the new DB, and updates the document title.
- **User badge** — `DISPLAY_NAME` (or name derived from email) + email. Resolved
  per-user from `users/<id>.env` so the badge always matches the active user.
- **Sync badge** — time since last Garmin fetch (green < 10 min, amber < 60 min,
  red otherwise); auto-refreshes every 30 s via `/api/health`.
- **Date range toolbar** — 7 / 14 / 30 / 90-day presets plus custom from/to.
- **⚙ Customize panel** — auto-discovers every `.chart-section`, per-chart
  visibility checkbox grid, persists in localStorage under `garmin-chart-prefs-v1`.
- **Info-icon tooltips** — thresholds + research citations on every metric card
  and most chart headers.
- **Entities tab** — pick any numeric metric(s) and chart them. The dashboard
  endpoint enriches summaries with `cycleDay`, `cycleLength`,
  `cycleFlowIntensity`, and one-hot `cyclePhaseMenstrual/Follicular/Ovulatory/
  Luteal` so cycle metrics appear in the picker for menstruating users.
- **Cycle dashboards** (auto-hidden when no cycle data): Vitals by Phase, Cycle-
  Day Curve, Cycle Calendar (60-day phase grid + flow markers), Sleep
  Architecture by Phase, Stress & Body Battery by Phase.

## Notes for Coding Agents

- **Editing live code on a Pi**: this repo is deployed at `/home/dan/garmin-data`
  on a Raspberry Pi 5. The fetcher polls every 5 min as a long-lived process;
  cron (`*/10`) self-heals via `run-user.sh`. To pick up code changes, kill the
  affected process(es) and rerun the relevant `scripts/run-<user>.sh` —
  `run-user.sh` is idempotent so it's safe to call when something is already
  running.
- **Today's data is always `is_complete=False`**: the agent is instructed not to
  compare cumulative metrics (steps, calories) for today against baselines.
- **Cache refresh**: the dashboard rebuilds `daily_summaries` automatically
  (throttled to once per 60 s). On startup the agent rebuilds the full 90-day
  cache.
- **BST / non-UTC timezone note**: `daily_stats.date` uses noon-UTC of the
  requested date as its timestamp, so rows always have the correct local
  calendar day.
- **Don't commit `users/*.env`**: they hold real Garmin and Anthropic
  credentials. Only the `*.env.example` templates belong in git.
