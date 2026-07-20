"""Home Assistant sensor fetcher — pulls entity history via the HA REST API.

Reads HA_URL, HA_TOKEN, and HA_ENTITIES (comma-separated entity IDs) from
environment variables.  No-ops silently when any of these are unset.

Data is stored in ha_sensor_daily: one row per (date, entity_id) with daily
stats and an overnight mean (22:00–08:00) for sleep-quality correlation.
Means are duration-weighted (each state held until the next change), since
HA only records state *changes* and samples are irregularly spaced.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_PAST_DAYS = 14


def _local_tz():
    """Timezone for day/hour bucketing: USER_TIMEZONE if set, else system local.

    HA returns UTC timestamps; grouping by their raw date/hour shifts the
    documented 22:00–08:00 overnight window during DST (23:00–09:00 local in
    BST) and can key readings near midnight to the wrong day.
    """
    tz_name = os.getenv("USER_TIMEZONE", "").strip()
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_name)
        except Exception:
            logger.warning("ha_fetch: unknown USER_TIMEZONE %r — using system local time", tz_name)
    return None  # astimezone(None) converts to the system local timezone


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def fetch_entity_history(
    ha_url: str, token: str, entity_id: str, past_days: int
) -> list[dict[str, Any]]:
    """Return a flat list of HA state objects for the entity."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=past_days)
    url = f"{ha_url}/api/history/period/{start.isoformat()}"
    r = requests.get(
        url,
        headers=_headers(token),
        params={"filter_entity_id": entity_id, "minimal_response": "true", "end_time": now.isoformat()},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data[0] if data and data[0] else []


def _aware_local(d: date, hour: int, tz) -> datetime:
    """Aware datetime at `hour` on local day `d` (tz=None → system local)."""
    naive = datetime.combine(d, time(hour))
    return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()


def _window_mean(
    intervals: list[tuple[datetime, datetime, float]],
    win_start: datetime,
    win_end: datetime,
) -> float | None:
    """Duration-weighted mean of a step function over [win_start, win_end)."""
    total = weight = 0.0
    for start, end, val in intervals:
        s, e = max(start, win_start), min(end, win_end)
        if e > s:
            w = (e - s).total_seconds()
            total += val * w
            weight += w
    return total / weight if weight else None


def build_daily_rows(
    ha_url: str, token: str, entity_id: str, past_days: int = DEFAULT_PAST_DAYS
) -> list[dict[str, Any]]:
    """Aggregate HA state history to one row per calendar day.

    HA only records a sample when the state *changes*, so samples are
    irregularly spaced and a plain arithmetic mean over-weights volatile
    periods.  Means are duration-weighted instead: each state's value counts
    for the time until the next state change (the trailing state runs to
    "now", and every interval is clipped at the aggregation-window edges).
    """
    states = fetch_entity_history(ha_url, token, entity_id, past_days)
    if not states:
        return []

    unit = (states[0].get("attributes") or {}).get("unit_of_measurement", "")

    tz = _local_tz()
    samples: list[tuple[datetime, float]] = []
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
        if dt.tzinfo is None:  # HA timestamps are UTC when the offset is absent
            dt = dt.replace(tzinfo=timezone.utc)
        samples.append((dt.astimezone(tz), val))  # bucket by LOCAL day/hour, not UTC
    if not samples:
        return []
    samples.sort(key=lambda p: p[0])

    now_local = datetime.now(timezone.utc).astimezone(tz)
    intervals: list[tuple[datetime, datetime, float]] = []
    for i, (start, val) in enumerate(samples):
        end = samples[i + 1][0] if i + 1 < len(samples) else max(now_local, start)
        intervals.append((start, end, val))

    daily: dict[date, list[float]] = defaultdict(list)  # sample values, for min/max
    for start, _end, val in intervals:
        daily[start.date()].append(val)

    fetched_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for day in sorted(daily):
        vals = daily[day]
        day_mean = _window_mean(
            intervals, _aware_local(day, 0, tz), _aware_local(day + timedelta(days=1), 0, tz)
        )
        if day_mean is None:  # zero-width coverage (duplicate timestamps) — fall back
            day_mean = sum(vals) / len(vals)
        # Overnight window 22:00–08:00 local, keyed to the morning date
        # (i.e. the night that *ended* on `day`).
        night_mean = _window_mean(
            intervals, _aware_local(day - timedelta(days=1), 22, tz), _aware_local(day, 8, tz)
        )
        rows.append({
            "date":           day.isoformat(),
            "entity_id":      entity_id,
            "mean_value":     round(day_mean, 3),
            "min_value":      round(min(vals), 3),
            "max_value":      round(max(vals), 3),
            "overnight_mean": round(night_mean, 3) if night_mean is not None else None,
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
