"""InsightScanner — the scan passes not already covered by
test_proactive_scanner.py (which covers composite-strain direction gating only).

These findings are self-injected into ``generate_scan_report``, so whatever the
scanner emits becomes the opening claims of a user-facing AI health narrative.
A wrong tier badge or a leaked cycle confounder ends up phrased as fact.
"""

from __future__ import annotations

import types

import pytest

from garmin_insights.insights.proactive import (
    BEHAVIOR_IMPACT_WINDOW_DAYS,
    InsightScanner,
    _is_female,
    _visible_confounders,
)
from garmin_insights.tools.analysis_tools import (
    AnomalyResult,
    ComparisonResult,
    TrendResult,
)


def _anomaly(metric: str, z: float) -> AnomalyResult:
    return AnomalyResult(
        metric=metric, date="2026-06-29", value=50.0, baseline_mean=52.0,
        baseline_std=2.0, z_score=z, direction="above" if z > 0 else "below",
    )


def _comparison(behavior: str, metric: str, p: float | None,
                n_with: int = 8, n_without: int = 20,
                mean_with: float = 60.0) -> ComparisonResult:
    return ComparisonResult(
        behavior=behavior, metric=metric, mean_with=mean_with, mean_without=70.0,
        n_with=n_with, n_without=n_without, difference=mean_with - 70.0,
        pct_change=-14.3, p_value=p, significant=p is not None and p < 0.05,
        cohens_d=-0.8,
    )


def _scanner(*, anomalies=None, trends=None, comparisons=None,
             baseline_days=None, suppressed=(), sex=None) -> InsightScanner:
    """Scanner over stubs, so each pass can be isolated from the others."""
    saved: list[dict] = []

    memory = types.SimpleNamespace(
        is_insight_suppressed=lambda name: name in suppressed,
        save_insight=lambda **kw: saved.append(kw),
    )
    analysis = types.SimpleNamespace(
        run_full_anomaly_scan=lambda: list(anomalies or []),
        detect_trend=lambda metric, days=14: (trends or {}).get(metric),
        compare_metric_with_behavior=lambda behavior, metric, days: (
            (comparisons or {}).get((behavior, metric))),
    )
    if baseline_days is not None:
        analysis.baseline_days_available = lambda window_days=30: baseline_days

    scanner = InsightScanner(memory, analysis, biological_sex=sex)
    scanner._saved_insights = saved  # type: ignore[attr-defined]
    return scanner


# ==================================================================
# scan_anomalies
# ==================================================================
def test_strain_direction_anomaly_gets_rule_metadata():
    findings = _scanner(anomalies=[_anomaly("restingHeartRate", 2.5)]).scan_anomalies()
    assert len(findings) == 1
    f = findings[0]
    assert f["evidence_tier"] in {"A", "B", "C"}
    assert f["claim_strength"]
    assert f["measurement_confidence"]
    assert "favourable_direction" not in f


def test_favourable_deviation_is_reported_without_a_strain_tier():
    """An anomalously LOW resting heart rate is a good day. Reporting it is
    fine; stamping a strain rule's evidence tier onto it is not."""
    findings = _scanner(anomalies=[_anomaly("restingHeartRate", -2.5)]).scan_anomalies()
    assert len(findings) == 1
    assert findings[0]["favourable_direction"] is True
    assert "evidence_tier" not in findings[0]


def test_favourable_direction_is_metric_aware():
    """HRV runs the other way: a HIGH value is the good one."""
    high_hrv = _scanner(anomalies=[_anomaly("avgOvernightHrv", 2.5)]).scan_anomalies()
    low_hrv = _scanner(anomalies=[_anomaly("avgOvernightHrv", -2.5)]).scan_anomalies()
    assert high_hrv[0].get("favourable_direction") is True
    assert "favourable_direction" not in low_hrv[0]


def test_anomaly_scan_passes_through_metrics_with_no_matching_rule():
    findings = _scanner(anomalies=[_anomaly("someUnknownMetric", 3.0)]).scan_anomalies()
    assert len(findings) == 1
    assert "evidence_tier" not in findings[0]


def test_empty_anomaly_scan_returns_empty():
    assert _scanner(anomalies=[]).scan_anomalies() == []


# ==================================================================
# Cycle-confounder gating
# ==================================================================
@pytest.mark.parametrize("sex,expected", [
    ("Female", True), ("female", True), ("F", True),
    ("Male", False), ("", False), (None, False),
])
def test_is_female_gate(sex, expected):
    assert _is_female(sex) is expected


def test_luteal_phase_confounder_is_stripped_for_non_female_users():
    """Cycle physiology cannot apply, and the identity block separately tells
    the model this user has no cycle data — a leaked confounder contradicts it."""
    confounders = ["alcohol", "luteal_phase", "late_training"]
    assert _visible_confounders(confounders, "Male") == ["alcohol", "late_training"]
    assert _visible_confounders(confounders, "") == ["alcohol", "late_training"]


