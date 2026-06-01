"""One-off: refresh activity_summary rows that were clobbered by the END-marker bug.

Reads SQLITE_DB_PATH / TOKEN_DIR / GARMINCONNECT_EMAIL from env (so it can be
invoked once per user-env file), wipes activity_summary, then walks the date
range and re-fetches START-only summary points via the Garmin Connect API.

Skips FIT/GPS entirely — activity_gps already holds the real track data and
has no unique constraint, so we leave it alone.

Usage:
    set -a && source users/dan.env && set +a
    python scripts/refetch_activity_summary.py 2025-02-10 2026-05-21
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timedelta

import pytz
from garminconnect import Garmin

from garmin_grafana.sqlite_manager import GarminDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("refetch")


def login() -> Garmin:
    token_dir = os.path.expanduser(os.environ["TOKEN_DIR"])
    g = Garmin()
    g.login(token_dir)
    log.info("Logged in via tokens at %s as %s", token_dir, getattr(g, "display_name", "?"))
    return g


def build_points(activity: dict) -> list[dict]:
    """Mirror get_activity_summary's START record (we omit the END marker)."""
    if "startTimeGMT" not in activity:
        return []
    start = datetime.strptime(activity["startTimeGMT"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.UTC)
    selector = start.strftime("%Y%m%dT%H%M%SUTC-") + (activity.get("activityType") or {}).get("typeKey", "Unknown")
    return [{
        "measurement": "ActivitySummary",
        "time": start.isoformat(),
        "tags": {
            "Device": os.getenv("GARMIN_DEVICENAME", "Unknown"),
            "Database_Name": "GarminDB",
            "ActivityID": activity.get("activityId"),
            "ActivitySelector": selector,
        },
        "fields": {
            "Activity_ID": activity.get("activityId"),
            "Device_ID": activity.get("deviceId"),
            "activityName": activity.get("activityName"),
            "activityType": (activity.get("activityType") or {}).get("typeKey"),
            "distance": activity.get("distance"),
            "elapsedDuration": activity.get("elapsedDuration"),
            "movingDuration": activity.get("movingDuration"),
            "averageSpeed": activity.get("averageSpeed"),
            "maxSpeed": activity.get("maxSpeed"),
            "calories": activity.get("calories"),
            "bmrCalories": activity.get("bmrCalories"),
            "averageHR": activity.get("averageHR"),
            "maxHR": activity.get("maxHR"),
            "locationName": activity.get("locationName"),
            "lapCount": activity.get("lapCount"),
            "hrTimeInZone_1": activity.get("hrTimeInZone_1"),
            "hrTimeInZone_2": activity.get("hrTimeInZone_2"),
            "hrTimeInZone_3": activity.get("hrTimeInZone_3"),
            "hrTimeInZone_4": activity.get("hrTimeInZone_4"),
            "hrTimeInZone_5": activity.get("hrTimeInZone_5"),
        },
    }]


def main(start_date: date, end_date: date) -> None:
    db_path = os.environ["SQLITE_DB_PATH"]
    log.info("DB: %s   range: %s → %s", db_path, start_date, end_date)

    garmin = login()
    db = GarminDB(db_path)

    with db._get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM activity_summary")
        log.info("Cleared activity_summary (%d rows removed)", cur.rowcount)
        conn.commit()

    cur_date = start_date
    total_acts = 0
    while cur_date <= end_date:
        ds = cur_date.isoformat()
        try:
            activities = garmin.get_activities_by_date(ds, ds) or []
        except Exception as e:
            log.error("Fetch failed for %s: %s", ds, e)
            cur_date += timedelta(days=1)
            continue

        points: list[dict] = []
        for act in activities:
            points.extend(build_points(act))
        if points:
            db.insert_points(points)
            total_acts += len(points)
            log.info("%s: wrote %d activity rows", ds, len(points))
        cur_date += timedelta(days=1)

    log.info("Done. Total activity rows written: %d", total_acts)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: refetch_activity_summary.py YYYY-MM-DD YYYY-MM-DD", file=sys.stderr)
        sys.exit(2)
    start = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    end = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
    main(start, end)
