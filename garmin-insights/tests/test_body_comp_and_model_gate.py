"""Regression tests: body-composition plumbing into daily summaries,
the shared to_kg / metabolic_age_years unit guards, and the
adaptive-thinking model allowlist."""

import sqlite3

import pytest

from garmin_insights.agent import _model_supports_adaptive
from garmin_insights.config import Settings
from garmin_insights.db.cache import CacheBuilder, _BASELINE_METRICS
from garmin_insights.db.memory import MemoryStore
from garmin_insights.db.sqlite_repo import SqliteRepo
from garmin_insights.stats_utils import metabolic_age_years, to_kg


# ---------------------------------------------------------------------------
# to_kg
# ---------------------------------------------------------------------------

def test_to_kg_converts_grams():
    assert to_kg(72080) == 72.08


def test_to_kg_idempotent_on_kg():
    assert to_kg(72.08) == 72.08


def test_to_kg_none_passthrough():
    assert to_kg(None) is None


# ---------------------------------------------------------------------------
# metabolic_age_years — Garmin returns a ms duration, not plain years
# ---------------------------------------------------------------------------

def test_metabolic_age_converts_ms_duration():
    # Observed live value for a 23-year metabolic age (dan.db, 2026-07-10)
    assert metabolic_age_years(725_809_298_000) == 23.0


def test_metabolic_age_idempotent_on_years():
    assert metabolic_age_years(32.0) == 32.0
    assert metabolic_age_years(24.5) == 24.5


def test_metabolic_age_none_passthrough():
    assert metabolic_age_years(None) is None


# ---------------------------------------------------------------------------
# Adaptive-thinking model gate (allowlist, not denylist)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("claude-sonnet-5", True),
    ("claude-fable-5", True),
    ("claude-mythos-5", True),
    ("claude-opus-4-8", True),
    ("claude-opus-4-6", True),
    ("claude-sonnet-4-6", True),
    ("claude-haiku-5", True),
    # Legacy models reject adaptive + effort — must get enabled+budget
    ("claude-sonnet-4-5", False),
    ("claude-sonnet-4-0", False),
    ("claude-haiku-4-5-20251001", False),
    ("claude-opus-4-5", False),
    ("claude-3-5-sonnet-20241022", False),
])
def test_model_supports_adaptive(model, expected):
    assert _model_supports_adaptive(model) is expected


# ---------------------------------------------------------------------------
# Body composition → daily summaries + baselines
# ---------------------------------------------------------------------------

@pytest.fixture
def db_with_weigh_in(tmp_path):
    db = str(tmp_path / "garmin.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE body_composition ("
        "time TEXT, device TEXT, weight REAL, bmi REAL, body_fat REAL, "
        "body_water REAL, bone_mass REAL, muscle_mass REAL, "
        "physique_rating REAL, visceral_fat REAL, metabolic_age REAL, "
        "PRIMARY KEY (time, device))"
    )
    # Two readings on the day — the later one must win. The winner's
    # metabolic_age is a raw ms duration, as rows fetched before the
    # fetcher-side conversion landed (and as Garmin returns it).
    conn.execute(
        "INSERT INTO body_composition VALUES "
        "('2026-07-10T06:00:00','scale',72500,21.2,17.0,57.0,3050,57000,NULL,9,33)"
    )
    conn.execute(
        "INSERT INTO body_composition VALUES "
        "('2026-07-10T07:01:00','scale',72080,21.1,16.6,57.2,3060,57070,NULL,9,"
        "725809298000)"
    )
    conn.commit()
    conn.close()
    return db


def test_body_comp_merged_into_daily_summary(db_with_weigh_in):
    settings = Settings(sqlite_db_path=db_with_weigh_in, anthropic_api_key="test")
    repo = SqliteRepo(settings)
    memory = MemoryStore(settings)
    memory.initialise_schema()
    cache = CacheBuilder(repo, memory)

    summary = cache.build_daily_summary("2026-07-10", is_complete=False)

    assert summary["weight_kg"] == 72.08  # latest reading of the day, in kg
    assert summary["bmi"] == 21.1
    assert summary["body_fat_pct"] == 16.6
    assert summary["body_water_pct"] == 57.2
    assert summary["muscle_mass_kg"] == 57.07
    assert summary["bone_mass_kg"] == 3.06
    assert summary["visceral_fat"] == 9.0
    assert summary["metabolic_age"] == 23.0  # normalised from ms duration


def test_body_comp_metrics_are_baselined():
    # detect_metric_trend / find_anomalies only see baselined summary metrics
    for metric in ("weight_kg", "body_fat_pct", "visceral_fat"):
        assert metric in _BASELINE_METRICS


# ---------------------------------------------------------------------------
# Local-day keying — a post-midnight BST weigh-in belongs to the NEXT local day
# ---------------------------------------------------------------------------

def test_body_comp_local_day_keying(tmp_path):
    import os
    import time as _time

    db = str(tmp_path / "garmin.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE body_composition ("
        "time TEXT, device TEXT, weight REAL, bmi REAL, body_fat REAL, "
        "body_water REAL, bone_mass REAL, muscle_mass REAL, "
        "physique_rating REAL, visceral_fat REAL, metabolic_age REAL, "
        "PRIMARY KEY (time, device))"
    )
    # 23:30 UTC on Jul 10 = 00:30 BST on Jul 11 — a just-after-midnight weigh-in
    conn.execute(
        "INSERT INTO body_composition (time, device, weight) VALUES "
        "('2026-07-10T23:30:00+00:00','scale',71500)"
    )
    conn.commit()
    conn.close()

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    _time.tzset()
    try:
        repo = SqliteRepo(Settings(sqlite_db_path=db, anthropic_api_key="test"))
        df_jul10 = repo.query_body_composition("2026-07-10", "2026-07-10")
        df_jul11 = repo.query_body_composition("2026-07-11", "2026-07-11")
        assert df_jul10.empty, "23:30 UTC reading must NOT key to its UTC date"
        assert not df_jul11.empty
        assert df_jul11.iloc[0]["date"] == "2026-07-11"
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        _time.tzset()
