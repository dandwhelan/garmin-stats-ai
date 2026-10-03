"""Regression tests for two render-robustness fixes:

  * sleep_timeline survives a row with sleep_start/sleep_end but a NULL
    sleep_time_seconds (previously int(NaN) 500'd /api/visualizations);
  * behavior_streak_calendar marks logged binary (no-value) days as 1.0
    instead of rendering them as unlogged.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

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


