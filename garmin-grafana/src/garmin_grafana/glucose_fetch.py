"""Nightscout-style glucose pull → ``glucose_readings``.

Juggluco and xDrip+ (Android) both serve a Nightscout-compatible
``/api/v1/entries`` feed from the phone, as does a real Nightscout site. This
module polls that feed once per fetch cycle and upserts the CGM readings into
the user's SQLite DB, where ``garmin_insights.insights.glucose`` analyses them.

Configuration (per user env):
  NIGHTSCOUT_URL         base URL, e.g. http://192.168.1.50:17580 (Juggluco's
                         built-in web server) or https://my-ns.example.com.
                         A URL that already ends in ``.json`` is used as-is.
  NIGHTSCOUT_TOKEN       optional Nightscout access token (sent as ?token=)
  NIGHTSCOUT_API_SECRET  optional API secret (sent SHA-1 hashed as api-secret)
  GLUCOSE_BACKFILL_DAYS  first-run look-back (default 14 — one sensor)

No-ops silently when NIGHTSCOUT_URL is unset. When the phone is away from
home the pull just fails (non-fatal); Juggluco keeps its history, so the next
successful pull backfills from the newest stored reading.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

SOURCE = "nightscout"
DEFAULT_BACKFILL_DAYS = 14
# Re-read a small overlap so a reading that arrived late is not skipped.
_OVERLAP_MIN = 30
# One reading a minute for 14 days, with headroom.
_MAX_COUNT = 30000

# Kept identical to garmin_insights.insights.glucose.SCHEMA_SQL (the two
# packages are installed independently, so neither imports the other).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS glucose_readings (
    time TEXT NOT NULL,
    source TEXT NOT NULL,
    mg_dl REAL NOT NULL,
    trend TEXT,
    device TEXT,
    sensor_id TEXT,
    record_type TEXT,
    PRIMARY KEY (time, source)
);
CREATE INDEX IF NOT EXISTS idx_glucose_readings_time ON glucose_readings (time);
"""


def entries_url(base: str) -> str:
    base = base.strip().rstrip("/")
    if base.endswith(".json"):
        return base
    if base.endswith("/api/v1"):
        return f"{base}/entries.json"
    return f"{base}/api/v1/entries.json"


def _latest_time(db_path: str) -> datetime | None:
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.executescript(SCHEMA_SQL)
        row = conn.execute(
            "SELECT MAX(time) FROM glucose_readings WHERE source = ?", (SOURCE,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    return datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_entries(payload: Any) -> list[dict[str, Any]]:
    """Nightscout entries JSON → reading rows. Non-SGV entries are skipped."""
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for e in payload:
        if not isinstance(e, dict):
            continue
        if e.get("type") not in (None, "sgv"):
            continue
        sgv = e.get("sgv")
        try:
            sgv = float(sgv)
        except (TypeError, ValueError):
            continue
        if not (40 <= sgv <= 500):  # Libre 2 reports 40-500; 39 / >500 are error codes
            continue
        ts = None
        if isinstance(e.get("date"), (int, float)):
            ts = datetime.fromtimestamp(e["date"] / 1000, tz=timezone.utc)
        elif isinstance(e.get("dateString"), str):
            try:
                ts = datetime.fromisoformat(e["dateString"].replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                ts = None
        if ts is None:
            continue
        rows.append({
            "time": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mg_dl": sgv,
            "trend": e.get("direction"),
            "device": e.get("device"),
            "sensor_id": e.get("sensor") or e.get("serial"),
        })
    return rows


def fetch_entries(url: str, since: datetime, token: str = "", api_secret: str = "") -> list[dict[str, Any]]:
    minutes = max(1, int((datetime.now(timezone.utc) - since).total_seconds() / 60))
    params: dict[str, Any] = {
        "count": min(_MAX_COUNT, minutes + 60),
        "find[date][$gte]": int(since.timestamp() * 1000),
    }
    if token:
        params["token"] = token
    headers = {"Accept": "application/json"}
    if api_secret:
        headers["api-secret"] = hashlib.sha1(api_secret.encode()).hexdigest()
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    rows = parse_entries(r.json())
    cutoff = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Not every server honours find[]; filter client-side as well.
    return [row for row in rows if row["time"] >= cutoff]


def upsert(db_path: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executemany(
            """
            INSERT INTO glucose_readings
                (time, source, mg_dl, trend, device, sensor_id, record_type)
            VALUES (?, ?, ?, ?, ?, ?, 'cgm')
            ON CONFLICT(time, source) DO UPDATE SET
                mg_dl = excluded.mg_dl, trend = excluded.trend,
                device = excluded.device, sensor_id = excluded.sensor_id
            """,
            [(r["time"], SOURCE, r["mg_dl"], r.get("trend"), r.get("device"), r.get("sensor_id"))
             for r in rows],
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def fetch_from_env() -> int:
    """Pull new readings for the user in the current env. Returns rows written."""
    base = os.getenv("NIGHTSCOUT_URL", "").strip()
    if not base:
        return 0
    db_path = os.getenv("SQLITE_DB_PATH", "garmin.db")
    try:
        backfill = int(os.getenv("GLUCOSE_BACKFILL_DAYS", str(DEFAULT_BACKFILL_DAYS)))
    except ValueError:
        backfill = DEFAULT_BACKFILL_DAYS
    try:
        latest = _latest_time(db_path)
        since = (latest - timedelta(minutes=_OVERLAP_MIN)) if latest else (
            datetime.now(timezone.utc) - timedelta(days=backfill)
        )
        rows = fetch_entries(
            entries_url(base), since,
            token=os.getenv("NIGHTSCOUT_TOKEN", "").strip(),
            api_secret=os.getenv("NIGHTSCOUT_API_SECRET", "").strip(),
        )
        n = upsert(db_path, rows)
        logger.info("glucose_readings: upserted %d Nightscout readings since %s", n, since.isoformat())
        return n
    except Exception as e:
        # Phone off the home network is routine — keep it to one line.
        logger.warning("glucose fetch failed (non-fatal): %s", e)
        return 0


if __name__ == "__main__":
    import dotenv
    dotenv.load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    fetch_from_env()
