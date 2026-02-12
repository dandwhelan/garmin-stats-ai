"""Proactive insight scanner — detects anomalies and patterns automatically.

This runs locally in Python (no LLM cost for detection). The LLM is only
invoked to interpret and explain the flagged findings.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from garmin_insights.db.memory import MemoryStore
from garmin_insights.knowledge.medical import INSIGHT_RULES, get_behavior_rules
from garmin_insights.tools.analysis_tools import AnalysisEngine, AnomalyResult, ComparisonResult

logger = logging.getLogger(__name__)


class InsightScanner:
    """Scans cached data for anomalies and behavior correlations."""

    def __init__(self, memory: MemoryStore, analysis: AnalysisEngine) -> None:
        self._memory = memory
        self._analysis = analysis

    def scan_anomalies(self) -> list[dict[str, Any]]:
        """Detect metric anomalies relative to baselines."""
        anomalies = self._analysis.run_full_anomaly_scan()
        findings = []
        for a in anomalies:
            # Find matching medical rules for richer context
            matching_rules = [
                r for r in INSIGHT_RULES
                if r.trigger_metric == a.metric and r.trigger_behavior is None
            ]
            finding = a.to_dict()
            if matching_rules:
                rule = matching_rules[0]
                finding["medical_context"] = rule.research_summary
                finding["citation"] = rule.research_citation
            findings.append(finding)
        return findings

    def scan_behavior_impacts(self, days: int = 30) -> list[dict[str, Any]]:
        """Analyze the impact of all logged lifestyle behaviors on key metrics."""
        behavior_rules = get_behavior_rules()
        findings = []

        for rule in behavior_rules:
            # Skip if we've already reported this recently
            if self._memory.is_insight_suppressed(rule.name):
                logger.debug("Skipping suppressed insight: %s", rule.name)
                continue

            result = self._analysis.compare_metric_with_behavior(
                behavior=rule.trigger_behavior,
                metric=rule.trigger_metric,
                days=days,
            )

            if result is None or result.n_with < 2 or result.n_without < 2:
                continue

            finding = result.to_dict()
            finding["rule_name"] = rule.name
            finding["medical_context"] = rule.research_summary
            finding["citation"] = rule.research_citation

            if result.significant:
                # Noteworthy — save this insight to prevent duplicate reporting
                try:
                    description = rule.description_template.format(**finding)
                except (KeyError, ValueError):
                    description = (
                        f"{rule.trigger_behavior} → {rule.trigger_metric}: "
                        f"{result.mean_with:.1f} vs {result.mean_without:.1f} "
                        f"(p={result.p_value:.3f})"
                    )

                self._memory.save_insight(
                    rule_name=rule.name,
                    description=description,
                    significance=result.p_value,
                    data=finding,
                )
                finding["description"] = description

            findings.append(finding)

        return findings

    def scan_trends(self) -> list[dict[str, Any]]:
        """Detect notable trends in key metrics."""
        trend_metrics = [
            "restingHeartRate", "avgOvernightHrv", "sleepScore",
            "stressPercentage", "bodyBatteryHighestValue",
            "totalSteps", "deepSleepSeconds",
        ]

        findings = []
        for metric in trend_metrics:
            result = self._analysis.detect_trend(metric, days=14)
            if result is None:
                continue
            if result.direction != "stable" and result.r_squared > 0.3:
                finding = result.to_dict()
                # Find matching medical rules
                matching = [
                    r for r in INSIGHT_RULES
                    if r.trigger_metric == metric and r.trigger_behavior is None
                ]
                if matching:
                    finding["medical_context"] = matching[0].research_summary
                    finding["citation"] = matching[0].research_citation
                findings.append(finding)

        return findings

    def run_full_scan(self) -> dict[str, list[dict[str, Any]]]:
        """Run all detection passes and return categorized findings."""
        return {
            "anomalies": self.scan_anomalies(),
            "behavior_impacts": self.scan_behavior_impacts(),
            "trends": self.scan_trends(),
        }
