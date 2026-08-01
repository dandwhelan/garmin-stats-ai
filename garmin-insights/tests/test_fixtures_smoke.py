"""Smoke tests for the shared ``sample_db`` / ``api_client`` fixtures.

These are deliberately shallow: they prove the scaffolding loads, is wired to
the right database, and produces non-degenerate data — so that the real
LifestyleService / VisualizationService / endpoint test suites built on top of
it can trust their inputs. They are not a substitute for those suites.
"""

from __future__ import annotations

import sqlite3

import pytest


# ------------------------------------------------------------------
# sample_db shape
# ------------------------------------------------------------------
def test_sample_db_has_expected_tables(sample_db):
    conn = sqlite3.connect(sample_db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    conn.close()
    # Raw fetcher-side tables plus the insights-side cache tables.
    assert {"daily_stats", "sleep_summary", "lifestyle_journal",
            "environment_daily", "daily_summaries", "baselines"} <= names


def test_sample_db_row_counts_match_generated_rows(sample_db, sample_rows):
    conn = sqlite3.connect(sample_db)
    for table in ("daily_stats", "sleep_summary", "environment_daily", "daily_summaries"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert n == len(sample_rows), f"{table} has {n} rows, expected {len(sample_rows)}"
    conn.close()


def test_sample_db_is_isolated_per_test(sample_db):
    """Each test gets its own copy — writes here must not leak to other tests."""
    conn = sqlite3.connect(sample_db)
    conn.execute("DELETE FROM daily_summaries")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()[0] == 0
    conn.close()


def test_sample_db_isolation_holds_after_previous_test_deleted_rows(sample_db, sample_rows):
    conn = sqlite3.connect(sample_db)
    n = conn.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()[0]
    conn.close()
    assert n == len(sample_rows)


def test_planted_strain_window_is_present(sample_rows):
    """The illness-like window is what the recovery analytics key on."""
    strain = [r for r in sample_rows if r["strain"]]
    baseline = [r for r in sample_rows if not r["strain"]]
    assert len(strain) == 5
    mean = lambda rows, k: sum(r[k] for r in rows) / len(rows)  # noqa: E731
    assert mean(strain, "rhr") > mean(baseline, "rhr") + 2
    assert mean(strain, "hrv") < mean(baseline, "hrv") - 5
    assert mean(strain, "resp") > mean(baseline, "resp") + 0.5


def test_alcohol_is_logged_with_a_numeric_dose(sample_db):
    """behavior_dose_response only picks up behaviours carrying a value."""
    conn = sqlite3.connect(sample_db)
    rows = conn.execute(
        "SELECT value FROM lifestyle_journal WHERE behavior='Alcohol'"
    ).fetchall()
    conn.close()
    assert len(rows) >= 15
    assert {r[0] for r in rows} > {1.0}, "doses should vary, not be constant"


# ------------------------------------------------------------------
# Service layer reads it
# ------------------------------------------------------------------
def test_lifestyle_service_reads_sample_db(sample_db, sample_dates):
    from garmin_insights.web.lifestyle_viz import LifestyleService

    start, end = sample_dates
    svc = LifestyleService(sample_db)
    df = svc._load_summaries(start, end)
    assert not df.empty
    assert {"restingHeartRate", "avgOvernightHrv", "sleepScore"} <= set(df.columns)


def test_illness_radar_spans_window_not_just_final_day(sample_db, sample_rows):
    """Guards the _prime_start behaviour: a short display window must still get
    z-scores on every day, because the rolling baseline is primed with history
    fetched from *before* the window."""
    from garmin_insights.web.lifestyle_viz import LifestyleService

    # A 7-day window — the case that used to collapse to a single point.
    start = sample_rows[-7]["date"]
    end = sample_rows[-1]["date"]
    out = LifestyleService(sample_db).illness_radar(start, end)

    assert len(out["series"]) == 7
    scored = [d for d in out["series"] if d["composite"] is not None]
    assert len(scored) == 7, "every day in the window should carry a z-score"


def test_analytics_survive_a_window_with_no_device_columns(tmp_path):
    """Regression: analytics must degrade to "no data", not raise, when the
    window's summaries carry none of the metric keys they read.

    ``DataFrame.get`` returns None for an absent column and
    ``pd.to_numeric(None)`` is a scalar, so the downstream .fillna/.dropna
    raised AttributeError. The API layer catches that and turns the whole
    analytic into {"error": ...}, which the dashboard renders as a silently
    empty chart — so nothing surfaced the breakage.
    """
    import json
    import sqlite3
    import types

    from garmin_insights.db.memory import MemoryStore
    from garmin_insights.web.lifestyle_viz import LifestyleService

    db = tmp_path / "sparse.db"
    MemoryStore(types.SimpleNamespace(sqlite_db_path=str(db))).initialise_schema()
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS lifestyle_journal (date TEXT, behavior TEXT,"
        " category TEXT, status INTEGER, value REAL, device TEXT,"
        " PRIMARY KEY (date, behavior));"
    )
    for day in ("2026-07-01", "2026-07-02", "2026-07-03"):
        conn.execute(
            "INSERT INTO daily_summaries (date, metric_json, computed_at)"
            " VALUES (?,?,datetime('now'))",
            (day, json.dumps({"restingHeartRate": 55.0})),
        )
    conn.commit()
    conn.close()

    svc = LifestyleService(str(db))
    start, end = "2026-07-01", "2026-07-03"
    # Each of these reads a column the rows above do not have.
    assert svc.recovery_debt(start, end) == [
        {"date": d, "wake_battery": None, "daily_deficit": 0.0,
         "cumulative_debt": 0.0}
        for d in ("2026-07-01", "2026-07-02", "2026-07-03")
    ]
    assert svc.step_distribution(start, end)["sorted_steps"] == []
    assert svc.who_intensity_target(start, end)["weeks"] != []  # zero-filled
    assert svc.stress_trigger_leaderboard(start, end)["triggers"] == []
    # Per-day rows with a null score, rather than an exception.
    assert [d["resilience"] for d in svc.stress_resilience(start, end)] == [None] * 3


def test_visualization_service_reads_sample_db(sample_db, sample_dates):
    from garmin_insights.web.visualizations import VisualizationService

    start, end = sample_dates
    out = VisualizationService(sample_db).sleep_timeline(start, end)
    assert isinstance(out, list) and out


# ------------------------------------------------------------------
# api_client
# ------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/api/dashboard",
    "/api/visualizations",
    "/api/lifestyle",
    "/api/environment",
])
def test_data_endpoints_return_200(api_client, sample_dates, path):
    start, end = sample_dates
    r = api_client.get(path, params={"user": "default", "start": start, "end": end})
    assert r.status_code == 200, r.text
    assert r.json()


