"""Tests for the overnight-physiology analytics built on `sleep_intraday`.

The sample database plants two signals these tests read back:
  * alcohol blunts the overnight heart-rate fall and pushes the nadir later
  * the strain window carries repeated SpO2 dips

Both are properties of the physiology being modelled, not of the arithmetic,
so they are the assertions worth making — a refactor that quietly stops
detecting them fails here.
"""

from __future__ import annotations

import json
import sqlite3
import types
from datetime import datetime, timedelta, timezone

import pytest

from garmin_insights.db.sqlite_repo import SqliteRepo
from garmin_insights.insights.overnight import (
    CONCERN_DIRECTIONS,
    OvernightService,
    _robust_baseline,
)
from garmin_insights.tools.query_tools import QueryToolHandler

_SCHEMA = """
CREATE TABLE sleep_intraday (
    time TEXT, device TEXT, activity_level INTEGER, activity_seconds INTEGER,
    stage_level INTEGER, stage_seconds INTEGER, restless_value INTEGER,
    spo2_reading INTEGER, respiration_value INTEGER, heart_rate INTEGER,
    stress_value INTEGER, body_battery INTEGER, hrv_value INTEGER
);
"""

_COLS = (
    "time, device, activity_level, activity_seconds, stage_level, stage_seconds,"
    " restless_value, spo2_reading, respiration_value, heart_rate, stress_value,"
    " body_battery, hrv_value"
)


def _sample(time, device="testdev", **kw):
    row = {
        "time": time, "device": device, "activity_level": None,
        "activity_seconds": None, "stage_level": None, "stage_seconds": None,
        "restless_value": None, "spo2_reading": None, "respiration_value": None,
        "heart_rate": None, "stress_value": None, "body_battery": None,
        "hrv_value": None,
    }
    row.update(kw)
    return tuple(row[c.strip()] for c in _COLS.split(","))


def _build(tmp_path, rows, name="on.db") -> str:
    db = str(tmp_path / name)
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.executemany(
        f"INSERT INTO sleep_intraday ({_COLS}) VALUES ({','.join('?' * 13)})", rows
    )
    conn.commit()
    conn.close()
    return db


def _window(rows):
    """Date range covering every night in the sample DB."""
    dates = sorted(r["date"] for r in rows)
    return dates[0], dates[-1]


