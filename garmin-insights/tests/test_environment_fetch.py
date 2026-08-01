"""Open-Meteo environment pipeline.

Runs at the end of every fetch cycle, re-pulling the whole 92-day window each
time — so the upsert being genuinely idempotent is what stops the table
growing duplicates forever. And because it is optional (most installs have no
HOME_LAT/HOME_LON), every skip and failure path has to be silent rather than
crash the fetcher it runs inside.

The HTTP layer is mocked; nothing here touches the network.
"""

from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("garmin_grafana", reason="garmin-grafana not installed")

from garmin_grafana import environment_fetch as ef  # noqa: E402

LAT, LON = 51.5074, -0.1278


# ------------------------------------------------------------------
# Canned Open-Meteo payloads
# ------------------------------------------------------------------
def _weather_payload(dates):
    n = len(dates)
    return {
        "daily": {
            "time": list(dates),
            "temperature_2m_max": [20.0 + i for i in range(n)],
            "temperature_2m_min": [10.0 + i for i in range(n)],
            "temperature_2m_mean": [15.0 + i for i in range(n)],
            "apparent_temperature_max": [22.0 + i for i in range(n)],
            "precipitation_sum": [0.5] * n,
            "wind_speed_10m_max": [18.0] * n,
            "uv_index_max": [4.0] * n,
        },
        "hourly": {
            "time": [f"{d}T{h:02d}:00" for d in dates for h in range(24)],
            # Humidity varies within the day so the daily mean is a real mean.
            "relative_humidity_2m": [60.0 + (h % 4) for _d in dates for h in range(24)],
        },
    }


def _air_payload(dates):
    hours = [f"{d}T{h:02d}:00" for d in dates for h in range(24)]
    n = len(hours)
    return {
        "hourly": {
            "time": hours,
            "pm2_5": [8.0 + (i % 3) for i in range(n)],
            "pm10": [14.0] * n,
            "ozone": [60.0] * n,
            "nitrogen_dioxide": [20.0] * n,
            "european_aqi": [30.0 + (i % 5) for i in range(n)],
            # Pollen peaks mid-afternoon; the reducer takes the daily max.
            "grass_pollen": [float(h) for _d in dates for h in range(24)],
            "birch_pollen": [1.0] * n,
            "alder_pollen": [None] * n,
            "mugwort_pollen": [None] * n,
            "olive_pollen": [None] * n,
            "ragweed_pollen": [None] * n,
        },
    }


@pytest.fixture
def dates():
    return ["2026-07-01", "2026-07-02", "2026-07-03"]


@pytest.fixture
def stub_openmeteo(monkeypatch, dates):
    """Replace both HTTP calls; record how many times each was hit."""
    calls = {"weather": 0, "air": 0, "params": []}

    def fake_weather(lat, lon, past_days):
        calls["weather"] += 1
        calls["params"].append(("weather", lat, lon, past_days))
        return _weather_payload(dates)

    def fake_air(lat, lon, past_days):
        calls["air"] += 1
        calls["params"].append(("air", lat, lon, past_days))
        return _air_payload(dates)

    monkeypatch.setattr(ef, "_fetch_weather", fake_weather)
    monkeypatch.setattr(ef, "_fetch_air_quality", fake_air)
    return calls