def test_unknown_user_is_rejected(api_client):
    r = api_client.get("/api/dashboard", params={"user": "nobody"})
    assert r.status_code == 404


def test_cycle_data_reaches_a_female_user(api_client, sample_dates):
    start, end = sample_dates
    body = api_client.get(
        "/api/lifestyle", params={"user": "default", "start": start, "end": end}
    ).json()
    assert body["cycle_hrv"].get("available") is not False, body["cycle_hrv"]

    mens = api_client.get(
        "/api/menstrual", params={"user": "default", "start": start, "end": end}
    ).json()
    assert mens["tracked"] is True
    assert mens["entries"]


def test_cycle_data_is_withheld_from_a_male_user(sample_settings, api_client, sample_dates):
    """The gate is server-side, so it must hold even though this user's DB
    *does* contain cycle rows — which is exactly the misconfiguration the
    gate exists to contain."""
    sample_settings.biological_sex = "Male"
    start, end = sample_dates

    body = api_client.get(
        "/api/lifestyle", params={"user": "default", "start": start, "end": end}
    ).json()
    assert body["cycle_hrv"]["available"] is False
    assert body["cycle_yearly"]["available"] is False

    mens = api_client.get(
        "/api/menstrual", params={"user": "default", "start": start, "end": end}
    ).json()
    assert mens["tracked"] is False
    assert mens["entries"] == []


def test_null_menstrual_flow_does_not_500_the_dashboard(api_client, sample_dates):
    """Regression: menstrual_flow is NULL on every non-period day and pandas
    surfaces it as float NaN. NaN is truthy, so `(v or "").upper()` let it
    through and raised AttributeError, 500-ing /api/dashboard outright."""
    from garmin_insights.web.app import _cycle_text

    assert _cycle_text(float("nan")) == ""
    assert _cycle_text(None) == ""
    assert _cycle_text("medium") == "MEDIUM"

    start, end = sample_dates
    r = api_client.get(
        "/api/dashboard", params={"user": "default", "start": start, "end": end}
    )
    assert r.status_code == 200, r.text
    summaries = r.json()["summaries"]
    # Flow is only set on the 5 menstrual days per 28-day cycle; the rest are
    # NULL in the DB and must land as 0, not blow up.
    intensities = {s.get("cycleFlowIntensity") for s in summaries}
    assert intensities == {0, 1, 2}, intensities


def test_lifestyle_returns_every_documented_key(api_client, sample_dates):
    """The endpoint wraps each analytic in a catch-all that degrades failures to
    {"error": ...}. Assert the full key set is present so a silently-broken
    analytic is visible, and surface any that errored."""
    start, end = sample_dates
    body = api_client.get(
        "/api/lifestyle", params={"user": "default", "start": start, "end": end}
    ).json()

    expected = {
        "dose_response", "caffeine_cutoff", "sleep_regularity", "social_jet_lag",
        "recovery_cost", "stress_resilience", "body_battery_decay", "illness_radar",
        "inflammation_index", "recovery_debt", "streak_calendar", "habit_half_life",
        "cooccurrence", "step_distribution", "fitness_age_delta", "who_target",
        "cycle_hrv", "cycle_yearly", "stress_hour_fingerprint", "stress_triggers",
        "research_scorecard",
    }
    assert expected <= set(body)

    errored = {
        k: v["error"] for k, v in body.items()
        if isinstance(v, dict) and "error" in v
    }
    assert not errored, f"analytics raised: {errored}"
