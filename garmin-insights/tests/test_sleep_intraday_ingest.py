"""SleepIntraday ingestion — previously fetched from Garmin but silently
dropped (insert_points had no handler), so the table existed forever empty.
These tests pin the new handler + the COALESCE merge semantics."""

from __future__ import annotations

import sqlite3

from garmin_grafana.sqlite_manager import GarminDB


def _point(time: str, fields: dict) -> dict:
    return {
        "measurement": "SleepIntraday",
        "time": time,
        "tags": {"Device": "TestWatch", "Database_Name": "GarminDB"},
        "fields": fields,
    }


def test_sleep_intraday_points_are_persisted(tmp_path):
    db = GarminDB(str(tmp_path / "g.db"))
    db.insert_points([
        _point("2026-06-15T01:00:00+00:00", {"spo2Reading": 95}),
        _point("2026-06-15T01:02:00+00:00", {"heartRate": 52}),
        _point("2026-06-15T01:04:00+00:00", {"hrvData": 48}),
    ])
    conn = sqlite3.connect(str(tmp_path / "g.db"))
    rows = conn.execute(
        "SELECT time, spo2_reading, heart_rate, hrv_value FROM sleep_intraday ORDER BY time"
    ).fetchall()
    conn.close()
    assert len(rows) == 3
    assert rows[0][1] == 95
    assert rows[1][2] == 52
    assert rows[2][3] == 48


def test_sleep_intraday_same_timestamp_types_merge_not_clobber(tmp_path):
    """Different sample types can share (time, device); a later point must fill
    its own column without nulling the earlier type's value."""
    db = GarminDB(str(tmp_path / "g.db"))
    ts = "2026-06-15T02:00:00+00:00"
    db.insert_points([_point(ts, {"spo2Reading": 94})])
    db.insert_points([_point(ts, {"stressValue": 12, "bodyBattery": 70})])
    db.insert_points([_point(ts, {"respirationValue": 14})])

    conn = sqlite3.connect(str(tmp_path / "g.db"))
    row = conn.execute(
        "SELECT spo2_reading, stress_value, body_battery, respiration_value, heart_rate "
        "FROM sleep_intraday WHERE time = ?", (ts,)
    ).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM sleep_intraday").fetchone()[0]
    conn.close()

    assert count == 1
    assert row == (94, 12, 70, 14, None)


def test_sleep_intraday_stage_and_movement_fields(tmp_path):
    db = GarminDB(str(tmp_path / "g.db"))
    db.insert_points([
        _point("2026-06-15T03:00:00+00:00",
               {"SleepStageLevel": 0, "SleepStageSeconds": 1800}),
        _point("2026-06-15T03:30:00+00:00",
               {"SleepMovementActivityLevel": 2, "SleepMovementActivitySeconds": 60,
                "sleepRestlessValue": 1}),
    ])
    conn = sqlite3.connect(str(tmp_path / "g.db"))
    stage = conn.execute(
        "SELECT stage_level, stage_seconds FROM sleep_intraday "
        "WHERE time = '2026-06-15T03:00:00+00:00'"
    ).fetchone()
    move = conn.execute(
        "SELECT activity_level, activity_seconds, restless_value FROM sleep_intraday "
        "WHERE time = '2026-06-15T03:30:00+00:00'"
    ).fetchone()
    conn.close()
    assert stage == (0, 1800)   # deep-sleep stage level 0 must persist (not be dropped as falsy)
    assert move == (2, 60, 1)
