# Handover: Glucose (Libre CGM) + Meal Log

Covers **what was built**, **how to set it up on the Pi**, **how it works
inside**, and **what to check / tune** once real sensor data arrives.
Roadmap and background: [`glucose.md`](glucose.md). For an AI agent taking over, with the full to-do list: [`handover-glucose-llm.md`](handover-glucose-llm.md).

---

## 1. What you get

| Where | What |
|---|---|
| Dashboard → *Fitness & Body* → **Glucose (CGM)** | Glucose curve (mmol/L) with the 3.9–7.8 "tight range" band, meal markers (▼), greyed-out artefacts, and a strip with mean / % in range / variability (CV). |
| Dashboard → *Fitness & Body* → **Meal Responses** | Per-food medians (rise, dip, servings, ✓ reliable after 3), a table of every meal's rise / time-to-peak / area / dip / flags, and "unlogged rises (did you eat?)". |
| Journal tab → **Log a Meal** | Time + description + macros. 📷 **Photo estimate** (Claude vision), **Barcode** look-up (Open Food Facts, with optional camera scan), one-tap chips for your most-logged meals, edit / delete, and each meal's response chip. |
| Journal tab → **Import Glucose** | LibreView CSV import (fallback when not using Juggluco). |
| Health Agent chat / scans | Tools `get_glucose_data` and `get_meal_responses`; daily summaries gain `glucose_*`, `meals_logged`, `meal_carbs_g`, `first_meal_hour`, `last_meal_hour`, so trends, anomalies, correlations and the Entities chart builder see them too. |

Each Libre sensor lasts 14 days, so glucose analytics only exist while one is
worn. The meal log works with or without a sensor.

---

## 2. Pi setup (one-off)

Paths below match the existing deployment (`/home/dan/garmin-data`, venv in
`.venv`, users `dan` / `helen`).

### 2.1 Update the code

```bash
cd /home/dan/garmin-data
git fetch origin
git checkout ccr-41f0f8c2-0gtqri   # or `main` once PR #85 is merged
git pull
.venv/bin/pip install -e garmin-grafana -e garmin-insights   # no new dependencies; safe to re-run
```

There are no migrations to run. The new tables (`glucose_readings`, `meals`)
are created automatically the first time they're used.

### 2.2 Phone: Juggluco (Android, Libre 2)

1. Install **Juggluco** (Play Store or its GitHub releases page).
2. **Check the sensor box:** it should say *FreeStyle Libre 2* and be in
   date. If it says plain *FreeStyle Libre* (Libre 1, NFC only), use
   **xDrip+** instead and tap the phone on the sensor at least every 8 h.
3. Apply the sensor, open Juggluco and tap the phone on it (NFC) to start
   it. Warm-up takes 60 min; after that it reads every minute over Bluetooth.
   Once Juggluco has started a sensor, LibreLink can't take it over (and the
   other way round), so use one app per sensor.
4. Juggluco → **Settings → Web server**: tick it on, and **untick "local
   only"** so the Pi can connect. The port is **17580**. If you set a
   secret there, put it in `NIGHTSCOUT_API_SECRET` below.
5. Android → Apps → Juggluco → Battery → **Unrestricted**, so it keeps
   running in the background.
6. Router: give the phone a **fixed LAN IP** (a DHCP reservation), e.g.
   `192.168.1.50`.

Check from the Pi that the phone answers:

```bash
curl "http://192.168.1.50:17580/api/v1/entries.json?count=3"
# → JSON list with "sgv" (mg/dL) and "date" (epoch ms)
```

If that path returns 404, try `.../sgv.json?count=3`, and point
`NIGHTSCOUT_URL` at that full URL. A URL ending in `.json` is used exactly
as given.

### 2.3 Env file

