"""End-to-end tests for the new fitness-marker surfacing (repo → tool → viz)."""

from __future__ import annotations

import json

from garmin_insights.db.sqlite_repo import SqliteRepo
from garmin_insights.tools.query_tools import QueryToolHandler
from garmin_insights.web.visualizations import VisualizationService


def test_repo_queries_new_tables(fake_settings):
    repo = SqliteRepo(fake_settings)
    assert not repo.query_race_predictions("2026-05-01", "2026-07-01").empty
    assert not repo.query_hill_score("2026-05-01", "2026-07-01").empty
    assert not repo.query_endurance_score("2026-05-01", "2026-07-01").empty
    assert not repo.query_vo2_max("2026-05-01", "2026-07-01").empty


def test_get_fitness_markers_tool(fake_settings):
    repo = SqliteRepo(fake_settings)
    handler = QueryToolHandler(repo, memory=None, analysis=None)
    out = json.loads(handler.get_fitness_markers("2026-05-01", "2026-07-01"))

    assert "vo2_max_running" in out
    assert out["vo2_max_running"]["2026-06-20"] == 50.0
    assert "vo2_max_cycling" in out

    # Race predictions formatted to human-readable times.
    assert "race_predictions" in out
    assert out["race_predictions"]["2026-06-01"]["5k"] == "25:00"
    assert out["race_predictions"]["2026-06-20"]["marathon"] == "3:55:00"

    # Fitness-age latest snapshot carries chronological + achievable context.
    assert out["fitness_age_latest"]["fitness_age"] == 34.0
    assert out["fitness_age_latest"]["chronological_age"] == 40.0

    assert out["endurance_score"]["2026-06-20"] == 7400
    assert out["hill_score"]["overall"]["2026-06-20"] == 58


def test_get_fitness_markers_empty_range(fake_settings):
    repo = SqliteRepo(fake_settings)
    handler = QueryToolHandler(repo, memory=None, analysis=None)
    out = json.loads(handler.get_fitness_markers("2020-01-01", "2020-02-01"))
    assert "message" in out


def test_get_fitness_markers_defaults_no_args(fake_settings):
    """Both dates optional — should not raise and should return a dict."""
    repo = SqliteRepo(fake_settings)
    handler = QueryToolHandler(repo, memory=None, analysis=None)
    out = json.loads(handler.get_fitness_markers())
    assert isinstance(out, dict)


def test_fitness_trajectory_viz(fake_settings):
    viz = VisualizationService(fake_settings.sqlite_db_path)
    out = viz.fitness_trajectory("2026-06-15", "2026-06-28")
    assert out["available"] is True
    # VO2 max series present and date-labelled.
    assert any(r.get("running") == 50.0 for r in out["vo2_max"])
    # Race predictions surfaced in minutes for the chart axis.
    assert any(r.get("5k") for r in out["race_predictions"])
    # Hill latest snapshot.
    assert out["hill_latest"]["overall"] == 58


def test_fitness_trajectory_unavailable(tmp_path):
    """A DB with no fitness tables returns available: False, not an exception."""
    import sqlite3

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    viz = VisualizationService(str(empty))
    out = viz.fitness_trajectory("2026-06-01", "2026-06-28")
    assert out["available"] is False
