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
| `garmin-insights/src/garmin_insights/agent.py` | Core Claude agent — tool-calling loop, prompt caching, streaming, per-model thinking config. Dynamic system blocks (`_identity_block`, `_cycle_context_block`, `_environment_context_block`, `_evidence_tier_block`) inject the active user's name + biological sex, current cycle phase (for menstruating users), today's environmental extremes (heat / poor AQ / high pollen, when above thresholds), and the evidence-tier output rules that govern how confidently the model phrases findings. The portable prompt merges per-day environment fields (`env_*`) directly into the daily summaries dict so paste-into-ChatGPT inherits the same context. |
| `garmin-insights/src/garmin_insights/tools/query_tools.py` | 19 tool definitions (Anthropic JSON schema) + handler methods. Token-efficiency helpers: `_round_floats` (1 d.p.), `_clean_records` (strips nulls + rounds), `_strip_zero_lifestyle` (drops zero-status entries, compacts to `["Behavior: N"]` strings). `get_daily_metrics` returns a date-keyed dict `{"YYYY-MM-DD": {...}}` (not an array) to remove repeated `"date"` fields. `get_my_baselines` strips null sub-fields. `get_environment_data` exposes Open-Meteo weather/AQ/pollen so the agent can quantify environmental confounders for RHR/HRV/respiration/sleep deviations. |
| `garmin-insights/src/garmin_insights/web/app.py` | FastAPI server — SSE chat, dashboard (auto cache-refresh + date params + cycle-field enrichment), scan endpoints (with optional date range), user/sync identity, `/api/visualizations`, `/api/lifestyle`, `/api/intraday/heatmap`, `/api/menstrual`, `/api/environment`, `/api/environment/recovery`, `/api/activities/{id}/export` |
| `garmin-grafana/src/garmin_grafana/environment_fetch.py` | Open-Meteo daily pipeline — pulls weather (temp / precip / humidity / UV), air quality (PM2.5/PM10/O₃/NO₂ + European AQI), and pollen (alder/birch/grass/mugwort/olive/ragweed) for the user's `HOME_LAT`/`HOME_LON`. Idempotent upsert into `environment_daily`. Runs at the end of each `fetch_write_bulk` cycle when `environment` is in `FETCH_SELECTION`. No-ops silently when lat/lon are not set. |
| `garmin-insights/src/garmin_insights/web/user_context.py` | Per-user agent + viz pool — one `HealthAgent` / `VisualizationService` / `LifestyleService` per `users/<id>.env`, lazily constructed |
| `garmin-insights/src/garmin_insights/web/visualizations.py` | `VisualizationService` — intraday heatmap, sleep timeline, anomaly z-score calendar, correlation matrix, 90-day behavior impact, environment↔recovery overlay (same-day heat/AQ/PM2.5 + next-day pollen vs RHR/HRV/respiration/sleep, with per-pair Pearson r) |
| `garmin-insights/src/garmin_insights/web/lifestyle_viz.py` | `LifestyleService` — 15 research-backed lifestyle analytics (SRI, social jet lag, illness-like recovery strain pattern, recovery debt, etc.) |
| `garmin-insights/src/garmin_insights/knowledge/medical.py` | 53 evidence-tier-graded insight rules (`InsightRule` with `evidence_tier`, `claim_strength`, `measurement_confidence`, `confounders`, `requires_user_context`). Includes the `multi_cause_recovery_strain` meta-rule, `baseline_reliability_guard`, `travel_circadian_disruption`, and an environmental cluster (`heat_recovery_confounder`, `air_quality_recovery_confounder`, `high_pollen_sleep_confounder`, `allergy_next_day_rhr_systemic` per Buekers 2023, `asthma_environmental_hr_marker` per Cokorudy 2024). |
| `garmin-insights/src/garmin_insights/insights/proactive.py` | `InsightScanner` — local anomaly + behavior + trend detection, enriched with tier metadata. `scan_composite_strain()` collapses concurrent RHR/HRV/respiration anomalies into a single ranked-contributor finding. |
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

