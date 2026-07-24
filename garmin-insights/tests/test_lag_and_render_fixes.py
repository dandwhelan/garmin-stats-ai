"""Regression tests for the lag-policy / render-robustness fix round.

Covers:
  * sleep_timeline survives a row with sleep_start/sleep_end but a NULL
    sleep_time_seconds (previously int(NaN) 500'd /api/visualizations);
  * behavior_dose_response joins a dose on day X to the night recorded on X+1
    (NEXT_DAY_LAG_METRICS policy — the same-day join paired it with the night
    BEFORE the behavior);
  * caffeine_cutoff groups nights by the previous day's caffeine, not same-day;
  * behavior_streak_calendar marks logged binary (no-value) days as 1.0 instead
    of rendering them as unlogged;
  * MemoryStore.recent_lifestyle_entries exists and feeds the composite-strain
    contributor ranking (previously an AttributeError made "logged:" entries
    permanently unreachable);
  * the lifestyle load cache evicts expired windows instead of growing forever.
"""

from __future__ import annotations

import json
import sqlite3
import time
import types
from datetime import datetime, timedelta

from garmin_insights.db.memory import MemoryStore
from garmin_insights.insights.proactive import InsightScanner
from garmin_insights.tools.analysis_tools import AnomalyResult
from garmin_insights.web.lifestyle_viz import LifestyleService
from garmin_insights.web.visualizations import VisualizationService


def _day(offset: int) -> str:
    return (datetime.now().date() - timedelta(days=offset)).isoformat()


# ------------------------------------------------------------------
# sleep_timeline NULL sleep_time_seconds
# ------------------------------------------------------------------

def test_sleep_timeline_survives_null_duration(tmp_path):
    db_path = str(tmp_path / "viz.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sleep_summary (date TEXT, time TEXT, "
                 "sleep_start TEXT, sleep_end TEXT, sleep_time_seconds REAL, "
                 "sleep_score REAL)")
    # Timestamps present, duration NULL — previously crashed on int(NaN).
    conn.execute("INSERT INTO sleep_summary VALUES ('2026-07-02', "
                 "'2026-07-02T07:00:00', '2026-07-01T23:00:00', "
                 "'2026-07-02T07:00:00', NULL, 80)")
    # Normal row keeps working unchanged.
    conn.execute("INSERT INTO sleep_summary VALUES ('2026-07-03', "
                 "'2026-07-03T07:00:00', '2026-07-02T23:30:00', "
                 "'2026-07-03T07:00:00', 25200, 75)")
    conn.commit()
    conn.close()

    out = VisualizationService(db_path).sleep_timeline("2026-07-01", "2026-07-04")
    by_date = {e["date"]: e for e in out}
    # NULL duration falls back to the bed→wake window (8 h).
    assert by_date["2026-07-02"]["duration_h"] == 8.0
    # Stored duration still wins when present.
    assert by_date["2026-07-03"]["duration_h"] == 7.0


# ------------------------------------------------------------------
# Lifestyle service fixtures
# ------------------------------------------------------------------

def _lifestyle_db(tmp_path) -> str:
    db_path = str(tmp_path / "life.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE daily_summaries (date TEXT PRIMARY KEY, metric_json TEXT)")
    conn.execute("CREATE TABLE lifestyle_journal (date TEXT, behavior TEXT, "
                 "category TEXT, status INTEGER, value REAL, device TEXT)")
    conn.commit()
    conn.close()
    return db_path


def test_dose_response_uses_next_nights_sleep(tmp_path):
    db_path = _lifestyle_db(tmp_path)
    conn = sqlite3.connect(db_path)
    dose_days = [_day(d) for d in (12, 10, 8, 6, 4)]
    for d in dose_days:
        conn.execute("INSERT INTO lifestyle_journal VALUES (?, 'Alcohol', "
                     "'LIFESTYLE', 1, 2, 'x')", (d,))
    # The night FOLLOWING a dose day scores 40; every other night 90.
    for offset in range(14, 0, -1):
        day = _day(offset)
        prev = _day(offset + 1)
        score = 40 if prev in dose_days else 90
        conn.execute("INSERT INTO daily_summaries VALUES (?, ?)", (
            day, json.dumps({"sleepScore": score, "avgOvernightHrv": 50,
                             "restingHeartRate": 55, "deepSleepSeconds": 3600}),
        ))
    conn.commit()
    conn.close()

    res = LifestyleService(db_path).behavior_dose_response(_day(14), _day(0))
    alcohol = next(b for b in res["behaviors"] if b["behavior"] == "Alcohol")
    # Every point must carry the affected (next) night, not the prior one.
    assert alcohol["points"], "expected dose points"
    assert all(p["sleepScore"] == 40 for p in alcohol["points"])


