"""Shared pytest fixtures.

Two families live here:

* ``fitness_db`` / ``fake_settings`` — a small DB with the slow-moving
  fitness-marker tables, used by the fitness-marker tests.
* ``sample_db`` / ``sample_settings`` / ``api_client`` — a fuller ~120-day
  synthetic dataset plus a FastAPI test client, intended as the shared base
  for LifestyleService, VisualizationService, cache and API-endpoint tests.

Schemas are written out longhand rather than imported from garmin-grafana's
``sqlite_manager``: that package is a sibling, not a dependency, and a
conftest that imports it would break the whole suite whenever it is absent.
Keep the column lists here in sync with ``sqlite_manager.py``.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import types
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest


@asynccontextmanager
async def _noop_lifespan(app):
    """Replaces the app lifespan, which would build real agents from the env."""
    yield


@pytest.fixture
def fitness_db(tmp_path):
    """A temp SQLite DB populated with the slow-moving fitness-marker tables.

    Schema mirrors garmin-grafana's sqlite_manager.py. Timestamps are ISO8601
    so SqliteRepo._query parses them as a datetime index.
    """
    db = tmp_path / "garmin_test.db"
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE vo2_max (time TEXT, device TEXT, vo2_max_value REAL,
                              vo2_max_value_cycling REAL, PRIMARY KEY (time, device));
        CREATE TABLE fitness_age (time TEXT, device TEXT, chronological_age REAL,
                                  fitness_age REAL, achievable_fitness_age REAL,
                                  PRIMARY KEY (time, device));
        CREATE TABLE endurance_score (time TEXT, device TEXT, endurance_score INTEGER,
                                      PRIMARY KEY (time, device));
        CREATE TABLE hill_score (time TEXT, device TEXT, strength_score INTEGER,
                                 endurance_score INTEGER, overall_score INTEGER,
                                 PRIMARY KEY (time, device));
        CREATE TABLE race_predictions (time TEXT, device TEXT, time_5k REAL,
                                       time_10k REAL, time_half_marathon REAL,
                                       time_marathon REAL, PRIMARY KEY (time, device));
        """
    )
    cur.executemany(
        "INSERT INTO vo2_max VALUES (?,?,?,?)",
        [
            ("2026-06-01T12:00:00", "dev", 48.0, 42.0),
            ("2026-06-10T12:00:00", "dev", 49.0, 43.0),
            ("2026-06-20T12:00:00", "dev", 50.0, 43.0),
        ],
    )
    cur.executemany(
        "INSERT INTO fitness_age VALUES (?,?,?,?,?)",
        [
            ("2026-06-01T12:00:00", "dev", 40.0, 35.0, 33.0),
            ("2026-06-20T12:00:00", "dev", 40.0, 34.0, 33.0),
        ],
    )
    cur.executemany(
        "INSERT INTO endurance_score VALUES (?,?,?)",
        [("2026-06-01T12:00:00", "dev", 7200), ("2026-06-20T12:00:00", "dev", 7400)],
    )
    cur.executemany(
        "INSERT INTO hill_score VALUES (?,?,?,?,?)",
        [("2026-06-20T12:00:00", "dev", 60, 55, 58)],
    )
    cur.executemany(
        "INSERT INTO race_predictions VALUES (?,?,?,?,?,?)",
        [
            ("2026-06-01T12:00:00", "dev", 1500.0, 3120.0, 6900.0, 14400.0),
            ("2026-06-20T12:00:00", "dev", 1470.0, 3060.0, 6780.0, 14100.0),
        ],
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def fake_settings(fitness_db):
    """Minimal stand-in for Settings — SqliteRepo only reads sqlite_db_path."""
    return types.SimpleNamespace(sqlite_db_path=fitness_db)


# ==================================================================
# sample_db — a ~120-day synthetic dataset
# ==================================================================
#
# Sized and shaped so the analytics that z-score against a
# rolling(30, min_periods=7) baseline have real history to prime against
# (see lifestyle_viz._prime_start), and so the behaviour-driven analytics
# have enough logged events to clear their min-occurrence gates.

SAMPLE_DAYS = 120

# Raw Garmin-side tables. Mirrors garmin-grafana's sqlite_manager.py — only
# the columns the insights side actually reads are declared.
_RAW_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY, time TEXT, device TEXT,
    active_kilocalories REAL, bmr_kilocalories REAL,
    total_steps INTEGER, total_distance_meters REAL,
    highly_active_seconds INTEGER, active_seconds INTEGER,
    sedentary_seconds INTEGER, sleeping_seconds INTEGER,
    moderate_intensity_minutes INTEGER, vigorous_intensity_minutes INTEGER,
    min_heart_rate INTEGER, max_heart_rate INTEGER, resting_heart_rate INTEGER,
    stress_percentage REAL, high_stress_percentage REAL,
    body_battery_highest_value INTEGER, body_battery_lowest_value INTEGER,
    body_battery_charged_value INTEGER, body_battery_drained_value INTEGER,
    body_battery_at_wake_time INTEGER, body_battery_during_sleep INTEGER,
    average_spo2 REAL
);
CREATE TABLE IF NOT EXISTS sleep_summary (
    date TEXT PRIMARY KEY, time TEXT, sleep_start TEXT, sleep_end TEXT, device TEXT,
    sleep_time_seconds INTEGER, deep_sleep_seconds INTEGER,
    light_sleep_seconds INTEGER, rem_sleep_seconds INTEGER,
    awake_sleep_seconds INTEGER, average_spo2_value REAL, lowest_spo2_value REAL,
    average_respiration_value REAL, awake_count INTEGER, avg_sleep_stress REAL,
    sleep_score INTEGER, restless_moments_count INTEGER,
    avg_overnight_hrv REAL, body_battery_change INTEGER, resting_heart_rate INTEGER
);
CREATE TABLE IF NOT EXISTS lifestyle_journal (
    date TEXT, behavior TEXT, category TEXT, status INTEGER, value REAL,
    device TEXT, PRIMARY KEY (date, behavior)
);
CREATE TABLE IF NOT EXISTS environment_daily (
    date TEXT PRIMARY KEY, latitude REAL, longitude REAL,
    temp_max_c REAL, temp_min_c REAL, temp_mean_c REAL, apparent_temp_max_c REAL,
    precipitation_mm REAL, wind_max_kmh REAL, humidity_mean REAL, uv_index_max REAL,
    pm25 REAL, pm10 REAL, o3 REAL, no2 REAL, european_aqi REAL,
    pollen_alder REAL, pollen_birch REAL, pollen_grass REAL,
    pollen_mugwort REAL, pollen_olive REAL, pollen_ragweed REAL, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS menstrual_cycle (
    date TEXT PRIMARY KEY, time TEXT, device TEXT, cycle_start_date TEXT,
    current_day_of_cycle INTEGER, current_cycle_phase TEXT,
    cycle_length INTEGER, predicted_cycle_length INTEGER, period_length INTEGER,
    menstrual_flow TEXT, pregnancy_status TEXT, symptoms TEXT, mood TEXT,
    notes TEXT, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS training_status (
    time TEXT, device TEXT, training_status TEXT,
    training_status_feedback_phrase TEXT, weekly_training_load REAL,
    fitness_trend TEXT, acwr_percent REAL, daily_training_load_acute REAL,
    daily_training_load_chronic REAL, max_training_load_chronic REAL,
    min_training_load_chronic REAL, daily_acute_chronic_workload_ratio REAL,
    heat_acclimation_percentage REAL, altitude_acclimation_percentage REAL,
    heat_trend TEXT, altitude_trend TEXT, current_altitude REAL,
    PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS training_readiness (
    time TEXT, device TEXT, level TEXT, score INTEGER, sleep_score INTEGER,
    sleep_score_factor_percent INTEGER, recovery_time INTEGER,
    recovery_time_factor_percent INTEGER, acwr_factor_percent INTEGER,
    acute_load REAL, stress_history_factor_percent INTEGER,
    hrv_factor_percent INTEGER, PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS stress_intraday (
    time TEXT, device TEXT, stress_level INTEGER, PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS body_battery_intraday (
    time TEXT, device TEXT, body_battery_level INTEGER, PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS heart_rate_intraday (
    time TEXT, device TEXT, heart_rate INTEGER, PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS steps_intraday (
    time TEXT, device TEXT, steps_count INTEGER, PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS breathing_rate_intraday (
    time TEXT, device TEXT, breathing_rate REAL, PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS hrv_intraday (
    time TEXT, device TEXT, hrv_value INTEGER, PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS vo2_max (
    time TEXT, device TEXT, vo2_max_value REAL, vo2_max_value_cycling REAL,
    PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS fitness_age (
    time TEXT, device TEXT, chronological_age REAL, fitness_age REAL,
    achievable_fitness_age REAL, PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS endurance_score (
    time TEXT, device TEXT, endurance_score INTEGER, PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS hill_score (
    time TEXT, device TEXT, strength_score INTEGER, endurance_score INTEGER,
    hill_score_classification_id INTEGER, overall_score INTEGER,
    hill_score_feedback_phrase_id INTEGER, vo2_max_precise_value REAL,
    PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS race_predictions (
    time TEXT, device TEXT, time_5k REAL, time_10k REAL,
    time_half_marathon REAL, time_marathon REAL, PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS body_composition (
    time TEXT, device TEXT, weight REAL, bmi REAL, body_fat REAL,
    body_water REAL, bone_mass REAL, muscle_mass REAL, physique_rating REAL,
    visceral_fat REAL, metabolic_age REAL, PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS activity_summary (
    activity_id INTEGER PRIMARY KEY, time TEXT, device TEXT,
    activity_name TEXT, activity_type TEXT, distance REAL,
    elapsed_duration REAL, moving_duration REAL, average_speed REAL,
    max_speed REAL, calories REAL, bmr_calories REAL, average_hr REAL,
    max_hr REAL, location_name TEXT, lap_count INTEGER,
    hr_time_in_zone_1 REAL, hr_time_in_zone_2 REAL, hr_time_in_zone_3 REAL,
    hr_time_in_zone_4 REAL, hr_time_in_zone_5 REAL, avg_run_cadence REAL,
    max_run_cadence REAL, avg_stride_length REAL, avg_vertical_oscillation REAL,
    avg_vertical_ratio REAL, avg_ground_contact_time REAL, avg_power REAL,
    max_power REAL, norm_power REAL
);
CREATE TABLE IF NOT EXISTS activity_gps (
    time TEXT, activity_id INTEGER, device TEXT, latitude REAL, longitude REAL,
    altitude REAL, distance REAL, duration_seconds REAL, heart_rate REAL,
    speed REAL, grade_adjusted_speed REAL, running_efficiency REAL,
    cadence INTEGER, fractional_cadence REAL, temperature REAL
);
CREATE TABLE IF NOT EXISTS sleep_intraday (
    time TEXT, device TEXT, spo2 REAL, respiration REAL, sleep_hr REAL,
    sleep_stress REAL, body_battery REAL, hrv REAL, sleep_stage TEXT,
    movement REAL, restlessness REAL, PRIMARY KEY (time, device)
);
CREATE TABLE IF NOT EXISTS ha_sensor_daily (
    date TEXT NOT NULL, entity_id TEXT NOT NULL, mean_value REAL,
    min_value REAL, max_value REAL, overnight_mean REAL, unit TEXT,
    fetched_at TEXT, PRIMARY KEY (date, entity_id)
);
"""

# Days (counting back from today) given an illness-like strain signature:
# RHR and respiration up, HRV down, together. Gives illness_radar /
# inflammation_index / scan_composite_strain something real to find.
_STRAIN_WINDOW = range(40, 45)


def _sample_rows(days: int = SAMPLE_DAYS):
    """Deterministically generate one record per day, newest day = today.

    Signals deliberately planted in the data:
      * weekend sleep midpoints shift ~70 min later  -> social jet lag
      * a 5-day RHR-up / HRV-down / respiration-up window -> illness radar
      * alcohol logged with a numeric dose, worse sleep that night
        -> dose-response + behaviour impact
      * a summer heat + pollen ramp -> environment/recovery correlations
    """
    rng = random.Random(20260731)
    today = datetime.utcnow().date()
    rows = []

    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        date = day.isoformat()
        weekend = day.weekday() >= 5
        strain = offset in _STRAIN_WINDOW

        # Alcohol on ~1 night in 4, weighted to weekends, with a dose.
        drinks = 0
        if rng.random() < (0.55 if weekend else 0.15):
            drinks = rng.choice([1, 2, 2, 3, 4])

        rhr = 55 + rng.gauss(0, 1.6) + (4.5 if strain else 0) + 1.2 * drinks
        hrv = 62 - rng.gauss(0, 4.0) - (11 if strain else 0) - 3.0 * drinks
        resp = 14.2 + rng.gauss(0, 0.4) + (1.4 if strain else 0)
        sleep_score = int(
            max(20, min(95, 82 + rng.gauss(0, 6) - 7 * drinks - (12 if strain else 0)))
        )
        sleep_secs = int(max(3 * 3600, rng.gauss(7.1 * 3600, 2400) - drinks * 900))

        # Sleep midpoint drifts later at weekends -> social jet lag signal.
        bed_hour = 23.2 + (1.2 if weekend else 0) + rng.gauss(0, 0.35)
        sleep_start = datetime.combine(day - timedelta(days=1), datetime.min.time()) \
            + timedelta(hours=bed_hour)
        sleep_end = sleep_start + timedelta(seconds=sleep_secs)

        steps = int(max(1200, rng.gauss(9000, 3000) - (3000 if strain else 0)))
        # A seasonal heat/pollen ramp across the window.
        season = math.sin((days - offset) / days * math.pi)

        rows.append({
            "date": date,
            "weekend": weekend,
            "strain": strain,
            "drinks": drinks,
            "rhr": round(rhr, 1),
            "hrv": round(hrv, 1),
            "resp": round(resp, 1),
            "sleep_score": sleep_score,
            "sleep_secs": sleep_secs,
            "sleep_start": sleep_start,
            "sleep_end": sleep_end,
            "steps": steps,
            "stress": round(max(5, rng.gauss(32, 9) + (10 if strain else 0)), 1),
            "bb_high": int(max(30, min(100, rng.gauss(78, 10)))),
            "bb_low": int(max(5, rng.gauss(24, 8))),
            "spo2": round(rng.gauss(95.5, 1.0), 1),
            "temp": round(12 + 14 * season + rng.gauss(0, 2), 1),
            "aqi": round(max(5, rng.gauss(34, 12) + 10 * season), 1),
            "pm25": round(max(1, rng.gauss(9, 4) + 4 * season), 1),
            "pollen_grass": round(max(0, rng.gauss(18, 12) * season), 1),
            "caffeine_late": rng.random() < 0.28,
            "late_meal": rng.random() < 0.22,
            # Training load: chronic drifts slowly, acute is spikier, so ACWR
            # moves through the "load spike" band without being pinned there.
            "acute_load": round(max(50, rng.gauss(320, 70)), 1),
            "chronic_load": round(max(80, rng.gauss(300, 25)), 1),
            "acwr": round(max(0.4, rng.gauss(1.05, 0.22)), 2),
            "heat_accl": round(max(0, min(100, 30 + 55 * season + rng.gauss(0, 5))), 1),
            "weight_kg": round(78.5 - (days - offset) * 0.012 + rng.gauss(0, 0.35), 2),
            "body_fat": round(19.5 - (days - offset) * 0.004 + rng.gauss(0, 0.3), 2),
            "bedroom_temp": round(17.5 + 4.5 * season + rng.gauss(0, 0.7), 1),
        })
    return rows


def _write_sample_db(path: str, rows) -> None:
    from garmin_insights.db.memory import MemoryStore

    MemoryStore(types.SimpleNamespace(sqlite_db_path=path)).initialise_schema()

    conn = sqlite3.connect(path)
    conn.executescript(_RAW_SCHEMA)
    cur = conn.cursor()

    for r in rows:
        noon = f"{r['date']}T12:00:00"
        cur.execute(
            "INSERT OR REPLACE INTO daily_stats (date, time, device, resting_heart_rate,"
            " total_steps, total_distance_meters, active_kilocalories, sedentary_seconds,"
            " sleeping_seconds, stress_percentage, high_stress_percentage,"
            " body_battery_highest_value, body_battery_lowest_value,"
            " body_battery_during_sleep, average_spo2, moderate_intensity_minutes,"
            " vigorous_intensity_minutes, min_heart_rate, max_heart_rate)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["date"], noon, "testdev", r["rhr"], r["steps"], r["steps"] * 0.78,
             r["steps"] * 0.045, 33000, r["sleep_secs"], r["stress"], r["stress"] / 3,
             r["bb_high"], r["bb_low"], r["bb_high"] - r["bb_low"], r["spo2"],
             int(r["steps"] / 400), int(r["steps"] / 1800), 48, 146),
        )
        cur.execute(
            "INSERT OR REPLACE INTO sleep_summary (date, time, sleep_start, sleep_end,"
            " device, sleep_time_seconds, deep_sleep_seconds, light_sleep_seconds,"
            " rem_sleep_seconds, awake_sleep_seconds, average_respiration_value,"
            " awake_count, avg_sleep_stress, sleep_score, restless_moments_count,"
            " avg_overnight_hrv, body_battery_change, resting_heart_rate,"
            " average_spo2_value, lowest_spo2_value)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            # sleep_summary.time is the WAKE timestamp (sleepEndTimestampGMT),
            # not noon — garmin_fetch.py stamps it that way, and the sleep
            # analytics derive the midpoint from it.
            (r["date"], r["sleep_end"].isoformat(), r["sleep_start"].isoformat(),
             r["sleep_end"].isoformat(),
             "testdev", r["sleep_secs"], int(r["sleep_secs"] * 0.19),
             int(r["sleep_secs"] * 0.52), int(r["sleep_secs"] * 0.22),
             int(r["sleep_secs"] * 0.07), r["resp"], 2 + r["drinks"],
             r["stress"] / 2, r["sleep_score"], 8 + r["drinks"] * 3, r["hrv"],
             r["bb_high"] - r["bb_low"], r["rhr"], r["spo2"], r["spo2"] - 3.5),
        )
        cur.execute(
            "INSERT OR REPLACE INTO environment_daily (date, latitude, longitude,"
            " temp_max_c, temp_min_c, temp_mean_c, apparent_temp_max_c, humidity_mean,"
            " uv_index_max, pm25, pm10, o3, no2, european_aqi, pollen_grass,"
            " pollen_birch, precipitation_mm, wind_max_kmh)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["date"], 51.5074, -0.1278, r["temp"] + 4, r["temp"] - 4, r["temp"],
             r["temp"] + 5, 68.0, 4.2, r["pm25"], r["pm25"] * 1.7, 61.0, 22.0,
             r["aqi"], r["pollen_grass"], r["pollen_grass"] * 0.3, 0.4, 18.0),
        )

        cur.execute(
            "INSERT OR REPLACE INTO training_status (time, device, training_status,"
            " weekly_training_load, fitness_trend, acwr_percent,"
            " daily_training_load_acute, daily_training_load_chronic,"
            " heat_acclimation_percentage, altitude_acclimation_percentage,"
            " heat_trend, altitude_trend, current_altitude)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (noon, "testdev", "MAINTAINING", r["steps"] / 12.0, "STABLE",
             r["acwr"], r["acute_load"], r["chronic_load"],
             r["heat_accl"], 12.0, "STABLE", "STABLE", 35.0),
        )
        cur.execute(
            "INSERT OR REPLACE INTO training_readiness (time, device, level, score,"
            " sleep_score, recovery_time, acute_load) VALUES (?,?,?,?,?,?,?)",
            (noon, "testdev", "MODERATE", max(1, min(100, r["sleep_score"] - 5)),
             r["sleep_score"], 600, r["acute_load"]),
        )
        cur.execute(
            "INSERT OR REPLACE INTO body_composition (time, device, weight, bmi,"
            " body_fat, body_water, bone_mass, muscle_mass, visceral_fat,"
            " metabolic_age) VALUES (?,?,?,?,?,?,?,?,?,?)",
            # weight in grams + metabolic age in ms, as Garmin reports them —
            # stats_utils.to_kg / metabolic_age_years normalise on read.
            (noon, "testdev", r["weight_kg"] * 1000, r["weight_kg"] / (1.78 ** 2),
             r["body_fat"], 55.0, 3.2 * 1000, 58.0 * 1000, 7.0,
             34 * 365 * 24 * 3600 * 1000),
        )
        cur.execute(
            "INSERT OR REPLACE INTO ha_sensor_daily (date, entity_id, mean_value,"
            " min_value, max_value, overnight_mean, unit, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (r["date"], "sensor.bedroom_temperature", r["bedroom_temp"],
             r["bedroom_temp"] - 1.5, r["bedroom_temp"] + 2.0,
             r["bedroom_temp"], "°C", noon),
        )

        # Hourly intraday series — the heatmap, body-battery decay and
        # stress-fingerprint analytics all read these.
        for hour in range(24):
            ts = f"{r['date']}T{hour:02d}:00:00"
            awake = 7 <= hour <= 22
            cur.execute(
                "INSERT OR REPLACE INTO stress_intraday VALUES (?,?,?)",
                (ts, "testdev", int(max(1, r["stress"] + (12 if awake else -18)))),
            )
            cur.execute(
                "INSERT OR REPLACE INTO body_battery_intraday VALUES (?,?,?)",
                # Charges overnight, drains through the day.
                (ts, "testdev", int(max(5, min(100,
                 r["bb_high"] - (hour - 7) * 3 if awake else r["bb_low"] + hour * 3)))),
            )
            cur.execute(
                "INSERT OR REPLACE INTO heart_rate_intraday VALUES (?,?,?)",
                (ts, "testdev", int(r["rhr"] + (22 if awake else 2))),
            )
            cur.execute(
                "INSERT OR REPLACE INTO steps_intraday VALUES (?,?,?)",
                (ts, "testdev", int(r["steps"] / 16) if awake else 0),
            )

        journal = []
        if r["drinks"]:
            journal.append(("Alcohol", "action", 1, float(r["drinks"])))
        if r["caffeine_late"]:
            journal.append(("Late Caffeine", "action", 1, 1.0))
        if r["late_meal"]:
            journal.append(("Late Meal", "action", 1, 1.0))
        if r["strain"]:
            journal.append(("Illness", "state", 1, 1.0))
        for behavior, category, status, value in journal:
            cur.execute(
                "INSERT OR REPLACE INTO lifestyle_journal"
                " (date, behavior, category, status, value, device) VALUES (?,?,?,?,?,?)",
                (r["date"], behavior, category, status, value, "testdev"),
            )

        # daily_summaries mirrors what DailySummaryCache would compute. Written
        # directly (rather than via the cache) so fixture consumers get a stable
        # payload that does not depend on cache internals.
        metrics = {
            "restingHeartRate": r["rhr"],
            "avgOvernightHrv": r["hrv"],
            "averageRespirationValue": r["resp"],
            "sleepScore": r["sleep_score"],
            "sleepTimeSeconds": r["sleep_secs"],
            "deepSleepSeconds": int(r["sleep_secs"] * 0.19),
            "remSleepSeconds": int(r["sleep_secs"] * 0.22),
            "lightSleepSeconds": int(r["sleep_secs"] * 0.52),
            "awakeSleepSeconds": int(r["sleep_secs"] * 0.07),
            "awakeCount": 2 + r["drinks"],
            "totalSteps": r["steps"],
            "stressPercentage": r["stress"],
            "highStressPercentage": r["stress"] / 3,
            "bodyBatteryHighestValue": r["bb_high"],
            "bodyBatteryLowestValue": r["bb_low"],
            "bodyBatteryDuringSleep": r["bb_high"] - r["bb_low"],
            "bodyBatteryAtWakeTime": r["bb_high"],
            "sedentarySeconds": 33000,
            "averageSpo2": r["spo2"],
            "lowestSpo2Value": r["spo2"] - 3.5,
            "moderateIntensityMinutes": int(r["steps"] / 400),
            "vigorousIntensityMinutes": int(r["steps"] / 1800),
            "is_complete": r["date"] != rows[-1]["date"],
        }
        lifestyle = {b: {"status": s, "value": v} for b, _c, s, v in journal}
        cur.execute(
            "INSERT OR REPLACE INTO daily_summaries (date, metric_json, lifestyle_json,"
            " computed_at) VALUES (?,?,?,datetime('now'))",
            (r["date"], json.dumps(metrics), json.dumps(lifestyle)),
        )

    _write_slow_markers(cur, rows)
    _write_activities(cur, rows)
    _write_cycle(cur, rows)

    conn.commit()
    conn.close()

    # Baselines over the completed days, matching what update_baselines writes.
    _write_baselines(path, rows)


def _write_slow_markers(cur, rows) -> None:
    """VO2 max / fitness age / race predictions etc.

    Garmin emits these irregularly, not daily, so they are written every ~10
    days — which is also what makes the fitness-trajectory chart extend its
    look-back. Keeping the sparse cadence here means tests inherit the real
    shape rather than a conveniently dense one.
    """
    for i, r in enumerate(rows):
        if i % 10:
            continue
        ts = f"{r['date']}T12:00:00"
        progress = i / max(1, len(rows) - 1)
        cur.execute(
            "INSERT OR REPLACE INTO vo2_max VALUES (?,?,?,?)",
            (ts, "testdev", round(46 + 4 * progress, 1), round(41 + 3 * progress, 1)),
        )
        cur.execute(
            "INSERT OR REPLACE INTO fitness_age VALUES (?,?,?,?,?)",
            (ts, "testdev", 40.0, round(36 - 2 * progress, 1), 33.0),
        )
        cur.execute(
            "INSERT OR REPLACE INTO endurance_score VALUES (?,?,?)",
            (ts, "testdev", int(7000 + 600 * progress)),
        )
        cur.execute(
            "INSERT OR REPLACE INTO hill_score VALUES (?,?,?,?,?,?,?,?)",
            (ts, "testdev", int(55 + 8 * progress), int(52 + 6 * progress),
             1, int(56 + 7 * progress), 1, round(46 + 4 * progress, 1)),
        )
        cur.execute(
            "INSERT OR REPLACE INTO race_predictions VALUES (?,?,?,?,?,?)",
            (ts, "testdev", 1540 - 70 * progress, 3200 - 140 * progress,
             7100 - 320 * progress, 14800 - 700 * progress),
        )


def _write_cycle(cur, rows, cycle_length: int = 28) -> None:
    """A regular 28-day cycle across the whole window.

    Present for every ``sample_db``, regardless of the sex on
    ``sample_settings`` — that is deliberate. The sex gate is enforced in the
    web layer, so the interesting test is "male user, cycle rows DO exist in
    the DB, endpoint still withholds them". A fixture that only wrote cycle
    rows for female users could never express that case.
    """
    phases = (
        # Flow vocabulary must match app._FLOW_INTENSITY (LIGHT/MEDIUM/HEAVY);
        # anything else silently scores 0.
        [("menstrual", "MEDIUM")] * 2 + [("menstrual", "LIGHT")] * 3
        + [("follicular", None)] * 8
        + [("ovulatory", None)] * 3
        + [("luteal", None)] * 12
    )
    for i, r in enumerate(rows):
        day_of_cycle = (i % cycle_length) + 1
        phase, flow = phases[day_of_cycle - 1]
        cycle_start = rows[i - (day_of_cycle - 1)]["date"] if i >= day_of_cycle - 1 \
            else rows[0]["date"]
        cur.execute(
            "INSERT OR REPLACE INTO menstrual_cycle (date, time, device,"
            " cycle_start_date, current_day_of_cycle, current_cycle_phase,"
            " cycle_length, predicted_cycle_length, period_length, menstrual_flow)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["date"], f"{r['date']}T12:00:00", "testdev", cycle_start,
             day_of_cycle, phase, cycle_length, cycle_length, 5, flow),
        )


def _write_activities(cur, rows) -> None:
    """A workout on roughly every third day, alternating run / ride."""
    for i, r in enumerate(rows):
        if i % 3:
            continue
        run = (i // 3) % 2 == 0
        cur.execute(
            "INSERT OR REPLACE INTO activity_summary (activity_id, time, device,"
            " activity_name, activity_type, distance, elapsed_duration,"
            " moving_duration, average_speed, max_speed, calories, average_hr,"
            " max_hr, hr_time_in_zone_1, hr_time_in_zone_2, hr_time_in_zone_3,"
            " hr_time_in_zone_4, hr_time_in_zone_5, avg_run_cadence, avg_power)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (10_000 + i, f"{r['date']}T18:00:00", "testdev",
             "Evening Run" if run else "Evening Ride",
             "running" if run else "cycling",
             8200.0 if run else 24500.0, 2700.0 if run else 3900.0,
             2650.0 if run else 3800.0, 3.1 if run else 6.4, 4.2 if run else 9.8,
             520.0 if run else 690.0, 148.0 if run else 132.0,
             172.0 if run else 158.0, 300.0, 700.0, 900.0, 600.0, 150.0,
             168.0 if run else None, None if run else 190.0),
        )


def _write_baselines(path: str, rows) -> None:
    import statistics

    from garmin_insights.db.memory import MemoryStore

    store = MemoryStore(types.SimpleNamespace(sqlite_db_path=path))
    series = {
        "restingHeartRate": [r["rhr"] for r in rows],
        "avgOvernightHrv": [r["hrv"] for r in rows],
        "sleepScore": [float(r["sleep_score"]) for r in rows],
        "totalSteps": [float(r["steps"]) for r in rows],
        "averageRespirationValue": [r["resp"] for r in rows],
    }
    for metric, values in series.items():
        last7, last30 = values[-7:], values[-30:]
        store.upsert_baseline(
            metric,
            statistics.fmean(last7), statistics.fmean(last30),
            statistics.stdev(last7), statistics.stdev(last30),
            min(last30), max(last30), values[-1],
        )


@pytest.fixture(scope="session")
def _sample_db_master(tmp_path_factory):
    """Build the dataset once per session — generation is the slow part."""
    path = tmp_path_factory.mktemp("sample") / "garmin_sample.db"
    rows = _sample_rows()
    _write_sample_db(str(path), rows)
    return str(path), rows


@pytest.fixture
def sample_db(_sample_db_master, tmp_path):
    """Path to a fresh ~120-day synthetic Garmin DB.

    Copied per test so a test that writes (notes, experiments, cache rebuilds)
    can't leak state into the next one.
    """
    import shutil

    master, _rows = _sample_db_master
    dest = tmp_path / "garmin_sample.db"
    shutil.copy(master, dest)
    return str(dest)


@pytest.fixture
def sample_rows(_sample_db_master):
    """The generated records backing ``sample_db``, for computing expectations."""
    _master, rows = _sample_db_master
    return rows


@pytest.fixture
def sample_dates(sample_rows):
    """(start, end) covering the whole generated window."""
    return sample_rows[0]["date"], sample_rows[-1]["date"]


@pytest.fixture
def sample_settings(sample_db):
    """Settings-alike pointing at ``sample_db``.

    Only the attributes the repo / memory / web layers actually read are
    populated; ``biological_sex`` is the one most tests will override.
    """
    return types.SimpleNamespace(
        sqlite_db_path=sample_db,
        display_name="Test User",
        garminconnect_email="test@example.com",
        biological_sex="Female",
        claude_model="claude-sonnet-5",
        claude_effort="low",
        user_timezone="Europe/London",
    )


# ==================================================================
# api_client — FastAPI TestClient over the sample DB
# ==================================================================

class _StubAgent:
    """Stands in for HealthAgent on the data endpoints.

    The real agent constructs an Anthropic client and rebuilds a 90-day cache
    on init. Every non-chat endpoint only reaches through it for ``_settings``,
    ``_repo``, ``_memory`` and ``_cache``, so those are all we provide — which
    keeps the API tests offline and key-free. Chat/scan endpoints are out of
    scope for this stub and will raise if exercised.
    """

    def __init__(self, settings):
        from garmin_insights.db.cache import CacheBuilder
        from garmin_insights.db.memory import MemoryStore
        from garmin_insights.db.sqlite_repo import SqliteRepo

        self._settings = settings
        self._repo = SqliteRepo(settings)
        self._memory = MemoryStore(settings)
        self._cache = CacheBuilder(self._repo, self._memory)

    def ensure_cache_fresh(self, days: int = 90) -> None:
        return None

    def get_scan_history(self, limit: int = 10, focus: str | None = None):
        return self._memory.get_scan_reports(limit=limit, focus=focus)

    def close(self) -> None:
        return None


@pytest.fixture
def api_client(sample_settings, monkeypatch):
    """FastAPI ``TestClient`` wired to a single user backed by ``sample_db``.

    Bypasses the app lifespan (which would build real agents from the
    environment) by injecting the module-level user pool directly.

    Usage::

        def test_dashboard(api_client):
            r = api_client.get("/api/dashboard?user=default&start=...&end=...")
            assert r.status_code == 200

    To exercise the sex-gated endpoints, set the sex before requesting the
    client::

        sample_settings.biological_sex = "Male"
    """
    from fastapi.testclient import TestClient

    from garmin_insights.web import app as app_module
    from garmin_insights.web.lifestyle_viz import LifestyleService
    from garmin_insights.web.sessions import SessionManager
    from garmin_insights.web.user_context import UserBundle
    from garmin_insights.web.visualizations import VisualizationService

    db = sample_settings.sqlite_db_path
    bundle = UserBundle(
        user_id="default",
        agent=_StubAgent(sample_settings),
        viz=VisualizationService(db),
        lifestyle=LifestyleService(db),
    )

    class _Pool:
        user_ids = ["default"]

        def has_user(self, user_id):
            return user_id == "default"

        def get(self, user_id):
            if user_id != "default":
                raise KeyError(user_id)
            return bundle

        def list_users(self):
            return [{"id": "default", "db_path": db}]

        def close(self):
            return None

    monkeypatch.setattr(app_module, "_users", _Pool(), raising=False)
    monkeypatch.setattr(
        app_module, "_sessions", SessionManager(ttl_seconds=60, max_sessions=8),
        raising=False,
    )
    # Skip the throttled cache rebuild the dashboard would otherwise trigger.
    monkeypatch.setattr(
        app_module, "_last_cache_refresh", {"default": datetime.utcnow()}, raising=False,
    )

    # The lifespan builds a real UserContext from the environment; the stub
    # pool above replaces it, so run the app without it.
    transport_app = app_module.app
    monkeypatch.setattr(transport_app.router, "lifespan_context", _noop_lifespan)
    with TestClient(transport_app) as client:
        yield client


@pytest.fixture
def two_user_client(sample_db, tmp_path, monkeypatch):
    """TestClient with two users on *separate* databases.

    Returns ``(client, marks)`` where ``marks`` maps user id -> a step count
    written only into that user's DB, so a test can prove which database a
    response actually came from rather than merely that it succeeded.

    Multi-user mode is the setting where a routing bug is a privacy incident:
    one user's health data served under another's id.
    """
    import shutil

    from fastapi.testclient import TestClient

    from garmin_insights.web import app as app_module
    from garmin_insights.web.lifestyle_viz import LifestyleService
    from garmin_insights.web.sessions import SessionManager
    from garmin_insights.web.user_context import UserBundle
    from garmin_insights.web.visualizations import VisualizationService

    marks = {"alice": 11111, "bob": 22222}
    bundles = {}
    paths = {}
    for uid, mark in marks.items():
        path = tmp_path / f"{uid}.db"
        shutil.copy(sample_db, path)
        paths[uid] = str(path)

        # Stamp a value unique to this user on every day, in both the raw
        # table and the summary cache the endpoints actually read.
        conn = sqlite3.connect(path)
        conn.execute("UPDATE daily_stats SET total_steps = ?", (mark,))
        rows = conn.execute("SELECT date, metric_json FROM daily_summaries").fetchall()
        for date, metric_json in rows:
            m = json.loads(metric_json)
            m["totalSteps"] = mark
            conn.execute("UPDATE daily_summaries SET metric_json = ? WHERE date = ?",
                         (json.dumps(m), date))
        conn.commit()
        conn.close()

        settings = types.SimpleNamespace(
            sqlite_db_path=str(path),
            display_name=uid.capitalize(),
            garminconnect_email=f"{uid}@example.com",
            biological_sex="Female" if uid == "alice" else "Male",
            claude_model="claude-sonnet-5",
            claude_effort="low",
            user_timezone="Europe/London",
            birth_date="",
            height_cm="",
        )
        bundles[uid] = UserBundle(
            user_id=uid,
            agent=_StubAgent(settings),
            viz=VisualizationService(str(path)),
            lifestyle=LifestyleService(str(path)),
        )

    class _Pool:
        user_ids = list(marks)

        def has_user(self, user_id):
            return user_id in bundles

        def get(self, user_id):
            if user_id not in bundles:
                raise KeyError(user_id)
            return bundles[user_id]

        def list_users(self):
            return [{"id": u, "db_path": paths[u]} for u in bundles]

        def close(self):
            return None

    monkeypatch.setattr(app_module, "_users", _Pool(), raising=False)
    monkeypatch.setattr(
        app_module, "_sessions", SessionManager(ttl_seconds=60, max_sessions=8),
        raising=False,
    )
    monkeypatch.setattr(
        app_module, "_last_cache_refresh",
        {u: datetime.utcnow() for u in bundles}, raising=False,
    )
    monkeypatch.setattr(app_module.app.router, "lifespan_context", _noop_lifespan)
    with TestClient(app_module.app) as client:
        yield client, marks