Add to `users/dan.env` (and `users/helen.env` if she wears one, with her
phone's IP):

```bash
NIGHTSCOUT_URL=http://192.168.1.50:17580
# NIGHTSCOUT_API_SECRET=...   # only if set in Juggluco / Nightscout
# NIGHTSCOUT_TOKEN=...        # Nightscout access-token alternative
# GLUCOSE_BACKFILL_DAYS=14    # look-back on the very first pull
```

`ANTHROPIC_API_KEY` is already set and also powers the photo estimate. The
barcode look-up needs the Pi to reach `world.openfoodfacts.org`, which it
can over normal internet.

### 2.4 Restart (the env file is only read at launch)

```bash
pkill -f garmin_grafana.garmin_fetch
pkill -f "garmin-insights web"
bash scripts/run-dan.sh
bash scripts/run-helen.sh           # this one owns the web server (START_WEB=true)
sudo systemctl restart garmin-insights-mcp@dan garmin-insights-mcp@helen   # MCP picks up the new tools
```

The */10 cron entries would also bring things back within 10 minutes.

### 2.5 Verify

```bash
tail -f logs/dan-fetch.log | grep -i glucose
# glucose_readings: upserted N Nightscout readings since ...
sqlite3 "$(grep ^SQLITE_DB_PATH users/dan.env | cut -d= -f2)" \
  "SELECT COUNT(*), MAX(time) FROM glucose_readings;"
```

Then open the dashboard (`http://pi5:8081`, or the Tailscale name). After
about an hour of readings, the **Glucose (CGM)** chart appears under
*Fitness & Body*. The first 24 h show grey on purpose: early-sensor readings
are excluded.

### 2.6 Away from home

The fetcher only reaches the phone on the home network. Away from it, the
pull fails quietly (one warning line in the log). Juggluco keeps its
history, and the next successful pull backfills from the last stored
reading. For live data while out, put the phone and the Pi on Tailscale and
use the phone's Tailscale IP in `NIGHTSCOUT_URL`.

### 2.7 Without Juggluco (LibreLink app)

Use the LibreLink app as normal. Every few days go to libreview.com →
*Glucose History* → **Download glucose data** → Journal tab →
**Import Glucose**. Re-importing an overlapping file is safe. The CSV only
has 15-minute points, so peak times are coarser.

---

## 3. Daily use

1. **Log meals when you eat.** Photo is quickest. Check the estimate (the
   status line says what it was unsure of), correct it, and save. For
   packets, type or scan the barcode and enter the grams you ate.
2. **Repeat staples on purpose.** A food only gets "✓ reliable" after 3
   clean servings, because the same meal's response varies a lot day to day.
3. **Keep other meals and workouts out of the 2 h after a test meal** where
   you can. Overlapping meals and exercise flag a response, and flagged
   responses are left out of the food ranking.
4. **Check "Unlogged rises".** These are daytime rises with no meal logged.
   Either log what you ate or ignore them (stress and hard training raise
   glucose too).
5. Ask the agent, e.g. *"which of my meals gave the biggest dip?"* or *"why
   was I sluggish at 3pm on Tuesday?"*

---

## 4. How it works

### 4.1 Data flow

```
Libre 2 ──BLE──▶ Juggluco (phone) ──HTTP /api/v1/entries.json──▶ garmin_fetch loop
                                                                  └─ glucose_fetch.fetch_from_env()
LibreView CSV ──▶ POST /api/glucose/import ─────────────────────▶ glucose_readings (mg/dL, UTC)
Journal form / photo / barcode ──▶ POST /api/meals ─────────────▶ meals (local wall-clock time)

glucose_readings ─▶ insights/glucose.py ─▶ flags + daily metrics ─┐
meals + glucose  ─▶ insights/meals.py   ─▶ per-meal responses ────┼▶ /api/glucose, /api/meals
                                                                  ├▶ CacheBuilder → daily_summaries
                                                                  └▶ agent tools
```

### 4.2 Files

| File | Role |
|---|---|
| `garmin-grafana/src/garmin_grafana/glucose_fetch.py` | Nightscout-style pull. Resumes from the newest stored reading minus 30 min and filters client-side, because Juggluco may ignore `find[]`. Keeps values 40–500 mg/dL (anything outside is an error code). Called at the end of every fetch cycle in `garmin_fetch.py`. |
| `garmin-insights/src/garmin_insights/insights/glucose.py` | Storage, LibreView CSV parser, dedupe, quality flags, daily metrics, chart series. |
| `garmin-insights/src/garmin_insights/insights/meals.py` | Meal storage, staples, daily meal metrics, Open Food Facts look-up, Claude photo estimate, response maths, food summary, unlogged rises. |
| `db/cache.py` | Merges `glucose_*` and meal metrics into `daily_summaries`; baselines `glucose_mean_mmol` / `glucose_cv_pct` / `glucose_overnight_mmol`. |
| `tools/query_tools.py` | `get_glucose_data`, `get_meal_responses`. |
| `agent.py` | System-prompt rule: mmol/L, personal trend only, no diabetes wording, no single-meal verdicts, dips explain slumps. |
| `web/app.py` | `/api/glucose`, `/api/glucose/import`, `/api/meals` (GET / POST / DELETE), `/api/meals/estimate`, `/api/meals/barcode/{code}`. |
| `web/static/` | Glucose chart, Meal Responses section, Journal meal form + glucose import. |
| `tests/test_glucose.py`, `tests/test_meals.py` | 42 tests. |

### 4.3 Tables

- **`glucose_readings`**: `time` (UTC `...Z`), `source` (`nightscout` /
  `libreview_csv`), `mg_dl`, `trend`, `device`, `sensor_id`, `record_type`.
  The primary key is `(time, source)`, so re-pulls and re-imports overwrite
  rather than duplicate.
- **`meals`**: `id`, `eaten_at` (local `YYYY-MM-DDTHH:MM`), `date`,
  `description`, `carbs_g`, `sugar_g`, `protein_g`, `fat_g`, `fibre_g`,
  `kcal`, `tags` (comma list), `source` (`manual` / `photo` / `barcode` /
  `repeat`), `estimate_json` (Claude's raw estimate, kept for audit).

### 4.4 Glucose quality rules (`insights/glucose.py`)

| Rule | Logic | Constant |
|---|---|---|
| Source dedupe | One reading per minute, preferring Nightscout. A CSV point within 5 min of a Nightscout point is dropped. | `_SOURCE_PRIORITY` |
| Sensor sessions | A new session starts on a serial change, after any gap ≥ 24 h, or after a gap ≥ 55 min once ≥ 13 days have run (a sensor swap). 15 days of history before the window are loaded, so a sensor that started earlier isn't mistaken for a new one. | `_SWAP_*`, `_NEW_SESSION_GAP_H` |
| Early sensor | The first 24 h of each session are flagged and excluded. | `EARLY_SENSOR_HOURS` |
| Compression low | Between 22:00 and 08:00: ≥ 1.5 mmol/L below the previous hour's median, falling at ≥ 0.04 mmol/L per min, recovering within 2 h. Flagged and excluded. | `_COMPRESSION_*` |
| Coverage gate | A day's metrics are only reported at ≥ 50 % coverage. | `MIN_DAY_COVERAGE_PCT` |

Daily metrics: mean, SD, CV, % in 3.9–7.8 / above / below, min, max,
overnight mean (00–06), fasting proxy (04–06), coverage.

### 4.5 Meal response (`insights/meals.py`)

| Metric | Definition |
|---|---|
| baseline | Median glucose in the 30 min before `eaten_at` |
| rise / time to peak | Highest point above baseline within 0–120 min |
| iAUC | Area above baseline, 0–120 min, in mmol/L·h |
| dip | How far glucose falls below baseline at 120–240 min (counted only if ≥ 0.3 mmol/L) |
| flags | `overlap` (another meal within 2 h), `exercise` (a Garmin activity during the rise), `partial` (< 85 % coverage) |

A response needs some glucose in the 30 min before the meal and ≥ 60 %
coverage after it. Food summaries use clean responses only and are
`reliable` from 3 servings. An unlogged rise is a daytime (06–23) rise of
≥ 2 mmol/L within 60 min, with no meal within ±60 min, no activity
overlapping it, and at most one flagged per 3 h.

Times: glucose is stored in UTC and converted to local wall-clock (DST-aware,
the same helper as the overnight analysis). Meals are stored in local time.
`last_meal_hour` belongs to the date it was eaten, while sleep is keyed to
the *wake* date, so the night after a 21:00 dinner on the 3rd is the sleep
row dated the 4th.

### 4.6 Photo estimate

The browser shrinks the photo to ≤ 1280 px JPEG and posts it to
`/api/meals/estimate`. The server makes one Claude call with a forced tool
(`record_meal_estimate`), so the reply is always structured: items with
grams, macros, tags, confidence and notes. **Nothing is saved until you press
Save.** The model is `CLAUDE_MODEL`, and the cost is roughly one small image
call per photo.

### 4.7 Barcode

`/api/meals/barcode/{code}` queries the Open Food Facts v2 API for per-100 g
macros and a serving size. The form scales them to the grams you enter
(default: one serving if known, else 100 g). The **Scan barcode** button only
appears in browsers that support `BarcodeDetector` (Chrome on Android).
Typing the digits always works.

---

## 5. To check and tune once real data arrives

1. **Juggluco endpoint**: confirm with the `curl` in 2.2. The parser
   accepts both `date` (ms) and `dateString`.
2. **Compression lows**: after a few nights, look at grey dips on the chart.
   If real lows are being hidden, or obvious lying-on-the-arm dips aren't,
   adjust `_COMPRESSION_DROP_MMOL` / `_COMPRESSION_MIN_RATE` in `glucose.py`.
3. **Sensor swaps without a serial number**: check the "Sensors" count in
   the stats strip after your first swap. The Nightscout feed may not carry
   the serial, so detection relies on the gap rule.
4. **Meal-time accuracy**: responses assume `eaten_at` is when you started
   eating. If peaks routinely land at 0–10 min, meals are being logged late.
5. **Photo estimates**: compare a few against packet labels to calibrate
   how far to trust them.

## 6. Troubleshooting

| Symptom | Check |
|---|---|
| No glucose chart | `SELECT COUNT(*) FROM glucose_readings;`. If it's zero, look in `logs/<user>-fetch.log` for `glucose fetch failed`, then run the `curl` from the Pi. |
| `glucose fetch failed: ... Connection refused / timed out` | Phone away from home, Juggluco killed by battery saver, "local only" still ticked, or the phone's IP changed. |
| Chart present but no daily numbers | Coverage < 50 %, or still inside the first 24 h of a sensor. |
| Photo estimate 503 | `ANTHROPIC_API_KEY` isn't set for the user running the web server. |
| Photo estimate 502 | API error. The web log has the message. |
| Barcode 404 | Product isn't on Open Food Facts. Enter the macros by hand. |
| New agent tools missing in Claude Desktop / Cursor | Restart `garmin-insights-mcp@<user>`. |

Run the tests: `cd garmin-insights && python -m pytest tests/test_glucose.py tests/test_meals.py`.
