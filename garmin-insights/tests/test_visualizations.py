"""VisualizationService chart builders.

Each method backs a dashboard chart, so the contract that matters is: correct
numbers when there is data, and a clean ``available: false`` (never an
exception, never a half-built payload) when there isn't — several of these
read optional tables that simply don't exist on many installs.

sleep_timeline / fitness_trajectory / behavior_impact are covered in
test_fixtures_smoke.py and test_fitness_markers.py; this file covers the rest.
"""

from __future__ import annotations

import sqlite3

import pytest

from garmin_insights.web.visualizations import VisualizationService


@pytest.fixture
def viz(sample_db):
    return VisualizationService(sample_db)


@pytest.fixture
def empty_viz(tmp_path):
    """A fresh install: every table exists (the fetcher creates them on first
    run) but nothing has been fetched yet. This is the state the
    ``available: false`` paths are written for."""
    import types

    from conftest import _RAW_SCHEMA
    from garmin_insights.db.memory import MemoryStore

    db = tmp_path / "bare.db"
    MemoryStore(types.SimpleNamespace(sqlite_db_path=str(db))).initialise_schema()
    conn = sqlite3.connect(db)
    conn.executescript(_RAW_SCHEMA)
    conn.commit()
    conn.close()
    return VisualizationService(str(db))


# ==================================================================
# Intraday heatmap
# ==================================================================
@pytest.mark.parametrize("metric", ["stress", "body_battery", "heart_rate", "steps"])
def test_heatmap_builds_a_dense_24h_matrix(viz, metric):
    out = viz.intraday_heatmap(metric, days=7)
    assert out["metric"] == metric
    assert out["hours"] == list(range(24))
    assert len(out["dates"]) == 7
    assert all(len(row) == 24 for row in out["matrix"])


def test_heatmap_matrix_rows_align_with_its_dates(viz):
    out = viz.intraday_heatmap("stress", days=5)
    assert len(out["matrix"]) == len(out["dates"])


def test_heatmap_reflects_the_planted_day_night_shape(viz):
    """The fixture holds stress high while awake (07-22) and low overnight."""
    out = viz.intraday_heatmap("stress", days=5)
    row = out["matrix"][-1]
    daytime = [row[h] for h in range(9, 20) if row[h] is not None]
    night = [row[h] for h in (1, 2, 3, 4) if row[h] is not None]
    assert daytime and night
    assert min(daytime) > max(night)


def test_heatmap_unknown_metric_returns_an_error_payload(viz):
    out = viz.intraday_heatmap("bogus", days=7)
    assert "unknown metric" in out["error"]
    assert "matrix" not in out


def test_heatmap_on_an_empty_db_returns_an_empty_matrix(empty_viz):
    out = empty_viz.intraday_heatmap("stress", days=7)
    assert out["dates"] == [] and out["matrix"] == []
    assert out["hours"] == list(range(24)), "the hour axis is still needed to render"


# ==================================================================
# Training / ACWR
# ==================================================================
def test_training_returns_one_entry_per_day(viz, sample_rows, sample_dates):
    out = viz.training(*sample_dates)
    assert len(out["training_status"]) == len(sample_rows)
    assert len(out["training_readiness"]) == len(sample_rows)


def test_training_keeps_only_the_latest_row_per_day(viz, sample_rows, sample_dates):
    """Garmin can emit several training-status rows a day; the chart plots one."""
    out = viz.training(*sample_dates)
    dates = [e["date"] for e in out["training_status"]]
    assert len(dates) == len(set(dates))


def test_training_carries_heat_acclimation(viz, sample_dates):
    """Acclimation rides in the same training-status payload — no extra fetch,
    and the dashboard's Heat Acclimation chart depends on it."""
    out = viz.training(*sample_dates)
    assert any(e.get("heat_acclimation") is not None for e in out["training_status"])


def test_training_on_an_empty_db_is_empty_not_an_error(empty_viz, sample_dates):
    out = empty_viz.training(*sample_dates)
    assert out["training_status"] == []
    assert out["training_readiness"] == []


# ==================================================================
# Body composition
# ==================================================================
def test_body_composition_normalises_masses_to_kg(viz, sample_rows, sample_dates):
    """Garmin reports grams; the chart axis is kg."""
    out = viz.body_composition(*sample_dates)
    assert out
    weights = [e["weight"] for e in out if e.get("weight") is not None]
    assert weights
    assert 40 < min(weights) < 200, f"looks unconverted: {min(weights)}"


