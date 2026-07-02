"""Composite strain detection — direction gating.

The composite "illness-like recovery strain pattern" finding must only fire
when triad metrics deviate in strain-indicating directions (RHR up, HRV down,
respiration up). A great recovery day (RHR anomalously low + HRV anomalously
high) deviates on two triad metrics but is the opposite of strain.
"""

from __future__ import annotations

import types

from garmin_insights.insights.proactive import InsightScanner
from garmin_insights.tools.analysis_tools import AnomalyResult


def _anomaly(metric: str, z: float) -> AnomalyResult:
    return AnomalyResult(
        metric=metric,
        date="2026-06-29",
        value=50.0,
        baseline_mean=52.0,
        baseline_std=2.0,
        z_score=z,
        direction="above" if z > 0 else "below",
    )


def _scanner(anomalies: list[AnomalyResult]) -> InsightScanner:
    # Bare stand-ins: memory lacks recent_lifestyle_entries / db_path and
    # analysis lacks baseline_days_available, so the enrichment helpers all
    # take their AttributeError fallbacks and the test isolates the gating.
    memory = types.SimpleNamespace()
    analysis = types.SimpleNamespace(run_full_anomaly_scan=lambda: anomalies)
    return InsightScanner(memory, analysis)


def test_no_composite_on_positive_deviations():
    """RHR below + HRV above baseline = best recovery day, not strain."""
    findings = _scanner([
        _anomaly("restingHeartRate", -2.2),
        _anomaly("avgOvernightHrv", 1.7),
    ]).scan_composite_strain()
    assert findings == []


def test_composite_fires_on_strain_directions():
    findings = _scanner([
        _anomaly("restingHeartRate", 2.0),
        _anomaly("avgOvernightHrv", -1.8),
    ]).scan_composite_strain()
    assert len(findings) == 1
    finding = findings[0]
    assert finding["rule_name"] == "multi_cause_recovery_strain"
    assert sorted(finding["deviating_metrics"]) == [
        "avgOvernightHrv", "restingHeartRate",
    ]


def test_mixed_directions_do_not_pair_up():
    """One strain-direction metric + one good-direction metric < 2 → no finding."""
    findings = _scanner([
        _anomaly("restingHeartRate", 2.0),   # strain
        _anomaly("avgOvernightHrv", 1.7),    # good
    ]).scan_composite_strain()
    assert findings == []


def test_respiration_up_counts_as_strain():
    findings = _scanner([
        _anomaly("averageRespirationValue", 1.6),
        _anomaly("avgOvernightHrv", -1.9),
    ]).scan_composite_strain()
    assert len(findings) == 1


def test_non_triad_metrics_ignored():
    findings = _scanner([
        _anomaly("totalSteps", 3.0),
        _anomaly("sleepScore", -2.5),
    ]).scan_composite_strain()
    assert findings == []