def test_caffeine_cutoff_grades_the_following_night(tmp_path):
    db_path = _lifestyle_db(tmp_path)
    conn = sqlite3.connect(db_path)
    late_days = [_day(d) for d in (10, 7, 4)]
    for d in late_days:
        conn.execute("INSERT INTO lifestyle_journal VALUES (?, 'Late Caffeine', "
                     "'LIFESTYLE', 1, 1, 'x')", (d,))
    # Night after late caffeine scores 50; all other nights 90.
    for offset in range(14, 0, -1):
        day = _day(offset)
        score = 50 if _day(offset + 1) in late_days else 90
        conn.execute("INSERT INTO daily_summaries VALUES (?, ?)", (
            day, json.dumps({"sleepScore": score, "deepSleepSeconds": 3600,
                             "avgOvernightHrv": 45, "awakeCount": 1}),
        ))
    conn.commit()
    conn.close()

    res = LifestyleService(db_path).caffeine_cutoff(_day(14), _day(0))
    groups = {g["group"]: g for g in res["groups"]}
    # Same-day join would have averaged the 90-score nights BEFORE the coffee.
    assert groups["Late caffeine"]["sleep_score"] == 50.0
    assert groups["No caffeine"]["sleep_score"] == 90.0


def test_streak_calendar_marks_binary_behaviors(tmp_path):
    db_path = _lifestyle_db(tmp_path)
    conn = sqlite3.connect(db_path)
    # Binary behavior: status=1, value NULL.
    conn.execute("INSERT INTO lifestyle_journal VALUES (?, 'Stretching', "
                 "'SELF_CARE', 1, NULL, 'x')", (_day(2),))
    # Valued behavior on another day.
    conn.execute("INSERT INTO lifestyle_journal VALUES (?, 'Alcohol', "
                 "'LIFESTYLE', 1, 3, 'x')", (_day(1),))
    conn.commit()
    conn.close()

    res = LifestyleService(db_path).behavior_streak_calendar(_day(3), _day(0))
    rows = {b["behavior"]: b for b in res["behaviors"]}
    dates = res["dates"]
    stretch = rows["Stretching"]["cells"][dates.index(_day(2))]
    alcohol = rows["Alcohol"]["cells"][dates.index(_day(1))]
    assert stretch == 1.0  # previously None — rendered as "not logged"
    assert alcohol == 3.0
    # Unlogged days stay empty.
    assert rows["Stretching"]["cells"][dates.index(_day(0))] is None


def test_load_cache_evicts_expired_windows(tmp_path):
    db_path = _lifestyle_db(tmp_path)
    svc = LifestyleService(db_path)
    svc._load_cache[("summaries", "2020-01-01", "2020-02-01")] = (
        time.monotonic() - 3600, __import__("pandas").DataFrame()
    )
    svc._load_summaries(_day(7), _day(0))
    assert ("summaries", "2020-01-01", "2020-02-01") not in svc._load_cache
    assert ("summaries", _day(7), _day(0)) in svc._load_cache


# ------------------------------------------------------------------
# recent_lifestyle_entries → composite-strain contributor ranking
# ------------------------------------------------------------------

def _memory_store(tmp_path) -> MemoryStore:
    # MemoryStore only reads settings.sqlite_db_path (same stand-in pattern
    # as conftest's fake_settings).
    return MemoryStore(types.SimpleNamespace(sqlite_db_path=str(tmp_path / "mem.db")))


def test_recent_lifestyle_entries_reads_journal(tmp_path):
    memory = _memory_store(tmp_path)
    conn = sqlite3.connect(memory.db_path)
    conn.execute("CREATE TABLE lifestyle_journal (date TEXT, behavior TEXT, "
                 "category TEXT, status INTEGER, value REAL, device TEXT)")
    conn.execute("INSERT INTO lifestyle_journal VALUES (?, 'Alcohol', "
                 "'LIFESTYLE', 1, 2, 'x')", (_day(1),))
    conn.execute("INSERT INTO lifestyle_journal VALUES (?, 'Stretching', "
                 "'SELF_CARE', 0, NULL, 'x')", (_day(1),))  # status=0 excluded
    conn.execute("INSERT INTO lifestyle_journal VALUES (?, 'Sauna', "
                 "'SELF_CARE', 1, NULL, 'x')", (_day(30),))  # too old
    conn.commit()
    conn.close()

    df = memory.recent_lifestyle_entries(hours=48)
    assert df["behavior"].tolist() == ["Alcohol"]


def test_recent_lifestyle_entries_missing_table(tmp_path):
    df = _memory_store(tmp_path).recent_lifestyle_entries(hours=48)
    assert df.empty


def test_composite_strain_ranks_logged_behaviors_first(tmp_path):
    memory = _memory_store(tmp_path)
    conn = sqlite3.connect(memory.db_path)
    conn.execute("CREATE TABLE lifestyle_journal (date TEXT, behavior TEXT, "
                 "category TEXT, status INTEGER, value REAL, device TEXT)")
    conn.execute("INSERT INTO lifestyle_journal VALUES (?, 'Alcohol', "
                 "'LIFESTYLE', 1, 2, 'x')", (_day(1),))
    conn.commit()
    conn.close()

    anomalies = [
        AnomalyResult(metric="restingHeartRate", date=_day(1), value=60,
                      baseline_mean=52, baseline_std=2, z_score=2.0,
                      direction="above"),
        AnomalyResult(metric="avgOvernightHrv", date=_day(1), value=30,
                      baseline_mean=45, baseline_std=5, z_score=-1.8,
                      direction="below"),
    ]
    analysis = types.SimpleNamespace(run_full_anomaly_scan=lambda: anomalies)
    findings = InsightScanner(memory, analysis).scan_composite_strain()
    assert len(findings) == 1
    # The user-logged behaviour must lead the ranked contributor list.
    assert findings[0]["ranked_contributors"][0] == "logged: Alcohol"
