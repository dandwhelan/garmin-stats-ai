"""Tests for the personal experiment engine (n-of-1 behaviour trials).

Covers the MemoryStore CRUD layer, the AnalysisEngine.evaluate_experiment
statistics (arm sizes, lag attribution, sufficiency gating), and the three
QueryToolHandler tools (validation, status filters, verdict paths, caveats).
"""

from __future__ import annotations

import json
import sqlite3
import types
from datetime import datetime, timedelta

import pytest

from garmin_insights.db.memory import MemoryStore
from garmin_insights.tools.analysis_tools import AnalysisEngine
from garmin_insights.tools.query_tools import QueryToolHandler


@pytest.fixture
def memory(tmp_path):
    db = tmp_path / "mem.db"
    store = MemoryStore(types.SimpleNamespace(sqlite_db_path=str(db)))
    store.initialise_schema()
    return store


def _handler(memory: MemoryStore) -> QueryToolHandler:
    # Experiment tools only touch memory + analysis, never the repo.
    return QueryToolHandler(repo=None, memory=memory, analysis=AnalysisEngine(memory))


def _seed_magnesium_days(memory: MemoryStore, n: int = 20) -> None:
    """Seed n consecutive days ending yesterday. Magnesium is logged on even
    offsets; deep sleep is HIGH the day AFTER a magnesium day (a lag-1 effect)
    and LOW otherwise, with a little deterministic jitter so neither arm has
    zero variance."""
    base = datetime.now() - timedelta(days=n)
    for o in range(n):
        d = (base + timedelta(days=o)).strftime("%Y-%m-%d")
        on = o % 2 == 0
        prev_on = o >= 1 and (o - 1) % 2 == 0  # yesterday had magnesium
        deep = (7000 if prev_on else 3400) + o * 20
        life = {"Magnesium": {"status": 1 if on else 0, "value": 1.0 if on else 0.0}}
        memory.upsert_daily_summary(
            d, {"deepSleepSeconds": deep, "is_complete": True}, life
        )


def _backdate(memory: MemoryStore, name: str, days_ago: int) -> None:
    """Push an experiment's started_at into the past so seeded history sits
    after it (a real experiment accrues days before it's evaluated)."""
    conn = sqlite3.connect(memory.db_path)
    started = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE experiments SET started_at = ? WHERE name = ?", (started, name))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------
# MemoryStore CRUD
# ----------------------------------------------------------------------
def test_experiment_crud_roundtrip(memory):
    eid = memory.create_experiment(
        "exp1", "Magnesium improves deep sleep", "Magnesium",
        "deepSleepSeconds", "increase", 1, 5,
    )
    assert isinstance(eid, int)

    row = memory.get_experiment("EXP1")  # case-insensitive lookup
    assert row is not None
    assert row["name"] == "exp1"
    assert row["status"] == "active"
    assert row["metric"] == "deepSleepSeconds"
    assert row["lag_days"] == 1
    assert "result" not in row  # no result recorded yet
    # started_at must be SQLite space-separated UTC, never a 'T'-separated
    # isoformat (which string-sorts after a space — see save_insight).
    assert " " in row["started_at"] and "T" not in row["started_at"]

    with pytest.raises(sqlite3.IntegrityError):
        memory.create_experiment("exp1", "dup", "b", "m")

    # Interim result updates result_json only, leaving status active.
    memory.record_experiment_result("exp1", {"n_on": 3, "n_off": 4})
    row = memory.get_experiment("exp1")
    assert row["result"] == {"n_on": 3, "n_off": 4}
    assert row["status"] == "active"

    memory.create_experiment("exp2", "Alcohol lowers sleep", "Alcohol",
                             "sleepScore", "decrease", 1, 5)
    assert {r["name"] for r in memory.get_experiments("active")} == {"exp1", "exp2"}

    memory.conclude_experiment("exp1", "supported", {"p_value": 0.01})
    row = memory.get_experiment("exp1")
    assert row["status"] == "completed"
    assert row["verdict"] == "supported"
    assert row["result"] == {"p_value": 0.01}
    assert row["ended_at"] is not None

    memory.abandon_experiment("exp2")
    assert memory.get_experiment("exp2")["status"] == "abandoned"
    assert memory.get_experiment("exp2")["ended_at"] is not None

    assert memory.get_experiments("active") == []
    assert [r["name"] for r in memory.get_experiments("completed")] == ["exp1"]
    assert [r["name"] for r in memory.get_experiments("abandoned")] == ["exp2"]
    assert len(memory.get_experiments()) == 2  # all statuses


