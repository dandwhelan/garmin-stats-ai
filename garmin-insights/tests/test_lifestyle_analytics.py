"""Correctness tests for LifestyleService analytics.

Distinct from test_fixtures_smoke.py, which only proves these run and return
the right shape. Here the expected values are computed independently from
``sample_rows`` (the records the fixture was generated from) and compared
against what the analytic returns — so a change that keeps the shape but
breaks the arithmetic fails.

Where an analytic's contract is a formula, the formula is asserted on a small
hand-built dataset rather than re-derived from the implementation, so the test
can't inherit the same mistake.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import types
from datetime import datetime, timedelta

import pytest

from garmin_insights.db.memory import MemoryStore
from garmin_insights.web.lifestyle_viz import LifestyleService, _prime_start


@pytest.fixture
def svc(sample_db):
    return LifestyleService(sample_db)


# ==================================================================
# Sleep timing
# ==================================================================
def test_social_jet_lag_matches_independently_computed_midpoints(svc, sample_rows, sample_dates):
    """The fixture shifts weekend bedtimes ~1.2h later at a constant duration,
    so the weekend midpoint must land ~1.2h later than the weekday one."""
    start, end = sample_dates
    out = svc.social_jet_lag(start, end)

    def midpoint_hours(r):
        mid = r["sleep_start"] + (r["sleep_end"] - r["sleep_start"]) / 2
        h = mid.hour + mid.minute / 60
        return h + 24 if h < 12 else h

    weekday = [midpoint_hours(r) for r in sample_rows if not r["weekend"]]
    weekend = [midpoint_hours(r) for r in sample_rows if r["weekend"]]

    assert out["weekday_n"] == len(weekday)
    assert out["weekend_n"] == len(weekend)
    assert out["weekday_midpoint_h"] == pytest.approx(statistics.fmean(weekday), abs=0.01)
    assert out["weekend_midpoint_h"] == pytest.approx(statistics.fmean(weekend), abs=0.01)
    # The planted shift, recovered end-to-end.
    assert out["delta_h"] == pytest.approx(1.2, abs=0.2)


def test_social_jet_lag_delta_is_absolute(svc, sample_dates):
    start, end = sample_dates
    out = svc.social_jet_lag(start, end)
    assert out["delta_h"] >= 0


def test_sleep_regularity_formula_on_a_controlled_series(tmp_path):
    """SRI proxy = 100 - std(midpoint hours over a 7-day window) * 25, clipped.

    Built by hand so the expected number comes from the documented formula,
    not from re-running the implementation's own arithmetic.
    """
    db = tmp_path / "sri.db"
    MemoryStore(types.SimpleNamespace(sqlite_db_path=str(db))).initialise_schema()
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sleep_summary (date TEXT PRIMARY KEY, time TEXT,"
        " sleep_time_seconds INTEGER)"
    )
    # A perfectly regular sleeper: wake at 07:00 every day after 8h.
    days = [datetime(2026, 6, 1) + timedelta(days=i) for i in range(10)]
    for d in days:
        wake = d.replace(hour=7)
        conn.execute("INSERT INTO sleep_summary VALUES (?,?,?)",
                     (d.date().isoformat(), wake.isoformat(), 8 * 3600))
    conn.commit()
    conn.close()

    out = LifestyleService(str(db)).sleep_regularity("2026-06-01", "2026-06-10")
    # Zero midpoint variance -> zero penalty -> a perfect 100.
    assert out["current"] == 100.0
    scored = [d["sri"] for d in out["series"] if d["sri"] is not None]
    assert scored and all(v == 100.0 for v in scored)
    # min_periods=3, so the first two days have no rolling std yet.
    assert [d["sri"] for d in out["series"][:2]] == [None, None]


def test_sleep_regularity_penalises_an_irregular_sleeper(tmp_path):
    db = tmp_path / "sri2.db"
    MemoryStore(types.SimpleNamespace(sqlite_db_path=str(db))).initialise_schema()
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sleep_summary (date TEXT PRIMARY KEY, time TEXT,"
        " sleep_time_seconds INTEGER)"
    )
    # Wake time alternates 05:00 / 09:00 -> a 2h midpoint swing.
    for i in range(10):
        d = datetime(2026, 6, 1) + timedelta(days=i)
        wake = d.replace(hour=5 if i % 2 == 0 else 9)
        conn.execute("INSERT INTO sleep_summary VALUES (?,?,?)",
                     (d.date().isoformat(), wake.isoformat(), 8 * 3600))
    conn.commit()
    conn.close()

    out = LifestyleService(str(db)).sleep_regularity("2026-06-01", "2026-06-10")
    assert out["current"] < 50, "a 2h midpoint swing should cost >50 points"


def test_sleep_regularity_advertises_it_is_a_proxy(svc, sample_dates):
    """The payload must not let the UI attach Tier-A SRI evidence to it."""
    out = svc.sleep_regularity(*sample_dates)
    assert out["method"] == "sleep_midpoint_consistency_proxy"
    assert "not the epoch-matching validated SRI" in out["note"]


# ==================================================================
# Steps / activity
# ==================================================================
def test_step_distribution_stats_match_the_source_rows(svc, sample_rows, sample_dates):
    steps = sorted((r["steps"] for r in sample_rows), reverse=True)
    n = len(steps)
    out = svc.step_distribution(*sample_dates)

    assert out["sorted_steps"] == steps
    assert out["median"] == int(statistics.median(steps))
    assert out["pct_over_7500"] == pytest.approx(
        round(100 * sum(s >= 7500 for s in steps) / n, 1))
    assert out["pct_over_10000"] == pytest.approx(
        round(100 * sum(s >= 10000 for s in steps) / n, 1))


def test_who_target_applies_the_2x_vigorous_equivalency(svc, sample_rows, sample_dates):
    """WHO counts a vigorous minute as two moderate ones; the target is 150
    moderate-equivalent minutes per week."""
    out = svc.who_intensity_target(*sample_dates)
    weeks = out["weeks"]
    assert weeks

    by_week = {w["week"]: w for w in weeks}
    for w in weeks:
        assert w["mod_equiv"] == w["moderate"] + 2 * w["vigorous"]
        assert w["target_pct"] == int(round(100 * w["mod_equiv"] / 150, 0))

    # Totals must account for every day in the window, none dropped.
    assert sum(w["moderate"] for w in weeks) == sum(
        int(r["steps"] / 400) for r in sample_rows)
    assert sum(w["vigorous"] for w in weeks) == sum(
        int(r["steps"] / 1800) for r in sample_rows)
    assert len(by_week) == len(weeks), "weeks must be unique"


# ==================================================================
# Recovery
# ==================================================================
def test_recovery_debt_accumulates_the_deficit_against_target(svc, sample_rows, sample_dates):
    out = svc.recovery_debt(*sample_dates, target=75)
    assert len(out) == len(sample_rows)

    running = 0.0
    for entry, row in zip(out, sample_rows):
        expected_deficit = 75 - row["bb_high"]
        running += expected_deficit
        assert entry["date"] == row["date"]
        assert entry["wake_battery"] == pytest.approx(row["bb_high"])
        assert entry["daily_deficit"] == pytest.approx(expected_deficit, abs=0.05)
        assert entry["cumulative_debt"] == pytest.approx(running, abs=0.05)


def test_recovery_debt_defaults_to_a_target_of_75(svc, sample_rows, sample_dates):
    """Pinned separately from the test above, which passes target explicitly and
    so would not notice the default drifting."""
    out = svc.recovery_debt(*sample_dates)
    assert out[0]["daily_deficit"] == pytest.approx(75 - sample_rows[0]["bb_high"], abs=0.05)


def test_recovery_debt_target_is_honoured(svc, sample_dates):
    lenient = svc.recovery_debt(*sample_dates, target=50)
    strict = svc.recovery_debt(*sample_dates, target=100)
    # A higher target means a larger deficit on every day.
    assert strict[-1]["cumulative_debt"] > lenient[-1]["cumulative_debt"]


def test_illness_radar_flags_the_planted_strain_window(svc, sample_rows, sample_dates):
    """The fixture plants 5 consecutive days of RHR-up / HRV-down /
    respiration-up. That is precisely the 3-axis signature the radar alerts on."""
    out = svc.illness_radar(*sample_dates)
    strain_dates = {r["date"] for r in sample_rows if r["strain"]}

    alert_dates = {a["date"] for a in out["alerts"]}
    assert alert_dates & strain_dates, (
        f"no alert raised inside the planted window; alerts were {sorted(alert_dates)}")
    # Not asserting alerts fall ONLY inside the window: random noise can put
    # three axes over z=1 on an ordinary day, and suppressing that would mean
    # asserting the detector is less sensitive than it is.

    by_date = {d["date"]: d for d in out["series"]}
    strain_composites = [by_date[d]["composite"] for d in strain_dates
                         if by_date.get(d, {}).get("composite") is not None]
    other_composites = [v["composite"] for d, v in by_date.items()
                        if d not in strain_dates and v["composite"] is not None]
    assert statistics.fmean(strain_composites) > statistics.fmean(other_composites) + 1.0


def test_illness_radar_alert_requires_all_three_axes(svc, sample_dates):
    """Documented rule: an alert fires only when RHR, inverted HRV AND
    respiration are all at z >= 1."""
    out = svc.illness_radar(*sample_dates)
    by_date = {d["date"]: d for d in out["series"]}
    for alert in out["alerts"]:
        entry = by_date[alert["date"]]
        assert entry["z_rhr"] >= 1
        assert entry["z_hrv_inv"] >= 1
        assert entry["z_resp"] >= 1


def test_inflammation_index_is_elevated_during_the_strain_window(svc, sample_rows, sample_dates):
    out = svc.inflammation_index(*sample_dates)
    strain_dates = {r["date"] for r in sample_rows if r["strain"]}
    scored = {d["date"]: d for d in out if d.get("index") is not None}

    strain = [v["index"] for d, v in scored.items() if d in strain_dates]
    rest = [v["index"] for d, v in scored.items() if d not in strain_dates]
    assert strain and rest
    assert statistics.fmean(strain) > statistics.fmean(rest)


def test_stress_resilience_scores_the_first_day_of_a_short_window(svc, sample_rows):
    """Regression for the _prime_start behaviour: a 7-day window must be scored
    on every day, using history fetched from before the window."""
    start = sample_rows[-7]["date"]
    end = sample_rows[-1]["date"]
    out = svc.stress_resilience(start, end)

    assert [d["date"] for d in out] == [r["date"] for r in sample_rows[-7:]]
    assert all(d["resilience"] is not None for d in out)
    assert all(0 <= d["resilience"] <= 100 for d in out)


def test_prime_start_reaches_back_far_enough_for_the_rolling_baseline():
    """The rolling window is 30 with min_periods=7, so the priming lookback
    must be at least 30 days for day one of a window to be scorable."""
    assert _prime_start("2026-07-31") == "2026-06-26"
    assert (datetime.fromisoformat("2026-07-31")
            - datetime.fromisoformat(_prime_start("2026-07-31"))).days >= 30


def test_prime_start_passes_through_a_malformed_date():
    assert _prime_start("not-a-date") == "not-a-date"


def test_body_battery_decay_computes_a_per_day_rate(svc, sample_dates):
    """The fixture drains body battery through waking hours, so every day
    should yield a positive decay rate bounded by its own peak/trough."""
    out = svc.body_battery_decay(*sample_dates)
    assert out
    for entry in out:
        assert entry["decay_per_hour"] is not None
        assert entry["peak"] >= entry["trough"]


# ==================================================================
# Behaviour analytics
# ==================================================================
def test_dose_response_pairs_each_dose_with_the_same_dates_night(svc, sample_rows, sample_dates):
    """Sleep is keyed to the WAKE date, so the night affected by a behaviour
    logged on date X is the sleep row recorded on X itself."""
    out = svc.behavior_dose_response(*sample_dates)
    alcohol = next(b for b in out["behaviors"] if b["behavior"] == "Alcohol")

    by_date = {r["date"]: r for r in sample_rows}
    assert alcohol["n"] == sum(1 for r in sample_rows if r["drinks"])
    for point in alcohol["points"]:
        row = by_date[point["date"]]
        assert point["value"] == float(row["drinks"])
        assert point["sleepScore"] == pytest.approx(row["sleep_score"])
        assert point["hrv"] == pytest.approx(row["hrv"])
        assert point["rhr"] == pytest.approx(row["rhr"])


def test_dose_response_drops_behaviours_with_too_few_numeric_logs(svc, sample_dates):
    """Documented gate: at least 5 numeric occurrences."""
    out = svc.behavior_dose_response(*sample_dates)
    assert all(b["n"] >= 5 for b in out["behaviors"])


def test_dose_response_shows_the_planted_alcohol_effect(svc, sample_dates):
    """Higher doses should track worse sleep — the fixture subtracts 7 points
    of sleep score per drink."""
    out = svc.behavior_dose_response(*sample_dates)
    alcohol = next(b for b in out["behaviors"] if b["behavior"] == "Alcohol")

    low = [p["sleepScore"] for p in alcohol["points"] if p["value"] <= 2]
    high = [p["sleepScore"] for p in alcohol["points"] if p["value"] >= 3]
    assert low and high
    assert statistics.fmean(high) < statistics.fmean(low)


def test_caffeine_groups_partition_the_window(svc, sample_rows, sample_dates):
    out = svc.caffeine_cutoff(*sample_dates)
    groups = {g["group"]: g for g in out["groups"]}
    assert set(groups) == {"Late caffeine", "Early-only", "No caffeine"}

    # Every day lands in exactly one group.
    assert sum(g["n"] for g in groups.values()) == len(sample_rows)
    assert groups["Late caffeine"]["n"] == sum(1 for r in sample_rows if r["caffeine_late"])
    # The fixture logs no early-only caffeine.
    assert groups["Early-only"]["n"] == 0


def test_caffeine_deltas_are_measured_against_the_no_caffeine_group(svc, sample_dates):
    out = svc.caffeine_cutoff(*sample_dates)
    groups = {g["group"]: g for g in out["groups"]}
    base = groups["No caffeine"]
    late = groups["Late caffeine"]

    assert base["sleep_score_delta_vs_none"] == 0
    assert late["sleep_score_delta_vs_none"] == pytest.approx(
        round(late["sleep_score"] - base["sleep_score"], 2))


def test_caffeine_sample_quality_reflects_n(svc, sample_dates):
    out = svc.caffeine_cutoff(*sample_dates)
    for g in out["groups"]:
        expected = "high" if g["n"] >= 14 else "medium" if g["n"] >= 7 else "low"
        assert g["sample_quality"] == expected


def test_habit_half_life_counts_and_recency(svc, sample_rows, sample_dates):
    _start, end = sample_dates
    out = svc.habit_half_life(end, lookback_days=90)
    by_behavior = {h["behavior"]: h for h in out}

    d_end = datetime.fromisoformat(end).date()
    window_start = (d_end - timedelta(days=90)).isoformat()
    alcohol_days = [r["date"] for r in sample_rows
                    if r["drinks"] and r["date"] >= window_start]

    alcohol = by_behavior["Alcohol"]
    assert alcohol["frequency_90d"] == len(alcohol_days)
    assert alcohol["last_logged"] == max(alcohol_days)
    assert alcohol["days_since"] == (
        d_end - datetime.fromisoformat(max(alcohol_days)).date()).days
    # Sorted most-recent first.
    assert [h["days_since"] for h in out] == sorted(h["days_since"] for h in out)


def test_cooccurrence_diagonal_is_the_behaviours_own_day_count(svc, sample_dates):
    out = svc.behavior_cooccurrence(*sample_dates)
    behaviors, matrix = out["behaviors"], out["matrix"]
    assert behaviors

    for i, _b in enumerate(behaviors):
        # A behaviour always co-occurs with itself on each day it is logged.
        assert matrix[i][i] == max(matrix[i])
    # Co-occurrence is symmetric.
    for i in range(len(behaviors)):
        for j in range(len(behaviors)):
            assert matrix[i][j] == matrix[j][i]


def test_streak_calendar_cells_align_with_the_date_axis(svc, sample_rows, sample_dates):
    out = svc.behavior_streak_calendar(*sample_dates)
    dates = out["dates"]
    assert dates == [r["date"] for r in sample_rows]

    alcohol = next(b for b in out["behaviors"] if b["behavior"] == "Alcohol")
    assert len(alcohol["cells"]) == len(dates)
    by_date = {r["date"]: r for r in sample_rows}
    for date, cell in zip(dates, alcohol["cells"]):
        drinks = by_date[date]["drinks"]
        assert cell == (pytest.approx(float(drinks)) if drinks else None)


def test_stress_trigger_leaderboard_thresholds_on_the_top_quintile(svc, sample_dates):
    out = svc.stress_trigger_leaderboard(*sample_dates)
    assert out["top_quintile_threshold"] is not None

    for trigger in out["triggers"]:
        assert trigger["behavior"]
        # lift is the frequency gap between high-stress and normal days.
        assert trigger["lift"] == pytest.approx(
            round(trigger["high_stress_freq"] - trigger["normal_stress_freq"], 3))
        assert trigger["odds_ratio"] > 0
        total = trigger["count_on_high"] + trigger["count_on_low"]
        expected = "high" if total >= 20 else "medium" if total >= 10 else "low"
        assert trigger["sample_quality"] == expected


# ==================================================================
# Slow-moving markers
# ==================================================================
def test_fitness_age_delta_returns_one_row_per_reporting_day(svc, sample_rows, sample_dates):
    """Garmin emits these irregularly; the fixture writes every 10th day."""
    out = svc.fitness_age_delta(*sample_dates)
    expected_days = len([i for i in range(len(sample_rows)) if i % 10 == 0])
    assert len(out) == expected_days
    assert all("fitness_age" in row for row in out)
    # Fitness age improves across the window in the fixture.
    assert out[-1]["fitness_age"] < out[0]["fitness_age"]


# ==================================================================
# Load cache
# ==================================================================
def test_load_cache_serves_repeat_reads_without_rereading(svc, sample_dates, sample_db):
    start, end = sample_dates
    first = svc._load_summaries(start, end)

    # Mutate the DB behind the service; the TTL cache should still serve the
    # original snapshot for this window.
    conn = sqlite3.connect(sample_db)
    conn.execute("DELETE FROM daily_summaries")
    conn.commit()
    conn.close()

    assert len(svc._load_summaries(start, end)) == len(first)


def test_load_cache_hands_out_copies_not_shared_frames(svc, sample_dates):
    """Callers mutate these frames in place (set_index, column assignment), so
    a shared object would corrupt every later reader."""
    start, end = sample_dates
    a = svc._load_summaries(start, end)
    a["injected"] = 1
    b = svc._load_summaries(start, end)
    assert "injected" not in b.columns


def test_prewarm_populates_all_three_windows(svc, sample_dates):
    start, end = sample_dates
    svc._load_cache.clear()
    svc.prewarm(start, end)
    assert ("summaries", start, end) in svc._load_cache
    assert ("summaries", _prime_start(start), end) in svc._load_cache
    assert ("journal", start, end) in svc._load_cache


def test_empty_window_returns_empty_not_an_error(svc):
    """A range with no data must degrade cleanly — the dashboard renders these
    directly."""
    far_future = ("2099-01-01", "2099-01-31")
    assert svc.behavior_dose_response(*far_future) == {"behaviors": []}
    assert svc.recovery_debt(*far_future) == []
    assert svc.step_distribution(*far_future)["median"] is None
    assert svc.social_jet_lag(*far_future)["delta_h"] is None
    assert svc.behavior_cooccurrence(*far_future) == {"behaviors": [], "matrix": []}
    assert svc.illness_radar(*far_future) == {"series": [], "alerts": []}