def test_body_composition_falls_back_to_a_year_when_the_window_is_empty(viz, sample_rows):
    """Weigh-ins are irregular; a blank card for a scale-using user is worse
    than showing an older, honestly-dated trend. The fallback looks back a
    year from `end`, so it only rescues windows near the present."""
    from datetime import datetime, timedelta

    future = datetime.now() + timedelta(days=3)
    start = future.strftime("%Y-%m-%d")
    end = (future + timedelta(days=3)).strftime("%Y-%m-%d")

    assert viz.body_composition(start, end), "should fall back rather than return nothing"


def test_body_composition_fallback_keeps_honest_dates(viz, sample_rows):
    """The fallback shows older readings; their real dates must be preserved
    so the chart doesn't imply they are recent."""
    from datetime import datetime, timedelta

    future = datetime.now() + timedelta(days=3)
    out = viz.body_composition(future.strftime("%Y-%m-%d"),
                               (future + timedelta(days=3)).strftime("%Y-%m-%d"))
    known_dates = {r["date"] for r in sample_rows}
    assert all(e["date"] in known_dates for e in out)


def test_body_composition_empty_db_returns_empty_list(empty_viz, sample_dates):
    assert empty_viz.body_composition(*sample_dates) == []


# ==================================================================
# HR zones
# ==================================================================
def test_hr_zones_aggregates_five_zones_per_activity_type(viz, sample_dates):
    out = viz.hr_zones(*sample_dates)
    assert out["by_type"]
    types_seen = {row["activity_type"] for row in out["by_type"]}
    assert {"running", "cycling"} <= types_seen
    for row in out["by_type"]:
        assert all(f"z{i}" in row for i in range(1, 6))


def test_hr_zones_are_converted_to_minutes(viz, sample_dates):
    """The fixture logs 300s in zone 1; the chart axis is minutes."""
    out = viz.hr_zones(*sample_dates)
    running = next(r for r in out["by_type"] if r["activity_type"] == "running")
    assert running["z1"] == pytest.approx(300 / 60 * running["activity_count"], rel=0.01)


def test_hr_zones_empty_db(empty_viz, sample_dates):
    assert empty_viz.hr_zones(*sample_dates)["by_type"] == []


# ==================================================================
# Correlations / anomaly calendar
# ==================================================================
def test_correlation_matrix_is_square_and_symmetric(viz, sample_dates):
    out = viz.correlations(*sample_dates)
    keys, matrix = out["keys"], out["matrix"]
    assert keys and len(matrix) == len(keys)
    assert all(len(row) == len(keys) for row in matrix)
    for i in range(len(keys)):
        for j in range(len(keys)):
            assert matrix[i][j] == matrix[j][i]


def test_correlation_matrix_diagonal_is_self_correlation(viz, sample_dates):
    out = viz.correlations(*sample_dates)
    for i in range(len(out["keys"])):
        value = out["matrix"][i][i]
        assert value is None or value == pytest.approx(1.0)


def test_correlation_values_are_in_range(viz, sample_dates):
    out = viz.correlations(*sample_dates)
    for row in out["matrix"]:
        for value in row:
            assert value is None or -1.0 <= value <= 1.0


def test_anomaly_calendar_matrix_aligns_with_its_axes(viz, sample_rows, sample_dates):
    out = viz.anomaly_calendar(*sample_dates)
    assert out["dates"] and out["keys"]
    assert len(out["matrix"]) == len(out["keys"])
    assert all(len(row) == len(out["dates"]) for row in out["matrix"])
    assert set(out["dates"]) <= {r["date"] for r in sample_rows}


def test_anomaly_calendar_scores_the_planted_strain_window_highest(viz, sample_rows,
                                                                  sample_dates):
    """RHR is elevated for 5 planted days; those columns should carry the
    largest positive z-scores in the RHR row."""
    out = viz.anomaly_calendar(*sample_dates)
    rhr_row = out["matrix"][out["keys"].index("restingHeartRate")]
    by_date = dict(zip(out["dates"], rhr_row))
    strain_dates = {r["date"] for r in sample_rows if r["strain"]}

    strain_z = [by_date[d] for d in strain_dates if by_date.get(d) is not None]
    other_z = [v for d, v in by_date.items() if d not in strain_dates and v is not None]
    assert strain_z and other_z
    assert sum(strain_z) / len(strain_z) > sum(other_z) / len(other_z)