# ----------------------------------------------------------------------
# AnalysisEngine.evaluate_experiment
# ----------------------------------------------------------------------
def test_evaluate_experiment_arms_direction_and_effect(memory):
    _seed_magnesium_days(memory, n=20)
    engine = AnalysisEngine(memory)
    start = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")

    res = engine.evaluate_experiment("Magnesium", "deepSleepSeconds", start,
                                     lag_days=1, min_days_per_arm=5)
    assert res is not None
    # Even behaviour days (0..18) each pair with the next (odd) day → 10 on.
    # Odd behaviour days pair with an even day, but day 19 → day 20 is missing.
    assert res["n_on"] == 10
    assert res["n_off"] == 9
    assert res["mean_on"] > res["mean_off"]      # deep sleep higher after magnesium
    assert res["difference"] > 0
    assert res["p_value"] is not None and res["p_value"] < 0.05
    assert res["effect_size"] == "large"
    assert res["sufficient_data"] is True
    assert res["behavior"] == "Magnesium"


def test_evaluate_experiment_sufficiency_gate(memory):
    _seed_magnesium_days(memory, n=20)
    engine = AnalysisEngine(memory)
    start = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
    # 10/9 arms — insufficient when we demand 15 per arm.
    res = engine.evaluate_experiment("Magnesium", "deepSleepSeconds", start,
                                     lag_days=1, min_days_per_arm=15)
    assert res["sufficient_data"] is False


def test_evaluate_experiment_lag_attributes_next_day(memory):
    base = datetime.now() - timedelta(days=15)

    def day(o):
        return (base + timedelta(days=o)).strftime("%Y-%m-%d")

    memory.upsert_daily_summary(day(0), {"deepSleepSeconds": 1000, "is_complete": True},
                                {"Magnesium": {"status": 1, "value": 1.0}})
    memory.upsert_daily_summary(day(1), {"deepSleepSeconds": 8000, "is_complete": True},
                                {"Magnesium": {"status": 0, "value": 0.0}})
    memory.upsert_daily_summary(day(2), {"deepSleepSeconds": 3000, "is_complete": True},
                                {"Magnesium": {"status": 0, "value": 0.0}})
    engine = AnalysisEngine(memory)

    # lag 1: day-0 magnesium is judged by day-1's deep sleep (8000), NOT day 0's
    # own 1000. Day-1 (off) pairs with day-2 (3000); day-2 → day-3 is missing.
    res = engine.evaluate_experiment("Magnesium", "deepSleepSeconds", day(0), lag_days=1)
    assert res["n_on"] == 1 and res["mean_on"] == 8000
    assert res["n_off"] == 1 and res["mean_off"] == 3000

    # lag 0: day-0 magnesium is judged by its own same-day value (1000).
    res0 = engine.evaluate_experiment("Magnesium", "deepSleepSeconds", day(0), lag_days=0)
    assert res0["mean_on"] == 1000


def test_evaluate_experiment_none_when_metric_absent(memory):
    _seed_magnesium_days(memory, n=10)
    engine = AnalysisEngine(memory)
    start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    assert engine.evaluate_experiment("Magnesium", "noSuchMetric", start) is None


# ----------------------------------------------------------------------
# Tool: start_experiment
# ----------------------------------------------------------------------
def test_start_experiment_success_and_duplicate(memory):
    _seed_magnesium_days(memory, n=20)
    h = _handler(memory)
    out = json.loads(h.start_experiment(
        "mag-sleep", "Magnesium improves my deep sleep", "Magnesium", "deepSleepSeconds"
    ))
    assert out["created"] is True
    assert out["experiment"]["name"] == "mag-sleep"
    assert out["experiment"]["status"] == "active"
    assert "protocol" in out and "lifestyle journal" in out["protocol"]
    assert "warning" not in out  # Magnesium is logged in the window

    dup = json.loads(h.start_experiment("mag-sleep", "x", "Magnesium", "deepSleepSeconds"))
    assert "error" in dup and "already exists" in dup["error"]


