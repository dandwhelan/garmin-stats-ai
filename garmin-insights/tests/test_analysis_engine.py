"""Tests for the AnalysisEngine correctness fixes.

Covers the statistical bugs fixed in this change:
  * z-scores normalise against the 30-day std (matching the 30-day mean),
    not the noisy 7-day std;
  * trend slopes regress against real calendar days, not row positions;
  * behavior→metric lag joins on the actual next date, not the next row;
  * social jet lag measures sleep-MIDPOINT shift, not duration variance;
  * Cohen's d effect size accompanies the behavior comparison.
"""

from __future__ import annotations

import sqlite3
import types
from datetime import datetime, timedelta

import numpy as np
import pytest

from garmin_insights.db.memory import MemoryStore
from garmin_insights.tools.analysis_tools import (
    AnalysisEngine,
    _baseline_scale,
    _cohens_d,
    _effect_size_label,
)


@pytest.fixture
def memory(tmp_path):
    db = tmp_path / "mem.db"
    store = MemoryStore(types.SimpleNamespace(sqlite_db_path=str(db)))
    store.initialise_schema()
    return store


def _iso(days_ago: int) -> str:
    return (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# _baseline_scale — the z-score denominator
# --------------------------------------------------------------------------
def test_baseline_scale_prefers_std_30d():
    assert _baseline_scale({"std_30d": 4.0, "std_7d": 1.0}) == 4.0


def test_baseline_scale_falls_back_to_std_7d_when_30d_missing():
    assert _baseline_scale({"std_30d": None, "std_7d": 2.5}) == 2.5
    assert _baseline_scale({"std_30d": 0, "std_7d": 2.5}) == 2.5


def test_baseline_scale_none_when_no_usable_std():
    assert _baseline_scale({"std_30d": 0, "std_7d": 0}) is None
    assert _baseline_scale({}) is None


# --------------------------------------------------------------------------
# detect_anomalies — z-score uses the 30-day std, not the 7-day std
# --------------------------------------------------------------------------
def test_detect_anomalies_uses_30d_std(memory):
    # 30-day std is wide (5), 7-day std is tiny (1). A value 6 above the mean is
    # ~1.2σ on the correct 30-day scale (below the 1.5σ threshold), but would be
    # a spurious 6σ on the old 7-day scale.
    memory.upsert_baseline("restingHeartRate", 55.0, 55.0, 1.0, 5.0, 45.0, 65.0, 61.0)
    memory.upsert_daily_summary(_iso(0), {"restingHeartRate": 61.0, "is_complete": True})

    engine = AnalysisEngine(memory)
    anomalies = engine.detect_anomalies("restingHeartRate", days=3, threshold_sigma=1.5)
    # 6 / 5 = 1.2σ -> not flagged with the correct denominator.
    assert anomalies == []


def test_detect_anomalies_flags_real_deviation(memory):
    memory.upsert_baseline("restingHeartRate", 55.0, 55.0, 1.0, 4.0, 45.0, 65.0, 65.0)
    memory.upsert_daily_summary(_iso(0), {"restingHeartRate": 65.0, "is_complete": True})

    engine = AnalysisEngine(memory)
    anomalies = engine.detect_anomalies("restingHeartRate", days=3, threshold_sigma=1.5)
    # 10 / 4 = 2.5σ -> flagged.
    assert len(anomalies) == 1
    assert anomalies[0].z_score == pytest.approx(2.5, abs=0.01)
    assert anomalies[0].direction == "above"


# --------------------------------------------------------------------------
# detect_trend — regress on real dates, robust to missing days
# --------------------------------------------------------------------------
def test_detect_trend_uses_calendar_days_with_gaps(memory):
    # Values rise 1 unit per CALENDAR day, but with a gap in the middle. A
    # row-index regression would over-steepen the slope; a date regression
    # recovers the true +1.0/day.
    base = datetime.utcnow() - timedelta(days=10)
    points = {0: 50.0, 1: 51.0, 2: 52.0, 5: 55.0, 6: 56.0}  # day 3,4 missing
    for offset, val in points.items():
        d = (base + timedelta(days=offset)).strftime("%Y-%m-%d")
        memory.upsert_daily_summary(d, {"avgOvernightHrv": val, "is_complete": True})

    engine = AnalysisEngine(memory)
    result = engine.detect_trend("avgOvernightHrv", days=12)
    assert result is not None
    assert result.slope_per_day == pytest.approx(1.0, abs=0.01)
    assert result.direction == "increasing"


# --------------------------------------------------------------------------
# compare_metric_with_behavior — date-based lag join + Cohen's d
# --------------------------------------------------------------------------
def test_behavior_lag_joins_on_actual_next_date(memory):
    # Alcohol on day D should pair with sleepScore on day D+1. There is a gap
    # (a day with no summary at all), so a positional +1 shift would mis-pair.
    base = datetime.utcnow() - timedelta(days=20)

    def day(offset):
        return (base + timedelta(days=offset)).strftime("%Y-%m-%d")

    # Day 0: alcohol -> day 1 sleep should be LOW (60)
    memory.upsert_daily_summary(day(0), {"sleepScore": 80, "is_complete": True},
                                {"Alcohol": {"status": 1}})
    memory.upsert_daily_summary(day(1), {"sleepScore": 60, "is_complete": True},
                                {"Alcohol": {"status": 0}})
    # Day 2 missing entirely (gap).
    # Day 3: alcohol -> day 4 sleep should be LOW (62)
    memory.upsert_daily_summary(day(3), {"sleepScore": 82, "is_complete": True},
                                {"Alcohol": {"status": 1}})
    memory.upsert_daily_summary(day(4), {"sleepScore": 62, "is_complete": True},
                                {"Alcohol": {"status": 0}})
    # Some clean high-sleep nights following non-alcohol days.
    memory.upsert_daily_summary(day(5), {"sleepScore": 85, "is_complete": True},
                                {"Alcohol": {"status": 0}})
    memory.upsert_daily_summary(day(6), {"sleepScore": 84, "is_complete": True},
                                {"Alcohol": {"status": 0}})

    engine = AnalysisEngine(memory)
    result = engine.compare_metric_with_behavior("Alcohol", "sleepScore", days=25)
    assert result is not None
    # Alcohol-night follow-ups (60, 62) must be the "with" group — proving the
    # lag landed on D+1 by date despite the day-2 gap.
    assert result.mean_with == pytest.approx(61.0, abs=0.01)
    assert result.mean_with < result.mean_without


def test_cohens_d_and_effect_label():
    a = [10.0, 11.0, 9.0, 10.5, 9.5]
    b = [20.0, 21.0, 19.0, 20.5, 19.5]
    d = _cohens_d(a, b)
    assert d is not None and abs(d) > 0.8
    assert _effect_size_label(d) == "large"
    assert _effect_size_label(0.1) == "negligible"
    assert _effect_size_label(None) is None
    assert _cohens_d([1.0], [2.0, 3.0]) is None  # too few points


# --------------------------------------------------------------------------
# detect_social_jet_lag — midpoint shift, not duration
# --------------------------------------------------------------------------
def _make_sleep_table(memory):
    conn = sqlite3.connect(memory.db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sleep_summary "
        "(date TEXT PRIMARY KEY, time TEXT, sleep_time_seconds INTEGER)"
    )
    conn.commit()
    conn.close()


def test_social_jet_lag_detects_shifted_midpoint_with_constant_duration(memory):
    """Same 8h duration every night, but weekends wake 3h later -> the midpoint
    shifts 3h. The old duration proxy would report zero jet lag; the fixed
    midpoint method must flag it."""
    _make_sleep_table(memory)
    conn = sqlite3.connect(memory.db_path)
    eight_h = 8 * 3600
    base = datetime.utcnow() - timedelta(days=21)
    for offset in range(21):
        d = base + timedelta(days=offset)
        dow = d.weekday()
        # Weekday wake 06:00, weekend wake 09:00 — both after 8h in bed.
        wake_hour = 9 if dow >= 5 else 6
        wake = d.replace(hour=wake_hour, minute=0, second=0, microsecond=0)
        conn.execute(
            "INSERT OR REPLACE INTO sleep_summary VALUES (?,?,?)",
            (d.strftime("%Y-%m-%d"), wake.strftime("%Y-%m-%dT%H:%M:%S"), eight_h),
        )
    conn.commit()
    conn.close()

    engine = AnalysisEngine(memory)
    result = engine.detect_social_jet_lag(days=25)
    assert result is not None
    assert result["diff_hours"] == pytest.approx(3.0, abs=0.1)
    assert result["social_jet_lag"] is True


def test_social_jet_lag_none_without_sleep_table(memory):
    # No sleep_summary table at all -> graceful None, not a crash.
    engine = AnalysisEngine(memory)
    assert engine.detect_social_jet_lag(days=25) is None
