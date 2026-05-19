# Garmin Stats AI — Claude Code Guide

## Project Overview

Two-module Python monorepo:

- **`garmin-grafana/`** — Data ingestion: fetches Garmin Connect metrics → SQLite
- **`garmin-insights/`** — AI analysis agent: FastAPI web server + CLI, powered by Claude (default `claude-sonnet-4-6`; set `CLAUDE_MODEL=claude-opus-4-7` for Opus)
- **`users/`** — Per-user `.env` files for multi-user mode (`*.env` git-ignored; `*.env.example` templates checked in)
- **`scripts/`** — Launchers for multi-user mode (`run-user.sh <username>`, `run-dan.sh`, `run-helen.sh`)

## Commands

### Setup
```bash
pip install -e garmin-grafana
pip install -e garmin-insights
```

### Fetch Garmin data
```bash
python -m garmin_grafana.garmin_fetch
```

### Run the web interface (primary way to use the app)
```bash
garmin-insights web
# or
garmin-web
# Open http://localhost:8080 (single-user)
# In multi-user mode the consolidated dashboard runs at WEB_PORT of the
# user whose env has START_WEB=true — e.g. http://localhost:8081
```

### Multi-user mode (one web server, N fetchers)
```bash
# Each user has users/<name>.env with their own SQLITE_DB_PATH, TOKEN_DIR.
# Exactly ONE user's env sets START_WEB=true and supplies the WEB_PORT for
# the shared dashboard. All other users set START_WEB=false — their fetcher
# still runs every 5 min, but they share the single web server via the
# user-picker dropdown in the header.
bash scripts/run-dan.sh    # dan.env: START_WEB=false → fetcher only
bash scripts/run-helen.sh  # helen.env: START_WEB=true, WEB_PORT=8081 → fetcher + web

# Generic launcher (used by the per-user wrappers):
bash scripts/run-user.sh <username>

# Cron — @reboot starts both users, */10 minutes self-heals if anything died.
# run-user.sh is idempotent: checks /proc/<pid>/environ for SQLITE_DB_PATH and
# skips relaunching what's already alive. Safe to invoke as often as you like.
# @reboot      sleep 20 && bash /home/dan/garmin-data/scripts/run-dan.sh
# @reboot      sleep 25 && bash /home/dan/garmin-data/scripts/run-helen.sh
# */10 * * * * bash /home/dan/garmin-data/scripts/run-dan.sh
# */10 * * * * bash /home/dan/garmin-data/scripts/run-helen.sh
```

### CLI tools
```bash
garmin-insights chat          # interactive terminal chat
garmin-insights scan          # one-off AI health scan
garmin-insights scan --weekly # weekly summary
garmin-insights status        # check DB + API connectivity
```

## Key Files

| File | Purpose |
|------|---------|
| `garmin-insights/src/garmin_insights/agent.py` | Core Claude agent — tool-calling loop, prompt caching, streaming, per-model thinking config. Dynamic system blocks (`_identity_block`, `_cycle_context_block`) inject the active user's name + biological sex and, for menstruating users, current cycle phase. |
| `garmin-insights/src/garmin_insights/tools/query_tools.py` | 17 tool definitions (Anthropic JSON schema) + handler methods |
| `garmin-insights/src/garmin_insights/web/app.py` | FastAPI server — SSE chat, dashboard (auto cache-refresh + date params + cycle-field enrichment), scan endpoints (with optional date range), user/sync identity, `/api/visualizations`, `/api/lifestyle`, `/api/intraday/heatmap`, `/api/menstrual` |
| `garmin-insights/src/garmin_insights/web/user_context.py` | Per-user agent + viz pool — one `HealthAgent` / `VisualizationService` / `LifestyleService` per `users/<id>.env`, lazily constructed |
| `garmin-insights/src/garmin_insights/web/visualizations.py` | `VisualizationService` — intraday heatmap, sleep timeline, anomaly z-score calendar, correlation matrix, 90-day behavior impact |
| `garmin-insights/src/garmin_insights/web/lifestyle_viz.py` | `LifestyleService` — 15 research-backed lifestyle analytics (SRI, social jet lag, illness radar, recovery debt, etc.) |
| `garmin-insights/src/garmin_insights/web/static/` | Frontend: `index.html`, `style.css`, `app.js` (date range toolbar, customize panel, info-icon tooltips, user/sync badges, Entities tab, ~17 secondary chart renderers) |
| `garmin-insights/src/garmin_insights/db/sqlite_repo.py` | SQLite query layer (pandas DataFrames) |
| `garmin-insights/src/garmin_insights/db/memory.py` | Memory store — baselines, insights, session history |
| `garmin-insights/src/garmin_insights/db/cache.py` | Daily summary + baseline cache builder |
| `garmin-insights/src/garmin_insights/config.py` | Settings via pydantic-settings + `.env`. `settings_for_user(user_id)` overlays `display_name`, `garminconnect_email`, and `biological_sex` from `users/<id>.env` so each user gets their own UI badge + AI persona |
| `garmin-grafana/src/garmin_grafana/garmin_fetch.py` | Garmin Connect poller — daily stats, intraday, activities, etc. |
| `garmin-grafana/src/garmin_grafana/sqlite_manager.py` | SQLite write layer for the fetcher |
| `users/*.env.example` | Per-user env templates for multi-user mode |
| `scripts/run-user.sh` | Generic launcher (sources `users/<name>.env`, starts fetcher + web) |

