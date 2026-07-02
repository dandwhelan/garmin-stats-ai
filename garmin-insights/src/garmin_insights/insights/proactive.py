"""Proactive insight scanner — detects anomalies and patterns automatically.

This runs locally in Python (no LLM cost for detection). The LLM is only
invoked to interpret and explain the flagged findings.

Findings carry evidence-tier metadata so the agent can match its output
language to the strength of the underlying rule, and a multi-cause composite
finding is emitted when several recovery markers deviate together — so the
agent ranks plausible contributors instead of naming a single cause.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from garmin_insights.db.memory import MemoryStore
from garmin_insights.knowledge.medical import INSIGHT_RULES, get_behavior_rules
from garmin_insights.stats_utils import benjamini_hochberg
from garmin_insights.tools.analysis_tools import AnalysisEngine, AnomalyResult, ComparisonResult

# Thresholds above which environmental factors warrant inclusion as ranked
# confounders. These are deliberately conservative — they err on the side
# of NOT crowding the contributors list with mild weather days.
_ENV_HEAT_APPARENT_C    = 28.0   # apparent max — physiological-load threshold
_ENV_AQI_EUROPEAN       = 60.0   # >60 = "poor" on EU scale
_ENV_PM25_UG_M3         = 25.0   # WHO 24-hour guideline
_ENV_POLLEN_GRAINS_M3   = 50.0   # generic "high" threshold for grass/ragweed

logger = logging.getLogger(__name__)


# Metrics whose simultaneous deviation should collapse into a single composite
# "illness-like recovery strain pattern" finding rather than three separate ones.
_STRAIN_TRIAD = ("restingHeartRate", "avgOvernightHrv", "averageRespirationValue")


def _is_female(biological_sex: str | None) -> bool:
    """Mirror the cycle-rule gate used in get_rules_summary_for_llm."""
    return (biological_sex or "").strip().lower().startswith("f")


def _visible_confounders(confounders, biological_sex: str | None) -> list[str]:
    """Drop cycle-only confounders for non-female users.

    Mirrors the logic in ``get_rules_summary_for_llm``: ``luteal_phase`` is
    physiologically meaningless for male users, so it must never surface in
    scanner output (composite ranked contributors or single-rule findings).
    """
    cs = list(confounders or [])
    if not _is_female(biological_sex):
        cs = [c for c in cs if c != "luteal_phase"]
    return cs


def _attach_rule_metadata(finding: dict[str, Any], rule, biological_sex: str | None = None) -> None:
    """Stamp tier + confounder fields onto a finding so the agent can decide
    how strongly to phrase its reply. For non-female users ``luteal_phase`` is
    stripped from the emitted confounder list (cycle physiology can't apply)."""
    finding["medical_context"] = rule.research_summary
    finding["citation"] = rule.research_citation
    finding["evidence_tier"] = rule.evidence_tier
    finding["claim_strength"] = rule.claim_strength
    finding["measurement_confidence"] = rule.measurement_confidence
    confounders = _visible_confounders(rule.confounders, biological_sex)
    if confounders:
        finding["confounders"] = confounders
    if rule.requires_user_context:
        finding["requires_user_context"] = True


class InsightScanner:
    """Scans cached data for anomalies and behavior correlations."""

    def __init__(
        self,
        memory: MemoryStore,
        analysis: AnalysisEngine,
        biological_sex: str | None = None,
    ) -> None:
        self._memory = memory
        self._analysis = analysis
        # Used to strip cycle-only confounders (luteal_phase) from emitted
        # findings for non-female users — same gate the LLM summary applies.
        self._biological_sex = biological_sex

    def scan_anomalies(self) -> list[dict[str, Any]]:
        """Detect metric anomalies relative to baselines."""
        anomalies = self._analysis.run_full_anomaly_scan()
        findings = []
        for a in anomalies:
            matching_rules = [
                r for r in INSIGHT_RULES
                if r.trigger_metric == a.metric and r.trigger_behavior is None
            ]
            finding = a.to_dict()
            if matching_rules:
                _attach_rule_metadata(finding, matching_rules[0], self._biological_sex)
            findings.append(finding)
        return findings

    def scan_composite_strain(self) -> list[dict[str, Any]]:
        """When two or three of RHR / HRV / respiration deviate together,
        emit a single multi-cause composite finding with ranked plausible
        contributors. This replaces three parallel single-cause anomalies
        with one tier-aware "illness-like recovery strain pattern" entry."""
        anomalies = self._analysis.run_full_anomaly_scan()
        by_metric = {a.metric: a for a in anomalies if a.metric in _STRAIN_TRIAD}
        if len(by_metric) < 2:
            return []

        composite_rule = next(
            (r for r in INSIGHT_RULES if r.name == "multi_cause_recovery_strain"),
            None,
        )
        if composite_rule is None:
            return []

        # Rank plausible contributors: user-logged behaviours in the last 48h
        # outrank generic confounders, because the user has positive evidence.
        # Environmental extremes (heat / poor AQ / high pollen) sit between
        # logged behaviours and generic confounders — they're positive evidence
        # from Open-Meteo but not user-confirmed, so they outrank generic
        # confounders but are subordinate to logged behaviours.
        ranked: list[str] = []
        recent_behaviors = self._recent_logged_behaviors(hours=48)
        for behavior in recent_behaviors:
            ranked.append(f"logged: {behavior}")
        for env_factor in self._recent_environmental_extremes(days=2):
            ranked.append(f"environment: {env_factor}")
        # Append generic confounders from the rule, dedup against ranked list.
        # luteal_phase is stripped here for non-female users so a male persona
        # never sees cycle physiology ranked as a possible contributor.
        for c in _visible_confounders(composite_rule.confounders, self._biological_sex):
            if not any(c in r for r in ranked):
                ranked.append(c)

        finding: dict[str, Any] = {
            "rule_name": composite_rule.name,
            "deviating_metrics": list(by_metric.keys()),
            "z_scores": {m: a.z_score for m, a in by_metric.items()},
            "ranked_contributors": ranked,
        }
        _attach_rule_metadata(finding, composite_rule, self._biological_sex)
        # Annotate sparse-baseline guard so the agent can prepend the
        # "low-confidence" prefix when warranted.
        baseline_days = self._available_baseline_days()
        if baseline_days is not None and baseline_days < 21:
            finding["baseline_days_available"] = baseline_days
            finding["baseline_low_confidence"] = True
        return [finding]

    def _recent_logged_behaviors(self, hours: int = 48) -> list[str]:
        """Return distinct lifestyle-journal labels logged in the last `hours`.
        Returns [] silently on any error — this is a best-effort enrichment."""
        try:
            df = self._memory.recent_lifestyle_entries(hours=hours)  # type: ignore[attr-defined]
        except AttributeError:
            return []
        except Exception as e:
            logger.debug("recent lifestyle entries unavailable: %s", e)
            return []
        try:
            return sorted({str(v) for v in df["behavior"].dropna().tolist()})
        except Exception:
            return []

    def _recent_environmental_extremes(self, days: int = 2) -> list[str]:
        """Return descriptive labels for environmental confounders that exceed
        the configured thresholds in the most recent N days.

        Pulls from the optional `environment_daily` table written by the
        Open-Meteo pipeline. Returns [] silently when no env data is available,
        so this is a no-op for users who haven't configured HOME_LAT/HOME_LON.
        """
        import sqlite3
        try:
            db_path = self._memory.db_path
        except AttributeError:
            return []
        end = datetime.utcnow().strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows: list[tuple] = []
        try:
            conn = sqlite3.connect(db_path, timeout=10)
            try:
                cur = conn.execute(
                    "SELECT apparent_temp_max_c, european_aqi, pm25, "
                    "pollen_grass, pollen_birch, pollen_ragweed "
                    "FROM environment_daily "
                    "WHERE date >= ? AND date <= ?",
                    (start, end),
                )
                rows = cur.fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            # Table doesn't exist for this DB yet — first run before fetcher
            # has populated environment_daily.
            return []
        except Exception as exc:
            logger.debug("environment lookup failed: %s", exc)
            return []
        if not rows:
            return []

        def _peak(idx: int) -> float | None:
            vals = [r[idx] for r in rows if r[idx] is not None]
            return max(vals) if vals else None

        labels: list[str] = []
        peak_apparent = _peak(0)
        if peak_apparent is not None and peak_apparent >= _ENV_HEAT_APPARENT_C:
            labels.append(f"heat (apparent {peak_apparent:.1f}°C)")
        peak_aqi = _peak(1)
        if peak_aqi is not None and peak_aqi >= _ENV_AQI_EUROPEAN:
            labels.append(f"poor air quality (EU AQI {peak_aqi:.0f})")
        peak_pm25 = _peak(2)
        if peak_pm25 is not None and peak_pm25 >= _ENV_PM25_UG_M3:
            labels.append(f"high PM2.5 ({peak_pm25:.1f} µg/m³)")
        pollen_peak = max(
            [v for v in (_peak(3), _peak(4), _peak(5)) if v is not None] or [0.0]
        )
        if pollen_peak >= _ENV_POLLEN_GRAINS_M3:
            labels.append(f"high pollen ({pollen_peak:.0f} grains/m³)")
        return labels

    def _available_baseline_days(self) -> int | None:
        """Best-effort count of consecutive days of baseline data available.
        Returns None if the analysis engine doesn't expose this."""
        try:
            return int(self._analysis.baseline_days_available())  # type: ignore[attr-defined]
        except AttributeError:
            return None
        except Exception as e:
            logger.debug("baseline_days_available not available: %s", e)
            return None

    def scan_behavior_impacts(self, days: int = 30) -> list[dict[str, Any]]:
        """Analyze the impact of all logged lifestyle behaviors on key metrics.

        Significance is decided with a Benjamini-Hochberg FDR correction across
        the whole behavior battery, not a per-test ``p<0.05``. With ~20 rules
        each Welch-tested independently, an uncorrected threshold surfaces ~1
        false-positive "significant" behavior per scan — the same multiple-
        comparisons trap that ``compute_correlation_matrix`` already guards
        against. The per-test ``p_value`` is preserved; only the ``significant``
        flag (which gates insight persistence) becomes FDR-aware.
        """
        behavior_rules = get_behavior_rules()

        # First pass: compute every comparison so BH can see the full set of
        # p-values before deciding which clear the corrected threshold.
        candidates: list[tuple[Any, ComparisonResult, dict[str, Any]]] = []
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
            _attach_rule_metadata(finding, rule, self._biological_sex)
            candidates.append((rule, result, finding))

        # BH correction across the battery. None p-values (n too small for a
        # t-test) are treated as not-significant by benjamini_hochberg.
        flags = benjamini_hochberg([r.p_value for _, r, _ in candidates])

        findings = []
        for (rule, result, finding), significant in zip(candidates, flags):
            finding["significant"] = significant
            finding["significant_correction"] = "benjamini_hochberg_q0.05"

            if significant:
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
                    _attach_rule_metadata(finding, matching[0], self._biological_sex)
                findings.append(finding)

        return findings

    def run_full_scan(self) -> dict[str, list[dict[str, Any]]]:
        """Run all detection passes and return categorized findings."""
        return {
            "anomalies": self.scan_anomalies(),
            "composite_strain": self.scan_composite_strain(),
            "behavior_impacts": self.scan_behavior_impacts(),
            "trends": self.scan_trends(),
        }
