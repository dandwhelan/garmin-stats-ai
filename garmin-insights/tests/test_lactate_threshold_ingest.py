"""LactateThreshold ingestion — the speed and heart-rate readings for a sport
arrive as two separate points sharing (time, sport, device), so a replacing
upsert let the second null out the first's column. Every stored
`speed_threshold` was NULL as a result. Same shape as the sleep_intraday
merge these tests mirror."""

from __future__ import annotations

import sqlite3

import pytest

# garmin-grafana is a sibling package, not a dependency of garmin-insights.
pytest.importorskip("garmin_grafana", reason="garmin-grafana not installed")

from garmin_grafana.sqlite_manager import GarminDB  # noqa: E402

_TS = "2026-06-15T00:00:00+00:00"


def _point(fields: dict, time: str = _TS) -> dict:
    """One point exactly as get_lactate_threshold emits it: a single field
    named for its sport, e.g. {"SpeedThreshold_RUNNING": 4.25}."""
    return {
        "measurement": "LactateThreshold",
        "time": time,
        "tags": {"Device": "TestWatch", "Database_Name": "GarminDB"},
        "fields": fields,
    }


def _rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT sport, speed_threshold, heart_rate_threshold "
            "FROM lactate_threshold ORDER BY sport"
        ).fetchall()
    finally:
        conn.close()


def test_speed_and_heart_rate_thresholds_coexist(tmp_path):
    """The two points describe one reading and must not overwrite each other."""
    path = tmp_path / "g.db"
    GarminDB(str(path)).insert_points([
        _point({"SpeedThreshold_RUNNING": 4.25}),
        _point({"HeartRateThreshold_RUNNING": 162}),
    ])
    assert _rows(path) == [("RUNNING", 4.25, 162)]


def test_arrival_order_does_not_decide_which_value_survives(tmp_path):
    """The endpoints dict fixes the order today; nothing should depend on it."""
    path = tmp_path / "g.db"
    GarminDB(str(path)).insert_points([
        _point({"HeartRateThreshold_RUNNING": 162}),
        _point({"SpeedThreshold_RUNNING": 4.25}),
    ])
    assert _rows(path) == [("RUNNING", 4.25, 162)]


def test_each_sport_keeps_its_own_row(tmp_path):
    """`sport` is part of the key, so cycling must not land on the running row."""
    path = tmp_path / "g.db"
    GarminDB(str(path)).insert_points([
        _point({"SpeedThreshold_RUNNING": 4.25}),
        _point({"HeartRateThreshold_RUNNING": 162}),
        _point({"SpeedThreshold_CYCLING": 7.1}),
    ])
    assert _rows(path) == [("CYCLING", 7.1, None), ("RUNNING", 4.25, 162)]


def test_a_malformed_point_does_not_roll_back_the_batch(tmp_path):
    """insert_points wraps one transaction: deriving the sport by blindly
    taking the first field key raised on an empty one, discarding every other
    measurement written in the same fetch cycle."""
    path = tmp_path / "g.db"
    db = GarminDB(str(path))
    db.insert_points([
        _point({"SpeedThreshold_RUNNING": 4.25}),
        _point({}),
        _point({"HeartRateThreshold_RUNNING": 162}),
    ])
    assert _rows(path) == [("RUNNING", 4.25, 162)]
