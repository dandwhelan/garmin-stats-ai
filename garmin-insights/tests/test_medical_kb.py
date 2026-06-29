"""Tests for the medical-KB relevance subsetting used by the portable prompt."""

from __future__ import annotations

from garmin_insights.knowledge.medical import (
    INSIGHT_RULES,
    count_visible_rules,
    get_rules_summary_for_llm,
    select_relevant_rule_names,
)

_ALWAYS = {"multi_cause_recovery_strain", "baseline_reliability_guard"}
_ENV = {
    "heat_recovery_confounder",
    "air_quality_recovery_confounder",
    "high_pollen_sleep_confounder",
    "allergy_next_day_rhr_systemic",
    "asthma_environmental_hr_marker",
}


def test_select_keeps_always_rules_even_with_no_signals():
    keep = select_relevant_rule_names(biological_sex="Male")
    assert keep == _ALWAYS


def test_select_matches_by_metric():
    keep = select_relevant_rule_names(metrics={"restingHeartRate"}, biological_sex="Male")
    # every RHR-triggered rule visible to a male user should be present
    rhr_rules = {
        r.name for r in INSIGHT_RULES
        if r.trigger_metric == "restingHeartRate" and r.sex_specific != "female"
    }
    assert rhr_rules <= keep
    assert _ALWAYS <= keep


def test_select_matches_behavior_case_insensitive():
    keep = select_relevant_rule_names(behaviors={"aLcOhOl"}, biological_sex="Male")
    alcohol_rules = {
        r.name for r in INSIGHT_RULES
        if (r.trigger_behavior or "").lower() == "alcohol"
    }
    assert alcohol_rules
    assert alcohol_rules <= keep


def test_select_matches_explicit_rule_name():
    keep = select_relevant_rule_names(rule_names={"vo2_max_plateau"}, biological_sex="Male")
    assert "vo2_max_plateau" in keep


def test_environmental_cluster_gated_on_flag():
    without = select_relevant_rule_names(metrics={"totalSteps"}, biological_sex="Male")
    assert not (_ENV <= without)
    with_env = select_relevant_rule_names(
        metrics={"totalSteps"}, include_environmental=True, biological_sex="Male"
    )
    assert _ENV <= with_env


def test_sex_filter_excludes_cycle_rules_for_male():
    # A female-only behaviour cannot pull a cycle rule into a male user's set.
    keep = select_relevant_rule_names(behaviors={"Period Day"}, biological_sex="Male")
    female_rule_names = {r.name for r in INSIGHT_RULES if r.sex_specific == "female"}
    assert keep.isdisjoint(female_rule_names)


def test_count_visible_rules_respects_sex():
    male = count_visible_rules("Male")
    female = count_visible_rules("Female")
    assert female == len(INSIGHT_RULES)
    assert male < female  # cycle rules dropped for male


def test_subset_render_only_includes_kept_rules_and_note():
    keep = {"alcohol_rem_sleep", "rhr_elevated", "multi_cause_recovery_strain"}
    out = get_rules_summary_for_llm("Male", only_rules=keep, subset_note=True)
    assert "Filtered to the rules relevant" in out
    assert "**alcohol_rem_sleep**" in out
    assert "**rhr_elevated**" in out
    # a rule NOT in the keep set must be absent
    assert "**late_caffeine_sleep**" not in out
    # subset is materially smaller than the full KB
    assert len(out) < len(get_rules_summary_for_llm("Male"))


def test_full_render_is_unchanged_by_default():
    """Regression guard: the default render (what the live agent caches) must not
    gain the subset note and must include every visible rule."""
    full = get_rules_summary_for_llm("Male")
    assert "Filtered to the rules relevant" not in full
    assert full.count("\n- **") == count_visible_rules("Male")
