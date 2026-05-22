# Garmin Stats AI

A privacy-first health analytics platform: fetches data from Garmin Connect, stores it locally in SQLite, and uses Claude AI to provide actionable health insights via a web dashboard and chat interface.

<img width="945" height="778" alt="image" src="https://github.com/user-attachments/assets/fe935c7f-1c88-457a-884a-430ef55922d9" />

> **⚠️ Not medical advice.** This tool detects deviations from your own personal
> baselines — it does not diagnose. The agent will never say "you are ill" or
> "you have sleep apnoea". If a pattern persists, talk to a clinician. See
> [Important disclaimers](#important-disclaimers) for the full version.
>
> **⚠️ Self-hosted, no built-in authentication.** This server holds extremely
> sensitive health data. See [Security](#security) before exposing it to anything.

## Project Structure

- **`garmin-grafana/`** — Data ingestion engine. Fetches metrics (HR, sleep, stress, HRV, activities, body composition) from Garmin Connect and writes them to SQLite.
- **`garmin-insights/`** — AI analysis layer. Web interface (dashboard + chat + custom-chart "Entities" tab) and CLI, powered by Claude. Defaults to `claude-sonnet-4-6`; opt into Opus by setting `CLAUDE_MODEL=claude-opus-4-7`. Multi-user aware: one web server can serve any number of users via a header dropdown, each backed by their own SQLite DB and AI agent instance.
- **`users/`** — Per-user `.env` files for multi-user mode (one Garmin account per file). Real `.env`s are git-ignored; `*.env.example` templates are checked in. Each file declares `DISPLAY_NAME`, `BIOLOGICAL_SEX`, `START_WEB`, plus its own DB / token paths.
- **`scripts/`** — Launcher scripts (`run-user.sh`, `run-dan.sh`, `run-helen.sh`) that start a fetcher (and optionally a web server) for one user, suitable for `cron @reboot`. Honours `START_WEB=false` so only one user's launcher owns the shared dashboard while others run fetchers only.

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
BIOLOGICAL_SEX=Female            # Male / Female — applied to the AI prompt for sex-specific
                                 # reference ranges and (for Female) menstrual-phase context
```

### Multi-user setup (one web server, N fetchers)

Each user gets their own `.env` file, their own SQLite database, and their own Garmin token directory. **One** user owns the shared web server (sets `START_WEB=true` and supplies `WEB_PORT`); all other users run a fetcher only (`START_WEB=false`). The single web instance serves every user through a header dropdown.

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

Copy `users/alice.env.example` to `users/alice.env` and fill in real values. The user who owns the shared dashboard:

```bash
# users/alice.env  ← this user owns the web server
GARMINCONNECT_EMAIL=alice@example.com
GARMINCONNECT_PASSWORD=her_password

SQLITE_DB_PATH=/home/pi/garmin-data/alice.db
TOKEN_DIR=/home/pi/.garminconnect-alice
ANTHROPIC_API_KEY=sk-ant-...

WEB_PORT=8081
START_WEB=true
DISPLAY_NAME=Alice
BIOLOGICAL_SEX=Female
```

And every other user — fetcher only, no web:

```bash
# users/bob.env  ← fetcher only, shares Alice's dashboard
GARMINCONNECT_EMAIL=bob@example.com
GARMINCONNECT_PASSWORD=his_password

SQLITE_DB_PATH=/home/pi/garmin-data/bob.db
TOKEN_DIR=/home/pi/.garminconnect-bob
ANTHROPIC_API_KEY=sk-ant-...

START_WEB=false
DISPLAY_NAME=Bob
BIOLOGICAL_SEX=Male
```

Also set `USERS` in the repo-root `.env` so the dashboard knows about every account:

```bash
# .env (repo root)
USERS=alice:/home/pi/garmin-data/alice.db,bob:/home/pi/garmin-data/bob.db
```

Launch both users (only Alice's run script actually starts a web server; Bob's runs the fetcher only):

```bash
bash scripts/run-alice.sh   # fetcher + web on port 8081
bash scripts/run-bob.sh     # fetcher only (START_WEB=false)
# Open http://localhost:8081 — switch between Alice and Bob via the header dropdown
```

To keep both users syncing forever — start on reboot **and** auto-restart anything that dies — add four lines to `crontab -e`:

```
# Start on reboot (small stagger so they don't hit Garmin auth at the exact same instant)
@reboot       sleep 20 && bash /home/pi/garmin-data/scripts/run-alice.sh >> /home/pi/garmin-data/logs/cron.log 2>&1
@reboot       sleep 25 && bash /home/pi/garmin-data/scripts/run-bob.sh   >> /home/pi/garmin-data/logs/cron.log 2>&1

# Self-heal every 10 minutes — if a fetcher or web server has died, relaunch it
*/10 * * * *  bash /home/pi/garmin-data/scripts/run-alice.sh >> /home/pi/garmin-data/logs/cron.log 2>&1
*/10 * * * *  bash /home/pi/garmin-data/scripts/run-bob.sh   >> /home/pi/garmin-data/logs/cron.log 2>&1
```

> **Safe to re-run.** `run-user.sh` checks `/proc/<pid>/environ` for each running fetcher / web process and only launches what's missing for that user. Running it again when both processes are already alive is a no-op — it logs "already running … skipping". That's what makes the 10-minute cron line a working watchdog.

## Usage

### Step 1 — Fetch your Garmin data

```bash
python -m garmin_grafana.garmin_fetch
```

This creates `garmin.db` and populates it with your health history (up to 1 year back on first run). The fetcher loop then re-checks every 5 minutes for new watch syncs.

On the first tick of each calendar day the fetcher also re-pulls the trailing 7 days, so late-logged lifestyle entries, sleep notes, or any other retroactive edits made in Garmin Connect for the past week get picked up — all upserts are idempotent on natural keys, so the daily re-pull doesn't duplicate anything. Override the window with `RESYNC_WINDOW_DAYS=N` (set to `0` to disable).

### Step 2 — Start the web interface

```bash
garmin-insights web
```

Open **http://localhost:8080** in your browser.

The web interface has three views:

- **Dashboard** — metric cards (sleep score, RHR, HRV, body battery, steps, stress) plus ~25 charts grouped into Recovery & Activity and Lifestyle & Health Insights sections (see below). Toolbar with 7 / 14 / 30 / 90-day presets and custom date range; ⚙ Customize panel for per-chart show/hide (persists in localStorage). Every metric card and chart header has an info-icon tooltip with thresholds and research citations.
- **Chat** — conversational AI with full access to your health data
- **Entities** — custom chart builder: pick any numeric metric(s) from your daily summary cache, choose 7 / 14 / 30 / 60 / 90 day range and line or bar chart type, and click Build

### Dashboard chart catalogue

Recovery & Activity:
- 14-day Trend (Sleep / RHR / HRV / Battery / Steps toggle), Sleep Architecture, Recovery Signals (normalized to 7-day baseline), Activity Intensity, Stress vs Body Battery
- Intraday Heatmap — 24h × N-day matrix for Stress / Body Battery / Heart Rate / Steps. Stress and Body Battery use Garmin's published bands (Rest / Low / Medium / High for stress; Low → Very High for Body Battery) with a fixed 0–100 scale and a band legend whose swatches are taken directly from the cell palette, so a cell's colour tells you the band at a glance. Heart Rate and Steps use a winsorised p2–p98 gradient so a couple of outlier hours don't compress the colour scale.
- Activity Map — GPS track viewer (Leaflet + OpenStreetMap). Activities listed newest-first. Pick any activity; the polyline auto-fits to the route. "Colour by" selector: Plain / Heart rate / Elevation. **Copy stats** button copies a formatted markdown block of all activity metrics (duration, pace, HR zones, elevation gain/loss, cadence, power, temperature — no GPS coordinates) to clipboard, ready to paste into any AI or doc.
- Sleep Timeline (bedtime/waketime drift) and Anomaly Calendar (z-score vs 30-day baseline)
- Behavior Impact (last 90 days, Sleep / HRV / RHR toggle) and Metric Correlation Matrix

Lifestyle & Health Insights. Every chart tooltip carries an evidence-tier chip (`[Tier A/B/C]`) so the user can see how strong the underlying research is. The agent is a **deviation detector, not a diagnostician**:
- Illness-like Recovery Strain Pattern (Tier B — Quer 2021 + Mishra 2022 *Lancet Digital Health* systematic review; **not diagnostic** — same pattern can follow alcohol, heat, poor sleep, late training, travel, DOMS, or luteal-phase physiology)
- Recovery Debt, Inflammation Index, Stress Resilience, Body Battery Decay Slope
- Sleep Regularity Index (Windred 2024) and Social Jet Lag dual-clock (weekday vs weekend midpoints)
- Behavior Recovery Cost (median days for HRV to return to baseline)
- Behavior Dose-Response (per-behavior scatter for behaviors logged with numeric values)
- Caffeine Timing Comparison (Drake 2013 late vs early vs none)
- Habit Half-Life, Behavior Streak Calendar, Behavior Co-occurrence, Stress Trigger Leaderboard
- Stress Hour-of-Day Fingerprint (weekday vs weekend)

Menstrual cycle (auto-hidden for users with no cycle data). Cycle phase is framed as a **confounder / context label**, never a single cause:
- Vitals by Menstrual Phase — RHR / HRV / Sleep score / Body Battery across follicular / ovulatory / luteal / menstrual phases (Shilaih 2017, Maijala 2022, Alzueta 2022, Symons Downs 2025 *Sports Med* SR)
- Cycle-Day Curve — RHR + HRV averaged at each day-of-cycle (Symons Downs 2025; Masuda 2025)
- Cycle Calendar — 60-day phase grid with flow-intensity markers
- Sleep Architecture by Phase — device-estimated deep / REM / light / awake minutes per phase (Baker 2007). Note: Garmin sleep-stage estimates differ meaningfully from polysomnography (Chinoy 2021; Schyvens 2024 Garmin validation) — read as a personal trend across phases, not clinical staging.
- Stress & Body Battery by Phase — daily autonomic-strain % + Body Battery drain across phases

### AI Health Scan

Below the dashboard, three buttons run a one-shot Claude analysis: General Scan, Morning Brief, Weekly Summary. An optional date-range picker scopes the scan to a specific window — leave blank for the default (last 7/14 days depending on focus).

### CLI alternatives

```bash
garmin-insights chat          # interactive terminal chat
garmin-insights scan          # one-off general health scan
garmin-insights scan --weekly # full weekly summary
garmin-insights status        # check DB + API connectivity
```

## Multi-user mode — how it works at runtime

The full setup steps live under [Multi-user setup (one web server, N fetchers)](#multi-user-setup-one-web-server-n-fetchers). At runtime:

- The web server reads `USERS` from the repo-root `.env` to learn every configured user.
- For each user, `Settings.settings_for_user(user_id)` overlays `DISPLAY_NAME`, `GARMINCONNECT_EMAIL`, and `BIOLOGICAL_SEX` from `users/<id>.env`, giving the UI badge and the AI agent the correct identity.
- One `HealthAgent` is constructed per user (lazy), each with its own SQLite DB, baselines, memory store, and chat session. There is no cross-contamination — switching the dropdown clears the chat session and reloads dashboard data against the active user's DB.
- The AI's system prompt is also personalised: each call appends an identity block (`You are speaking with <Name>. Biological sex: <M/F>...`) and, for female users with cycle data, a "Current Menstrual Cycle Context" block stating today's phase + day so the model interprets vitals cycle-aware without an extra tool round-trip.
- When `USERS` is unset the app runs in single-user mode using the root `.env`'s `SQLITE_DB_PATH`, and the dropdown is hidden.

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
| **Menstrual cycle** | Cycle phase, day-of-cycle, predicted vs observed cycle length, period length, flow intensity, symptoms / mood / notes — when tracked in Garmin Connect |
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

**3. Claude AI agent** — Uses `claude-sonnet-4-6` by default (or `claude-opus-4-7` if `CLAUDE_MODEL` is set) with extended thinking. The agent has 18 callable tools, can reason about multiple metrics together, cites research from a built-in knowledge base, and remembers conversation context across sessions.

### What the agent can answer

- **"Is my body showing recovery strain?"** → runs the multi-signal scanner and reports an *illness-like recovery strain pattern* with ranked plausible contributors (alcohol, late training, travel, luteal phase, etc.) — never a single-cause diagnosis
- **"Does alcohol affect my HRV?"** → t-test on alcohol vs. non-alcohol nights, with the tier-A citation (Ebrahim 2013 + PLOS Digital Health 2026 ~21k cohort)
- **"Is my training load risky?"** → reports ACWR as a *load-spike context signal* (Tier C — Impellizzeri 2020 critique), never as injury prediction
- **"Which behaviours hurt my sleep most?"** → batch-runs comparisons across all logged behaviours
- **"Has my recovery been declining?"** → trend detection on HRV + RHR + body battery floor, suppressed to low-confidence if &lt;21 days of baseline
- **"How is my device-estimated deep sleep trending?"** → personal trend vs your own baseline; Garmin sleep-stage estimates differ from polysomnography, so the agent will not call you "deficient" against population norms

## Medical Knowledge Base

The agent has **48 evidence-tier-graded insight rules** in `garmin-insights/src/garmin_insights/knowledge/medical.py`. Each rule includes a research citation, a plain-language summary of the finding, the metric pattern it triggers on, **and tier metadata** (`evidence_tier`, `claim_strength`, `measurement_confidence`, `confounders`, `requires_user_context`). The full knowledge base is injected into the AI's system prompt and cached, so every response is grounded in published research — but the agent matches its language to the strength of the evidence (see the [Evidence Tier System](#evidence-tier-system) below).

### Studies referenced

| Topic | Study |
|---|---|
| **Caffeine half-life and sleep** | Drake et al., 2013, *Journal of Clinical Sleep Medicine*; Gardiner et al., 2023, *Sleep Medicine Reviews* (meta-analysis) |
| **Caffeine and cortisol response** | Lovallo et al., 2005, *Psychosomatic Medicine* |
| **Alcohol and REM sleep suppression** | Ebrahim et al., 2013, *Alcoholism: Clinical & Experimental Research*; *PLOS Digital Health*, 2026 (wearable cohort, ~21k adults — dose-dependent) |
| **Blue light and melatonin suppression** | Chang et al., 2015, *Proceedings of the National Academy of Sciences* |
| **HRV decline as overtraining marker** | Plews et al., 2013, *International Journal of Sports Physiology & Performance* |
| **Wearable RHR for early illness detection** | Radin et al., 2020, *The Lancet Digital Health* |
| **Illness-like recovery strain pattern (RHR + HRV + respiration) — non-diagnostic** | Quer et al., 2021, *Nature Medicine*; Mishra et al., 2022, *Lancet Digital Health* (systematic review) |
| **Respiration rate as inflammation/illness signal** | Natarajan et al., 2020, *BMJ Open* |
| **ACWR as load-spike context signal (NOT an injury predictor)** | Gabbett, 2016, *BJSM* (original concept); Impellizzeri et al., 2020, *BJSM* (critique); Wang et al., 2024, *BJSM* (training-load research limitations) |
| **Overreaching: rising load + falling HRV** | Bellenger et al., 2016, *Sports Medicine* |
| **Polarized vs. grey-zone training** | Seiler, 2010, *International Journal of Sports Physiology* |
| **VO2 max plateau dynamics & cardiorespiratory fitness ↔ mortality** | Bacon et al., 2013, *PLOS ONE* (training meta-analysis); Han et al., 2024, *BJSM* (overview of meta-analyses, >20M observations, 199 cohorts) |
| **RHR and all-cause / CV mortality** | Cooney et al., 2010, *American Journal of Cardiology*; Aune et al., 2017, *CMAJ* (dose-response meta-analysis) |
| **Cortisol, stress, and sleep quality** | Adam et al., 2017, *Psychoneuroendocrinology* |
| **Allostatic load and burnout** | McEwen, 2007, *Physiological Reviews* |
| **Exercise and sleep quality (meta-analysis)** | Kredlow et al., 2015, *Journal of Behavioral Medicine* |
| **Vigorous exercise before bed** | Stutz et al., 2019, *Sports Medicine* (review); Leota et al., 2025, *Nature Communications* (~4M nights of wearable data) |
| **Cold-water immersion and parasympathetic tone** | Mooventhan & Nivethitha, 2014, *North American Journal of Medical Sciences* |
| **Morning sunlight and circadian rhythm** | Figueiro et al., 2017, *Sleep Health* |
| **Allergic inflammation and HR/sleep** | Shaaban et al., 2008, *European Respiratory Journal*; Togias, 2000, *Journal of Allergy and Clinical Immunology* |
| **Migraine prodrome and HRV** | Miglis, 2018, *Current Pain & Headache Reports* |
| **Stretching and HPA-axis activation** | Corey et al., 2012, *PM&R Journal* |
| **Diet quality and energy/fatigue** | Haghighatdoost et al., 2012, *Public Health Nutrition* |
| **Heavy meals and sleep architecture** | Crispim et al., 2011, *Journal of Clinical Sleep Medicine* |
| **Late meals and circadian disruption** | Kinsey & Ormsbee, 2015, *Nutrients* |
| **Intermittent fasting and metabolic flexibility** | de Cabo & Mattson, 2019, *New England Journal of Medicine* |
| **Device-estimated deep / REM as a personal trend** | Ohayon et al., 2004, *Sleep* (PSG meta-analysis); Chinoy et al., 2021, *Sleep* (consumer wearable vs PSG validation); Schyvens et al., 2024 (Garmin sleep-stage validation). **Consumer-wearable sleep-stage estimates differ meaningfully from polysomnography** — the agent treats this as a personal trend, not clinical staging. |
| **REM sleep decline as a long-term mortality signal (PSG cohort)** | Leary et al., 2020, *JAMA Neurology* |
| **Social jet lag and metabolic health** | Wittmann et al., 2006, *Chronobiology International* |
| **Sedentary behaviour and stress** | Choi et al., 2019, *JAMA Internal Medicine* |
| **Sleep fragmentation and HRV recovery** | Stein & Pu, 2012, *Sleep Medicine Reviews* |
| **Visceral fat and HRV** | Felber Dietrich et al., 2006, *European Heart Journal* |
| **Hydration and resting heart rate** | Watso & Farquhar, 2019, *Nutrients* |
| **Overnight SpO2 — clinician-screening signal only, NOT a diagnostic sleep study** | Kapur et al., 2017, *Journal of Clinical Sleep Medicine* (AASM Clinical Practice Guideline for Adult OSA Diagnostic Testing) |
| **Period-day RHR/HRV shift (luteal phase physiology — use as confounder, not single cause)** | Shilaih et al., 2017, *Scientific Reports* (wrist wearable); Alzueta / de Zambotti / Baker, 2022 (Oura: luteal HR↑, skin temp↑, RMSSD↓); Symons Downs et al., 2025, *Sports Medicine* (systematic review — wearable HRV across reproductive life stages); Nakagawa et al., 2020, *J Clin Med*; Brar et al., 2015, *J Women's Health* |
| **Temperature / HR / HRV across the menstrual cycle (Oura)** *(dashboard tooltip)* | Maijala et al., 2022, *International Journal of Women's Health* |
| **ML classification of cycle phase from sleeping HR** | Masuda et al., 2025 |
| **Cycle phase × sleep architecture (device-estimated, personal-trend framing)** | Baker & Driver, 2007, *Sleep Medicine Reviews*; PMS &amp; sleep quality cross-sectional, 2025 (PMC11842786) |
| **Follicular-phase training window** | Janse de Jonge, 2019, *Sports Medicine* |
| **Travel & time-zone disruption (wearable cohort)** | Lechat et al., 2025, *SLEEP* (Oura cohort, ~1.5M nights) |
| **HRV/RHR baseline reliability** | Plews et al., 2013, *Sports Medicine* (requires ~21-30 days of continuous data) |
| **DOMS — RHR/HRV signature mimics illness** | Cheung et al., 2003, *Sports Medicine*; Twist & Eston, 2005, *Journal of Sports Sciences* |
| **Pet-in-bedroom sleep fragmentation** | Patel et al., 2017, *Mayo Clinic Proceedings* |
| **Acute emotional stress and HRV suppression** | Thayer & Lane, 2009, *Neuroscience & Biobehavioral Reviews* |
| **Travel / first-night effect on sleep** | Waterhouse et al., 2007, *Journal of Sleep Research* |
| **Weekly activity guidelines (moderate / vigorous minutes)** | WHO Physical Activity Guidelines, 2020; Bull et al., 2020, *British Journal of Sports Medicine* |
| **Fitness age from VO2 max (Garmin's age-sex normative model)** | Nes et al., 2013, *Medicine & Science in Sports & Exercise* |
| **Alcohol — next-morning RHR elevation** | Sagawa et al., 2011, *Alcohol & Alcoholism* |

### What the rules cover

Auto-counted from `medical.py` — **48 rules total**, broken down by category as tagged on each `InsightRule`:

- **Sleep** (14 rules) — caffeine timing, alcohol, screens, heavy/late meals, device-estimated deep / REM as personal trend, fragmentation, SpO2 screening, sleep regularity, social jet lag, travel circadian disruption, PMS sleep architecture, pet in bedroom
- **Lifestyle** (11 rules) — cold exposure, sunlight, allergies, migraines, stretching, meal quality, fasting, hydration, alcohol acute HR, period-day RHR/HRV
- **Exercise & training** (8 rules) — exercise sleep benefit, late workouts, ACWR load-spike context signal, grey-zone training, VO2 max plateau, VO2 max longevity, weekly activity targets, follicular training window
- **Recovery** (9 rules) — HRV decline, illness-like recovery strain pattern (multi-signal), respiration, overreaching, DOMS, cycle-vs-sleep-loss confound, cardio reserve drift, baseline reliability guard, **multi-cause recovery strain meta-rule** (ranked plausible contributors)
- **Stress** (5 rules) — autonomic-strain patterns, body battery floor, autonomic HRV / cognition, allostatic load, sedentary stress
- **Body composition** (1 rule) — visceral fat / HRV coupling

Menstrual-cycle-aware rules are spread across the categories they affect (Recovery, Lifestyle, Exercise) rather than living in a separate bucket.

### Evidence Tier System

The agent is a **deviation detector, not a diagnostician**. Garmin data is excellent at detecting deviations from personal baselines and weak for absolute medical claims. Every rule is graded:

| Tier | Meaning | Agent phrasing |
|---|---|---|
| **A** (14 rules) | Meta-analysis / guideline / large wearable cohort | "Well-established in research." |
| **B** (23 rules) | Wearable-validated, context-dependent | "Observed in wearable studies; not diagnostic." |
| **C** (11 rules) | Plausible but mixed evidence — requires personal-log confirmation | "Plausible contributor — strongest if your own logs confirm it." |
| **D** (0 rules) | Reserved (experimental / preprint / company source). The Ultrahuman 2025 bioRxiv preprint and medRxiv preprints were pruned in favour of peer-reviewed alternatives. | n/a |

The agent enforces mandatory wording substitutions: never "diagnose"; "illness-like recovery strain pattern" instead of "you are getting ill"; "physiological / autonomic strain" instead of Garmin "mental stress"; "device-estimated" / "personal trend" for sleep stages; "load-spike context signal" for ACWR; "screening signal worth discussing with a clinician" for SpO2.

**Multi-cause confounder layer.** When ≥2 of RHR / HRV / respiration deviate together, the scanner emits a single ranked-contributor finding — user-logged behaviours (alcohol, late training, travel, DOMS) from the last 48h outrank generic confounders, so the agent never blames a single cause when several plausible ones are on the table.

**Baseline reliability guard.** When fewer than 21 days of HRV/RHR baseline data are available, findings are tagged low-confidence and the agent prepends "Low-confidence (sparse baseline):" to deviation findings.

### Important disclaimers

- **Personal baselines, not population norms.** When the agent says something is "high" or "low", it compares to *your own* 7-day and 30-day rolling averages, not generic medical reference ranges.
- **Today's data is incomplete.** The agent is instructed never to compare today's cumulative metrics (steps, calories, stress duration) against baselines — only overnight metrics (sleep score, RHR, HRV, body-battery-at-wake) are valid for the current day.
- **This is not medical advice and not a diagnosis.** The studies inform pattern recognition and education. The agent flags signals worth discussing with a clinician — it never says "you are ill", "you have sleep apnoea", or "you will be injured". If you see a persistent illness-like recovery strain pattern, an upward RHR drift, or recurring overnight SpO2 dips, talk to your doctor.

## AI Architecture (technical)

The agent defaults to **`claude-sonnet-4-6`** (fast, cost-effective). Set `CLAUDE_MODEL=claude-opus-4-7` to opt into Opus for deeper reasoning.

- **Per-model thinking** — Opus uses `adaptive` thinking (Claude decides depth); Sonnet uses `enabled` with an 8,000-token budget. Both reason about health patterns before responding.
- **Prompt caching** — the medical knowledge system prompt (**~11.7k chars** after optimisation — down from ~27k; first-sentence rule summaries, abbreviated citations and tier tags) is cached, reducing API costs by ~80% on repeat queries. Dynamic per-call blocks (date, identity, cycle context, evidence-tier output rules) are appended uncached so they always reflect the latest state.
- **18 analysis tools** — query daily metrics, sleep, activity, body composition, training readiness, lifestyle behaviours, menstrual cycle; detect trends, anomalies, correlations; the multi-signal recovery-strain scanner; social-jet-lag detector; baselines; user profile / session memory
- **48 medical rules with evidence tiers (A / B / C)** — full knowledge base with tier badges and confounders injected into the system prompt
- **Tier-aware output framing** — the agent matches its language to the tier of the rule it is citing, and uses ranked plausible contributors instead of single-cause claims when multiple recovery markers deviate together
- **Per-session memory** — each browser tab has its own conversation history, separate from CLI sessions
- **True token streaming** — the web chat uses Server-Sent Events to stream tokens as Claude generates them
- **Copy prompt** — a button in the scan/chat area generates a single self-contained portable prompt (system context + 30-day data snapshot, minified and optimised) that can be pasted into any external LLM (Claude.ai, ChatGPT, Gemini, etc.) without needing API keys
- **Copy stats** (Activity Map) — exports all stats for the selected activity as formatted markdown to clipboard; no GPS coordinates included
- **User identity in the header** — web UI shows a name badge (`DISPLAY_NAME` or name derived from the Garmin email) and a colour-coded last-sync badge that auto-refreshes every 30 s (green < 10 min, amber < 60 min, red otherwise). In multi-user mode a dropdown switches between configured users; the badge, AI agent, chat session, and dashboard all repoint to the selected user.
- **Cycle-aware AI (framed as confounder, not cause)** — for users whose `BIOLOGICAL_SEX=Female` and who have data in the `menstrual_cycle` table, every Claude API call gets a dynamic system block with the user's current phase + day. The block instructs the model to use cycle phase as a context label, not a single explanation — luteal-phase RHR↑ / HRV↓ is normal physiology and should be ranked against sleep loss, alcohol, late training, heat, and travel before being attributed to phase. Male users are explicitly told they have no cycle data so the model doesn't fabricate cycle interpretations.

### Dashboard analytics pipeline (no LLM cost)

The dashboard's secondary charts are powered by two Python services that aggregate the SQLite tables in-process, alongside the AI agent:

- **`web/visualizations.py` — `VisualizationService`**: intraday heatmap (stress/HR/body battery), sleep timeline, anomaly z-score calendar, metric correlation matrix, 90-day behavior-impact comparison
- **`web/lifestyle_viz.py` — `LifestyleService`**: 21 research-backed analytics including Sleep Regularity Index (Windred 2024 — Tier A), social jet lag (Wittmann 2006 — Tier B), illness-like recovery strain pattern (Quer 2021 + Mishra 2022 *Lancet Digital Health* SR — Tier B, non-diagnostic), inflammation index (Tier C — composite physiological-strain z-score, not a measurement of inflammatory biomarkers), recovery debt, stress resilience, body battery decay slope, behavior dose-response, caffeine cutoff comparison (Drake 2013 + 2023 meta-analysis — Tier A), recovery cost, streak calendar, habit half-life, co-occurrence matrix, hour-of-day stress fingerprint, stress trigger leaderboard, step-count distribution (Paluch 2022 — Tier A), fitness-age delta (Nes 2013 + Han 2024 BJSM overview — Tier A), WHO weekly-intensity target tracking (Bull 2020 — Tier A), cycle-day HRV/RHR (Symons Downs 2025 *Sports Med* SR; Masuda 2025), per-cycle yearly view, plus a research-signal scorecard
- **Three endpoints** — `/api/visualizations`, `/api/lifestyle`, `/api/intraday/heatmap`. All accept `start`/`end` query params and fan out service calls via `asyncio.gather` for parallel loading.

## Security

This app has **no authentication layer**. Anyone who can reach the web port can
read your full health history and chat with the agent as you. Treat it like a
local-only tool.

**Safe defaults:**
- Bind to `127.0.0.1` (`WEB_HOST=127.0.0.1`) if you only use it from the same
  machine. The default `0.0.0.0` exposes it to your whole LAN.
- For remote access, put it behind **Tailscale**, a WireGuard VPN, or an SSH
  tunnel — *not* a public reverse proxy.
- Never expose the port directly to the internet. Your `.env` file contains
  your Garmin password and an Anthropic API key; the database contains years
  of biometric data.
- Anthropic API key: scope it to this project and set a monthly budget cap in
  the Anthropic console so a runaway loop or leaked key can't drain your account.

## Cost

You pay for Anthropic API usage. There is no subscription to this project.

Rough costs at typical usage (Sonnet 4.6, the default model):
- **Chat**: a handful of cents per long conversation thanks to prompt caching
  (the ~12k-char medical knowledge base is cached, cutting ~80% of system-prompt
  tokens on repeat queries).
- **Daily AI health scan**: a few cents per run.
- **Opus** (`CLAUDE_MODEL=claude-opus-4-7`): roughly 5× more per call. Worth it
  for deep weekly reports; overkill for everyday chat.
- Set a monthly spend cap in the [Anthropic console](https://console.anthropic.com).

### Free alternative: bring-your-own LLM

If you don't want to pay for the API at all, the dashboard has a **"Copy
prompt"** button that serialises your full health context (system prompt + last
90 days of data, minified and rounded) into a single block. Paste it into any
free LLM — Claude.ai free tier, ChatGPT, Gemini, a local model — and you get
the same analytical depth without an API key. You lose live tool-calling (the
LLM can't pull more data on demand), but for one-off "what does my week look
like" questions it works well.

## Privacy

All data stays local. Nothing is sent to external servers except:
- Garmin Connect API (to fetch your own data)
- Anthropic API (to generate AI responses — only the content of your queries and health summaries, not raw data)

## Troubleshooting

- **Login issues**: Delete the token directory (default `~/.garminconnect`, or `TOKEN_DIR` if set) to clear stale tokens, then re-run the fetcher
- **No data in dashboard**: Run the fetcher first. The dashboard now auto-refreshes its cache every 60 s; if data still doesn't appear, restart the web server to trigger a full 90-day cache rebuild.
- **Database locked**: Only one process should write to a `garmin.db` at a time. In multi-user mode each user has their own DB file, so there is no contention between users.
- **Duplicate processes on restart**: `run-user.sh` is now idempotent — it skips launching anything that's already running for that user (matched by `SQLITE_DB_PATH` in `/proc/<pid>/environ`). Re-running is safe and is exactly how the 10-minute self-heal cron works.
- **Steps / daily metrics seem one day behind** (BST/non-UTC timezones): Older versions of the fetcher stored `daily_stats` rows under the previous UTC date. The current code uses noon-UTC of the requested date as the timestamp, so `daily_stats.date` always matches the local calendar day. If you have stale mis-dated rows, delete them and let the fetcher rewrite them: `DELETE FROM daily_stats WHERE date >= 'YYYY-MM-DD'`.

## For developers

See [CLAUDE.md](CLAUDE.md) for architecture details, file map, and instructions for extending the agent.

## Garmin Connect — unofficial API

This project uses the community [`garminconnect`](https://github.com/cyberjunky/python-garminconnect)
library, which scrapes Garmin Connect's web endpoints. Garmin does not publish
a public API for personal data, so:

- **Use at your own risk.** Garmin's terms of service permit personal data
  access through their official apps. Automated scraping is in a grey area.
  Excessive polling could in theory get your account rate-limited or locked.
- The default fetcher runs every 5 minutes, which has been fine in practice
  for the maintainers, but Garmin can change their endpoints or anti-bot
  measures at any time.
- If Garmin Connect updates break things, expect a delay before the upstream
  `garminconnect` library catches up.
- **MFA / 2FA**: if your Garmin account has MFA enabled, you'll be prompted
  once during the initial login to cache an OAuth token; subsequent fetches
  use the token.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Short version: it's a hobby project, I
can't promise fixes or reviews, but well-scoped bug reports and small PRs are
welcome.

## License

[MIT](LICENSE) © 2026 Dan Whelan.

The bundled `garmin-grafana/` module retains its upstream BSD-3-Clause license
(see `garmin-grafana/LICENSE`).

## Credits

The `garmin-grafana/` data-ingestion module is derived from [**garmin-grafana**](https://github.com/arpanghosh8453/garmin-grafana) by [Arpan Ghosh](https://github.com/arpanghosh8453), which provides the Garmin Connect polling logic (`garmin_fetch.py`). This project adapts it to write to SQLite instead of InfluxDB and pairs it with a Claude-powered analysis layer and a FastAPI dashboard. Huge thanks to the upstream project — its original LICENSE is retained in `garmin-grafana/LICENSE`.
