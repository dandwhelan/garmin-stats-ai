"""Tests for the pure token-shaping / formatting helpers in query_tools."""

from __future__ import annotations

import pandas as pd

from garmin_insights.tools.query_tools import (
    STATE_LIFESTYLE_CATEGORIES,
    _clean_records,
    _fmt_race_time,
    _marker_series,
    _night_label,
    _strip_zero_lifestyle,
    aggregate_workouts,
    get_all_tools_anthropic,
    split_lifestyle_by_category,
)


def test_fmt_race_time_minutes():
    assert _fmt_race_time(1500) == "25:00"
    assert _fmt_race_time(1470) == "24:30"


def test_fmt_race_time_hours():
    assert _fmt_race_time(11400) == "3:10:00"


def test_fmt_race_time_invalid():
    assert _fmt_race_time(None) is None
    assert _fmt_race_time(0) is None
    assert _fmt_race_time(-5) is None


def test_marker_series_keeps_recent_and_dates():
    df = pd.DataFrame(
        {
            "time": [f"2026-06-{d:02d}T12:00:00" for d in range(1, 13)],
            "vo2_max_value": list(range(40, 52)),
        }
    )
    out = _marker_series(df, "vo2_max_value", keep=3)
    assert list(out.keys()) == ["2026-06-10", "2026-06-11", "2026-06-12"]
    assert out["2026-06-12"] == 51.0


def test_marker_series_scale():
    df = pd.DataFrame({"time": ["2026-06-01T00:00:00"], "weight": [80000]})
    out = _marker_series(df, "weight", scale=0.001)
    assert out["2026-06-01"] == 80.0


def test_marker_series_missing_column():
    df = pd.DataFrame({"time": ["2026-06-01T00:00:00"], "x": [1]})
    assert _marker_series(df, "nope") is None


def test_strip_zero_lifestyle_drops_zero_entries():
    summaries = [
        {
            "date": "2026-06-01",
            "lifestyle": {
                "Caffeine": {"status": 1, "value": 2.0},
                "Alcohol": {"status": 0, "value": 0.0},
                "Stretching": {"status": 1, "value": 0.0},
            },
        }
    ]
    out = _strip_zero_lifestyle(summaries)
    lf = out[0]["lifestyle"]
    assert "Caffeine: 2" in lf
    assert "Stretching" in lf  # binary occurrence → bare name
    assert all("Alcohol" not in item for item in lf)


def test_split_lifestyle_partitions_actions_and_states():
    summaries = [
        {
            "date": "2026-06-26",
            "lifestyle": {
                "Alcohol": {"status": 1, "value": 3.0},
                "Morning Caffeine": {"status": 1, "value": 2.0},
                "Asthma symptoms": {"status": 1, "value": 0.0},
                "Allergy Severity": {"status": 1, "value": 3.0},
                "Injured": {"status": 1, "value": 0.0},
                "Sleep Sounds": {"status": 0, "value": 0.0},  # dropped
            },
        }
    ]
    cat_map = {
        "Alcohol": "LIFESTYLE",
        "Morning Caffeine": "LIFESTYLE",
        "Asthma symptoms": "CUSTOM",
        "Allergy Severity": "CUSTOM",
        "Injured": "LIFE_STATUS",
        "Sleep Sounds": "SLEEP_RELATED",
    }
    out = split_lifestyle_by_category(summaries, cat_map)[0]
    assert out["lifestyle"] == ["Alcohol: 3", "Morning Caffeine: 2"]
    assert out["states_symptoms"] == ["Asthma symptoms", "Allergy Severity: 3", "Injured"]
    assert "Sleep Sounds" not in out.get("lifestyle", [])


def test_split_lifestyle_unknown_category_defaults_to_action():
    # A behaviour with no category mapping should NOT be treated as a state.
    summaries = [{"date": "d", "lifestyle": {"Mystery": {"status": 1, "value": 0.0}}}]
    out = split_lifestyle_by_category(summaries, {})[0]
    assert out["lifestyle"] == ["Mystery"]
    assert "states_symptoms" not in out


def test_state_categories_are_the_expected_set():
    assert STATE_LIFESTYLE_CATEGORIES == {"LIFE_STATUS", "CUSTOM", "CUSTOM_SLEEP_RELATED"}


def test_aggregate_workouts_sums_per_type():
    activities = [
        {"date": "2026-06-01", "type": "walking", "min": 30.0, "km": 2.0, "kcal": 100},
        {"date": "2026-06-01", "type": "walking", "min": 20.0, "km": 1.5, "kcal": 80},
        {"date": "2026-06-02", "type": "strength_training", "min": 45.0, "kcal": 200},
    ]
    out = aggregate_workouts(activities)
    walking = next(r for r in out if r["type"] == "walking")
    strength = next(r for r in out if r["type"] == "strength_training")
    assert walking == {"type": "walking", "sessions": 2, "min": 50.0, "km": 3.5, "kcal": 180}
    # strength has no distance → km dropped; sorted by minutes desc keeps walking first
    assert "km" not in strength
    assert strength["sessions"] == 1 and strength["min"] == 45.0
    assert out[0]["type"] == "walking"  # higher total minutes


def test_aggregate_workouts_empty():
    assert aggregate_workouts([]) == []


def test_clean_records_strips_nulls_and_rounds():
    out = _clean_records([{"a": 1.23456, "b": None, "c": "x"}])
    assert out == [{"a": 1.2, "c": "x"}]


def test_night_label_spans_prev_to_wake():
    assert _night_label("2026-06-28") == "2026-06-27→2026-06-28"


def test_night_label_invalid_passthrough():
    assert _night_label("not-a-date") == "not-a-date"


def test_tools_cache_control_only_on_last():
    """Regression guard: the Anthropic API caps cache_control markers, and the
    tools list must carry exactly one — on the final entry."""
    tools = get_all_tools_anthropic(None)
    with_cc = [t["name"] for t in tools if "cache_control" in t]
    assert with_cc == [tools[-1]["name"]]
    assert "get_fitness_markers" in {t["name"] for t in tools}
