"""Home Assistant sensor fetcher — pulls entity history via the HA REST API.

Reads HA_URL, HA_TOKEN, and HA_ENTITIES (comma-separated entity IDs) from
environment variables.  No-ops silently when any of these are unset.

Data is stored in ha_sensor_daily: one row per (date, entity_id) with daily
stats and an overnight mean (22:00–08:00) for sleep-quality correlation.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_PAST_DAYS = 14


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def fetch_entity_history(
    ha_url: str, token: str, entity_id: str, past_days: int
) -> list[dict[str, Any]]:
    """Return a flat list of HA state objects for the entity."""
    start = datetime.now(timezone.utc) - timedelta(days=past_days)
    url = f"{ha_url}/api/history/period/{start.isoformat()}"
    r = requests.get(
        url,
        headers=_headers(token),
        params={"filter_entity_id": entity_id, "minimal_response": "true"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data[0] if data and data[0] else []


def build_daily_rows(
    ha_url: str, token: str, entity_id: str, past_days: int = DEFAULT_PAST_DAYS
) -> list[dict[str, Any]]:
    """Aggregate HA state history to one row per calendar day."""
    states = fetch_entity_history(ha_url, token, entity_id, past_days)
    if not states:
        return []

    unit = (states[0].get("attributes") or {}).get("unit_of_measurement", "")

    daily: dict[str, list[float]] = defaultdict(list)
    overnight: dict[str, list[float]] = defaultdict(list)  # keyed to morning date

    for s in states:
        try:
            val = float(s["state"])
        except (ValueError, KeyError, TypeError):
            continue
        ts_raw = s.get("last_changed") or s.get("last_updated", "")
        try:
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except Exception:
            continue
        day = dt.date().isoformat()
        daily[day].append(val)
        hour = dt.hour
        if hour >= 22 or hour < 8:
            # Attribute to the date of the morning (i.e. the "night of")
            night_key = (dt.date() if hour < 8 else dt.date() + timedelta(days=1)).isoformat()
            overnight[night_key].append(val)

    fetched_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for day in sorted(daily):
        vals = daily[day]
        night_vals = overnight.get(day, [])
        rows.append({
            "date":           day,
            "entity_id":      entity_id,
            "mean_value":     round(sum(vals) / len(vals), 3),
            "min_value":      round(min(vals), 3),
            "max_value":      round(max(vals), 3),
            "overnight_mean": round(sum(night_vals) / len(night_vals), 3) if night_vals else None,
            "unit":           unit,
            "fetched_at":     fetched_at,
        })
    return rows


def upsert_rows(db_path: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ha_sensor_daily (
                date           TEXT NOT NULL,
                entity_id      TEXT NOT NULL,
                mean_value     REAL,
                min_value      REAL,
                max_value      REAL,
                overnight_mean REAL,
                unit           TEXT,
                fetched_at     TEXT,
                PRIMARY KEY (date, entity_id)
            )
        """)
        conn.executemany(
            """INSERT OR REPLACE INTO ha_sensor_daily
               (date, entity_id, mean_value, min_value, max_value,
                overnight_mean, unit, fetched_at)
               VALUES (:date, :entity_id, :mean_value, :min_value, :max_value,
                       :overnight_mean, :unit, :fetched_at)""",
            rows,
        )
        conn.commit()
    return len(rows)


def fetch_from_env() -> None:
    """Read HA_URL / HA_TOKEN / HA_ENTITIES from env and fetch + store."""
    ha_url   = (os.getenv("HA_URL") or "").rstrip("/")
    ha_token = os.getenv("HA_TOKEN") or ""
    entities_raw = os.getenv("HA_ENTITIES") or ""
    db_path  = os.getenv("SQLITE_DB_PATH", "garmin.db")

    if not ha_url or not ha_token or not entities_raw:
        logger.debug("ha_fetch: HA_URL / HA_TOKEN / HA_ENTITIES not set — skipping")
        return

    entities   = [e.strip() for e in entities_raw.split(",") if e.strip()]
    past_days  = int(os.getenv("HA_PAST_DAYS", str(DEFAULT_PAST_DAYS)))

    for entity_id in entities:
        try:
            rows = build_daily_rows(ha_url, ha_token, entity_id, past_days)
            n = upsert_rows(db_path, rows)
            logger.info("ha_fetch: %d rows upserted for %s", n, entity_id)
        except Exception as exc:
            logger.warning("ha_fetch failed for %s: %s", entity_id, exc)
