# Garmin Stats AI

A privacy-first health analytics platform: fetches data from Garmin Connect, stores it locally in SQLite, and uses Claude AI to provide actionable health insights via a web dashboard and chat interface.

## Project Structure

- **`garmin-grafana/`** — Data ingestion engine. Fetches metrics (HR, sleep, stress, HRV, activities, body composition) from Garmin Connect and writes them to SQLite.
- **`garmin-insights/`** — AI analysis layer. Web interface (dashboard + chat) and CLI, powered by Claude (`claude-sonnet-4-6` by default; override with `CLAUDE_MODEL=claude-opus-4-7`).

## Prerequisites

- Python 3.11+
- Garmin Connect account
- Anthropic API key ([get one here](https://console.anthropic.com))

## Installation

```bash
git clone https://github.com/dandwhelan/garmin-stats-ai.git
cd garmin-stats-ai

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -e garmin-grafana
pip install -e garmin-insights
```

## Configuration

Create a `.env` file in the **root of the repository** (`garmin-stats-ai/.env`) — both modules look for it in the current working directory, so running commands from the repo root means they share it automatically:

```
garmin-stats-ai/
├── .env          ← create this file here
├── garmin-grafana/
├── garmin-insights/
└── README.md
```

```bash
# Garmin credentials
GARMINCONNECT_EMAIL=your@email.com
GARMINCONNECT_PASSWORD=yourpassword

# Database path — single-user mode only (ignored when USERS is set)
SQLITE_DB_PATH=/home/yourname/garmin-stats-ai/garmin.db

# Token cache directory (optional — default: ~/.garminconnect)
TOKEN_DIR=~/.garminconnect

# Claude AI (for garmin-insights)
ANTHROPIC_API_KEY=sk-ant-...

# Optional overrides
CLAUDE_MODEL=claude-opus-4-7   # default: claude-sonnet-4-6
SCAN_TIMES=06:00,12:00,18:00,22:00

# Multi-user mode — replaces SQLITE_DB_PATH (see "Multi-user setup" section below)
# USERS=alice:/data/alice.db,bob:/data/bob.db
```

## Usage

### Step 1 — Fetch your Garmin data

```bash
python -m garmin_grafana.garmin_fetch
```

This creates `garmin.db` and populates it with your health history (up to 1 year back on first run). Re-run daily to keep data fresh.

### Step 2 — Start the web interface

```bash
garmin-insights web
```

Open **http://localhost:8080** in your browser.

The web interface has two views:

- **Dashboard** — metric cards (sleep score, RHR, HRV, body battery, steps, stress) with 14-day trend charts and AI scan buttons
- **Chat** — conversational AI with full access to your health data

### CLI alternatives

```bash
garmin-insights chat          # interactive terminal chat
garmin-insights scan          # one-off general health scan
garmin-insights scan --weekly # full weekly summary
garmin-insights status        # check DB + API connectivity
```

## Multi-user setup

Multiple Garmin accounts can share a single server instance. Each user gets their own SQLite database and Garmin token directory.

**1. Create per-user env files** (see `users/alice.env.example` and `users/bob.env.example` for templates):

```bash
# users/alice.env
GARMINCONNECT_EMAIL=alice@example.com
GARMINCONNECT_PASSWORD=alices_password
SQLITE_DB_PATH=/home/pi/garmin-data/alice.db
TOKEN_DIR=/home/pi/.garminconnect-alice
```

**2. Fetch data per user** (run separately for each account):

```bash
# Load alice's env then fetch
set -a && source users/alice.env && set +a
python -m garmin_grafana.garmin_fetch
```

**3. Tell the insights server about all users** via the `USERS` env var:

```bash
USERS=alice:/home/pi/garmin-data/alice.db,bob:/home/pi/garmin-data/bob.db
```

The web interface routes each logged-in user to their own database. When `USERS` is unset the app runs in single-user mode using `SQLITE_DB_PATH`.

## Example questions for the chat

- "How has my sleep been this week compared to my baseline?"
- "Does alcohol affect my overnight HRV? Show me the data."
- "What's been happening with my resting heart rate over the last month?"
- "Am I recovering well enough between workouts?"
- "Which behaviors have the biggest impact on my sleep score?"

## What Is Being Analysed

### Data collected from Garmin Connect (24 tables)

| Category | Metrics |
|---|---|
| **Sleep** | Sleep score, deep / REM / light / awake durations, overnight HRV, sleep stress, restless moments, awakenings, overnight SpO2, respiration rate |
| **Heart & autonomic** | Resting HR, min/max HR, intraday HR, intraday HRV, breathing rate |
| **Stress & body battery** | Stress %, high-stress %, body battery (peak / floor / charged / drained / at-wake), intraday stress and battery |
| **Activity** | Steps, distance, floors, sedentary / active / highly-active seconds, moderate & vigorous intensity minutes, calories (active + BMR) |
| **Workouts** | Per-activity HR zones (Z1–Z5), pace, distance, duration, calories, GPS tracks |
| **Body composition** | Weight, BMI, body fat %, muscle mass, bone mass, body water, visceral fat |
| **Performance** | VO2 max, lactate threshold, hill score (strength + endurance), endurance score, race predictions (5k / 10k / HM / marathon), fitness age |
| **Training load** | Training readiness score, recovery time, ACWR (acute:chronic workload ratio), HRV factor, stress history factor |
| **Lifestyle** | User-tagged behaviours (alcohol, caffeine, screens, exercise, meals, etc.) from the Garmin Connect lifestyle journal |

### How the analysis pipeline works

```
Raw Garmin data           →  Daily summary cache    →  Statistical analysis  →  Claude AI
(per-minute, per-day)        (one row per day)         (Python, no LLM cost)    (interprets + cites studies)
```

**1. Caching** — A nightly/on-demand pass condenses raw measurements into one `daily_summaries` row per day (about 30 numeric fields per day), plus rolling 7-day and 30-day baselines per metric. The AI sees these compact summaries, not the raw per-minute data — so queries are fast and cheap.

**2. Statistical engine (`tools/analysis_tools.py`)** — Pure Python, runs on your machine. Provides:
- **Welch's t-test** to compare metric distributions on days with vs. without a logged behaviour
- **Linear regression** for trend detection (slope, R², direction)
- **Z-score anomaly detection** vs. 30-day rolling baseline
- **Pearson correlation** between any pair of metrics
- **Multi-signal illness detector** — combines RHR + HRV + respiration z-scores against personal baseline (Quer 2021)
- **Social jet lag detector** — compares weekday vs. weekend sleep duration variance

**3. Claude AI agent** — Uses `claude-sonnet-4-6` by default (or `claude-opus-4-7` if `CLAUDE_MODEL` is set) with extended thinking. The agent has 17 callable tools, can reason about multiple metrics together, cites research from a built-in knowledge base, and remembers conversation context across sessions.

### What the agent can answer

- **"Am I getting sick?"** → runs the multi-signal illness detector
- **"Does alcohol affect my HRV?"** → t-test on alcohol vs. non-alcohol nights
- **"Is my training load sustainable?"** → checks ACWR ratio + HRV trend together
- **"Which behaviours hurt my sleep most?"** → batch-runs comparisons across all logged behaviours
- **"Has my recovery been declining?"** → trend detection on HRV + RHR + body battery floor
- **"Am I getting enough deep sleep?"** → checks deep-sleep % vs. recommended 13–23% range

## Medical Knowledge Base

The agent has **34 evidence-backed insight rules** in `garmin-insights/src/garmin_insights/knowledge/medical.py`. Each rule includes a research citation, a plain-language summary of the finding, and the metric pattern it triggers on. The full knowledge base is injected into the AI's system prompt and cached, so every response is grounded in published research.

### Studies referenced

| Topic | Study |
|---|---|
| **Caffeine half-life and sleep** | Drake et al., 2013, *Journal of Clinical Sleep Medicine* |
| **Caffeine and cortisol response** | Lovallo et al., 2005, *Psychosomatic Medicine* |
| **Alcohol and REM sleep suppression** | Ebrahim et al., 2013, *Alcoholism: Clinical & Experimental Research* |
| **Blue light and melatonin suppression** | Chang et al., 2015, *Proceedings of the National Academy of Sciences* |
| **HRV decline as overtraining marker** | Plews et al., 2013, *International Journal of Sports Physiology & Performance* |
| **Wearable RHR for early illness detection** | Radin et al., 2020, *The Lancet Digital Health* |
| **Multi-signal illness pattern (RHR + HRV + respiration)** | Quer et al., 2021, *Nature Medicine* |
| **Respiration rate as inflammation/illness signal** | Natarajan et al., 2020, *BMJ Open* |
| **Acute:chronic workload ratio and injury risk** | Gabbett, 2016, *British Journal of Sports Medicine* |
| **Overreaching: rising load + falling HRV** | Bellenger et al., 2016, *Sports Medicine* |
| **Polarized vs. grey-zone training** | Seiler, 2010, *International Journal of Sports Physiology* |
| **VO2 max plateau dynamics** | Bacon et al., 2013, *PLOS ONE (meta-analysis)* |
| **RHR and all-cause mortality** | Cooney et al., 2010, *American Journal of Cardiology* |
| **Cortisol, stress, and sleep quality** | Adam et al., 2017, *Psychoneuroendocrinology* |
| **Allostatic load and burnout** | McEwen, 2007, *Physiological Reviews* |
| **Exercise and sleep quality (meta-analysis)** | Kredlow et al., 2015, *Journal of Behavioral Medicine* |
| **Vigorous exercise before bed** | Stutz et al., 2019, *Sports Medicine* |
| **Cold-water immersion and parasympathetic tone** | Mooventhan & Nivethitha, 2014, *North American Journal of Medical Sciences* |
| **Morning sunlight and circadian rhythm** | Figueiro et al., 2017, *Sleep Health* |
| **Allergic inflammation and HR/sleep** | Galli et al., 2008, *Nature* |
| **Migraine prodrome and HRV** | Miglis, 2018, *Current Pain & Headache Reports* |
| **Stretching and HPA-axis activation** | Corey et al., 2012, *PM&R Journal* |
| **Diet quality and energy/fatigue** | Haghighatdoost et al., 2012, *Public Health Nutrition* |
| **Heavy meals and sleep architecture** | Crispim et al., 2011, *Journal of Clinical Sleep Medicine* |
| **Late meals and circadian disruption** | Kinsey & Ormsbee, 2015, *Nutrients* |
| **Intermittent fasting and metabolic flexibility** | de Cabo & Mattson, 2019, *New England Journal of Medicine* |
| **Deep sleep and memory consolidation** | Walker, 2017, *Why We Sleep (UC Berkeley)* |
| **REM sleep decline and mortality risk** | Leary et al., 2020, *JAMA Neurology* |
| **Social jet lag and metabolic health** | Wittmann et al., 2006, *Chronobiology International* |
| **Sedentary behaviour and stress** | Choi et al., 2019, *JAMA Internal Medicine* |
| **Sleep fragmentation and HRV recovery** | Stein & Pu, 2012, *Sleep Medicine Reviews* |
| **Visceral fat and HRV** | Felber Dietrich et al., 2006, *European Heart Journal* |
| **Hydration and resting heart rate** | Watso & Farquhar, 2019, *Nutrients* |
| **Overnight SpO2 and sleep-disordered breathing** | Berry et al., 2017, *AASM Clinical Practice Guidelines* |

### What the rules cover

- **Sleep** (8 rules) — caffeine timing, alcohol, screens, heavy/late meals, deep-sleep ratio, REM decline, fragmentation, SpO2
- **Recovery** (5 rules) — HRV decline, multi-signal illness, respiration, overreaching, cardio reserve drift
- **Stress** (3 rules) — cortisol patterns, body battery floor, sedentary stress
- **Exercise & training** (5 rules) — exercise sleep benefit, late workouts, ACWR injury risk, grey-zone training, VO2 max plateau
- **Lifestyle** (10 rules) — caffeine, cold exposure, sunlight, allergies, migraines, stretching, meal quality, fasting, hydration
- **Body composition** (1 rule) — visceral fat / HRV coupling

### Important disclaimers

- **Personal baselines, not population norms.** When the agent says something is "high" or "low", it compares to *your own* 7-day and 30-day rolling averages, not generic medical reference ranges.
- **Today's data is incomplete.** The agent is instructed never to compare today's cumulative metrics (steps, calories, stress duration) against baselines — only overnight metrics (sleep score, RHR, HRV, body-battery-at-wake) are valid for the current day.
- **This is not medical advice.** The studies inform pattern recognition and education. The agent flags signals worth discussing with a clinician — it is not a diagnostic tool. If you see a persistent illness signature, RHR drift, or sleep-disordered breathing pattern, talk to your doctor.

## AI Architecture (technical)

The agent uses **Claude `claude-sonnet-4-6`** by default (set `CLAUDE_MODEL=claude-opus-4-7` for Opus) with:

- **Extended thinking** — Claude reasons about complex health patterns before responding (adaptive mode for Opus, 8k-token budget for Sonnet)
- **Prompt caching** — the large medical knowledge system prompt (~2.6k tokens) is cached, reducing API costs by ~80% on repeat queries
- **17 analysis tools** — query daily metrics, sleep, activity, body composition, training readiness, lifestyle behaviours; detect trends, anomalies, correlations; the multi-signal illness scanner; social-jet-lag detector; baselines; user profile / session memory
- **34 medical rules** — full knowledge base injected into the system prompt
- **Per-session memory** — each browser tab has its own conversation history, separate from CLI sessions
- **True token streaming** — the web chat uses Server-Sent Events to stream tokens as Claude generates them

## Privacy

All data stays local. Nothing is sent to external servers except:
- Garmin Connect API (to fetch your own data)
- Anthropic API (to generate AI responses — only the content of your queries and health summaries, not raw data)

## Troubleshooting

- **Login issues**: Delete `~/.garminconnect` to clear stale tokens, then re-run the fetcher
- **No data in dashboard**: Run the fetcher first, then restart the web server so it rebuilds the cache
- **Database locked**: Only one process should write to `garmin.db` at a time — don't run the fetcher while the web server is actively caching
- **Permission denied creating venv (Linux/Pi)**: The directory is owned by root. Fix it with `sudo chown -R $USER:$USER /path/to/dir`, then re-run `python3 -m venv .venv` without `sudo`. Never use `sudo` with `venv`, `pip`, or `source` — always fix the underlying ownership instead.
- **`sudo source` not found**: `source` is a shell built-in, not a command — it can never be run with `sudo`. Activate the venv as your normal user: `source .venv/bin/activate`.

## For developers

See [CLAUDE.md](CLAUDE.md) for architecture details, file map, and instructions for extending the agent.