## Environment Variables (.env / users/<name>.env)

```bash
# Garmin fetcher
GARMINCONNECT_EMAIL=your@email.com
GARMINCONNECT_PASSWORD=your_password
SQLITE_DB_PATH=/path/to/garmin.db
TOKEN_DIR=/home/you/.garminconnect   # separate per-user in multi-user mode

# Insights agent (same db as fetcher)
ANTHROPIC_API_KEY=sk-ant-...
SQLITE_DB_PATH=/path/to/garmin.db   # must match fetcher

# UI identity
DISPLAY_NAME=Alice                  # shown in header badge; derived from email if omitted
BIOLOGICAL_SEX=Female               # Male / Female — applied to AI prompt for sex-specific
                                    # reference ranges; controls menstrual cycle context

# Model (optional)
CLAUDE_MODEL=claude-sonnet-4-6      # default; set claude-opus-4-7 for Opus

# Web server (optional)
WEB_HOST=0.0.0.0
WEB_PORT=8080                       # only honoured for the user whose env has START_WEB=true

# Multi-user web mode (per-user env files only)
START_WEB=true                      # exactly ONE user sets this true and owns WEB_PORT;
                                    # all other users set START_WEB=false (fetcher-only)
```

## Architecture

```
User (browser)
     │  HTTP + SSE
     ▼
FastAPI (web/app.py)
     │
     ▼
HealthAgent (agent.py)
     │  tool calls (manual loop)
     ▼
QueryToolHandler (tools/query_tools.py)
     │
     ├── SqliteRepo  → garmin.db  (raw Garmin measurements)
     └── MemoryStore → garmin.db  (daily summaries, baselines, sessions)
```

## Claude API Design

- **Model**: defaults to `claude-sonnet-4-6`; set `CLAUDE_MODEL=claude-opus-4-7` to opt into Opus
- **Per-model thinking**: Opus → `{"type": "adaptive"}`; Sonnet (and any non-Opus) → `{"type": "enabled", "budget_tokens": 8000}`
- **Prompt caching**: System prompt (medical knowledge, ~2k tokens) has `cache_control: {"type": "ephemeral"}` — cached after the first call, saving ~80% of system prompt tokens on repeat queries
- **Tool loop**: Manual (not automatic function calling) — dispatches tool calls, appends results, loops until `stop_reason == "end_turn"` (max 10 rounds)
- **Streaming**: `chat_stream()` generator used by the SSE endpoint; yields status messages during tool calls, final text when done

## Adding a New Tool

1. Add a method to `QueryToolHandler` in `query_tools.py`
2. Add its Anthropic JSON schema to `get_all_tools_anthropic()` in the same file
3. The method is automatically callable by Claude — no other registration needed

## Database Schema (SQLite)

All data lives in a single `garmin.db`. Key tables:
- `daily_stats` — RHR, steps, stress, body battery (one row per day)
- `sleep_summary` — sleep score, HRV, deep/REM/light sleep
- `activity_summary` — workouts with HR, distance, calories
- `lifestyle_journal` — user-logged behaviors (alcohol, caffeine, etc.)
- `body_composition` — weight, body fat, BMI
- `training_readiness` — Garmin training readiness score + factors
- `menstrual_cycle` — per-day cycle phase, day-of-cycle, predicted/observed cycle length, flow intensity, symptoms (only populated for users who track cycles in Garmin Connect)
- `daily_summaries` — pre-computed cache used by the LLM (faster than raw queries)
- `baselines` — 7-day and 30-day rolling averages per metric
- `sessions` — conversation summaries for cross-session continuity
- `user_profile` — user notes/preferences saved by the agent

## Web UI Features

