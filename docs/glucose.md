# Glucose (FreeStyle Libre) + Food + Garmin

Goal: combine CGM glucose, a food log and Garmin recovery data to answer
*what's my peak time of day?*, *which foods leave me sluggish?*, and *what
should I change and test next?*

Glucose is treated as a **personal trend**, never a diagnosis. The ranges used
(3.9–7.8 mmol/L, "time in tight range") describe typical non-diabetic
physiology; the sensor reads tissue fluid ~5–15 min behind blood, roughly
±10 % (worse when low).

## Getting readings in

| Route | When | Setup |
|---|---|---|
| **Juggluco** (Android, recommended) | Libre 2 | Install Juggluco, scan the sensor once with NFC; it then reads over Bluetooth every minute. Settings → *Web server* → enable (port 17580). Give the phone a fixed LAN IP and set `NIGHTSCOUT_URL=http://<phone-ip>:17580` in `users/<name>.env`. |
| **xDrip+** (Android) | Libre 1 (NFC-only) or Libre 2 | Enable its local web service, point `NIGHTSCOUT_URL` at it. With a Libre 1, scan at least every 8 h (the sensor only holds 8 h of 15-min history). |
| **Nightscout site** | Any uploader | `NIGHTSCOUT_URL=https://…` plus `NIGHTSCOUT_TOKEN` or `NIGHTSCOUT_API_SECRET`. |
| **LibreView CSV** | Official LibreLink app | libreview.com → Glucose History → *Download glucose data* → Journal tab → *Import Glucose*. Re-importing overlaps is safe. |

The fetcher pulls every cycle and resumes from the newest stored reading, so
time away from home Wi-Fi is backfilled once the phone is back (Juggluco
keeps its own history).

Notes on spare sensors: check the expiry date on the box. Scanning a sensor
with Juggluco stops LibreLink connecting to it — pick one app per sensor.

## Data quality (built in)

- **First 24 h of each sensor** — flagged and excluded from daily metrics.
- **Compression lows** — fast, deep, short overnight dips from lying on the
  sensor arm — flagged and excluded.
- **Coverage gate** — a day's metrics are reported only with ≥50 % sensor coverage.
- **Source dedupe** — a CSV point within 5 min of a 1-min Nightscout reading is dropped.

Pi setup and internals: [`handover-glucose.md`](handover-glucose.md). Agent/LLM handover + full to-do list: [`handover-glucose-llm.md`](handover-glucose-llm.md).

## Roadmap

1. **Glucose ingestion** ✅ — `glucose_readings`, Nightscout pull, CSV import,
   quality flags, daily metrics in `daily_summaries`, dashboard chart,
   `get_glucose_data` agent tool.
2. **Meal log** ✅ — photo → Claude vision macro estimate, Open Food Facts barcode
   lookup, one-tap staple meals. Per-meal response: peak rise, 2 h iAUC,
   time-to-peak, and the 2–4 h **post-meal dip** (Wyatt 2021, *Nat Metab* —
   predicts hunger / energy slumps). Unlogged-rise detection ("meal at 15:40?")
   and non-food rises labelled from Garmin activities (hard training) and the
   journal (alcohol, stress).
3. **Check-ins + analytics** — 2–3 h post-meal energy / focus / hunger sliders.
   Food ranking only for foods eaten ≥3 times (responses to identical meals
   vary — Hall 2018; PREDICT). Same food at different times of day; last-meal-
   to-sleep gap vs overnight HR nadir / HRV (ties into `insights/overnight.py`);
   meal response modified by last night's sleep, Body Battery, stress at meal
   time and post-meal walks (PREDICT: poor sleep worsens next-day responses).
   Sluggishness link: dips vs energy ratings, stress, Body Battery. Personal
   **peak-time-of-day** curve from energy ratings, glucose stability, Body
   Battery and stress by hour.
4. **"What to test next" engine** — ranks hypotheses by effect size × how easy
   to change × sensor days left, and turns each into a `start_experiment`
   protocol (e.g. *"dinner after 20:00: spike +40 %, deep sleep −12 min, n=6 —
   test eating by 19:30 for 5 days"*). Sensor planning (repeat staples, a
   standard test breakfast on days 2 / 7 / 12). Cycle-phase links for
   menstruating users (luteal glucose responses run higher). A per-person
   response model so food logging still gives *predicted* responses once the
   sensors run out.

## Prior art reused / considered

- Juggluco, xDrip+, Nightscout — collection (reused via the Nightscout entries API).
- LibreView CSV export format — import.
- `cgmquantify` (Python) — glycaemic-variability formulas (MAGE etc.) for phase 3.
- Open Food Facts — barcode → macros for phase 2.
- ZOE, Levels, Lingo, Veri, January AI do glucose + food; none combine it with
  Garmin recovery data or n-of-1 statistics.
