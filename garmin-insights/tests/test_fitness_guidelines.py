"""insights/fitness.py — evidence-based activity dose + HRV-guided findings."""

import math
from datetime import date, timedelta

import pytest

from garmin_insights.insights import fitness
from garmin_insights.knowledge.medical import INSIGHT_RULES

TODAY = date(2026, 11, 20)


def _day(offset, **kw):
    return {"date": (TODAY - timedelta(days=offset)).isoformat(), "is_complete": True, **kw}


# ---------------------------------------------------------------- steps
def test_steps_median_and_status():
    rows = [_day(i, totalSteps=s) for i, s in enumerate([5000, 6000, 8000, 9000, 6500, 7200, 4000], 1)]
    out = fitness.steps_dose(rows)
    assert out["median_steps"] == 6500
    assert out["status"] == "below"
    assert out["steps_to_target"] == 500
    assert out["pct_days_ge_7000"] == round(100 * 3 / 7)


def test_steps_ignore_incomplete_and_zero_days():
    rows = [_day(i, totalSteps=8000) for i in range(1, 8)]
    rows += [_day(0, totalSteps=200, is_complete=False), _day(9, totalSteps=0)]
    out = fitness.steps_dose(rows)
    assert out["days"] == 7 and out["status"] == "meets"


def test_steps_need_enough_days():
    assert fitness.steps_dose([_day(1, totalSteps=9000)]) is None


# ---------------------------------------------------------------- MVPA
def test_weekend_warrior_pattern_is_recognised():
    rows = [_day(i, moderateIntensityMinutes=0, vigorousIntensityMinutes=0) for i in range(1, 6)]
    rows += [_day(6, moderateIntensityMinutes=90), _day(7, moderateIntensityMinutes=80)]
    out = fitness.weekly_mvpa(rows)
    assert out["mvpa_equiv"] == 170
    assert out["pattern"] == "weekend_warrior"


def test_regular_pattern_and_vigorous_counts_double():
    rows = [_day(i, moderateIntensityMinutes=20, vigorousIntensityMinutes=5) for i in range(1, 8)]
    out = fitness.weekly_mvpa(rows)
    assert out["mvpa_equiv"] == 210  # 7 × (20 + 2×5)
    assert out["pattern"] == "regular"
    assert out["vigorous_days"] == 7


def test_below_guideline():
    rows = [_day(i, moderateIntensityMinutes=10) for i in range(1, 8)]
    assert fitness.weekly_mvpa(rows)["pattern"] == "below_guideline"


# ---------------------------------------------------------------- strength
def _act(offset, kind="strength_training", secs=1800):
    return {"time": f"{(TODAY - timedelta(days=offset)).isoformat()}T18:00:00",
            "activity_type": kind, "elapsed_duration": secs}


def test_strength_volume_counts_distinct_days():
    acts = [_act(o) for o in (1, 4, 8, 11, 15, 18, 22, 25)] + [_act(2, "running")]
    out = fitness.strength_volume(acts, TODAY)
    assert out["days_per_week"] == 2.0
    assert out["minutes_per_week"] == 60
    assert out["status"] == "meets" and out["in_30_60_min_range"]


def test_strength_unknown_without_any_recordings():
    assert fitness.strength_volume([], TODAY) is None


def test_strength_zero_when_only_cardio_recorded():
    out = fitness.strength_volume([_act(3, "running")], TODAY)
    assert out["sessions_per_week"] == 0 and out["status"] == "below"


# ---------------------------------------------------------------- HRV
def _hrv_rows(baseline_ms, recent_ms, baseline_days=30):
    rows = [_day(i, avgOvernightHrv=baseline_ms + (3 if i % 2 else -3))
            for i in range(7, 7 + baseline_days)]
    rows += [_day(i, avgOvernightHrv=recent_ms) for i in range(0, 7)]
    return rows


def test_hrv_within_range():
    out = fitness.hrv_readiness(_hrv_rows(50, 50), TODAY)
    assert out["status"] == "within_normal"
    assert out["ln_band_low"] < out["ln_rolling_7d"] < out["ln_band_high"]


def test_hrv_below_range_suggests_easing_off():
    out = fitness.hrv_readiness(_hrv_rows(50, 40), TODAY)
    assert out["status"] == "below_normal"
    assert "low intensity" in out["guidance"]
    assert out["rolling_7d_ms"] == pytest.approx(40, abs=0.1)