# Environmental context (optional — leave unset to skip Open-Meteo fetches)
HOME_LAT=51.5074                    # latitude for daily weather / air quality / pollen
HOME_LON=-0.1278                    # longitude
ENVIRONMENT_PAST_DAYS=92            # Open-Meteo lookback window per fetch (default 92, max 92)

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
- **Prompt caching**: System prompt (medical knowledge with tier badges + confounders, **~11.7k chars** after optimisation) has `cache_control: {"type": "ephemeral"}` — cached after the first call, saving ~80% of system prompt tokens on repeat queries. Dynamic per-call blocks (`_today_block`, `_evidence_tier_block`, `_identity_block`, `_cycle_context_block`) are appended uncached so identity, date, cycle phase, and tier-language rules always reflect the latest state. KB was reduced from ~27k: first-sentence summaries only, abbreviated citations (`[Author Year]`), abbreviated tier tags (`[A, strong]` not `[Tier A, strong_association]`).
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
- `environment_daily` — per-day weather + air quality + pollen for the user's home location (Open-Meteo). Columns: `temp_min/mean/max_c`, `apparent_temp_max_c`, `precipitation_mm`, `wind_max_kmh`, `humidity_mean`, `uv_index_max`, `pm25`, `pm10`, `o3`, `no2`, `european_aqi`, `pollen_alder/birch/grass/mugwort/olive/ragweed`. Empty when `HOME_LAT`/`HOME_LON` not configured.
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
- **Dashboard chart catalogue** (~25 sections total). Every chart tooltip carries an evidence-tier chip (`[Tier A/B/C]`) so the user can see how strong the underlying research is:
  - Recovery & Activity: 14-day Trend, Sleep Architecture (Tier B, medium-confidence measurement — Garmin sleep-stage estimates differ from PSG), Recovery Signals (normalized), Activity Intensity, Stress vs Body Battery (Tier B, autonomic strain — not validated mental stress), Intraday Heatmap (stress/BB/HR toggle), Sleep Timeline (bedtime/waketime drift), Anomaly Calendar (z-score), Behavior Impact (90d, Sleep/HRV/RHR toggle), Correlation Matrix, Training Load — ACWR (Tier C, load-spike context signal, **not** an injury predictor — Impellizzeri 2020 critique cited)
  - Lifestyle & Health Insights: **Illness-like Recovery Strain Pattern** (Tier B, Quer 2021 + Lancet Digital Health 2022 SR — explicitly non-diagnostic), Recovery Debt, Inflammation Index (Tier C — composite physiological-strain z-score, not a measurement of inflammatory biomarkers), SRI (Tier A, Windred 2024), Social Jet Lag dual-clock (Tier B), Stress Resilience, Body Battery Decay, Behavior Recovery Cost, Dose-Response (per-behavior picker), Caffeine Cutoff (Tier A, Drake 2013 + 2023 meta-analysis), Habit Half-Life, Streak Calendar, Co-occurrence Matrix, Stress Trigger Leaderboard, Stress Hour-of-Day Fingerprint, Step-Count CDF (Tier A, Paluch 2022), WHO Activity Target (Tier A, Bull 2020), VO2 Max / Fitness Age (Tier A, Han 2024 BJSM overview, >20M observations)
  - Menstrual Cycle (auto-hidden for users with no cycle data): Vitals by Phase (Shilaih 2017, Maijala 2022, Alzueta 2022, Symons Downs 2025 Sports Med SR), Cycle-Day Curve (Symons Downs 2025; Masuda 2025), Cycle Calendar (60-day phase grid with flow markers), Sleep Architecture by Phase (Tier B medium-confidence; Baker 2007), Stress & Body Battery by Phase. Cycle phase is framed throughout as a **confounder / context label**, not a single explanation.
  - Environmental Context (auto-hidden when no `HOME_LAT`/`HOME_LON`): Weather & Air Quality, Air Quality (EU AQI & PM2.5), Pollen by species, and **Environment ↔ Recovery** (Tier B) — overlays apparent T / EU AQI / PM2.5 / next-day pollen against RHR / HRV / respiration / sleep score with a per-pair Pearson r table. Pollen uses next-day lag per **Buekers 2023** (n=72, 2,497 person-days, allergic-rhinitis wearable telemonitoring — +0.08 bpm next-day RHR per symptom-point); heat/AQ/PM2.5 use same-day per **Niu 2020** PM2.5↔HRV meta-analysis; complementary sources: **Lin 2022** (24-study wearable env review), **Cokorudy 2024** (asthma digital-marker SR), **Baniak 2023** (bedroom T & sleep), **Matzke 2024** (heat exposure & activity).