def test_luteal_phase_confounder_is_kept_for_female_users():
    confounders = ["alcohol", "luteal_phase"]
    assert _visible_confounders(confounders, "Female") == confounders


def test_confounder_gate_applies_to_emitted_anomaly_findings():
    male = _scanner(anomalies=[_anomaly("restingHeartRate", 2.5)], sex="Male")
    findings = male.scan_anomalies()
    assert "luteal_phase" not in findings[0].get("confounders", [])


# ==================================================================
# scan_trends
# ==================================================================
def test_trend_is_reported_when_directional_and_well_fitted():
    trends = {"restingHeartRate": TrendResult("restingHeartRate", "increasing",
                                              0.4, 0.75, 14)}
    findings = _scanner(trends=trends).scan_trends()
    assert len(findings) == 1
    assert findings[0]["direction"] == "increasing"
    assert findings[0]["evidence_tier"]


def test_stable_trends_are_not_reported():
    trends = {"restingHeartRate": TrendResult("restingHeartRate", "stable", 0.0, 0.9, 14)}
    assert _scanner(trends=trends).scan_trends() == []


def test_poorly_fitted_trends_are_not_reported():
    """r² <= 0.3 is noise, not a trend — reporting it would manufacture a
    narrative out of scatter."""
    trends = {"restingHeartRate": TrendResult("restingHeartRate", "increasing",
                                              0.4, 0.30, 14)}
    assert _scanner(trends=trends).scan_trends() == []

    trends["restingHeartRate"] = TrendResult("restingHeartRate", "increasing",
                                             0.4, 0.31, 14)
    assert len(_scanner(trends=trends).scan_trends()) == 1


def test_trend_scan_tolerates_metrics_with_no_result():
    assert _scanner(trends={}).scan_trends() == []


# ==================================================================
# scan_behavior_impacts
# ==================================================================
def _behavior_rule_keys(n=3):
    from garmin_insights.knowledge.medical import get_behavior_rules
    return [(r.trigger_behavior, r.trigger_metric, r.name)
            for r in get_behavior_rules()[:n]]


def test_behavior_impact_applies_bh_correction_not_raw_p():
    """With ~20 independent Welch tests, an uncorrected p<0.05 surfaces roughly
    one false 'significant' behaviour per scan."""
    keys = _behavior_rule_keys(3)
    comparisons = {
        (b, m): _comparison(b, m, p)
        # 0.04 clears a raw threshold but not BH across a family this size.
        for (b, m, _n), p in zip(keys, [0.04, 0.9, 0.95])
    }
    findings = _scanner(comparisons=comparisons).scan_behavior_impacts()

    assert findings
    assert all(f["significant_correction"] == "benjamini_hochberg_q0.05" for f in findings)
    borderline = next(f for f in findings if f["p_value"] == 0.04)
    assert borderline["significant"] is False


def test_strongly_significant_behaviour_survives_correction_and_is_saved():
    keys = _behavior_rule_keys(3)
    comparisons = {
        (b, m): _comparison(b, m, p)
        for (b, m, _n), p in zip(keys, [0.0001, 0.9, 0.95])
    }
    scanner = _scanner(comparisons=comparisons)
    findings = scanner.scan_behavior_impacts()

    winner = next(f for f in findings if f["p_value"] == 0.0001)
    assert winner["significant"] is True
    assert winner["description"]
    # Persisted so it isn't re-reported on the next scan.
    assert scanner._saved_insights


def test_non_significant_findings_are_not_persisted():
    keys = _behavior_rule_keys(2)
    comparisons = {(b, m): _comparison(b, m, 0.8) for b, m, _n in keys}
    scanner = _scanner(comparisons=comparisons)
    scanner.scan_behavior_impacts()
    assert scanner._saved_insights == []


def test_suppressed_rules_are_skipped():
    keys = _behavior_rule_keys(2)
    comparisons = {(b, m): _comparison(b, m, 0.001) for b, m, _n in keys}
    suppressed = {keys[0][2]}
    findings = _scanner(comparisons=comparisons,
                        suppressed=suppressed).scan_behavior_impacts()
    assert all(f["rule_name"] != keys[0][2] for f in findings)


def test_underpowered_comparisons_are_dropped():
    """n<2 in either arm can't support a t-test."""
    keys = _behavior_rule_keys(2)
    comparisons = {
        keys[0][:2]: _comparison(*keys[0][:2], 0.001, n_with=1),
        keys[1][:2]: _comparison(*keys[1][:2], 0.001, n_without=1),
    }
    assert _scanner(comparisons=comparisons).scan_behavior_impacts() == []