# ----------------------------------------------------------------------
# Night boundaries
# ----------------------------------------------------------------------
def test_evening_samples_belong_to_the_next_mornings_night(tmp_path):
    """A 23:00 sample and an 06:00 sample are the SAME night.

    `sleep_summary` is stamped with the wake date, so the overnight series has
    to split at noon to stay aligned with it. Splitting on the calendar date
    would file the two halves of one night as two nights.
    """
    rows = []
    # 22:00 UTC on the 3rd through 06:00 UTC on the 4th, one sample a minute.
    base = datetime(2026, 6, 3, 22, 0, tzinfo=timezone.utc)
    for i in range(8 * 60):
        rows.append(_sample((base + timedelta(minutes=i)).isoformat(),
                            heart_rate=60 - i // 100))
    svc = OvernightService(_build(tmp_path, rows))
    nights = svc.nights("2026-06-03", "2026-06-05")
    assert len(nights) == 1
    # In UTC (and every zone east of it through +11) the 22:00 sample is
    # already past noon, so the night is filed under the following morning.
    assert nights[0]["night_of"] in {"2026-06-04", "2026-06-05"}
    assert nights[0]["hr_samples"] == 8 * 60


def test_missing_table_is_reported_not_raised(tmp_path):
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()
    result = OvernightService(db).summary("2026-06-01", "2026-06-30")
    assert result["available"] is False
    assert result["nights"] == []
    assert "message" in result


def test_duplicate_timestamps_across_devices_do_not_raise(tmp_path):
    """Two devices can write the same timestamp, so the index has duplicates.

    Anything that aligns columns by timestamp raises on a duplicated index;
    the stage maths has to stay row-local.
    """
    base = datetime(2026, 6, 3, 22, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(8 * 60):
        ts = (base + timedelta(minutes=i)).isoformat()
        rows.append(_sample(ts, device="watch", heart_rate=60 - i // 100))
        rows.append(_sample(ts, device="strap", heart_rate=61 - i // 100))
    for j in range(16):
        ts = (base + timedelta(minutes=30 * j)).isoformat()
        rows.append(_sample(ts, device="watch", stage_level=j % 4, stage_seconds=1800))
        rows.append(_sample(ts, device="strap", stage_level=j % 4, stage_seconds=1800))
    nights = OvernightService(_build(tmp_path, rows)).nights("2026-06-03", "2026-06-05")
    assert len(nights) == 1
    assert nights[0]["stage_transitions"] > 0


# ----------------------------------------------------------------------
# Per-night metrics
# ----------------------------------------------------------------------
def test_sparse_night_reports_counts_but_no_derived_statistics(tmp_path):
    """Ten samples cannot support a nadir or a dip percentage.

    The night still appears — its absence would look like a missing night
    rather than a thin one — but every derived field stays None.
    """
    base = datetime(2026, 6, 3, 23, 0, tzinfo=timezone.utc)
    rows = [
        _sample((base + timedelta(minutes=i * 10)).isoformat(), heart_rate=58 + i)
        for i in range(10)
    ]
    night = OvernightService(_build(tmp_path, rows)).nights("2026-06-03", "2026-06-05")[0]
    assert night["hr_samples"] == 10
    assert "hr_dip_pct" not in night
    assert "hr_nadir" not in night


def test_waso_counts_only_awakenings_between_first_and_last_sleep(tmp_path):
    """Settling down and getting up are not fragmented sleep.

    The awake segments that bracket the night are excluded; only the awake
    segment in the middle counts toward WASO.
    """
    base = datetime(2026, 6, 3, 22, 0, tzinfo=timezone.utc)
    # awake, light, deep, AWAKE, light, rem, light, awake  (30 min each)
    levels = [3, 1, 0, 3, 1, 2, 1, 3]
    rows = [
        _sample((base + timedelta(minutes=30 * j)).isoformat(),
                stage_level=lvl, stage_seconds=1800)
        for j, lvl in enumerate(levels)
    ]
    night = OvernightService(_build(tmp_path, rows)).nights("2026-06-03", "2026-06-05")[0]
    assert night["waso_episodes"] == 1
    assert night["waso_minutes"] == 30.0
    # Stage minutes likewise cover only the sleep period, so the bracketing
    # awake segments never inflate the awake total.
    assert night["stage_minutes"]["awake"] == 30.0


def test_desaturation_run_counts_as_one_event(tmp_path):
    """A sustained dip is one desaturation, not one per sample below baseline."""
    base = datetime(2026, 6, 3, 22, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(240):
        # A single 10-minute plateau 6 points below an otherwise flat 97%.
        spo2 = 91 if 100 <= i < 110 else 97
        rows.append(_sample((base + timedelta(minutes=i)).isoformat(), spo2_reading=spo2))
    night = OvernightService(_build(tmp_path, rows)).nights("2026-06-03", "2026-06-05")[0]
    assert night["desat_events"] == 1
    assert night["spo2_min"] == 91.0


def test_hrv_trajectory_reports_direction_not_just_the_mean(tmp_path):
    base = datetime(2026, 6, 3, 22, 0, tzinfo=timezone.utc)
    rows = [
        _sample((base + timedelta(minutes=i)).isoformat(), hrv_value=30 + i // 10)
        for i in range(300)
    ]
    night = OvernightService(_build(tmp_path, rows)).nights("2026-06-03", "2026-06-05")[0]
    assert night["hrv_last_third"] > night["hrv_first_third"]
    assert night["hrv_trend_pct"] > 0


def test_negative_sentinels_are_not_averaged_in(tmp_path):
    """Garmin writes -1 for 'no reading'; averaging it in drags every mean down."""
    base = datetime(2026, 6, 3, 22, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(300):
        rows.append(_sample((base + timedelta(minutes=i)).isoformat(),
                            respiration_value=-1 if i % 3 == 0 else 14))
    night = OvernightService(_build(tmp_path, rows)).nights("2026-06-03", "2026-06-05")[0]
    assert night["respiration_mean"] == 14.0
    assert night["respiration_samples"] == 200


# ----------------------------------------------------------------------
# Baselines and deviations
# ----------------------------------------------------------------------
def test_robust_baseline_needs_history_and_spread():
    assert _robust_baseline([10.0] * 3) is None          # too few nights
    assert _robust_baseline([10.0] * 20) is None         # no spread -> no z-scale
    median, scale = _robust_baseline([8.0, 9, 10, 11, 12, 13, 14, 30.0])
    assert median == pytest.approx(11.5)
    # One 30 does not blow up a MAD-derived scale the way an SD would.
    assert scale < 4.0


def test_deviations_only_fire_in_the_concern_direction(sample_db, sample_rows):
    """An unusually GOOD night must never be narrated as a finding.

    Every reported deviation has to point the way `CONCERN_DIRECTIONS` says is
    a worry for that metric.
    """
    start, end = _window(sample_rows)
    result = OvernightService(sample_db).summary(start, end)
    for dev in result["deviations"]:
        concern = CONCERN_DIRECTIONS[dev["metric"]]
        assert dev["z_score"] * concern > 0, f"{dev['metric']} flagged the good way"


# ----------------------------------------------------------------------
# The planted signals, read back off the sample database
# ----------------------------------------------------------------------
def test_alcohol_blunts_the_hr_fall_and_delays_the_nadir(sample_db, sample_rows):
    start, end = _window(sample_rows)
    nights = OvernightService(sample_db).nights(start, end)
    drinks = {r["date"]: r["drinks"] for r in sample_rows}

    def mean(field, wet):
        vals = [
            n[field] for n in nights
            if n.get(field) is not None
            and ((drinks.get(n["night_of"], 0) >= 2) is wet)
        ]
        assert vals, f"no {'drinking' if wet else 'dry'} nights carried {field}"
        return sum(vals) / len(vals)

    assert mean("hr_dip_pct", wet=True) < mean("hr_dip_pct", wet=False)
    assert mean("hr_nadir_pct_of_night", wet=True) > mean("hr_nadir_pct_of_night", wet=False)


def test_strain_window_shows_a_higher_desaturation_burden(sample_db, sample_rows):
    start, end = _window(sample_rows)
    nights = OvernightService(sample_db).nights(start, end)
    strain = {r["date"]: r["strain"] for r in sample_rows}
    hit = [n["desat_index_per_hour"] for n in nights
           if strain.get(n["night_of"]) and n.get("desat_index_per_hour") is not None]
    calm = [n["desat_index_per_hour"] for n in nights
            if not strain.get(n["night_of"]) and n.get("desat_index_per_hour") is not None]
    assert hit and calm
    assert sum(hit) / len(hit) > sum(calm) / len(calm)


def test_nights_before_the_series_existed_are_simply_absent(sample_db, sample_rows):
    """The fixture only writes the recent nights, as a real database does.

    The window is longer than the recorded history, and that has to come back
    as fewer nights rather than as blanks or an error.
    """
    start, end = _window(sample_rows)
    nights = OvernightService(sample_db).nights(start, end)
    assert 0 < len(nights) < len(sample_rows)
    assert all(start <= n["night_of"] <= end for n in nights)


# ----------------------------------------------------------------------
# Agent tool + HTTP endpoint
# ----------------------------------------------------------------------
def test_agent_tool_truncates_to_the_requested_nights(sample_db):
    handler = QueryToolHandler(
        repo=SqliteRepo(types.SimpleNamespace(sqlite_db_path=sample_db)),
        memory=None,
        analysis=None,
    )
    out = json.loads(handler.get_overnight_physiology(days=60, nights=3))
    assert out["available"] is True
    assert len(out["nights"]) == 3
    # The baselines still come from the whole window, not the returned slice.
    assert out["nights_analysed"] > 3


def test_agent_tool_reports_absence_without_raising(tmp_path):
    db = str(tmp_path / "bare.db")
    sqlite3.connect(db).close()
    handler = QueryToolHandler(
        repo=SqliteRepo(types.SimpleNamespace(sqlite_db_path=db)),
        memory=None,
        analysis=None,
    )
    assert json.loads(handler.get_overnight_physiology())["available"] is False


def test_overnight_endpoint_returns_nights_and_baselines(api_client):
    r = api_client.get("/api/overnight?user=default")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["nights"], "no nights returned"
    assert "baselines" in body and "deviations" in body
    first = body["nights"][0]
    assert {"night_of", "span_hours", "samples"} <= set(first)


def test_overnight_endpoint_rejects_an_unknown_user(api_client):
    assert api_client.get("/api/overnight?user=nobody").status_code == 404


# ----------------------------------------------------------------------
# What actually reaches the model
# ----------------------------------------------------------------------
def _scanner(db):
    from garmin_insights.db.memory import MemoryStore
    from garmin_insights.insights.proactive import InsightScanner
    from garmin_insights.tools.analysis_tools import AnalysisEngine

    memory = MemoryStore(types.SimpleNamespace(sqlite_db_path=db))
    return InsightScanner(memory, AnalysisEngine(memory), "Male")


def test_scanner_overnight_findings_carry_their_evidence_tier(sample_db):
    """A finding without tier metadata leaves the agent free to overclaim."""
    findings = _scanner(sample_db).scan_overnight()
    for f in findings:
        assert f["source"] in {"overnight_physiology", "overnight_screening"}
        if "rule_name" in f:
            assert f["evidence_tier"] in {"A", "B", "C", "D"}
            assert f["medical_context"]


def test_overnight_pass_is_part_of_the_full_scan(sample_db):
    """run_full_scan is what the scan report and the portable prompt inject.

    An overnight pass that exists but isn't in this dict reaches nothing.
    """
    assert "overnight" in _scanner(sample_db).run_full_scan()


def test_scanner_survives_a_database_with_no_overnight_series(tmp_path, sample_db):
    """Databases predating the SleepIntraday handler must scan, not crash."""
    import shutil

    db = str(tmp_path / "no_series.db")
    shutil.copy(sample_db, db)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE sleep_intraday")
    conn.commit()
    conn.close()
    assert _scanner(db).scan_overnight() == []
    assert "overnight" in _scanner(db).run_full_scan()


@pytest.fixture
def portable_agent(sample_db, monkeypatch):
    """A real HealthAgent over the sample DB — no API key is needed to build
    a prompt, only to send one."""
    from garmin_insights.agent import HealthAgent
    from garmin_insights.config import Settings

    settings = Settings(
        sqlite_db_path=sample_db,
        anthropic_api_key="sk-test-not-used",
        biological_sex="Male",
        display_name="Test",
    )
    return HealthAgent(settings)


def test_portable_prompt_embeds_the_nights_themselves(portable_agent):
    """The receiving model has no tools, so an un-embedded section is invisible."""
    prompt = portable_agent.build_portable_prompt(focus="morning")
    assert "## Overnight physiology" in prompt
    assert "hr_dip_pct" in prompt
    # The caveat has to travel with the number, not sit only in the KB.
    assert "not a clinical ODI" in prompt.lower() or "NOT a clinical ODI" in prompt


def test_portable_prompt_never_names_a_tool_the_reader_cannot_call(portable_agent):
    for focus in ("morning", "night", "general", "weekly"):
        prompt = portable_agent.build_portable_prompt(focus=focus)
        assert "get_overnight_physiology" not in prompt, focus


def test_live_scan_prompts_point_at_the_tool(portable_agent):
    """The live agent HAS tools — the mention must survive there."""
    from garmin_insights.agent import _SCAN_PROMPTS

    pointed = [f for f, p in _SCAN_PROMPTS.items() if "get_overnight_physiology" in p]
    assert "morning" in pointed, "the morning brief is where night shape matters most"
    assert len(pointed) >= 3