## Dashboard Data Endpoints

`loadDashboard()` in `app.js` fans out to:

| Endpoint | Returns | Default window |
|---|---|---|
| `GET /api/dashboard?start=&end=` | Daily summaries + 7/30-day baselines for the cards and primary charts | last 14 days |
| `GET /api/visualizations?start=&end=` | `sleep_timeline`, `behavior_impact`, `correlations`, `anomaly_calendar` | last 30 days |
| `GET /api/lifestyle?start=&end=` | 15 lifestyle analytics: SRI, social jet lag, illness radar, recovery debt, inflammation index, resilience, BB decay, recovery cost, dose-response, caffeine cutoff, streak calendar, habit half-life, co-occurrence, fingerprint, trigger leaderboard | last 90 days |
| `GET /api/intraday/heatmap?metric=&days=` | 24h × N-day matrix for `stress` / `body_battery` / `heart_rate` | 14 days |
| `GET /api/menstrual?start=&end=` | Raw `menstrual_cycle` rows for the window — phase, day-of-cycle, flow, predicted length | last 30 days |
| `GET /api/environment?start=&end=` | Daily weather + air quality + pollen rows from `environment_daily`. Returns `available: false` when the user has no home location configured. | last 30 days |
| `GET /api/environment/recovery?start=&end=` | Date-aligned join of `environment_daily` with `daily_summaries` for the Environment ↔ Recovery overlay. Returns per-day environmental drivers (apparent T, EU AQI, PM2.5, pollen peak) + recovery markers (RHR, HRV, respiration, sleep score) plus per-pair Pearson r values. Uses **next-day lag** for pollen↔RHR (Buekers 2023) and same-day for heat/AQ/PM2.5↔HRV (Niu 2020 meta-analysis). `available: false` when no home location is set. | last 60 days |
| `GET /api/behavior-environment?behavior=&drivers=` | Cross-tab a logged lifestyle behavior (e.g. `Allergy Symptoms`, `Asthma symptoms`) against comma-separated `environment_daily` columns. Returns per-day entries, on-/off-day mean recovery deltas (RHR/HRV/respiration/sleep), and Pearson r per (driver, marker) pair. Hidden in UI when `n_logged == 0`. | last 90 days |
| `GET /api/bedroom-temp-sleep?start=&end=` | Overnight bedroom temperature (`ha_sensor_daily.overnight_mean` where `entity_id LIKE '%bedroom%'`) joined to sleep score / HRV / awakening count / RHR. Pearson r per pair. `available: false` when no HA bedroom entity is configured. | last 60 days |
| `GET /api/behavior-root-cause?behavior=&lookback_hours=` | Per-event scan for a logged behavior (e.g. `Migraines`): prior N-hour lifestyle log + same-day environment extremes + today vs yesterday recovery. Returns one entry per logged day. | last 180 days |
| `GET /api/activities/gps?start=&end=` | Activities with GPS tracks in the window (summary + point count) | last 30 days |
| `GET /api/activities/{id}/track` | GPS polyline for one activity (lat, lon, alt, HR, speed, cadence, power, temp) | — |
| `GET /api/activities/{id}/export` | Formatted markdown stats block for one activity — all metrics except GPS coordinates. Used by the "Copy stats" clipboard button. | — |
| `POST /api/scan` (body: `focus`, optional `start_date`, `end_date`) | Markdown AI report scoped to focus + optional window | focus-dependent |

All endpoints share the `_resolve_range(start, end, default_days)` helper so omitting either bound falls back to "ending today, going back N days".

## Evidence-Tier System

Every rule in `knowledge/medical.py` carries an `evidence_tier` (A / B / C; D is reserved). The agent matches its output language to the tier so the user can see how strong each claim is. **Garmin data detects deviations from personal baselines — it does not diagnose.**