- **User picker** — when `USERS` is set, a dropdown in the header lets a viewer switch between configured users. Switching clears the chat session, refetches `/api/health`, reloads the dashboard against the new DB, and updates the document title.
- **User badge** — shows `DISPLAY_NAME` (or name derived from Garmin email) and the email address in the header. Resolved per-user from `users/<id>.env` so the badge always matches the active user.
- **Sync badge** — shows time since last Garmin fetch (green < 10 min, amber < 60 min, red otherwise); auto-refreshes every 30 s via `/api/health`
- **Date range toolbar** — 7 / 14 / 30 / 90-day presets plus custom from/to inputs; drives `/api/dashboard?start=&end=` (and the secondary loaders below)
- **⚙ Customize panel** — auto-discovers every `.chart-section`, renders a per-chart visibility checkbox grid, persists state in localStorage under `garmin-chart-prefs-v1`
- **Info-icon tooltips** — every metric card and most chart headers have an inline `i` icon with thresholds and a one-line research citation
- **AI Health Scan date range** — optional `start_date` / `end_date` row above the scan buttons; passed to `generate_scan_report`
- **Entities tab** — custom chart builder: pick any numeric metric(s) from `daily_summaries`, choose 7/14/30/60/90 day range and line or bar type, click Build. The dashboard endpoint enriches summaries with cycle fields (`cycleDay`, `cycleLength`, `cycleFlowIntensity`, `cyclePhaseMenstrual/Follicular/Ovulatory/Luteal`) so cycle metrics appear alongside sleep / RHR / HRV in the picker for menstruating users.
- **Dashboard chart catalogue** (~25 sections total):
  - Recovery & Activity: 14-day Trend, Sleep Architecture, Recovery Signals (normalized), Activity Intensity, Stress vs Body Battery, Intraday Heatmap (stress/BB/HR toggle), Sleep Timeline (bedtime/waketime drift), Anomaly Calendar (z-score), Behavior Impact (90d, Sleep/HRV/RHR toggle), Correlation Matrix
  - Lifestyle & Health Insights: Illness Radar (Quer 2021), Recovery Debt, Inflammation Index, SRI (Windred 2024), Social Jet Lag dual-clock, Stress Resilience, Body Battery Decay, Behavior Recovery Cost, Dose-Response (per-behavior picker), Caffeine Cutoff (Drake 2013), Habit Half-Life, Streak Calendar, Co-occurrence Matrix, Stress Trigger Leaderboard, Stress Hour-of-Day Fingerprint
  - Menstrual Cycle (auto-hidden for users with no cycle data): Vitals by Phase (RHR / HRV / Sleep / Body Battery; Shilaih 2017, Maijala 2022), Cycle-Day Curve (Lyu 2025), Cycle Calendar (60-day phase grid with flow markers), Sleep Architecture by Phase (Baker 2007), Stress & Body Battery by Phase

## Dashboard Data Endpoints

`loadDashboard()` in `app.js` fans out to:

| Endpoint | Returns | Default window |
|---|---|---|
| `GET /api/dashboard?start=&end=` | Daily summaries + 7/30-day baselines for the cards and primary charts | last 14 days |
| `GET /api/visualizations?start=&end=` | `sleep_timeline`, `behavior_impact`, `correlations`, `anomaly_calendar` | last 30 days |
| `GET /api/lifestyle?start=&end=` | 15 lifestyle analytics: SRI, social jet lag, illness radar, recovery debt, inflammation index, resilience, BB decay, recovery cost, dose-response, caffeine cutoff, streak calendar, habit half-life, co-occurrence, fingerprint, trigger leaderboard | last 90 days |
| `GET /api/intraday/heatmap?metric=&days=` | 24h × N-day matrix for `stress` / `body_battery` / `heart_rate` | 14 days |
| `GET /api/menstrual?start=&end=` | Raw `menstrual_cycle` rows for the window — phase, day-of-cycle, flow, predicted length | last 30 days |
| `POST /api/scan` (body: `focus`, optional `start_date`, `end_date`) | Markdown AI report scoped to focus + optional window | focus-dependent |

All endpoints share the `_resolve_range(start, end, default_days)` helper so omitting either bound falls back to "ending today, going back N days".

## Notes

- Today's data is always marked `is_complete=False` — the agent is instructed not to compare cumulative metrics (steps, calories) for today against baselines
- The dashboard rebuilds the `daily_summaries` cache automatically (throttled to once per 60 s) so fresh fetcher data appears without restarting the web server
- On startup the agent rebuilds the full 90-day cache
- The medical knowledge base (`knowledge/medical.py`) contains 37 evidence-backed insight rules injected into the system prompt (covers sleep, lifestyle, recovery, training load, illness detection, body composition, menstrual cycle)
- For menstruating users the agent's system prompt also gets a dynamic "Current Menstrual Cycle Context" block listing today's phase + day, so replies don't need a tool round-trip to know where the user is in their cycle. The block warns the model not to flag normal luteal-phase RHR rises / HRV dips as illness or overtraining.
- The user identity block is dynamic too — male users are explicitly told "this user does NOT have menstrual cycle data" so the model doesn't fabricate cycle interpretations
- **BST / non-UTC timezone note**: `daily_stats.date` uses noon-UTC of the requested date as its timestamp, so rows are always labelled with the correct local calendar day regardless of timezone offset