# ==================================================================
# Environment ↔ recovery
# ==================================================================
def test_environment_recovery_uses_next_day_lag_for_pollen(viz, sample_dates):
    """Buekers 2023: allergy burden shows up in NEXT-day RHR. Same-day for
    heat / AQ / PM2.5 per Niu 2020."""
    from garmin_insights.stats_utils import NEXT_DAY_LAG_METRICS

    out = viz.environment_recovery(*sample_dates)
    assert out.get("available") is not False
    assert out["entries"]
    assert NEXT_DAY_LAG_METRICS, "the shared lag policy must be non-empty"

    by_driver = {(c["driver"], c["marker"]): c for c in out["correlations"]}
    pollen = [c for (d, _m), c in by_driver.items() if "pollen" in d]
    same_day = [c for (d, _m), c in by_driver.items()
                if d in ("apparent_temp_max_c", "european_aqi", "pm25")]
    assert pollen and same_day
    assert all(c["lag"] == "next-day" for c in pollen), "pollen must use next-day lag"
    assert all(c["lag"] == "same-day" for c in same_day), "heat / AQ / PM2.5 are same-day"
    # The driver name carries the lag too, so the UI label can't disagree.
    assert all("lag1" in d for (d, _m) in by_driver if "pollen" in d)


def test_environment_recovery_correlations_are_fdr_corrected(viz, sample_dates):
    out = viz.environment_recovery(*sample_dates)
    for pair in out["correlations"]:
        assert "n" in pair and "significant" in pair
        assert pair["p"] is None or 0.0 <= pair["p"] <= 1.0


def test_environment_recovery_unavailable_without_environment_data(empty_viz, sample_dates):
    out = empty_viz.environment_recovery(*sample_dates)
    assert out["available"] is False


# ==================================================================
# Behaviour × environment
# ==================================================================
def test_behavior_environment_impact_splits_on_and_off_days(viz, sample_dates):
    out = viz.behavior_environment_impact(
        "Alcohol", ["european_aqi", "pm25"], *sample_dates)
    assert out.get("available") is not False
    assert out["entries"]
    assert out["n_logged"] > 0


def test_behavior_environment_impact_unknown_behavior_reports_zero_logged(viz, sample_dates):
    """The UI hides the panel on n_logged == 0 — it must say so, not error."""
    out = viz.behavior_environment_impact(
        "Never Logged Behaviour", ["european_aqi"], *sample_dates)
    assert out["n_logged"] == 0


def test_behavior_environment_impact_rejects_a_bad_column(viz, sample_dates):
    """env_columns is interpolated into the SQL, so a bad name must fail
    closed rather than raise out of the endpoint."""
    out = viz.behavior_environment_impact(
        "Alcohol", ["no_such_column"], *sample_dates)
    assert out["available"] is False


def test_behavior_environment_impact_unavailable_on_empty_db(empty_viz, sample_dates):
    out = empty_viz.behavior_environment_impact("Alcohol", ["pm25"], *sample_dates)
    assert out["available"] is False


# ==================================================================
# Bedroom temperature × sleep
# ==================================================================
def test_bedroom_temp_sleep_joins_ha_data_to_recovery(viz, sample_dates):
    out = viz.bedroom_temp_sleep(*sample_dates)
    assert out.get("available") is not False
    assert out["entries"]
    assert any(e.get("bedroom_overnight_c") is not None for e in out["entries"])


def test_bedroom_temp_sleep_unavailable_without_an_ha_entity(empty_viz, sample_dates):
    """Most installs have no Home Assistant — that is a clean 'unavailable',
    not an error."""
    out = empty_viz.bedroom_temp_sleep(*sample_dates)
    assert out["available"] is False
    assert out["entries"] == []


# ==================================================================
# Behaviour root cause
# ==================================================================
def test_behavior_root_cause_returns_one_entry_per_logged_day(viz, sample_rows, sample_dates):
    out = viz.behavior_root_cause("Alcohol", *sample_dates)
    assert out["available"] is True
    logged = [r for r in sample_rows if r["drinks"]]
    assert len(out["events"]) == len(logged)


def test_behavior_root_cause_surfaces_prior_window_confounders(viz, sample_dates):
    out = viz.behavior_root_cause("Alcohol", *sample_dates, lookback_hours=48)
    event = out["events"][-1]
    # Each event carries the surrounding context the panel ranks causes from.
    assert {"date", "prior_behaviors", "same_day_behaviors", "env",
            "recovery_today", "recovery_prev_day"} <= set(event)


def test_behavior_root_cause_unlogged_behavior_is_available_but_empty(viz, sample_dates):
    """'Available, nothing logged' and 'not available' are different states —
    the panel renders them differently."""
    out = viz.behavior_root_cause("Never Logged Behaviour", *sample_dates)
    assert out["available"] is True
    assert out["events"] == []