def test_start_experiment_rejects_bad_direction(memory):
    _seed_magnesium_days(memory, n=20)
    h = _handler(memory)
    out = json.loads(h.start_experiment(
        "e", "h", "Magnesium", "deepSleepSeconds", expected_direction="sideways"
    ))
    assert "error" in out and "expected_direction" in out["error"]


def test_start_experiment_rejects_unknown_metric(memory):
    _seed_magnesium_days(memory, n=20)
    h = _handler(memory)
    out = json.loads(h.start_experiment("e", "h", "Magnesium", "notARealMetric"))
    assert "error" in out
    assert out["example_metrics"] and len(out["example_metrics"]) >= 1


def test_start_experiment_warns_when_behavior_not_logged(memory):
    _seed_magnesium_days(memory, n=20)  # only Magnesium is present
    h = _handler(memory)
    out = json.loads(h.start_experiment(
        "cold-shower", "Cold showers raise my HRV", "Cold Shower", "deepSleepSeconds"
    ))
    assert out["created"] is True
    assert "warning" in out and "not yet logged" in out["warning"]
    assert out["experiment"]["behavior"] == "Cold Shower"  # stored as typed


# ----------------------------------------------------------------------
# Tool: get_experiments
# ----------------------------------------------------------------------
def test_get_experiments_tool_statuses(memory):
    _seed_magnesium_days(memory, n=20)
    h = _handler(memory)
    h.start_experiment("mag-sleep", "h", "Magnesium", "deepSleepSeconds")

    active = json.loads(h.get_experiments("active"))
    assert len(active) == 1
    assert active[0]["name"] == "mag-sleep"
    assert "days_elapsed" in active[0]

    assert "message" in json.loads(h.get_experiments("completed"))
    assert "error" in json.loads(h.get_experiments("bogus"))


# ----------------------------------------------------------------------
# Tool: evaluate_experiment (verdict paths + caveats)
# ----------------------------------------------------------------------
def test_evaluate_experiment_tool_supported(memory):
    _seed_magnesium_days(memory, n=20)
    memory.create_experiment("mag-sleep", "Magnesium improves deep sleep",
                             "Magnesium", "deepSleepSeconds", "increase", 1, 5)
    _backdate(memory, "mag-sleep", 20)
    h = _handler(memory)

    out = json.loads(h.evaluate_experiment("mag-sleep"))
    assert out["verdict"] == "supported"
    assert out["result"]["n_on"] >= 5 and out["result"]["n_off"] >= 5
    assert out["result"]["difference"] > 0
    assert out["interim"] is True
    assert isinstance(out["caveats"], list) and len(out["caveats"]) >= 5


def test_evaluate_experiment_tool_insufficient(memory):
    _seed_magnesium_days(memory, n=6)  # arms of 3 and 2 — below the default 5
    memory.create_experiment("mag-sleep", "h", "Magnesium", "deepSleepSeconds",
                             "increase", 1, 5)
    _backdate(memory, "mag-sleep", 6)
    h = _handler(memory)

    out = json.loads(h.evaluate_experiment("mag-sleep"))
    assert out["verdict"] == "inconclusive_insufficient_data"
    assert "more day" in out["verdict_detail"]
    assert out["caveats"]  # caveats present even when inconclusive


def test_evaluate_experiment_tool_conclude_and_abandon(memory):
    _seed_magnesium_days(memory, n=20)
    memory.create_experiment("mag-sleep", "h", "Magnesium", "deepSleepSeconds",
                             "increase", 1, 5)
    _backdate(memory, "mag-sleep", 20)
    h = _handler(memory)

    out = json.loads(h.evaluate_experiment("mag-sleep", conclude=True))
    assert out.get("concluded") is True
    row = memory.get_experiment("mag-sleep")
    assert row["status"] == "completed"
    assert row["verdict"] == out["verdict"]

    memory.create_experiment("caffeine-hrv", "h", "Caffeine", "avgOvernightHrv",
                             "decrease", 1, 5)
    out2 = json.loads(h.evaluate_experiment("caffeine-hrv", abandon=True))
    assert out2["abandoned"] is True
    assert memory.get_experiment("caffeine-hrv")["status"] == "abandoned"


def test_evaluate_experiment_tool_unknown_name(memory):
    h = _handler(memory)
    out = json.loads(h.evaluate_experiment("does-not-exist"))
    assert "error" in out
    assert "active_experiments" in out