def test_duplicate_comparisons_are_reported_once():
    """Two rules whose behaviours fuzzy-match the same logged label produce an
    identical comparison. Keeping both double-counts the finding and pads the
    BH correction family, making everything look less significant."""
    from garmin_insights.knowledge.medical import get_behavior_rules

    # The dedup key is (rule.trigger_metric, arm sizes, means), so the rules
    # must genuinely share a trigger metric for the collision to arise.
    shared = [r for r in get_behavior_rules() if r.trigger_metric == "sleepScore"][:3]
    assert len(shared) == 3

    identical = {
        (r.trigger_behavior, r.trigger_metric): ComparisonResult(
            behavior=r.trigger_behavior, metric="sleepScore", mean_with=60.0,
            mean_without=70.0, n_with=8, n_without=20, difference=-10.0,
            pct_change=-14.3, p_value=0.001, significant=True, cohens_d=-0.8,
        )
        for r in shared
    }
    findings = _scanner(comparisons=identical).scan_behavior_impacts()
    assert len(findings) == 1


def test_distinct_comparisons_on_the_same_metric_are_both_kept():
    """Dedup must key on the comparison, not merely the metric — two real
    behaviours affecting sleep differently are two findings."""
    from garmin_insights.knowledge.medical import get_behavior_rules

    shared = [r for r in get_behavior_rules() if r.trigger_metric == "sleepScore"][:2]
    comparisons = {
        (r.trigger_behavior, r.trigger_metric): ComparisonResult(
            behavior=r.trigger_behavior, metric="sleepScore", mean_with=60.0 + i * 5,
            mean_without=70.0, n_with=8 + i, n_without=20, difference=-10.0,
            pct_change=-14.3, p_value=0.001, significant=True, cohens_d=-0.8,
        )
        for i, r in enumerate(shared)
    }
    assert len(_scanner(comparisons=comparisons).scan_behavior_impacts()) == 2


def test_behavior_impact_window_is_the_declared_constant():
    """The portable prompt states this window alongside a shorter data
    snapshot; a silent change would make the prompt lie."""
    captured = {}
    keys = _behavior_rule_keys(1)

    def capture(behavior, metric, days):
        captured["days"] = days
        return None

    analysis = types.SimpleNamespace(
        run_full_anomaly_scan=list,
        detect_trend=lambda metric, days=14: None,
        compare_metric_with_behavior=capture,
    )
    memory = types.SimpleNamespace(is_insight_suppressed=lambda n: False,
                                   save_insight=lambda **kw: None)
    InsightScanner(memory, analysis).scan_behavior_impacts()
    assert captured["days"] == BEHAVIOR_IMPACT_WINDOW_DAYS == 30
    assert keys  # rules exist to iterate


# ==================================================================
# Sparse-baseline guard
# ==================================================================
def test_composite_finding_is_flagged_low_confidence_under_21_days():
    """Documented rule: under 21 days of baseline, the agent must prepend
    'Low-confidence (sparse baseline):' — this flag is its trigger."""
    scanner = _scanner(
        anomalies=[_anomaly("restingHeartRate", 2.5), _anomaly("avgOvernightHrv", -2.2)],
        baseline_days=14,
    )
    finding = scanner.scan_composite_strain()[0]
    assert finding["baseline_low_confidence"] is True
    assert finding["baseline_days_available"] == 14


def test_composite_finding_is_not_flagged_at_21_days_or_more():
    scanner = _scanner(
        anomalies=[_anomaly("restingHeartRate", 2.5), _anomaly("avgOvernightHrv", -2.2)],
        baseline_days=21,
    )
    finding = scanner.scan_composite_strain()[0]
    assert "baseline_low_confidence" not in finding


def test_missing_baseline_day_count_does_not_flag_or_crash():
    """The helper is best-effort; an analysis engine without the method must
    simply omit the flag."""
    scanner = _scanner(
        anomalies=[_anomaly("restingHeartRate", 2.5), _anomaly("avgOvernightHrv", -2.2)],
    )
    finding = scanner.scan_composite_strain()[0]
    assert "baseline_low_confidence" not in finding


# ==================================================================
# run_full_scan
# ==================================================================
def test_run_full_scan_returns_every_category():
    scanner = _scanner(
        anomalies=[_anomaly("restingHeartRate", 2.5)],
        trends={"restingHeartRate": TrendResult("restingHeartRate", "increasing",
                                                0.4, 0.8, 14)},
    )
    out = scanner.run_full_scan()
    assert set(out) >= {"anomalies", "composite_strain", "behavior_impacts", "trends"}
    assert out["anomalies"] and out["trends"]


def test_run_full_scan_on_real_data_is_json_safe(sample_settings):
    """The findings are serialised into the scan prompt, so anything numpy or
    NaN in here breaks the report rather than the scanner."""
    import json

    from garmin_insights.db.memory import MemoryStore
    from garmin_insights.tools.analysis_tools import AnalysisEngine

    memory = MemoryStore(sample_settings)
    memory.initialise_schema()
    scanner = InsightScanner(memory, AnalysisEngine(memory),
                             biological_sex=sample_settings.biological_sex)

    out = scanner.run_full_scan()
    json.dumps(out)  # must not raise
    assert set(out) >= {"anomalies", "composite_strain", "behavior_impacts", "trends"}