@pytest.fixture
def env_db(tmp_path):
    db = tmp_path / "env.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE environment_daily (
            date TEXT PRIMARY KEY, latitude REAL, longitude REAL,
            temp_max_c REAL, temp_min_c REAL, temp_mean_c REAL,
            apparent_temp_max_c REAL, precipitation_mm REAL, wind_max_kmh REAL,
            humidity_mean REAL, uv_index_max REAL, pm25 REAL, pm10 REAL,
            o3 REAL, no2 REAL, european_aqi REAL, pollen_alder REAL,
            pollen_birch REAL, pollen_grass REAL, pollen_mugwort REAL,
            pollen_olive REAL, pollen_ragweed REAL, fetched_at TEXT
        );
    """)
    conn.commit()
    conn.close()
    return str(db)


# ==================================================================
# Hourly reducers
# ==================================================================
def test_hourly_to_daily_averages_within_each_day():
    times = ["2026-07-01T00:00", "2026-07-01T01:00", "2026-07-02T00:00"]
    assert ef._hourly_to_daily(times, [10.0, 20.0, 5.0]) == {
        "2026-07-01": 15.0, "2026-07-02": 5.0}


def test_hourly_to_daily_ignores_nulls_in_the_mean():
    times = ["2026-07-01T00:00", "2026-07-01T01:00", "2026-07-01T02:00"]
    assert ef._hourly_to_daily(times, [10.0, None, 20.0]) == {"2026-07-01": 15.0}


def test_a_fully_null_day_means_none_not_zero():
    """Zero would read as 'measured and clean'; None reads as 'not measured'."""
    times = ["2026-07-01T00:00", "2026-07-01T01:00"]
    assert ef._hourly_to_daily(times, [None, None]) == {"2026-07-01": None}


def test_hourly_max_takes_the_daily_peak():
    """Pollen is reported as a daily peak, matching how CAMS/Google do it —
    a mean would wash out the afternoon spike that actually causes symptoms."""
    times = [f"2026-07-01T{h:02d}:00" for h in range(24)]
    values = [float(h) for h in range(24)]
    assert ef._hourly_max(times, values) == {"2026-07-01": 23.0}


def test_hourly_max_skips_nulls():
    times = ["2026-07-01T00:00", "2026-07-01T01:00"]
    assert ef._hourly_max(times, [None, 7.0]) == {"2026-07-01": 7.0}


def test_mean_of_no_numbers_is_none():
    assert ef._mean([None, None]) is None
    assert ef._mean([]) is None


# ==================================================================
# build_daily_rows
# ==================================================================
def test_build_rows_merges_both_endpoints_per_date(stub_openmeteo, dates):
    rows = ef.build_daily_rows(LAT, LON, past_days=3)
    assert [r["date"] for r in rows] == dates

    first = rows[0]
    assert first["temp_max_c"] == 20.0
    assert first["latitude"] == LAT and first["longitude"] == LON
    assert first["pm25"] is not None
    assert first["european_aqi"] is not None


def test_build_rows_reduces_pollen_to_a_daily_peak(stub_openmeteo):
    rows = ef.build_daily_rows(LAT, LON, past_days=3)
    assert rows[0]["pollen_grass"] == 23.0


def test_build_rows_carries_nulls_for_unreported_pollen_species(stub_openmeteo):
    """Outside Europe the pollen fields come back empty — they must stay null
    rather than collapse to 0."""
    rows = ef.build_daily_rows(LAT, LON, past_days=3)
    assert rows[0]["pollen_alder"] is None
    assert rows[0]["pollen_ragweed"] is None


def test_build_rows_computes_a_daily_humidity_mean(stub_openmeteo):
    rows = ef.build_daily_rows(LAT, LON, past_days=3)
    assert rows[0]["humidity_mean"] == pytest.approx(61.5)


def test_build_rows_stamps_fetched_at(stub_openmeteo):
    rows = ef.build_daily_rows(LAT, LON, past_days=3)
    assert rows[0]["fetched_at"]


def test_build_rows_survives_an_empty_weather_payload(monkeypatch):
    monkeypatch.setattr(ef, "_fetch_weather", lambda *a, **k: {})
    monkeypatch.setattr(ef, "_fetch_air_quality", lambda *a, **k: {})
    assert ef.build_daily_rows(LAT, LON, past_days=3) == []


# ==================================================================
# upsert_rows — idempotency is the whole point
# ==================================================================
def test_upsert_writes_every_row(env_db, stub_openmeteo):
    rows = ef.build_daily_rows(LAT, LON, past_days=3)
    assert ef.upsert_rows(env_db, rows) == 3

    conn = sqlite3.connect(env_db)
    assert conn.execute("SELECT COUNT(*) FROM environment_daily").fetchone()[0] == 3
    conn.close()


def test_upsert_is_idempotent_across_repeated_runs(env_db, stub_openmeteo):
    """The fetcher re-pulls the whole 92-day window every cycle; without a real
    upsert the table would grow duplicates on every run, forever."""
    rows = ef.build_daily_rows(LAT, LON, past_days=3)
    for _ in range(4):
        ef.upsert_rows(env_db, rows)

    conn = sqlite3.connect(env_db)
    assert conn.execute("SELECT COUNT(*) FROM environment_daily").fetchone()[0] == 3
    conn.close()


def test_upsert_overwrites_a_revised_reading(env_db, stub_openmeteo):
    """Open-Meteo revises recent days as observations replace forecasts, so a
    later run must win rather than be ignored."""
    rows = ef.build_daily_rows(LAT, LON, past_days=3)
    ef.upsert_rows(env_db, rows)

    rows[0]["temp_max_c"] = 99.0
    ef.upsert_rows(env_db, rows)

    conn = sqlite3.connect(env_db)
    value = conn.execute("SELECT temp_max_c FROM environment_daily WHERE date = ?",
                         (rows[0]["date"],)).fetchone()[0]
    conn.close()
    assert value == 99.0


def test_upsert_of_nothing_writes_nothing(env_db):
    assert ef.upsert_rows(env_db, []) == 0


# ==================================================================
# fetch_from_env — the optional-feature guards
# ==================================================================
def test_skips_silently_when_no_home_location_is_set(monkeypatch, env_db):
    """Most installs never set these; the fetcher must not fail because of it."""
    monkeypatch.setenv("SQLITE_DB_PATH", env_db)
    monkeypatch.delenv("HOME_LAT", raising=False)
    monkeypatch.delenv("HOME_LON", raising=False)
    monkeypatch.delenv("HOME_LATITUDE", raising=False)
    monkeypatch.delenv("HOME_LONGITUDE", raising=False)

    assert ef.fetch_from_env() == 0


def test_skips_on_a_malformed_coordinate(monkeypatch, env_db):
    monkeypatch.setenv("SQLITE_DB_PATH", env_db)
    monkeypatch.setenv("HOME_LAT", "not-a-number")
    monkeypatch.setenv("HOME_LON", "-0.1278")
    assert ef.fetch_from_env() == 0


def test_accepts_the_long_form_env_var_names(monkeypatch, env_db, stub_openmeteo):
    monkeypatch.setenv("SQLITE_DB_PATH", env_db)
    monkeypatch.delenv("HOME_LAT", raising=False)
    monkeypatch.delenv("HOME_LON", raising=False)
    monkeypatch.setenv("HOME_LATITUDE", str(LAT))
    monkeypatch.setenv("HOME_LONGITUDE", str(LON))

    assert ef.fetch_from_env() == 3


def test_a_network_failure_is_swallowed(monkeypatch, env_db):
    """This runs at the tail of each fetch cycle — an Open-Meteo outage must
    not take down the Garmin fetch that already succeeded."""
    monkeypatch.setenv("SQLITE_DB_PATH", env_db)
    monkeypatch.setenv("HOME_LAT", str(LAT))
    monkeypatch.setenv("HOME_LON", str(LON))

    def boom(*a, **k):
        raise ConnectionError("open-meteo unreachable")

    monkeypatch.setattr(ef, "_fetch_weather", boom)
    assert ef.fetch_from_env() == 0


def test_past_days_is_read_from_the_environment(monkeypatch, env_db, stub_openmeteo):
    monkeypatch.setenv("SQLITE_DB_PATH", env_db)
    monkeypatch.setenv("HOME_LAT", str(LAT))
    monkeypatch.setenv("HOME_LON", str(LON))
    monkeypatch.setenv("ENVIRONMENT_PAST_DAYS", "30")

    ef.fetch_from_env()
    assert all(p[3] == 30 for p in stub_openmeteo["params"])


def test_default_past_days_is_the_free_tier_maximum():
    """Open-Meteo caps past_days at 92 on the free tier; requesting more 400s."""
    assert ef.DEFAULT_PAST_DAYS == 92


def test_fetch_and_store_hits_each_endpoint_once(env_db, stub_openmeteo):
    ef.fetch_and_store(env_db, LAT, LON, past_days=3)
    assert stub_openmeteo["weather"] == 1
    assert stub_openmeteo["air"] == 1
