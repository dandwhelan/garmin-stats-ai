# LLM Handover — Glucose (Libre CGM) + Food + Garmin

For an AI coding agent picking up this workstream cold. Read this **whole
file**, then `CLAUDE.md` (repo-wide conventions), then
[`handover-glucose.md`](handover-glucose.md) (the human-facing Pi setup +
internals) and [`glucose.md`](glucose.md) (the product roadmap).

---

## 0. TL;DR

- **Goal:** combine FreeStyle Libre glucose data, a food log, and the
  existing Garmin data so the app can tell the user (a) their peak time of
  day, (b) which foods leave them sluggish or feeling worse, and (c) **what
  to change and test next**.
- **Done (phases 1–2):** glucose ingestion with quality filtering, the meal
  log (photo / barcode / staples), the glucose response for each meal, two
  agent tools, two dashboard sections, the Journal UI, 42 tests, and docs.
- **Where it lives:** branch `ccr-41f0f8c2-0gtqri`, **PR #85**
  (`dandwhelan/garmin-stats-ai`). It's a draft, CI is green on 3.11 and 3.12,
  and it has no merge conflicts. **Not merged.** Only the user merges.
- **Next:** see §7. The priorities are: real-data verification on the Pi,
  then phase 3 (energy check-ins, sluggishness link, peak-time-of-day curve,
  how the same food behaves at different times of day), then phase 4 (the
  "what to test next" engine).
- **Nothing has touched real hardware or live external APIs yet.** Juggluco,
  a LibreView export, Claude vision and Open Food Facts were all exercised
  only through tests and mocks. Treat §6 as the risk list.

---

## 1. The user and how to work with them

