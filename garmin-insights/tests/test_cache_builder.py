"""CacheBuilder — the layer everything the LLM sees flows through.

``daily_summaries`` feeds the dashboard, the agent's ``get_daily_metrics``
tool, the portable prompt and every baseline-derived analytic at once, so a
bug here is simultaneously wrong in four places while looking fine in each.

Body-composition merging is covered in test_body_comp_and_model_gate.py; this
file covers the rest of the build, the note merge, and baseline computation.
"""

from __future__ import annotations

import sqlite3
import types
from datetime import datetime, timedelta

import pytest

from garmin_insights.db.cache import CacheBuilder, _BASELINE_METRICS
from garmin_insights.db.memory import MemoryStore
from garmin_insights.db.sqlite_repo import SqliteRepo


@pytest.fixture
def builder(sample_settings):
    repo = SqliteRepo(sample_settings)
    memory = MemoryStore(sample_settings)
    memory.initialise_schema()
    return CacheBuilder(repo, memory), memory


def _iso(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# ==================================================================
# build_daily_summary — column mapping
# ==================================================================
def test_summary_carries_the_camelcase_daily_stats_subset(builder, sample_rows):
    cache, _memory = builder
    row = sample_rows[-3]
    summary = cache.build_daily_summary(row["date"])

    assert summary["date"] == row["date"]
    assert summary["restingHeartRate"] == pytest.approx(row["rhr"])
    assert summary["totalSteps"] == pytest.approx(row["steps"])
    assert summary["stressPercentage"] == pytest.approx(row["stress"])
    assert summary["bodyBatteryHighestValue"] == pytest.approx(row["bb_high"])


def test_summary_carries_the_camelcase_sleep_subset(builder, sample_rows):
    cache, _memory = builder
    row = sample_rows[-3]
    summary = cache.build_daily_summary(row["date"])

    assert summary["sleepScore"] == pytest.approx(row["sleep_score"])
    assert summary["avgOvernightHrv"] == pytest.approx(row["hrv"])
    assert summary["averageRespirationValue"] == pytest.approx(row["resp"])
    assert summary["sleepTimeSeconds"] == pytest.approx(row["sleep_secs"])


def test_summary_includes_the_keys_the_kb_rules_depend_on(builder, sample_rows):
    """sedentary_stress_coupling and overnight_spo2_disordered_breathing can
    only fire if these reach the cache — they used to live only in raw tables."""
    cache, _memory = builder
    summary = cache.build_daily_summary(sample_rows[-3]["date"])
    assert "sedentarySeconds" in summary
    assert "lowestSpo2Value" in summary
    assert "bodyBatteryDuringSleep" in summary


def test_colliding_keys_are_prefixed_rather_than_overwritten(builder, sample_rows):
    """restingHeartRate exists in BOTH daily_stats and sleep_summary. The
    daily-stats value must win and the sleep one must be kept separately, not
    silently clobber it."""
    cache, _memory = builder
    summary = cache.build_daily_summary(sample_rows[-3]["date"])
    assert "restingHeartRate" in summary
    assert "sleep_restingHeartRate" in summary


def test_lifestyle_journal_is_stored_alongside_the_metrics(builder, sample_rows):
    cache, memory = builder
    drinking_day = next(r for r in reversed(sample_rows) if r["drinks"])
    cache.build_daily_summary(drinking_day["date"])

    stored = memory.get_daily_summary(drinking_day["date"])
    assert stored["lifestyle"]["Alcohol"]["value"] == pytest.approx(drinking_day["drinks"])
    assert stored["lifestyle"]["Alcohol"]["status"] == 1


def test_training_and_acclimation_fields_are_prefixed_and_merged(builder, sample_rows):
    cache, _memory = builder
    summary = cache.build_daily_summary(sample_rows[-3]["date"])
    assert summary["training_score"] is not None
    assert summary["heatAcclimationPercentage"] is not None


def test_missing_day_still_produces_a_summary_skeleton(builder):
    """A date with no raw data must not raise — the dashboard builds ranges
    that legitimately include empty days."""
    cache, _memory = builder
    summary = cache.build_daily_summary("2099-01-01")
    assert summary == {"date": "2099-01-01", "is_complete": True}


# ==================================================================
# is_complete
# ==================================================================
def test_is_complete_defaults_true_and_is_persisted(builder, sample_rows):
    cache, memory = builder
    date = sample_rows[-3]["date"]
    cache.build_daily_summary(date)
    assert memory.get_daily_summary(date)["is_complete"] is True


def test_today_is_marked_incomplete(builder, sample_rows):
    """The agent is instructed not to compare today's cumulative metrics
    (steps, calories) against baselines — this flag is how it knows."""
    cache, memory = builder
    today = sample_rows[-1]["date"]
    cache.build_daily_summary(today, is_complete=False)
    assert memory.get_daily_summary(today)["is_complete"] is False


# ==================================================================
# Sleep is keyed to the WAKE date
# ==================================================================
def test_sleep_lands_on_the_wake_date_not_the_night_it_started(builder, sample_rows):
    """A sleep_summary row dated X is the night that ENDED on the morning of X.
    Getting this backwards shifts every sleep metric by a day."""
    cache, _memory = builder
    row = sample_rows[-3]
    summary = cache.build_daily_summary(row["date"])

    # The fixture's sleep for this row starts the PREVIOUS evening.
    assert row["sleep_start"].date().isoformat() < row["date"]
    assert row["sleep_end"].date().isoformat() == row["date"]
    assert summary["sleepScore"] == pytest.approx(row["sleep_score"])


# ==================================================================
# Daily notes merge
# ==================================================================
def test_note_is_merged_into_the_summary_the_agent_reads(builder, sample_rows):
    """Notes are user-authored ground truth; they must survive cache rebuilds
    and reach the agent without a separate lookup."""
    cache, memory = builder
    date = sample_rows[-3]["date"]
    cache.build_daily_summary(date)
    memory.upsert_daily_note(date, "Ran a hard 10k, slept badly")

    assert memory.get_daily_summary(date)["note"] == "Ran a hard 10k, slept badly"
    ranged = memory.get_daily_summaries_range(date, date)
    assert ranged[0]["note"] == "Ran a hard 10k, slept badly"


def test_note_survives_a_summary_rebuild(builder, sample_rows):
    cache, memory = builder
    date = sample_rows[-3]["date"]
    memory.upsert_daily_note(date, "kept")
    cache.build_daily_summary(date)
    assert memory.get_daily_summary(date)["note"] == "kept"


def test_days_without_a_note_have_no_note_key(builder, sample_rows):
    cache, memory = builder
    date = sample_rows[-4]["date"]
    cache.build_daily_summary(date)
    assert "note" not in memory.get_daily_summary(date)


def test_empty_note_clears_the_day(builder, sample_rows):
    cache, memory = builder
    date = sample_rows[-3]["date"]
    cache.build_daily_summary(date)
    memory.upsert_daily_note(date, "temporary")
    memory.upsert_daily_note(date, "")
    assert "note" not in memory.get_daily_summary(date)


# ==================================================================
# build_range
# ==================================================================
def test_build_range_skips_dates_already_cached(builder, sample_rows, sample_db):
    cache, _memory = builder
    start, end = sample_rows[-5]["date"], sample_rows[-1]["date"]

    # sample_db ships with the cache already warm; clear this window first.
    conn = sqlite3.connect(sample_db)
    conn.execute("DELETE FROM daily_summaries WHERE date >= ? AND date <= ?", (start, end))
    conn.commit()
    conn.close()

    assert cache.build_range(start, end) == 5
    assert cache.build_range(start, end) == 0, "second pass should find nothing to do"


def test_build_range_force_rebuilds_everything(builder, sample_rows):
    """The trailing-window rebuild depends on force ignoring the cache — late
    arrivals (retroactive Garmin edits, a late weigh-in) reach the cache only
    through this path."""
    cache, _memory = builder
    start, end = sample_rows[-5]["date"], sample_rows[-1]["date"]
    cache.build_range(start, end)
    assert cache.build_range(start, end, force=True) == 5


def test_build_range_picks_up_data_that_landed_after_the_first_build(
        builder, sample_rows, sample_db):
    cache, memory = builder
    date = sample_rows[-3]["date"]
    cache.build_daily_summary(date)

    conn = sqlite3.connect(sample_db)
    conn.execute("UPDATE daily_stats SET total_steps = 12345 WHERE date = ?", (date,))
    conn.commit()
    conn.close()

    # Without force the stale row survives; with force it is corrected.
    cache.build_range(date, date)
    assert memory.get_daily_summary(date)["totalSteps"] != 12345
    cache.build_range(date, date, force=True)
    assert memory.get_daily_summary(date)["totalSteps"] == 12345


# ==================================================================
# update_baselines
# ==================================================================
def test_baselines_exclude_today(builder, sample_rows):
    """Today is incomplete; folding its partial totals into a baseline would
    drag every cumulative metric down."""
    cache, memory = builder
    cache.build_range(sample_rows[0]["date"], sample_rows[-1]["date"], force=True)
    # Mark today incomplete the way refresh() does.
    cache.build_daily_summary(sample_rows[-1]["date"], is_complete=False)

    baselines = cache.update_baselines()
    steps_30d = [r["steps"] for r in sample_rows[-31:-1]]
    assert baselines["totalSteps"]["avg_30d"] == pytest.approx(
        sum(steps_30d) / len(steps_30d), rel=0.02)


def test_baselines_use_sample_sd_not_population_sd(builder, sample_rows):
    """ddof=1 — matches analysis_tools. Population SD understates spread on a
    7-point window, which would make z-scores too large and over-flag."""
    import statistics

    cache, _memory = builder
    cache.build_range(sample_rows[0]["date"], sample_rows[-1]["date"], force=True)
    cache.build_daily_summary(sample_rows[-1]["date"], is_complete=False)

    baselines = cache.update_baselines()
    rhr_7d = [r["rhr"] for r in sample_rows[-8:-1]]
    assert baselines["restingHeartRate"]["std_7d"] == pytest.approx(
        statistics.stdev(rhr_7d), rel=0.02)
    assert baselines["restingHeartRate"]["std_7d"] != pytest.approx(
        statistics.pstdev(rhr_7d), rel=1e-6)


def test_every_declared_baseline_metric_is_written(builder, sample_rows):
    cache, memory = builder
    cache.build_range(sample_rows[0]["date"], sample_rows[-1]["date"], force=True)
    cache.update_baselines()

    stored = memory.get_baselines()
    for metric in _BASELINE_METRICS:
        assert metric in stored, f"{metric} declared but never baselined"


def test_respiration_is_baselined(builder, sample_rows):
    """Without it, detect_illness_signature and the composite strain triad are
    permanently blind to elevated respiration."""
    cache, memory = builder
    cache.build_range(sample_rows[0]["date"], sample_rows[-1]["date"], force=True)
    cache.update_baselines()
    assert memory.get_baselines()["averageRespirationValue"]["avg_30d"] is not None


def test_baseline_std_is_none_for_a_single_sample(sample_settings, tmp_path):
    """SD is undefined for n=1; emitting 0 would make every later value look
    like an infinite z-score."""
    db = tmp_path / "sparse.db"
    settings = types.SimpleNamespace(sqlite_db_path=str(db))
    memory = MemoryStore(settings)
    memory.initialise_schema()
    cache = CacheBuilder(SqliteRepo(settings), memory)

    memory.upsert_daily_summary(_iso(1), {"restingHeartRate": 55.0, "is_complete": True})
    baselines = cache.update_baselines()
    assert baselines["restingHeartRate"]["avg_7d"] == 55.0
    assert baselines["restingHeartRate"]["std_7d"] is None


def test_baselines_are_none_when_there_is_no_data(tmp_path):
    db = tmp_path / "empty.db"
    settings = types.SimpleNamespace(sqlite_db_path=str(db))
    memory = MemoryStore(settings)
    memory.initialise_schema()
    cache = CacheBuilder(SqliteRepo(settings), memory)

    baselines = cache.update_baselines()
    assert all(v["avg_30d"] is None for v in baselines.values())


# ==================================================================
# refresh
# ==================================================================
def test_refresh_promotes_stale_incomplete_past_days(builder, sample_rows):
    """A mid-day build leaves is_complete=False. Without promotion that partial
    snapshot (e.g. 107 steps at 09:43) is never replaced with the day's final
    totals, because build_range skips dates that already have a row."""
    cache, memory = builder
    yesterday = _iso(1)
    memory.upsert_daily_summary(yesterday, {"date": yesterday, "totalSteps": 107.0,
                                            "is_complete": False})

    cache.refresh(days=3)

    promoted = memory.get_daily_summary(yesterday)
    assert promoted["is_complete"] is True
    assert promoted["totalSteps"] != 107.0


def test_refresh_always_marks_today_incomplete(builder):
    cache, memory = builder
    cache.refresh(days=3)
    assert memory.get_daily_summary(_iso(0))["is_complete"] is False


def test_refresh_forces_the_trailing_window_once_per_day(builder, monkeypatch):
    """The trailing rebuild is what lets late-arriving raw data reach the
    cache; running it on every poll would be wasteful, never would freeze the
    day's first snapshot."""
    cache, _memory = builder
    calls: list[bool] = []
    original = cache.build_range

    def spy(start, end, force=False):
        calls.append(force)
        return original(start, end, force=force)

    monkeypatch.setattr(cache, "build_range", spy)

    cache.refresh(days=3)
    assert calls.count(True) == 1, "first refresh should force the trailing window"

    calls.clear()
    cache.refresh(days=3)
    assert calls.count(True) == 0, "same day again should not re-force"
