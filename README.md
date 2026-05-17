# Garmin Stats AI

A privacy-first health analytics platform: fetches data from Garmin Connect, stores it locally in SQLite, and uses Claude AI to provide actionable health insights via a web dashboard and chat interface.

## Project Structure

- **`garmin-grafana/`** — Data ingestion engine. Fetches metrics (HR, sleep, stress, HRV, activities, body composition) from Garmin Connect and writes them to SQLite.
- **`garmin-insights/`** — AI analysis layer. Web interface (dashboard + chat + custom-chart "Entities" tab) and CLI, powered by Claude. Defaults to `claude-sonnet-4-6`; opt into Opus by setting `CLAUDE_MODEL=claude-opus-4-7`.
- **`users/`** — Per-user `.env` files for multi-user mode (one Garmin account per file). Real `.env`s are git-ignored; `*.env.example` templates are checked in.
- **`scripts/`** — Launcher scripts (`run-user.sh`, `run-dan.sh`, `run-helen.sh`) that start a fetcher + web server for one user, suitable for `cron @reboot`.

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

### Single-user setup

Create a `.env` file in the **root of the repository** (`garmin-stats-ai/.env`):

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

# Shared database path (use an absolute path)
SQLITE_DB_PATH=/home/yourname/garmin-stats-ai/garmin.db

# Token cache directory (optional; defaults to ~/.garminconnect)
TOKEN_DIR=/home/yourname/.garminconnect

# Claude AI (for garmin-insights)
ANTHROPIC_API_KEY=sk-ant-...

# Optional overrides
CLAUDE_MODEL=claude-sonnet-4-6   # default; set to claude-opus-4-7 for Opus
WEB_PORT=8080
DISPLAY_NAME=Alice
```

### Multi-user setup (Path A — separate processes)

Each user gets their own `.env` file, their own SQLite database, their own Garmin token directory, and their own web server port.

```
garmin-stats-ai/
├── users/
│   ├── alice.env.example   ← template (committed)
│   ├── alice.env           ← real credentials (git-ignored)
│   └── bob.env             ← real credentials (git-ignored)
├── scripts/
│   ├── run-user.sh         ← generic launcher: run-user.sh <username>
│   ├── run-alice.sh        ← shortcut: exec run-user.sh alice
│   └── run-bob.sh
└── ...
```

Copy `users/alice.env.example` to `users/alice.env` and fill in real values:

```bash
GARMINCONNECT_EMAIL=alice@example.com
GARMINCONNECT_PASSWORD=her_password

SQLITE_DB_PATH=/home/pi/garmin-data/alice.db
TOKEN_DIR=/home/pi/.garminconnect-alice
ANTHROPIC_API_KEY=sk-ant-...

WEB_PORT=8081
DISPLAY_NAME=Alice
```

Repeat for each additional user with a **different** `SQLITE_DB_PATH`, `TOKEN_DIR`, and `WEB_PORT`.

Launch both users (each gets an independent fetcher + web server):

```bash
bash scripts/run-alice.sh
bash scripts/run-bob.sh
# Alice: http://localhost:8081
# Bob:   http://localhost:8082
```

To start automatically on boot, add to crontab (`crontab -e`):

```
@reboot sleep 10 && bash /home/pi/garmin-data/scripts/run-alice.sh
@reboot sleep 10 && bash /home/pi/garmin-data/scripts/run-bob.sh
```

> **Tip:** `run-user.sh` sources the user's `.env` file, so each process only ever sees one user's credentials and database. If a process is already running for that user, kill it first or add a guard check (see Troubleshooting).

## Usage

### Step 1 — Fetch your Garmin data

```bash
python -m garmin_grafana.garmin_fetch
```

This creates `garmin.db` and populates it with your health history (up to 1 year back on first run). The fetcher loop then re-checks every 5 minutes for new watch syncs.

### Step 2 — Start the web interface

```bash
garmin-insights web
```

Open **http://localhost:8080** in your browser.

The web interface has three views:

- **Dashboard** — metric cards (sleep score, RHR, HRV, body battery, steps, stress) with 14-day trend charts and AI scan buttons
- **Chat** — conversational AI with full access to your health data
- **Entities** — custom chart builder: pick any numeric metric(s) from your daily summary cache, choose 7 / 14 / 30 / 60 / 90 day range and line or bar chart type, and click Build

### CLI alternatives

```bash
garmin-insights chat          # interactive terminal chat
garmin-insights scan          # one-off general health scan
garmin-insights scan --weekly # full weekly summary
garmin-insights status        # check DB + API connectivity
```

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

**3. Claude AI agent** — Uses `claude-opus-4-7` with adaptive thinking. The agent has 17 callable tools, can reason about multiple metrics together, cites research from a built-in knowledge base, and remembers conversation context across sessions.

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

The agent defaults to **`claude-sonnet-4-6`** (fast, cost-effective). Set `CLAUDE_MODEL=claude-opus-4-7` to opt into Opus for deeper reasoning.

- **Per-model thinking** — Opus uses `adaptive` thinking (Claude decides depth); Sonnet uses `enabled` with an 8,000-token budget. Both reason about health patterns before responding.
- **Prompt caching** — the large medical knowledge system prompt (~2.6k tokens) is cached, reducing API costs by ~80% on repeat queries
- **17 analysis tools** — query daily metrics, sleep, activity, body composition, training readiness, lifestyle behaviours; detect trends, anomalies, correlations; the multi-signal illness scanner; social-jet-lag detector; baselines; user profile / session memory
- **34 medical rules** — full knowledge base injected into the system prompt
- **Per-session memory** — each browser tab has its own conversation history, separate from CLI sessions
- **True token streaming** — the web chat uses Server-Sent Events to stream tokens as Claude generates them
- **User identity in the header** — web UI shows a name badge (from `DISPLAY_NAME` or Garmin email) and a colour-coded last-sync badge that auto-refreshes every 30 s (green < 10 min, amber < 60 min, red otherwise)

## Privacy

All data stays local. Nothing is sent to external servers except:
- Garmin Connect API (to fetch your own data)
- Anthropic API (to generate AI responses — only the content of your queries and health summaries, not raw data)

## Troubleshooting

- **Login issues**: Delete the token directory (default `~/.garminconnect`, or `TOKEN_DIR` if set) to clear stale tokens, then re-run the fetcher
- **No data in dashboard**: Run the fetcher first. The dashboard now auto-refreshes its cache every 60 s; if data still doesn't appear, restart the web server to trigger a full 90-day cache rebuild.
- **Database locked**: Only one process should write to a `garmin.db` at a time. In multi-user mode each user has their own DB file, so there is no contention between users.
- **Duplicate processes on restart**: `run-user.sh` launches a new fetcher + web server on every call. Before restarting, kill the old processes first: `pkill -f "garmin_fetch"` / `pkill -f "garmin-insights web"`. A future guard in `run-user.sh` will handle this automatically.
- **Steps / daily metrics seem one day behind** (BST/non-UTC timezones): Older versions of the fetcher stored `daily_stats` rows under the previous UTC date. The current code uses noon-UTC of the requested date as the timestamp, so `daily_stats.date` always matches the local calendar day. If you have stale mis-dated rows, delete them and let the fetcher rewrite them: `DELETE FROM daily_stats WHERE date >= 'YYYY-MM-DD'`.

## For developers

See [CLAUDE.md](CLAUDE.md) for architecture details, file map, and instructions for extending the agent.