| Tier | Meaning | What the agent says |
|---|---|---|
| **A** | Meta-analysis / guideline / strong wearable-scale cohort | "Well-established in research." |
| **B** | Wearable-validated but context-dependent | "Observed in wearable studies; not diagnostic." |
| **C** | Plausible but mixed evidence — requires personal-log confirmation | "Plausible contributor — strongest if your own logs confirm it." |
| **D** | Reserved (experimental / preprint / company source). No rules currently use D — preprints were pruned in favour of peer-reviewed alternatives. | "Experimental — treat as a personal tracking hypothesis." |

Each rule also carries:
- `claim_strength` — `causal` / `strong_association` / `weak_association` / `hypothesis`
- `measurement_confidence` — `high` / `medium` / `low` (e.g., Garmin sleep-stage estimates are `medium`)
- `confounders` — list of other plausible explanations the agent should rank against the primary cause
- `requires_user_context` — `True` when the rule only fires if the user has logged the relevant behaviour

### Mandatory wording substitutions (enforced via `_evidence_tier_block` in `agent.py`)

| Don't say | Say instead |
|---|---|
| "You are getting ill" / "diagnose" | "Illness-like recovery strain pattern" |
| Garmin "stress" as mental stress | "Physiological / autonomic strain" |
| Absolute deep/REM clinical norms | "Device-estimated" / "personal trend vs your own baseline" |
| ACWR "injury prediction" | "Load-spike context signal" |
| SpO2 "sleep apnoea" | "Screening signal worth discussing with a clinician" |

### Multi-cause confounder layer

When two or more of RHR / HRV / respiration deviate together, `InsightScanner.scan_composite_strain()` (in `insights/proactive.py`) emits a single `multi_cause_recovery_strain` finding with ranked plausible contributors. User-logged behaviours from the last 48h outrank generic confounders. The agent presents this as a ranked list, never a single cause.

### Baseline reliability guard

When fewer than 21 days of HRV/RHR baseline data are available, findings are tagged `baseline_low_confidence=True` and the agent prepends "Low-confidence (sparse baseline):" to deviation findings.

## Notes

- Today's data is always marked `is_complete=False` — the agent is instructed not to compare cumulative metrics (steps, calories) for today against baselines
- The dashboard rebuilds the `daily_summaries` cache automatically (throttled to once per 60 s) so fresh fetcher data appears without restarting the web server
- On startup the agent rebuilds the full 90-day cache
- The medical knowledge base (`knowledge/medical.py`) contains **53 evidence-tier-graded insight rules** (14 Tier A, 28 Tier B, 11 Tier C) injected into the system prompt (~13.4k chars after optimisation — covers sleep, lifestyle, recovery, training load, illness-like patterns, body composition, menstrual cycle, travel, environmental confounders (heat / air quality / pollen + Buekers 2023 systemic allergy + Cokorudy 2024 asthma-marker), and a meta-rule for multi-cause attribution)
- **Portable prompt** (`build_portable_prompt`) — used by the "Copy prompt" button. Serialises the full system context + data snapshot for pasting into any external LLM. Data is minified (no indent), floats rounded to 1 d.p., baselines null-stripped, zero lifestyle entries dropped, lifestyle compacted to `["Behavior: N"]` strings, daily summaries as a date-keyed dict with menstrual cycle fields merged inline, separate cycle array eliminated.
- **`get_daily_metrics` tool format** — returns `{"YYYY-MM-DD": {metrics...}}` (date-keyed dict, not array). Removes the repeated `"date"` field from every row and allows O(1) date lookup by the model.
- For menstruating users the agent's system prompt also gets a dynamic "Current Menstrual Cycle Context" block listing today's phase + day. The block frames cycle phase as a **confounder / context label**, not a single explanation, and reminds the model that luteal-phase RHR↑ / HRV↓ is normal physiology — confounded by sleep loss, alcohol, late training, heat, and travel before it should be attributed to phase.
- The user identity block is dynamic too — male users are explicitly told "this user does NOT have menstrual cycle data" so the model doesn't fabricate cycle interpretations
- **BST / non-UTC timezone note**: `daily_stats.date` uses noon-UTC of the requested date as its timestamp, so rows are always labelled with the correct local calendar day regardless of timezone offset
