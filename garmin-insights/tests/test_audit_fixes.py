"""Regression tests for the correctness / data-integrity audit fixes.

Covers:
  * insert_points re-raises after rollback (no silently lost batches);
  * activity_gps dedup migration + upsert on (activity_id, time, device);
  * weigh-in upload fails closed on unverifiable token ownership;
  * behavior_impact joins behavior day X to the night recorded on X+1;
  * _resolve_range rejects malformed / inverted ranges and bounds the window;
  * HA overnight aggregation buckets by local (USER_TIMEZONE) hours, not UTC;
  * HA daily/overnight means are duration-weighted, not per-sample arithmetic.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

# garmin-grafana is a sibling package, not a dependency of garmin-insights.
# Without this guard a missing install turns into a collection error that
# aborts the ENTIRE suite rather than skipping just this module.
pytest.importorskip("garmin_grafana", reason="garmin-grafana not installed")

from garmin_grafana.sqlite_manager import GarminDB  # noqa: E402
from garmin_grafana import ha_fetch  # noqa: E402
from garmin_insights.garmin_upload import GarminUploadError, _verify_token_owner
from garmin_insights.stats_utils import NEXT_DAY_LAG_METRICS
from garmin_insights.web.visualizations import VisualizationService


# ------------------------------------------------------------------
# insert_points error propagation
# ------------------------------------------------------------------

def test_insert_points_reraises_after_rollback(tmp_path, monkeypatch):
    db = GarminDB(str(tmp_path / "g.db"))

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(GarminDB, "_upsert", _boom)
    point = {"measurement": "DailyStats", "time": "2026-07-01T12:00:00+00:00",
             "tags": {}, "fields": {}}
    with pytest.raises(sqlite3.OperationalError):
        db.insert_points([point])


# ------------------------------------------------------------------
# activity_gps dedup + upsert
# ------------------------------------------------------------------

_LEGACY_GPS_SCHEMA = """CREATE TABLE activity_gps (
    time TEXT, activity_id INTEGER, device TEXT, latitude REAL, longitude REAL,
    altitude REAL, distance REAL, duration_seconds REAL, heart_rate REAL,
    speed REAL, grade_adjusted_speed REAL, running_efficiency REAL,
    cadence INTEGER, fractional_cadence REAL, temperature REAL,
    accumulated_power INTEGER, power INTEGER, activity_name TEXT)"""


def _gps_point(lat: float) -> dict:
    return {"measurement": "ActivityGPS", "time": "2026-07-01T10:00:00+00:00",
            "tags": {"Device": "Watch", "ActivityID": 111},
            "fields": {"Latitude": lat, "HeartRate": 140.0}}


def test_activity_gps_migration_dedupes_legacy_rows(tmp_path):
    db_path = str(tmp_path / "g.db")
    conn = sqlite3.connect(db_path)
    conn.execute(_LEGACY_GPS_SCHEMA)
    for _ in range(3):  # legacy blind-insert duplication
        conn.execute("INSERT INTO activity_gps (time, activity_id, device, latitude) "
                     "VALUES ('2026-07-01T10:00:00+00:00', 111, 'Watch', 51.5)")
    conn.execute("INSERT INTO activity_gps (time, activity_id, device, latitude) "
                 "VALUES ('2026-07-01T10:00:01+00:00', 111, 'Watch', 51.6)")
    conn.commit()
    conn.close()

    GarminDB(db_path)  # _init_db runs the one-time migration

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM activity_gps").fetchone()[0] == 2
    # unique index exists so future blind inserts are impossible
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_activity_gps_unique'"
    ).fetchone() is not None
    conn.close()


def test_activity_gps_refetch_upserts_not_duplicates(tmp_path):
    db_path = str(tmp_path / "g.db")
    db = GarminDB(db_path)
    db.insert_points([_gps_point(51.5)])
    db.insert_points([_gps_point(51.7)])  # re-fetch of the same activity

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT COUNT(*), MAX(latitude) FROM activity_gps").fetchone()
    conn.close()
    assert rows == (1, 51.7)


# ------------------------------------------------------------------
# weigh-in upload fails closed
# ------------------------------------------------------------------

class _OpaqueProfileGarmin:
    """Garmin stub whose profile userName is an opaque handle (not an email)."""

    def connectapi(self, path):
        return {"userName": "a1b2c3d4-opaque-handle"}


def test_upload_refuses_unverifiable_token_ownership(tmp_path):
    # No account_owner.txt marker in the token dir and no email-form userName:
    # ownership is unverifiable and the upload must be refused, not attempted.
    with pytest.raises(GarminUploadError, match="Cannot verify"):
        _verify_token_owner(_OpaqueProfileGarmin(), str(tmp_path), "dan@example.com")


def test_upload_accepts_marker_verified_tokens(tmp_path):
    (tmp_path / "account_owner.txt").write_text("dan@example.com\n")
    _verify_token_owner(_OpaqueProfileGarmin(), str(tmp_path), "dan@example.com")


def test_upload_refuses_mismatched_owner(tmp_path):
    (tmp_path / "account_owner.txt").write_text("helen@example.com\n")
    with pytest.raises(GarminUploadError, match="refusing"):
        _verify_token_owner(_OpaqueProfileGarmin(), str(tmp_path), "dan@example.com")


# ------------------------------------------------------------------
# behavior_impact night alignment
# ------------------------------------------------------------------

def test_behavior_impact_uses_next_nights_sleep(tmp_path):
    db_path = str(tmp_path / "viz.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE lifestyle_journal (date TEXT, behavior TEXT, "
                 "category TEXT, status INTEGER, value REAL, device TEXT)")
    conn.execute("CREATE TABLE daily_summaries (date TEXT PRIMARY KEY, metric_json TEXT)")

    today = datetime.now().date()
    behavior_days = {today - timedelta(days=d) for d in (10, 8, 6)}
    # The night FOLLOWING a behavior day scores 40; every other night 90.
    # With the old same-day join the "with" mean would be 90, not 40.
    for d in range(15, 0, -1):
        day = today - timedelta(days=d)
        score = 40 if (day - timedelta(days=1)) in behavior_days else 90
        conn.execute("INSERT INTO daily_summaries VALUES (?, ?)", (
            day.isoformat(),
            json.dumps({"sleepScore": score, "avgOvernightHrv": 50,
                        "restingHeartRate": 55}),
        ))
    for day in behavior_days:
        conn.execute("INSERT INTO lifestyle_journal VALUES (?, 'Alcohol', "
                     "'lifestyle', 1, 2, 'x')", (day.isoformat(),))
    conn.commit()
    conn.close()

    res = VisualizationService(db_path).behavior_impact(days=90, min_occurrences=3)
    row = next(r for r in res if r["behavior"] == "Alcohol")
    assert row["sleep_with"] == 40.0
    assert row["sleep_without"] == 90.0


def test_lag_policy_covers_chart_metrics():
    # The three metrics the dashboard chart displays are all night-derived and
    # must be in the shared lag policy — if one is removed, the chart silently
    # regresses to the same-day join.
    assert {"sleepScore", "avgOvernightHrv", "restingHeartRate"} <= NEXT_DAY_LAG_METRICS


# ------------------------------------------------------------------
# _resolve_range validation
# ------------------------------------------------------------------

def test_resolve_range_validation():
    from fastapi import HTTPException
    from garmin_insights.web import app as webapp

    start, end = webapp._resolve_range(None, None, default_days=14)
    assert start < end

    for bad in [("garbage", None), (None, "2026-13-45"), ("2026-07-10", "2026-07-01")]:
        with pytest.raises(HTTPException) as exc:
            webapp._resolve_range(*bad)
        assert exc.value.status_code == 400

    # multi-millennium span is clamped, not honoured
    start, end = webapp._resolve_range("0001-01-01", None)
    span = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
    assert span == webapp.MAX_RANGE_DAYS

    # a far-future end is clamped to today + 7 (small allowance for the
    # 5 forecast days the pollen chart requests from environment_daily)
    _, end = webapp._resolve_range(None, "9999-01-01")
    assert end == (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")


# ------------------------------------------------------------------
# HA overnight aggregation timezone
# ------------------------------------------------------------------

def test_ha_overnight_uses_local_hours(monkeypatch):
    monkeypatch.setenv("USER_TIMEZONE", "Europe/London")
    states = [
        # 21:30 UTC = 22:30 BST -> inside the 22:00-08:00 local window,
        # attributed to the morning of 07-02. Under the old UTC grouping this
        # sample fell OUTSIDE the window entirely.
        {"state": "21.0", "last_changed": "2026-07-01T21:30:00Z",
         "attributes": {"unit_of_measurement": "C"}},
        # 06:30 UTC = 07:30 BST -> overnight, morning of 07-02
        {"state": "19.0", "last_changed": "2026-07-02T06:30:00Z"},
        # 07:30 UTC = 08:30 BST -> outside the window (old code included it)
        {"state": "25.0", "last_changed": "2026-07-02T07:30:00Z"},
    ]
    monkeypatch.setattr(ha_fetch, "fetch_entity_history", lambda *a, **k: states)
    rows = ha_fetch.build_daily_rows("http://x", "tok", "sensor.bedroom", 14)
    by_date = {r["date"]: r for r in rows}
    # Duration-weighted: 21.0 holds 22:30→07:30 BST (9 h); 19.0 holds
    # 07:30→08:30 but is clipped at the 08:00 window edge (0.5 h).
    assert by_date["2026-07-02"]["overnight_mean"] == round((21.0 * 9 + 19.0 * 0.5) / 9.5, 3)
    # 22:30 BST belongs to local day 07-01 even though it's 07-01 21:30 UTC
    assert by_date["2026-07-01"]["mean_value"] == 21.0


def test_ha_means_are_duration_weighted(monkeypatch):
    """A stable overnight state must not be out-voted by a burst of morning
    state changes — each state counts for how long it held, not for how often
    HA happened to record a change."""
    monkeypatch.setenv("USER_TIMEZONE", "Europe/London")
    states = [
        # 22:00 local: 18.0 holds for the whole night (no changes until 07:00)
        {"state": "18.0", "last_changed": "2026-07-01T21:00:00Z",
         "attributes": {"unit_of_measurement": "C"}},
        # Burst of changes 07:00–08:00 local, 20 min apart
        {"state": "22.0", "last_changed": "2026-07-02T06:00:00Z"},
        {"state": "23.0", "last_changed": "2026-07-02T06:20:00Z"},
        {"state": "24.0", "last_changed": "2026-07-02T06:40:00Z"},
        # 08:00 local: outside the 22:00–08:00 window; also ends the 24.0 interval
        {"state": "30.0", "last_changed": "2026-07-02T07:00:00Z"},
    ]
    monkeypatch.setattr(ha_fetch, "fetch_entity_history", lambda *a, **k: states)
    rows = ha_fetch.build_daily_rows("http://x", "tok", "sensor.bedroom", 14)
    by_date = {r["date"]: r for r in rows}

    # Plain arithmetic mean of the 4 in-window samples was (18+22+23+24)/4 = 21.75,
    # dominated by the volatile morning hour. Duration-weighted:
    # 18.0 × 9 h + (22+23+24) × 20 min each, over 10 h = 18.5.
    assert by_date["2026-07-02"]["overnight_mean"] == 18.5

    # The daily mean is duration-weighted too, over the 00:00–24:00 local day:
    # 18.0 × 7 h + (22+23+24) × ⅓ h + 30.0 × 16 h (trailing state clipped at day end).
    assert by_date["2026-07-02"]["mean_value"] == round((18 * 7 + 23 + 30 * 16) / 24, 3)

    # min/max stay sample-based; the 18.0 sample sits on local day 07-01 (22:00 BST).
    assert by_date["2026-07-02"]["min_value"] == 22.0
    assert by_date["2026-07-02"]["max_value"] == 30.0

    # 07-01: only the 18.0 state overlaps the day (22:00→midnight); no coverage
    # of the previous night's window. The trailing 30.0 interval runs to "now"
    # but must not fabricate rows for sample-less later days.
    assert by_date["2026-07-01"]["mean_value"] == 18.0
    assert by_date["2026-07-01"]["overnight_mean"] is None
    assert set(by_date) == {"2026-07-01", "2026-07-02"}
