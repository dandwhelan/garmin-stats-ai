# Garmin Stats AI — Claude Code Guide

## Project Overview

Two-module Python monorepo:

- **`garmin-grafana/`** — Data ingestion: fetches Garmin Connect metrics → SQLite
- **`garmin-insights/`** — AI analysis agent: FastAPI web server + CLI, powered by Claude (`claude-sonnet-4-6` by default; override with `CLAUDE_MODEL=claude-opus-4-7`)

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
| `garmin-insights/src/garmin_insights/agent.py` | Core Claude agent — tool-calling loop, prompt caching, streaming |
| `garmin-insights/src/garmin_insights/tools/query_tools.py` | 17 tool definitions (Anthropic JSON schema) + handler methods |
| `garmin-insights/src/garmin_insights/tools/analysis_tools.py` | Statistical engine — Welch's t-test, linear regression, z-score anomaly detection, Pearson correlation, illness detector, social-jet-lag detector |
| `garmin-insights/src/garmin_insights/web/app.py` | FastAPI server — SSE chat, dashboard, scan endpoints |
| `garmin-insights/src/garmin_insights/web/static/` | Frontend: `index.html`, `style.css`, `app.js` |
| `garmin-insights/src/garmin_insights/db/sqlite_repo.py` | SQLite query layer (pandas DataFrames) |
| `garmin-insights/src/garmin_insights/db/memory.py` | Memory store — baselines, insights, session history |
| `garmin-insights/src/garmin_insights/db/cache.py` | Daily summary + baseline cache builder |
| `garmin-insights/src/garmin_insights/config.py` | Settings via pydantic-settings + `.env` |

## Environment Variables (.env)

```bash
# Garmin fetcher
GARMINCONNECT_EMAIL=your@email.com
GARMINCONNECT_PASSWORD=your_password
SQLITE_DB_PATH=/path/to/garmin.db
TOKEN_DIR=~/.garminconnect      # optional; use separate dirs per user in multi-user mode

# Optional fetcher date range — set MANUAL_START_DATE to force a bulk backfill
MANUAL_START_DATE=2025-01-01    # YYYY-MM-DD; presence triggers bulk mode
MANUAL_END_DATE=2025-06-01      # YYYY-MM-DD; defaults to today

# Insights agent (same db)
ANTHROPIC_API_KEY=sk-ant-...
SQLITE_DB_PATH=/path/to/garmin.db
CLAUDE_MODEL=claude-opus-4-7   # optional override (default: claude-sonnet-4-6)
WEB_HOST=0.0.0.0               # optional
WEB_PORT=8080                  # optional
SCAN_TIMES=06:00,12:00,18:00,22:00  # optional scheduled scan times

# Multi-user mode (insights server only; the fetcher ignores USERS)
USERS=alice:/data/alice.db,bob:/data/bob.db
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

- **Model**: `claude-sonnet-4-6` by default (set `CLAUDE_MODEL=claude-opus-4-7` for Opus). Opus uses `thinking: {"type": "adaptive"}`; Sonnet uses `{"type": "enabled", "budget_tokens": 8000}`
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

## Notes

- Today's data is always marked `is_complete=False` — the agent is instructed not to compare cumulative metrics (steps, calories) for today against baselines
- The cache is refreshed on agent startup and every 5 minutes via the dashboard endpoint
- The medical knowledge base (`knowledge/medical.py`) contains 34 evidence-backed insight rules injected into the system prompt (covers sleep, lifestyle, recovery, training load, illness detection, body composition)
- **Multi-user mode**: set `USERS=alice:/data/alice.db,bob:/data/bob.db` to route each user to their own database in the **insights server**. Per-user env templates are in `users/alice.env.example` and `users/bob.env.example`. Each user also needs a separate `TOKEN_DIR` for Garmin auth tokens. The **fetcher does not read `USERS`** — it must be run once per user with that user's env file sourced (`set -a && source users/alice.env && set +a && python -m garmin_grafana.garmin_fetch`).
- **Manual date range for fetcher**: set `MANUAL_START_DATE` (and optionally `MANUAL_END_DATE`) in YYYY-MM-DD format to force a bulk historical backfill. Presence of `MANUAL_START_DATE` is what triggers bulk mode in `garmin_fetch.py`; remove it to return to normal incremental fetching.
