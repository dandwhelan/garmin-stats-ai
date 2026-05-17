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
# Open http://localhost:8080
```

### Multi-user mode (one process per Garmin account)
```bash
# Each user has users/<name>.env with their own SQLITE_DB_PATH, TOKEN_DIR, WEB_PORT
bash scripts/run-dan.sh    # fetcher + web on WEB_PORT=8082
bash scripts/run-helen.sh  # fetcher + web on WEB_PORT=8081

# Generic launcher (used by the per-user wrappers):
bash scripts/run-user.sh <username>

# Cron @reboot (add to crontab -e):
# @reboot sleep 10 && bash /home/dan/garmin-data/scripts/run-dan.sh
# @reboot sleep 10 && bash /home/dan/garmin-data/scripts/run-helen.sh
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
| `garmin-insights/src/garmin_insights/agent.py` | Core Claude agent — tool-calling loop, prompt caching, streaming, per-model thinking config |
| `garmin-insights/src/garmin_insights/tools/query_tools.py` | 17 tool definitions (Anthropic JSON schema) + handler methods |
| `garmin-insights/src/garmin_insights/web/app.py` | FastAPI server — SSE chat, dashboard (auto cache-refresh), scan endpoints, user/sync identity |
| `garmin-insights/src/garmin_insights/web/static/` | Frontend: `index.html`, `style.css`, `app.js` (user badge, sync badge, Entities tab) |
| `garmin-insights/src/garmin_insights/db/sqlite_repo.py` | SQLite query layer (pandas DataFrames) |
| `garmin-insights/src/garmin_insights/db/memory.py` | Memory store — baselines, insights, session history |
| `garmin-insights/src/garmin_insights/db/cache.py` | Daily summary + baseline cache builder |
| `garmin-insights/src/garmin_insights/config.py` | Settings via pydantic-settings + `.env` — adds `display_name`, `garminconnect_email` |
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

# Model (optional)
CLAUDE_MODEL=claude-sonnet-4-6      # default; set claude-opus-4-7 for Opus

# Web server (optional)
WEB_HOST=0.0.0.0
WEB_PORT=8080                       # use a unique port per user in multi-user mode
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
- `daily_summaries` — pre-computed cache used by the LLM (faster than raw queries)
- `baselines` — 7-day and 30-day rolling averages per metric
- `sessions` — conversation summaries for cross-session continuity
- `user_profile` — user notes/preferences saved by the agent

## Web UI Features

- **User badge** — shows `DISPLAY_NAME` (or name derived from Garmin email) and the email address in the header
- **Sync badge** — shows time since last Garmin fetch (green < 10 min, amber < 60 min, red otherwise); auto-refreshes every 30 s via `/api/health`
- **Entities tab** — custom chart builder: pick any numeric metric(s) from `daily_summaries`, choose 7/14/30/60/90 day range and line or bar type, click Build

## Notes

- Today's data is always marked `is_complete=False` — the agent is instructed not to compare cumulative metrics (steps, calories) for today against baselines
- The dashboard rebuilds the `daily_summaries` cache automatically (throttled to once per 60 s) so fresh fetcher data appears without restarting the web server
- On startup the agent rebuilds the full 90-day cache
- The medical knowledge base (`knowledge/medical.py`) contains 34 evidence-backed insight rules injected into the system prompt (covers sleep, lifestyle, recovery, training load, illness detection, body composition)
- **BST / non-UTC timezone note**: `daily_stats.date` uses noon-UTC of the requested date as its timestamp, so rows are always labelled with the correct local calendar day regardless of timezone offset