def test_hrv_uses_todays_row_last_night():
    rows = _hrv_rows(50, 50)
    rows[-7]["is_complete"] = False  # today — still has last night's HRV
    assert fitness.hrv_readiness(rows, TODAY)["nights_in_window"] == 7


def test_hrv_sparse_baseline_flagged_or_skipped():
    assert fitness.hrv_readiness(_hrv_rows(50, 50, baseline_days=10), TODAY) is None
    out = fitness.hrv_readiness(_hrv_rows(50, 50, baseline_days=15), TODAY)
    assert out["baseline_low_confidence"] is True


# ---------------------------------------------------------------- KB wiring
def test_every_rule_the_scanner_names_exists():
    names = {r.name for r in INSIGHT_RULES}
    assert {
        "daily_steps_dose_response", "weekend_warrior_mvpa", "who_exercise_guidelines",
        "vigorous_intermittent_activity", "muscle_strengthening_weekly", "hrv_guided_training",
    } <= names


def test_scanner_fitness_pass_on_real_data(sample_settings):
    import json

    from garmin_insights.db.cache import CacheBuilder
    from garmin_insights.db.memory import MemoryStore
    from garmin_insights.db.sqlite_repo import SqliteRepo
    from garmin_insights.insights.proactive import InsightScanner
    from garmin_insights.tools.analysis_tools import AnalysisEngine

    memory = MemoryStore(sample_settings)
    memory.initialise_schema()
    repo = SqliteRepo(sample_settings)
    CacheBuilder(repo, memory).refresh(days=60)
    scanner = InsightScanner(memory, AnalysisEngine(memory))
    findings = scanner.scan_fitness_guidelines()
    json.dumps(findings)  # must be serialisable into the scan prompt
    assert {f["kind"] for f in findings} >= {"steps", "weekly_mvpa", "hrv_readiness", "rhr_trend"}
    for f in findings:
        assert f["source"] == "fitness_guidelines"
        assert f["evidence_tier"] in {"A", "B", "C"}


# ---------------------------------------------------------------- cadence
def _samples(day: str, bursts: dict[int, int], step_min: int = 15):
    """15-min buckets for a UTC day; ``bursts`` maps bucket index → steps."""
    from datetime import datetime as dt

    base = dt.fromisoformat(f"{day}T00:00:00+00:00")
    return [((base + timedelta(minutes=i * step_min)).isoformat(), bursts.get(i, 0))
            for i in range(24 * 60 // step_min)]


@pytest.fixture
def utc_tz():
    """Pin the process zone to UTC (local-day keying) and restore it after."""
    import os
    import time

    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_best_30min_cadence_finds_brisk_bout(utc_tz):
    out = fitness.best_30min_cadence(_samples("2026-11-10", {40: 1650, 41: 1650, 60: 900}))
    assert out["2026-11-10"] == pytest.approx(110.0)


def test_cadence_needs_contiguous_buckets(utc_tz):
    samples = [s for s in _samples("2026-11-10", {40: 1650, 42: 1650}) if not s[0].endswith("10:15:00+00:00")]
    out = fitness.best_30min_cadence(samples)
    assert out["2026-11-10"] < 100


def test_cadence_refuses_coarse_buckets():
    assert fitness.best_30min_cadence(_samples("2026-11-10", {10: 6000}, step_min=60)) is None


def test_cadence_summary_counts_moderate_days():
    per_day = {f"2026-11-{d:02d}": v for d, v in zip(range(1, 9), [80, 95, 101, 120, 60, 105, 99, 100])}
    out = fitness.cadence_summary(per_day)
    assert out["days_ge_100_spm"] == 4 and out["days"] == 8


# ---------------------------------------------------------------- RHR trend
def test_rhr_trend_falling():
    rows = [_day(i, restingHeartRate=52) for i in range(1, 31)]
    rows += [_day(i, restingHeartRate=56) for i in range(61, 91)]
    out = fitness.rhr_trend(rows, TODAY)
    assert out["change_bpm"] == -4 and out["direction"] == "falling"


def test_rhr_trend_needs_both_windows():
    rows = [_day(i, restingHeartRate=52) for i in range(1, 31)]
    assert fitness.rhr_trend(rows, TODAY) is None
