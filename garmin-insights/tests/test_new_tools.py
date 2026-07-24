"""Tests for the AI-tooling expansion: intraday digest, new tools, effort map,
tool-registry invariants, and scan-report persistence."""

from __future__ import annotations

import json
import sqlite3
import types
from datetime import datetime, timezone

import pandas as pd
import pytest

from garmin_insights.agent import _effort_for_focus, _SCAN_PROMPTS, _FOCUS_SNAPSHOT_DAYS
from garmin_insights.db.sqlite_repo import SqliteRepo
from garmin_insights.tools.query_tools import (
    QueryToolHandler,
    get_all_tools_anthropic,
    hourly_intraday_digest,
    _truncate_long_lists,
)


# ----------------------------------------------------------------------
# hourly_intraday_digest (pure helper)
# ----------------------------------------------------------------------
def _local_day_and_hour(ts: datetime) -> tuple[str, str]:
    """Expected local bucket for a UTC timestamp, matching the digest's tz rule."""
    local = ts.astimezone()
    return local.strftime("%Y-%m-%d"), f"{local.hour:02d}:00"


def _series_df(samples: list[tuple[datetime, float]]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(t) for t, _ in samples])
    return pd.DataFrame({"value": [v for _, v in samples]}, index=idx)


def test_hourly_digest_mean_buckets_and_extremes():
    base = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)  # mid-day, no date wrap
    date, hour_10 = _local_day_and_hour(base)
    df = _series_df([
        (base, 40.0),
        (base.replace(minute=30), 60.0),
        (base.replace(hour=11), 80.0),
        (base.replace(hour=11, minute=10), -1.0),   # unmeasured sentinel — dropped
    ])
    digest = hourly_intraday_digest(df, date, agg="mean")
    assert digest is not None
    assert digest["n_samples"] == 3
    assert digest["hourly"][hour_10] == {"avg": 50, "min": 40, "max": 60}
    assert digest["peak"]["value"] == 80
    assert digest["low"]["value"] == 40
    assert digest["date"] == date


def test_hourly_digest_sum_agg_and_peak_hour():
    base = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    date, hour_12 = _local_day_and_hour(base)
    df = _series_df([
        (base, 100.0),
        (base.replace(minute=15), 200.0),
        (base.replace(hour=13), 50.0),
    ])
    digest = hourly_intraday_digest(df, date, agg="sum")
    assert digest["hourly"][hour_12] == 300
    assert digest["peak_hour"] == {"hour": hour_12, "sum": 300}


def test_hourly_digest_filters_other_days_and_naive_utc():
    base = datetime(2026, 6, 15, 12, 0)  # naive — treated as UTC
    date, _ = _local_day_and_hour(base.replace(tzinfo=timezone.utc))
    df = _series_df([
        (base, 55.0),
        (datetime(2026, 6, 16, 12, 0), 99.0),  # different day — excluded
    ])
    digest = hourly_intraday_digest(df, date, agg="mean")
    assert digest["n_samples"] == 1
    assert digest["peak"]["value"] == 55


def test_hourly_digest_empty_returns_none():
    assert hourly_intraday_digest(pd.DataFrame(), "2026-06-15") is None
    df = _series_df([(datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc), 50.0)])
    assert hourly_intraday_digest(df, "2020-01-01") is None


# ----------------------------------------------------------------------
# _truncate_long_lists
# ----------------------------------------------------------------------
def test_truncate_long_lists_keeps_tail_with_marker():
    payload = {"series": list(range(100)), "small": [1, 2, 3]}
    out = _truncate_long_lists(payload, keep=10)
    assert out["small"] == [1, 2, 3]
    assert len(out["series"]) == 11  # marker + 10 kept
    assert "90 earlier entries omitted" in out["series"][0]
    assert out["series"][1:] == list(range(90, 100))


# ----------------------------------------------------------------------
# _effort_for_focus
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "focus,configured,expected",
    [
        ("weekly", "low", "medium"),     # raised to the floor
        ("general", "low", "medium"),
        ("weekly", "medium", None),      # already at floor — no override
        ("weekly", "high", None),        # never lowered
        ("morning", "low", None),        # no floor for briefings
        ("night", "low", None),
        ("unknown", "low", None),
    ],
)
def test_effort_for_focus(focus, configured, expected):
    assert _effort_for_focus(focus, configured) == expected


def test_every_scan_focus_has_prompt_and_snapshot_window():
    # The scheduler maps 22:00/23:00 → "night"; a missing prompt silently fell
    # back to "general" before.
    for focus in ("morning", "midday", "evening", "night", "general", "weekly"):
        assert focus in _SCAN_PROMPTS
        assert focus in _FOCUS_SNAPSHOT_DAYS