- **Owner:** Dan (repo `dandwhelan/garmin-stats-ai`). A second user, Helen,
  runs on the same Pi (multi-user mode; Helen's env owns the web server).
- **Style preferences** (from the user's settings): keep explanations
  **brief and to the point**, **offer options**, and **work autonomously**
  once a task is agreed. End a turn with a short A/B/C list of next steps.
- **Hardware:** an Android phone, plus spare **Libre sensors left over from
  a ZOE test**. The user reports they "seem NFC-style". They're most likely
  Libre 2 (NFC to start, then Bluetooth), possibly Libre 1 (NFC only). This
  hasn't been confirmed; the box label decides. The sensors may be past
  their expiry date.
- The app runs on a **Raspberry Pi 5** at `/home/dan/garmin-data` (venv
  `.venv`), started by cron via `scripts/run-user.sh`, and the dashboard is
  at `http://pi5:8081`. There's also an MCP server per user under systemd.
- **Git rules for this repo/session:** develop on the designated branch,
  open **draft** PRs, never merge, never force-push, and add the commit
  trailers given by the harness. Don't put model names in commits or PRs.

---

## 2. Current state

| Item | State |
|---|---|
| Branch | `ccr-41f0f8c2-0gtqri` (3 commits on top of `main` @ `067d5c6`) |
| PR | #85, draft, title "Libre CGM glucose ingestion + meal log with per-meal glucose response" |
| CI | `.github/workflows/tests.yml`: pytest on 3.11 and 3.12, green on head `9938bf8` |
| Tests | `garmin-insights/tests/test_glucose.py` (24), `test_meals.py` (18); full suite passes |
| Commits | `43c7542` phase 1 · `6a1f5ec` lifestyle-cache thread-safety fix (an unrelated intermittent CI failure) · `9938bf8` phase 2 + Pi handover doc |
| After this doc | `docs/handover-glucose-llm.md` (this file) and `scripts/dev/glucose_demo.py` |

If the PR has been merged by the time you read this, follow the harness
rules: restart the branch from the latest `main` and open a **new** PR.

---

## 3. Architecture

```
Libre 2 ─BLE→ Juggluco (Android) ─HTTP /api/v1/entries.json→ garmin_fetch loop (every 5 min)
                                                              └ garmin_grafana/glucose_fetch.py
LibreView CSV ─→ POST /api/glucose/import ────────────────────→ glucose_readings  (mg/dL, UTC)
Journal "Log a Meal" (manual / photo / barcode / staple) ─────→ meals             (local wall-clock)

insights/glucose.py : load → dedupe → flag_quality → day_metrics / summary / intraday_series
insights/meals.py   : get_meals → meal_response (per meal) → food_summary, unlogged_rises
          │
          ├→ db/cache.py CacheBuilder._load_range/_compose → daily_summaries (glucose_*, meals_*)
          ├→ tools/query_tools.py  get_glucose_data, get_meal_responses  (agent + MCP)
          └→ web/app.py  /api/glucose, /api/glucose/import, /api/meals*, → static/app.js
```

### 3.1 Files added or changed

| File | What it does / what changed |
|---|---|
| `garmin-grafana/src/garmin_grafana/glucose_fetch.py` | **New.** Nightscout-style pull. `entries_url()`, `parse_entries()` (keeps SGV 40–500 mg/dL; reads `date` ms or `dateString`), `fetch_entries()` (sends `count` + `find[date][$gte]`, then filters client-side too), `upsert()`, `fetch_from_env()` (no-op without `NIGHTSCOUT_URL`; resumes from `MAX(time)` minus 30 min; first run goes back `GLUCOSE_BACKFILL_DAYS`). Keeps its own copy of the DDL, because the two packages don't import each other. |
| `garmin-grafana/src/garmin_grafana/garmin_fetch.py` | Calls `glucose_fetch.fetch_from_env()` at the end of each bulk cycle, next to the environment / HA fetches. Failures are non-fatal. |
| `garmin-insights/src/garmin_insights/insights/glucose.py` | **New.** `SCHEMA_SQL`, `upsert_readings`, `load_readings` (dedupe: one reading per minute, preferring `nightscout`; a CSV point within 5 min of a Nightscout point is dropped), `parse_libreview_csv` (+ `GlucoseImportError`), `flag_quality` (sessions / `early_sensor` / `compression_low`), `day_metrics`, `daily_metrics`, `load_flagged` (loads 15 extra days before the window so session starts are correct), `intraday_series` (resamples to 5 / 15 / 60 min), `summary`. Reuses `_system_zone` / `_to_local_naive` from `insights/overnight.py` (DST-aware). |
| `garmin-insights/src/garmin_insights/insights/meals.py` | **New.** `SCHEMA_SQL`, `save_meal` / `get_meal` / `get_meals` / `delete_meal`, `recent_staples`, `daily_meal_metrics`, `lookup_barcode` (Open Food Facts v2), `estimate_from_photo` (Claude vision with a forced tool `record_meal_estimate`), `meal_response`, `analyse`, `food_summary`, `unlogged_rises` (5-min resample + `searchsorted`, linear time), `_activity_windows` (reads `activity_summary.time` as GMT). |
| `garmin-insights/src/garmin_insights/db/memory.py` | `_SCHEMA_SQL` now has `GLUCOSE_SCHEMA_SQL + MEALS_SCHEMA_SQL` appended. |
| `garmin-insights/src/garmin_insights/db/cache.py` | `_load_range` loads `glucose_daily_metrics` and `daily_meal_metrics`; `_compose` merges the keys listed in `GLUCOSE_SUMMARY_KEYS` plus the meal metrics. `_BASELINE_METRICS` gains `glucose_mean_mmol`, `glucose_cv_pct`, `glucose_overnight_mmol`. |
| `garmin-insights/src/garmin_insights/tools/query_tools.py` | `get_glucose_data` (36th tool) and `get_meal_responses` (37th), with handlers and schemas. The tool-list cache marker stays on the last definition. |
| `garmin-insights/src/garmin_insights/agent.py` | One new `_SYSTEM_PROMPT` bullet, "Glucose (CGM)": mmol/L, personal trend only, no diabetes wording, no single-meal verdicts, use dips to explain slumps. |
| `garmin-insights/src/garmin_insights/web/app.py` | `GlucoseImportRequest`, `MealRequest`, `MealEstimateRequest`; `_rebuild_days()`; endpoints `GET /api/glucose`, `POST /api/glucose/import`, `GET/POST /api/meals`, `DELETE /api/meals/{id}`, `POST /api/meals/estimate`, `GET /api/meals/barcode/{code}`. |
| `garmin-insights/src/garmin_insights/web/lifestyle_viz.py` | `_load_lock` around `_load_cache`. This fixed an intermittent "OrderedDict mutated during iteration" from `/api/lifestyle`'s parallel analytics. Unrelated to glucose, but it was blocking CI. |
| `web/static/index.html` | Sections `#glucose-section` and `#meal-response-section`; Journal `#meal-form` and `#glucose-import`. |
| `web/static/app.js` | `loadGlucose` (fetches `/api/glucose` and `/api/meals` in parallel), `renderGlucose` (meal ▼ markers, excluded-point dataset, tight-range band), `renderMealResponses`, the meal-form functions (`loadMealLog`, `saveMeal`, `_shrinkImage`, `estimateMealPhoto`, `lookupMealBarcode`, `scanBarcodePhoto`, `initMealForm`), `importGlucoseCsv`. Both sections are registered in `CHART_CATEGORIES` under **fitness**. |
| `web/static/style.css` | `.glucose-*`, `.meal-*`, `.resp-chip` styles, tokens only. |
| `users/*.env.example` | A commented `NIGHTSCOUT_*` / `GLUCOSE_BACKFILL_DAYS` block. |
| `CLAUDE.md` | Key-files rows, env vars, tables, endpoints, tool count 37. |
| `docs/glucose.md` | Plan + roadmap (phase 2 marked ✅). |
| `docs/handover-glucose.md` | Human handover: Pi setup, daily use, internals, tuning, troubleshooting. |
| `scripts/dev/glucose_demo.py` | Offline demo server with synthetic glucose and meals (see §5). |

### 3.2 Tables

```sql
glucose_readings(time TEXT /*UTC ...Z*/, source TEXT /*nightscout|libreview_csv*/, mg_dl REAL,
                 trend TEXT, device TEXT, sensor_id TEXT, record_type TEXT /*cgm|historic|scan*/,
                 PRIMARY KEY(time, source))
meals(id INTEGER PK, eaten_at TEXT /*local YYYY-MM-DDTHH:MM*/, date TEXT, description TEXT,
      carbs_g, sugar_g, protein_g, fat_g, fibre_g, kcal REAL, tags TEXT /*comma list*/,
      source TEXT /*manual|photo|barcode|repeat*/, estimate_json TEXT, created_at TEXT)
```

### 3.3 Keys added to `daily_summaries`

`glucose_mean_mmol, glucose_sd_mmol, glucose_cv_pct, glucose_tir_pct,
glucose_above_pct, glucose_below_pct, glucose_min_mmol, glucose_max_mmol,
glucose_overnight_mmol, glucose_fasting_mmol, glucose_coverage_pct`, plus
`meals_logged, meal_carbs_g, first_meal_hour, last_meal_hour` (decimal local
hours).

Two quirks:
- Only `SUMMARY_KEYS` are merged. `glucose_readings`,
  `glucose_compression_lows` and `glucose_early_sensor` stay out of the
  summaries.
- `last_meal_hour` sits on the date the meal was eaten. Sleep is keyed to the
  **wake** date, so dinner on day D pairs with the `sleep_summary` row dated
  D+1.

### 3.4 Key thresholds (all module-level constants)

| Constant | Value | File |
|---|---|---|
| `TIGHT_LOW_MMOL` / `TIGHT_HIGH_MMOL` | 3.9 / 7.8 | glucose.py |
| `EARLY_SENSOR_HOURS` | 24 | glucose.py |
| `_SWAP_MIN_GAP_MIN` / `_SWAP_MIN_SESSION_DAYS` / `_NEW_SESSION_GAP_H` | 55 / 13 / 24 | glucose.py |
| `_COMPRESSION_HOURS` / `_DROP_MMOL` / `_MIN_RATE` / `_MAX_MIN` / `_RECOVER_MMOL` | (22, 8) / 1.5 / 0.04 per min / 120 / 0.6 | glucose.py |
| `MIN_DAY_COVERAGE_PCT` | 50 | glucose.py |
| `_BASELINE_MIN` / `_PEAK_WINDOW_MIN` / `_DIP_WINDOW` | 30 / 120 / (120, 240) | meals.py |
| `_MIN_WINDOW_COVERAGE` / `_DIP_MIN_MMOL` / `RELIABLE_SERVINGS` | 0.6 / 0.3 / 3 | meals.py |
| `_UNLOGGED_RISE_MMOL` / `_WITHIN_MIN` / `_MEAL_GAP_MIN` / `_HOURS` | 2.0 / 60 / 60 / (6, 23) | meals.py |

---

## 4. Design decisions (and why). Don't undo these without a reason.

1. **Store mg/dL + UTC, show mmol/L + local time.** mg/dL is Nightscout's
   native unit and UTC is unambiguous. The user is in the UK, so everything
   user-facing (and the agent) uses mmol/L. There is **no units setting
   yet** (see §7, P3-9).
2. **Pull from the phone, not push to the Pi.** Juggluco and xDrip+ already
   serve a Nightscout API, and Juggluco keeps its history, so a pull
   backfills on its own after the phone has been away from home. A push
   receiver would need an authenticated public write endpoint.
3. **Artefacts are flagged, not deleted.** The raw data stays and the chart
   shows excluded points in grey. Metrics, responses and baselines use only
   clean readings.
4. **Meals use local wall-clock time.** That's how people log meals. Glucose
   is converted to local time with the DST-aware helper before comparing.
5. **Single-meal responses are never verdicts.** A food is `reliable` only
   from 3 clean servings, because responses to identical meals vary a lot
   (Hall 2018; Berry 2020). Responses flagged `overlap` or `exercise` are
   kept out of the food ranking.
6. **The photo estimate is never auto-saved.** It fills the form; the user
   corrects it and saves. It uses a forced tool call for structured output
   and passes no `thinking` param (forced `tool_choice` doesn't combine with
   extended thinking). The raw estimate is kept in `estimate_json`.
7. **Unlogged-rise detection excludes activities**, because hard training
   raises glucose too.
8. **Wording follows the repo's evidence-tier ethos:** "personal trend",
   never diabetes or prediabetes; the 3.9–7.8 band is described as typical
   non-diabetic physiology.
9. **Zero new Python dependencies.** `requests`, `pandas` and `numpy` were
   already present. The `np.trapezoid` → `np.trapz` fallback is there for
   numpy < 2.

---

## 5. How to develop and verify

### 5.1 Setup in a fresh container

```bash
pip install -e "garmin-insights[dev]"
pip install --no-deps -e garmin-grafana     # full install fails building fitparse in some sandboxes
pip install requests python-dotenv pytz garminconnect
cd garmin-insights && python -m pytest -q   # all should pass
```

### 5.2 Test conventions

- Pin the timezone in glucose and meal tests: `monkeypatch.setenv("TZ","UTC")`
  plus `overnight._system_zone.cache_clear()`. The zone lookup is
  `lru_cache`d. Both new test files have an autouse fixture for this.
- Use `api_client` / `sample_settings` from `tests/conftest.py` for API and
  cache tests. The stub agent has no `_client`, so inject a fake with
  `.messages.create()` for estimate tests (see `test_meals.py::_FakeClient`).
- Network calls (Open Food Facts, Nightscout) are monkeypatched; there are
  no live calls in tests.

### 5.3 Verifying the UI

```bash
cd garmin-insights && python ../scripts/dev/glucose_demo.py   # http://127.0.0.1:8099
```

In this cloud sandbox the CDNs (`cdn.jsdelivr.net`, `unpkg.com`) are
**blocked**, so the page loads without Chart.js. To get around it, fetch the
libraries from npm and route the requests in Playwright:

```bash
mkdir cdn && cd cdn && npm pack chart.js@4.4.1 marked@9.1.6 dompurify@3.1.6 leaflet@1.9.4
for f in *.tgz; do mkdir -p "${f%.tgz}" && tar -xzf "$f" -C "${f%.tgz}"; done
```

```js
await page.route(/(jsdelivr|unpkg)/, r => {
  const u = r.request().url();
  const map = { 'chart.umd': 'chart.js-4.4.1/package/dist/chart.umd.js',
                'marked.min': 'marked-9.1.6/package/marked.min.js',
                'purify.min': 'dompurify-3.1.6/package/dist/purify.min.js',
                'leaflet.js': 'leaflet-1.9.4/package/dist/leaflet.js' };
  const k = Object.keys(map).find(k => u.includes(k));
  return k ? r.fulfill({ path: 'cdn/' + map[k], contentType: 'application/javascript' })
           : r.fulfill({ body: '', contentType: 'text/css' });
});
// Category groups may be collapsed: remove the 'collapsed' class from .chart-group, then
// screenshot #glucose-section, #meal-response-section and (Journal tab) #meal-form.
```

The leaflet CSS integrity error that appears in the console is a by-product
of this workaround and harmless.

### 5.4 Repo conventions you must keep

- Write every chart colour in `app.js` as `tc('#hex')`, never a bare literal.
  CSS uses tokens only.
- A new dashboard section must be added to `CHART_CATEGORIES` by element id,
  or it falls into "More".
- A new agent tool needs a handler method plus a schema in
  `get_all_tools_anthropic()`. MCP picks it up automatically. A **write**
  tool must be hidden by `mcp_server.filter_tool_defs` unless `--allow-writes`
  is passed (check the write-tool list there).
- Update `CLAUDE.md` (key files, tables, endpoints, tool count) in the same
  PR as any change to those things.
- A UI string that the zh-CN catalog (PR #83) can't translate because it's
  built from numbers should follow the bilingual-renderer pattern from
  commit `0ef4705`. **The glucose and meal UI is English-only right now**
  (see §7, T-4).

---

## 6. Known gaps and unverified assumptions (the risk list)

| # | Risk | How to check / fix |
|---|---|---|
| R1 | **Juggluco endpoint path/format unverified.** The code assumes `GET /api/v1/entries.json` on port 17580 returning `[{sgv, date, direction, device}]`. | On the Pi: `curl "http://<phone>:17580/api/v1/entries.json?count=3"`. If the path differs, `NIGHTSCOUT_URL` can hold a full `.json` URL. |
| R2 | **xDrip+ web-service port/path unverified** (needed for Libre 1). | Check xDrip+ docs or settings; the same URL override applies. |
| R3 | **Sensor model unconfirmed** (Libre 1 vs 2) and expiry unknown. | Ask the user to read the box. |
| R4 | **Serial number probably absent from the Nightscout feed**, so sensor-swap detection relies on the gap rule. | After the first swap, check `summary()["sensors"]`. If it's wrong, consider Juggluco's sensor endpoint, or a manual "new sensor" button that writes a session marker. |
| R5 | **Compression-low thresholds are literature-plausible guesses**, never tuned on real nights. | Inspect grey dips after 3–5 nights; tune the `_COMPRESSION_*` constants; add a regression test from real data. |
| R6 | **LibreView CSV parsing was tested only on hand-written samples** (UK and US layouts). Real exports can have extra header lines or locale separators. | Import a real file; extend `parse_libreview_csv` and add a fixture from it with values anonymised. |
| R7 | **Photo estimate never called live.** Model id comes from `CLAUDE_MODEL`; image limits are 5 MB, jpeg/png/webp/gif. | Do one real call. Confirm the forced tool works on the configured model and check the latency on the Pi. |
| R8 | **Open Food Facts never called live** (sandbox network). | `curl https://world.openfoodfacts.org/api/v2/product/<ean>.json?fields=product_name,nutriments` from the Pi. |
| R9 | **`BarcodeDetector` may need a secure context.** The dashboard is plain `http://pi5:8081`. | If the Scan button never appears on the phone, the typed-digits fallback still works. Tailscale-serve HTTPS (already used for Web Bluetooth) also fixes it. |
| R10 | **The glucose chart uses a category x-axis,** so time gaps aren't drawn to scale. Meal markers snap to the next plotted point and are approximate for windows over 30 days (hourly buckets). | Move to a time scale (needs a Chart.js date adapter from an allowed CDN) or insert null points at gaps. |
| R11 | **`activity_summary.time` is assumed to be GMT** in `meals._activity_windows`. | Check against one known activity on the Pi; adjust if it's local time. |
| R12 | **Response timing assumes `eaten_at` is when eating started.** Late logging moves the baseline window onto the rise. | Phase 2.5: snap the meal to the detected rise onset when it is within ±30 min, or warn when the peak is under 10 min after the logged time. |
| R13 | **No glucose rules in the medical KB / `InsightScanner`,** so scans and the proactive scanner never mention glucose unless the agent calls the tools itself. | §7, P3-6 / P3-7. |
| R14 | **The portable prompt** gets the daily `glucose_*` / meal keys through the summaries, but no individual meals or responses. | §7, P3-8. |
| R15 | **A single-process web server** runs the photo-estimate call in the default executor; a slow API call takes a thread. | Fine at this scale; note it if adding more blocking calls. |

---

## 7. To-do list (priority order)

Each item has its acceptance criteria. Mark items done in `docs/glucose.md`
and update `CLAUDE.md` as you go.

### Phase 2.9: real-data shakedown (do first, with the user)
- **S-1** The user follows `handover-glucose.md` §2 on the Pi. Collect: the
  `curl` output from the phone, the `dan-fetch.log` lines, and
  `SELECT COUNT(*), MIN(time), MAX(time) FROM glucose_readings`.
  *Done when readings land every fetch cycle.*
- **S-2** Fix whatever R1, R2, R6 and R8 reveal, and add a regression test
  for each fix.
- **S-3** After 3–5 nights, tune the compression-low rule against real dips
  (R5) and confirm the sensor count after a swap (R4).
- **S-4** Do one live photo estimate and one barcode look-up from the phone
  (R7, R8, R9).

### Phase 3: check-ins and analytics
- **P3-1 Energy check-ins.** New table, e.g. `checkins(id, at local, energy
  1-5, focus 1-5, hunger 1-5, mood 1-5 optional, note)`. A Journal-tab
  widget with sliders and a **"2–3 h after your last meal"** nudge (the
  frontend can show a banner when the newest meal is 2–3 h old and has no
  check-in after it). Endpoints `GET/POST /api/checkins`. Daily summary
  keys `checkin_energy_mean` and `afternoon_energy`. Agent tool
  `get_checkins`. *Tests: CRUD, summary merge, API.*
- **P3-2 Sluggishness link.** For each meal, attach the nearest check-in at
  90–240 min, then report energy against dip / rise / iAUC / carbs (Spearman
  r with n and p, using `stats_utils` + BH-FDR across the pairs), and rank
  meals by the energy that follows them. Add this to `meals.analyse` output
  and the Meal Responses section. *Acceptance: with synthetic data where big
  dips precede low energy, the correlation is significant and the ranking
  correct.*
- **P3-3 Peak-time-of-day curve.** Hourly profile (local 06–23) combining:
  check-in energy (where present), glucose stability (rolling SD /
  time-in-range by hour), Garmin stress (`stress_intraday`) and body battery
  (`body_battery_intraday`). Z-score each against the user's own data and
  blend into a 0–100 index with a confidence band; report the top 2-hour
  window and the slump window. New dashboard section, probably under
  Overview or Lifestyle. Reuse the intraday loaders in
  `web/visualizations.py` (`intraday heatmap`). *Acceptance: synthetic data
  with a planted 10:00 peak and 15:00 slump is recovered.*
- **P3-4 Same food at different times of day.** In `food_summary`, split
  each description by time bucket (breakfast < 11, lunch 11–16, dinner > 16)
  and report the difference in median rise when both buckets have ≥2 clean
  servings. Also the evening vs morning response across all meals.
- **P3-5 Late meals and sleep.** Join `last_meal_hour` (day D) with the next
  night (summary D+1 and `overnight_cache` for night D→D+1): `hr_dip_pct`,
  `hr_nadir_pct_of_night`, HRV, deep sleep, and overnight glucose. Report
  the gap between last meal and sleep onset in hours, set against those
  values. Feed P4.
- **P3-6 KB rules** (`knowledge/medical.py`). Add evidence-tier rules with
  confounders and the right tier:
  - post-meal dip → hunger / energy (Wyatt 2021 *Nat Metab*, Tier B);
  - personal variability in responses (Berry 2020 *Nat Med* PREDICT 1;
    Zeevi 2015 *Cell*, Tier B);
  - poor sleep → worse next-day glucose response (Tsereteli 2022
    *Diabetologia*, Tier B);
  - post-meal walking lowers the rise (Buffey 2022 *Sports Med*
    meta-analysis, Tier A);
  - late eating → worse glucose and sleep (Tier B/C);
  - CGM accuracy caveats (Tier note).
  **Update `test_medical_kb.py` counts and the KB size note in `CLAUDE.md`**
  (the prompt-size budget is about 16.7k chars, so keep summaries short).
- **P3-7 Scanner findings.** Add `InsightScanner.scan_glucose()` (category
  `glucose`) emitting: today's or yesterday's high-CV or low-TIR day against
  baseline, big unlogged rises, a reliable worst food, and a large post-meal
  dip pattern. These then reach `generate_scan_report`'s injected context and
  the CLI scan. Respect concern directions; good days never alarm.
- **P3-8 Portable prompt.** Embed the last N meals with their responses and
  the `foods` summary in `build_portable_prompt`, compact like the workouts
  block.
- **P3-9 Units setting.** `GLUCOSE_UNITS=mmol|mgdl` per user
  (`config.settings_for_user` overlay). Convert at the API edge and in agent
  tool output. Internal keys keep the `_mmol` suffix, so add `units` to the
  payloads.

### Phase 4: "What to test next" engine
- **P4-1 Hypothesis generator.** A new module `insights/suggestions.py`.
  Candidates come from the phase 3 outputs:
  - late dinner → sleep or overnight HR;
  - a food with a big rise or dip → afternoon energy;
  - post-meal walk vs none (needs activities, or a "walked after" tag);
  - breakfast composition (protein-first vs carb-first, from macros);
  - carbs per meal ≥ X → dip;
  - meal order / fibre.

  Score each as effect size × confidence (n, p) × ease of change × sensor
  days left. Output: a statement, the evidence, and a proposed protocol
  (behaviour, arms, days, metric, expected direction).
- **P4-2 One-tap experiment.** Turn a suggestion into `start_experiment`.
  The current engine splits on **Garmin lifestyle-journal behaviours**, so
  either (a) extend `experiments` with a `source='meals'` behaviour resolver
  (days where a meal tag or time condition holds, e.g. "last meal before
  19:30"), or (b) ask the user to log a Garmin journal behaviour. **(a) is
  recommended.** Adjust `AnalysisEngine.evaluate_experiment` to accept a
  date set from a callable. *Tests:* resolver, evaluation, and the existing
  experiment tests still pass.
- **P4-3 Sensor planner.** Given sensor start and end (from sessions), plan
  the 14 days: repeat staples, a standard test breakfast on days 2 / 7 / 12,
  and one experiment arm per half. Show it in the Journal tab.
- **P4-4 Per-user response model.** Once there are ≥30 clean responses, fit
  a small regularised regression (carbs, sugar, fibre, protein, fat, time of
  day, last night's sleep score, previous-day body battery, stress at meal
  time, walked-after) to predict rise and dip. Show predicted vs actual, and
  use it to give **predicted** responses for meals logged with no sensor
  worn, clearly labelled as predictions. Keep it to numpy, or sklearn as an
  optional extra.
- **P4-5 Cycle-phase context** for female users (Helen). Compare glucose
  responses by phase from `menstrual_cycle` (luteal responses often run
  higher), framed as context, matching the existing cycle wording.

### Tech debt / polish
- **T-1** Time-scale glucose chart (R10).
- **T-2** Snap meals to rise onset, or warn about late logging (R12).
- **T-3** Agent write tool `log_meal` (hidden from MCP unless
  `--allow-writes`), so the user can log meals from chat: "I just had a
  ham sandwich".
- **T-4** zh-CN strings for the new UI, using the `0ef4705` pattern, once
  PR #83's catalog lands.
- **T-5** Show a meal photo thumbnail. Photos aren't stored today; storing
  them would need a size cap and a cleanup policy.
- **T-6** Handle LibreView export variants as they turn up (R6).
- **T-7** A "new sensor applied" button that writes an explicit session
  boundary (R4).
- **T-8** Confirm the `activity_summary.time` timezone (R11).

---

## 8. Ops cheat-sheet (the Pi)

```bash
cd /home/dan/garmin-data && git pull && .venv/bin/pip install -e garmin-grafana -e garmin-insights
pkill -f garmin_grafana.garmin_fetch; pkill -f "garmin-insights web"
bash scripts/run-dan.sh; bash scripts/run-helen.sh
sudo systemctl restart garmin-insights-mcp@dan garmin-insights-mcp@helen
tail -f logs/dan-fetch.log | grep -i glucose
```

The env vars per user are `NIGHTSCOUT_URL`, `NIGHTSCOUT_API_SECRET` or
`NIGHTSCOUT_TOKEN` (optional), and `GLUCOSE_BACKFILL_DAYS`. The photo
estimate uses the existing `ANTHROPIC_API_KEY` and `CLAUDE_MODEL`. Full
steps and troubleshooting are in `handover-glucose.md`.

## 9. Research references used

- Wyatt P. et al. 2021, *Nature Metabolism*: post-meal glucose dips predict
  hunger and energy intake (PREDICT 2).
- Berry S. et al. 2020, *Nature Medicine*: large person-to-person variation
  in meal responses (PREDICT 1).
- Zeevi D. et al. 2015, *Cell*: personalised glycaemic responses.
- Hall H. et al. 2018, *PLoS Biology*: "glucotypes"; variability in people
  without diabetes.
- Tsereteli N. et al. 2022, *Diabetologia*: sleep and next-day glucose
  response.
- Buffey A. et al. 2022, *Sports Medicine*: light walking after meals
  lowers glucose.
- Battelino T. et al. 2023 consensus / Shah V. et al. 2019: CGM metrics and
  normative non-diabetic ranges (time in tight range 3.9–7.8).

Verify any citation before putting it into the KB. The KB requires exact
author / year / journal.
