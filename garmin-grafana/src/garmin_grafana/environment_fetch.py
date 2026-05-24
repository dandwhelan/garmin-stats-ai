"""Open-Meteo environment fetcher — daily weather + air quality + pollen.

Fills the `environment_daily` table for one home location per user. Lat/lon
come from HOME_LAT / HOME_LON env vars. Free, no API key required.

Two endpoints are hit per run:
  - https://api.open-meteo.com/v1/forecast         (weather, all locations)
  - https://air-quality-api.open-meteo.com/v1/air-quality  (AQ + pollen;
    pollen values populate only inside Europe — non-EU rows get null pollen)
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# Open-Meteo allows past_days up to 92 on the free tier. We re-fetch the
# whole window every run; upserts on date PK keep it idempotent.
DEFAULT_PAST_DAYS = 92

_WEATHER_DAILY_FIELDS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "apparent_temperature_max",
    "precipitation_sum",
    "wind_speed_10m_max",
    "uv_index_max",
]

_AQ_DAILY_FIELDS = [
    "pm2_5",
    "pm10",
    "ozone",
    "nitrogen_dioxide",
    "european_aqi",
]

# Open-Meteo pollen is hourly only — we reduce to a daily peak ourselves
# (the daytime max, matching how Google/CAMS report a pollen index).
_AQ_HOURLY_POLLEN = [
    "alder_pollen",
    "birch_pollen",
    "grass_pollen",
    "mugwort_pollen",
    "olive_pollen",
    "ragweed_pollen",
]


def _mean(values: list[float | None]) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _fetch_weather(lat: float, lon: float, past_days: int) -> dict[str, Any]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ",".join(_WEATHER_DAILY_FIELDS),
        "hourly": "relative_humidity_2m",
        "past_days": past_days,
        "forecast_days": 5,
        "timezone": "auto",
    }
    r = requests.get(WEATHER_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _fetch_air_quality(lat: float, lon: float, past_days: int) -> dict[str, Any]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(_AQ_DAILY_FIELDS + _AQ_HOURLY_POLLEN),
        "past_days": past_days,
        "forecast_days": 5,
        "timezone": "auto",
    }
    r = requests.get(AIR_QUALITY_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _hourly_to_daily(times: list[str], values: list[float | None], reducer=_mean) -> dict[str, float | None]:
    """Group an hourly series (ISO timestamps) into per-day means."""
    by_day: dict[str, list[float | None]] = {}
    for t, v in zip(times, values):
        day = t[:10]
        by_day.setdefault(day, []).append(v)
    return {day: reducer(vals) for day, vals in by_day.items()}


def _hourly_max(times: list[str], values: list[float | None]) -> dict[str, float | None]:
    by_day: dict[str, list[float]] = {}
    for t, v in zip(times, values):
        if not isinstance(v, (int, float)):
            continue
        by_day.setdefault(t[:10], []).append(v)
    return {d: (max(vs) if vs else None) for d, vs in by_day.items()}


def build_daily_rows(lat: float, lon: float, past_days: int = DEFAULT_PAST_DAYS) -> list[dict[str, Any]]:
    """Fetch both endpoints and merge into one row per date."""
    weather = _fetch_weather(lat, lon, past_days)
    air = _fetch_air_quality(lat, lon, past_days)

    daily = weather.get("daily", {}) or {}
    dates: list[str] = daily.get("time", []) or []
    rows: dict[str, dict[str, Any]] = {}

    def daily_col(key: str) -> list:
        return daily.get(key, []) or []

    for i, date in enumerate(dates):
        rows[date] = {
            "date": date,
            "latitude": lat,
            "longitude": lon,
            "temp_max_c":          daily_col("temperature_2m_max")[i]      if i < len(daily_col("temperature_2m_max")) else None,
            "temp_min_c":          daily_col("temperature_2m_min")[i]      if i < len(daily_col("temperature_2m_min")) else None,
            "temp_mean_c":         daily_col("temperature_2m_mean")[i]     if i < len(daily_col("temperature_2m_mean")) else None,
            "apparent_temp_max_c": daily_col("apparent_temperature_max")[i] if i < len(daily_col("apparent_temperature_max")) else None,
            "precipitation_mm":    daily_col("precipitation_sum")[i]       if i < len(daily_col("precipitation_sum")) else None,
            "wind_max_kmh":        daily_col("wind_speed_10m_max")[i]      if i < len(daily_col("wind_speed_10m_max")) else None,
            "uv_index_max":        daily_col("uv_index_max")[i]            if i < len(daily_col("uv_index_max")) else None,
        }

    # Humidity is hourly in the weather payload — average to daily.
    hourly_w = weather.get("hourly", {}) or {}
    humid_daily = _hourly_to_daily(hourly_w.get("time", []) or [], hourly_w.get("relative_humidity_2m", []) or [])
    for date, val in humid_daily.items():
        rows.setdefault(date, {"date": date, "latitude": lat, "longitude": lon})["humidity_mean"] = val

    # Air quality: hourly → daily mean for pollutants, daily max for european_aqi.
    hourly_a = air.get("hourly", {}) or {}
    aq_times = hourly_a.get("time", []) or []
    pm25_d = _hourly_to_daily(aq_times, hourly_a.get("pm2_5", []) or [])
    pm10_d = _hourly_to_daily(aq_times, hourly_a.get("pm10", []) or [])
    o3_d   = _hourly_to_daily(aq_times, hourly_a.get("ozone", []) or [])
    no2_d  = _hourly_to_daily(aq_times, hourly_a.get("nitrogen_dioxide", []) or [])
    aqi_d  = _hourly_max(aq_times, hourly_a.get("european_aqi", []) or [])
    # Pollen: take the daily PEAK, not the mean. Concentrations are ~zero
    # overnight and peak midday, so a 24h mean roughly halves the daytime
    # level and reads far below the peak-based index Google/CAMS report.
    pollen_d = {
        name: _hourly_max(aq_times, hourly_a.get(name, []) or [])
        for name in _AQ_HOURLY_POLLEN
    }

    for date in set(list(pm25_d.keys()) + list(rows.keys())):
        row = rows.setdefault(date, {"date": date, "latitude": lat, "longitude": lon})
        row["pm25"]         = pm25_d.get(date)
        row["pm10"]         = pm10_d.get(date)
        row["o3"]           = o3_d.get(date)
        row["no2"]          = no2_d.get(date)
        row["european_aqi"] = aqi_d.get(date)
        row["pollen_alder"]   = pollen_d["alder_pollen"].get(date)
        row["pollen_birch"]   = pollen_d["birch_pollen"].get(date)
        row["pollen_grass"]   = pollen_d["grass_pollen"].get(date)
        row["pollen_mugwort"] = pollen_d["mugwort_pollen"].get(date)
        row["pollen_olive"]   = pollen_d["olive_pollen"].get(date)
        row["pollen_ragweed"] = pollen_d["ragweed_pollen"].get(date)

    fetched_at = datetime.now(timezone.utc).isoformat()
    for row in rows.values():
        row["fetched_at"] = fetched_at

    return sorted(rows.values(), key=lambda r: r["date"])


_COLUMNS = (
    "date", "latitude", "longitude",
    "temp_max_c", "temp_min_c", "temp_mean_c", "apparent_temp_max_c",
    "precipitation_mm", "wind_max_kmh", "humidity_mean", "uv_index_max",
    "pm25", "pm10", "o3", "no2", "european_aqi",
    "pollen_alder", "pollen_birch", "pollen_grass",
    "pollen_mugwort", "pollen_olive", "pollen_ragweed",
    "fetched_at",
)


def upsert_rows(db_path: str, rows: list[dict[str, Any]]) -> int:
    """Insert/replace rows into environment_daily. Returns count written."""
    if not rows:
        return 0
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        placeholders = ", ".join(["?"] * len(_COLUMNS))
        update_cols = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "date")
        sql = (
            f"INSERT INTO environment_daily ({', '.join(_COLUMNS)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(date) DO UPDATE SET {update_cols}"
        )
        for row in rows:
            cursor.execute(sql, tuple(row.get(c) for c in _COLUMNS))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def fetch_and_store(db_path: str, lat: float, lon: float, past_days: int = DEFAULT_PAST_DAYS) -> int:
    """One-shot: pull Open-Meteo data and upsert into the given DB."""
    rows = build_daily_rows(lat, lon, past_days)
    n = upsert_rows(db_path, rows)
    logger.info("environment_daily: upserted %d rows (lat=%.4f lon=%.4f past_days=%d)",
                n, lat, lon, past_days)
    return n


def fetch_from_env() -> int:
    """Read SQLITE_DB_PATH / HOME_LAT / HOME_LON from env and fetch.

    Silently no-ops when lat/lon are unset so existing single-user setups
    don't fail. Returns the number of rows written (0 if skipped).
    """
    db_path = os.getenv("SQLITE_DB_PATH", "garmin.db")
    lat_raw = os.getenv("HOME_LAT") or os.getenv("HOME_LATITUDE")
    lon_raw = os.getenv("HOME_LON") or os.getenv("HOME_LONGITUDE")
    if not lat_raw or not lon_raw:
        logger.info("environment_daily: HOME_LAT/HOME_LON not set — skipping weather fetch")
        return 0
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except ValueError:
        logger.warning("environment_daily: invalid HOME_LAT/HOME_LON (%r/%r) — skipping", lat_raw, lon_raw)
        return 0
    past_days = int(os.getenv("ENVIRONMENT_PAST_DAYS", str(DEFAULT_PAST_DAYS)))
    try:
        return fetch_and_store(db_path, lat, lon, past_days)
    except Exception as e:
        logger.warning("environment_daily fetch failed: %s", e)
        return 0


if __name__ == "__main__":
    import dotenv
    dotenv.load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    fetch_from_env()