# ----------------------------------------------------------------------
# Tool registry invariants
# ----------------------------------------------------------------------
def test_tool_registry_invariants():
    tools = get_all_tools_anthropic(None)
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)), "duplicate tool names"
    # Exactly one cache marker, and it is on the LAST definition — a marker
    # stranded mid-list stops caching everything after it.
    marked = [i for i, t in enumerate(tools) if "cache_control" in t]
    assert marked == [len(tools) - 1]
    # Every declared tool must resolve to a handler method, or the dispatch
    # returns "Unknown tool" at runtime.
    for name in names:
        assert callable(getattr(QueryToolHandler, name, None)), f"no handler for {name}"
    for expected in (
        "get_training_status", "get_intraday_data", "get_lifestyle_analytics",
        "get_behavior_root_cause", "get_bedroom_environment",
        "get_environment_forecast", "scan_all_anomalies",
    ):
        assert expected in names


# ----------------------------------------------------------------------
# New handler methods against a temp DB
# ----------------------------------------------------------------------
@pytest.fixture
def tool_db(tmp_path):
    db = tmp_path / "tools_test.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE stress_intraday (time TEXT, device TEXT, stress_level INTEGER,
                                      PRIMARY KEY (time, device));
        CREATE TABLE training_status (time TEXT, device TEXT, training_status INTEGER,
                                      training_status_feedback_phrase TEXT,
                                      weekly_training_load REAL, fitness_trend INTEGER,
                                      acwr_percent REAL, daily_training_load_acute REAL,
                                      daily_training_load_chronic REAL,
                                      daily_acute_chronic_workload_ratio REAL,
                                      heat_acclimation_percentage REAL,
                                      altitude_acclimation_percentage REAL,
                                      PRIMARY KEY (time, device));
        CREATE TABLE ha_sensor_daily (date TEXT, entity_id TEXT, mean_value REAL,
                                      min_value REAL, max_value REAL,
                                      overnight_mean REAL, unit TEXT,
                                      fetched_at TEXT,
                                      PRIMARY KEY (date, entity_id));
        CREATE TABLE environment_daily (date TEXT PRIMARY KEY, temp_max_c REAL,
                                        apparent_temp_max_c REAL, european_aqi REAL,
                                        pm25 REAL, pollen_grass REAL);
        CREATE TABLE activity_summary (activity_id INTEGER, time TEXT, device TEXT,
                                       activity_name TEXT, activity_type TEXT,
                                       distance REAL, elapsed_duration REAL,
                                       moving_duration REAL, average_speed REAL,
                                       max_speed REAL, calories REAL, average_hr REAL,
                                       max_hr REAL, hr_time_in_zone_1 REAL,
                                       hr_time_in_zone_2 REAL, hr_time_in_zone_3 REAL,
                                       hr_time_in_zone_4 REAL, hr_time_in_zone_5 REAL,
                                       avg_power REAL, norm_power REAL,
                                       PRIMARY KEY (activity_id));
        """
    )
    conn.executemany(
        "INSERT INTO stress_intraday VALUES (?,?,?)",
        [
            ("2026-06-15T10:00:00+00:00", "dev", 30),
            ("2026-06-15T10:30:00+00:00", "dev", 50),
            ("2026-06-15T14:00:00+00:00", "dev", 80),
            ("2026-06-15T15:00:00+00:00", "dev", -1),
        ],
    )
    conn.executemany(
        "INSERT INTO training_status VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("2026-06-15T06:00:00", "dev", 3, "PRODUCTIVE", 400.0, 1, 90.0,
             350.0, 380.0, 0.9, 20.0, 0.0),
            ("2026-06-15T18:00:00", "dev", 3, "MAINTAINING", 420.0, 1, 95.0,
             360.0, 385.0, 0.95, 25.0, 0.0),
        ],
    )
    conn.executemany(
        "INSERT INTO ha_sensor_daily VALUES (?,?,?,?,?,?,?,?)",
        [
            ("2026-06-15", "sensor.bedroom_temp", 21.5, 19.0, 24.0, 20.2, "°C", "x"),
            ("2026-06-16", "sensor.bedroom_temp", 22.0, 19.5, 25.0, 21.0, "°C", "x"),
        ],
    )
    conn.executemany(
        "INSERT INTO environment_daily VALUES (?,?,?,?,?,?)",
        [
            ("2099-01-01", 18.0, 17.5, 30.0, 8.0, 0.0),
            ("2099-01-02", 28.0, 31.0, 65.0, 26.0, 120.0),
        ],
    )
    conn.execute(
        "INSERT INTO activity_summary VALUES (1, '2026-06-15T07:00:00', 'dev', "
        "'Morning Run', 'running', 5000.0, 1800.0, 1750.0, 2.8, 3.5, 400.0, "
        "150.0, 175.0, 120.0, 600.0, 700.0, 300.0, 80.0, NULL, NULL)"
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def handler(tool_db):
    repo = SqliteRepo(types.SimpleNamespace(sqlite_db_path=tool_db))
    return QueryToolHandler(repo=repo, memory=None, analysis=None)


def test_get_intraday_data_digest(handler):
    out = json.loads(handler.get_intraday_data("2026-06-15", "stress"))
    assert out["metric"] == "stress"
    assert out["n_samples"] == 3          # -1 sentinel dropped
    assert out["peak"]["value"] == 80
    assert out["low"]["value"] == 30


def test_get_intraday_data_unknown_metric(handler):
    out = json.loads(handler.get_intraday_data("2026-06-15", "nonsense"))
    assert "error" in out
    assert "stress" in out["available_metrics"]


def test_get_training_status_latest_per_day(handler):
    out = json.loads(handler.get_training_status("2026-06-15", "2026-06-15"))
    day = out["2026-06-15"]
    assert day["training_status_feedback_phrase"] == "MAINTAINING"  # latest wins
    assert day["daily_acute_chronic_workload_ratio"] == 0.95  # 2 d.p. — not rounded to 1
    assert day["weekly_training_load"] == 420


def test_get_bedroom_environment(handler):
    out = json.loads(handler.get_bedroom_environment("2026-06-15", "2026-06-16"))
    assert out["available"] is True
    sensor = out["sensors"]["sensor.bedroom_temp"]
    assert sensor["unit"] == "°C"
    assert sensor["days"]["2026-06-15"]["overnight"] == 20.2


def test_get_bedroom_environment_unavailable(tmp_path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    repo = SqliteRepo(types.SimpleNamespace(sqlite_db_path=str(empty)))
    h = QueryToolHandler(repo=repo, memory=None, analysis=None)
    out = json.loads(h.get_bedroom_environment("2026-06-15", "2026-06-16"))
    assert out["available"] is False


def test_get_environment_forecast_labels_forecast(handler, monkeypatch):
    # Freeze "today" before the fixture's far-future rows so both are returned.
    import garmin_insights.tools.query_tools as qt

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2099, 1, 1, 12, 0)

    monkeypatch.setattr(qt, "datetime", _FakeDT)
    out = json.loads(handler.get_environment_forecast())
    assert out["available"] is True
    assert "FORECAST" in out["note"]
    assert out["entries"]["2099-01-02"]["pollen_grass"] == 120
    # zero pollen stripped
    assert "pollen_grass" not in out["entries"]["2099-01-01"]


def test_get_activity_history_includes_zones_and_speed(handler):
    out = json.loads(handler.get_activity_history("2026-06-15", "2026-06-15"))
    row = out[0]
    assert row["hr_time_in_zone_2"] == 600
    assert row["hr_time_in_zone_3"] == 700
    assert row["average_speed"] == 2.8
    # nulls stripped — power columns absent for this activity
    assert "avg_power" not in row


def test_scan_all_anomalies_uses_engine(tool_db):
    class _FakeResult:
        def to_dict(self):
            return {"metric": "restingHeartRate", "z_score": 2.1}

    fake_analysis = types.SimpleNamespace(run_full_anomaly_scan=lambda: [_FakeResult()])
    repo = SqliteRepo(types.SimpleNamespace(sqlite_db_path=tool_db))
    h = QueryToolHandler(repo=repo, memory=None, analysis=fake_analysis)
    out = json.loads(h.scan_all_anomalies())
    assert out[0]["z_score"] == 2.1

    fake_empty = types.SimpleNamespace(run_full_anomaly_scan=lambda: [])
    h2 = QueryToolHandler(repo=repo, memory=None, analysis=fake_empty)
    assert "message" in json.loads(h2.scan_all_anomalies())


def test_get_lifestyle_analytics_rejects_unknown(handler):
    out = json.loads(handler.get_lifestyle_analytics("not_a_thing"))
    assert "error" in out
    assert "sleep_regularity" in out["available"]


# ----------------------------------------------------------------------
# Scan-report persistence (memory store)
# ----------------------------------------------------------------------
def test_scan_report_roundtrip(tmp_path):
    from garmin_insights.db.memory import MemoryStore

    store = MemoryStore(types.SimpleNamespace(sqlite_db_path=str(tmp_path / "m.db")))
    store.initialise_schema()
    store.save_scan_report("weekly", "All good this week.", None, None)
    store.save_scan_report("morning", "Sleep was short.", "2026-06-01", "2026-06-07")

    reports = store.get_scan_reports(limit=5)
    assert [r["focus"] for r in reports] == ["morning", "weekly"]  # newest first
    assert reports[0]["start_date"] == "2026-06-01"

    weekly_only = store.get_scan_reports(limit=5, focus="weekly")
    assert len(weekly_only) == 1
    assert weekly_only[0]["report"] == "All good this week."
