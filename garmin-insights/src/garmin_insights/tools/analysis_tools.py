"""Statistical analysis tools — correlation, trend detection, anomaly flagging.

These run locally in Python (no LLM cost) and can also be invoked by the
LLM agent as tools via the tool-calling interface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats
import difflib

from garmin_insights.db.memory import MemoryStore
from garmin_insights.stats_utils import benjamini_hochberg, pearson_r_p

logger = logging.getLogger(__name__)


@dataclass
class ComparisonResult:
    behavior: str
    metric: str
    mean_with: float | None
    mean_without: float | None
    n_with: int
    n_without: int
    difference: float | None
    pct_change: float | None
    p_value: float | None
    significant: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "behavior": self.behavior,
            "metric": self.metric,
            "mean_with": round(self.mean_with, 2) if self.mean_with is not None else None,
            "mean_without": round(self.mean_without, 2) if self.mean_without is not None else None,
            "n_with": self.n_with,
            "n_without": self.n_without,
            "difference": round(self.difference, 2) if self.difference is not None else None,
            "pct_change": round(self.pct_change, 1) if self.pct_change is not None else None,
            "p_value": round(self.p_value, 4) if self.p_value is not None else None,
            "significant": self.significant,
        }


@dataclass
class TrendResult:
    metric: str
    direction: str  # "increasing", "decreasing", "stable"
    slope_per_day: float
    r_squared: float
    days_analyzed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "direction": self.direction,
            "slope_per_day": round(self.slope_per_day, 3),
            "r_squared": round(self.r_squared, 3),
            "days_analyzed": self.days_analyzed,
        }


@dataclass
class AnomalyResult:
    metric: str
    date: str
    value: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    direction: str  # "above" or "below"

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "date": self.date,
            "value": round(self.value, 2),
            "baseline_mean": round(self.baseline_mean, 2),
            "baseline_std": round(self.baseline_std, 2),
            "z_score": round(self.z_score, 2),
            "direction": self.direction,
        }


# Metrics that accumulate throughout the day — incomplete days give false readings
_CUMULATIVE_METRICS = {
    "totalSteps", "totalDistanceMeters", "activeKilocalories",
    "moderateIntensityMinutes", "vigorousIntensityMinutes",
    "stressPercentage", "highStressPercentage",
    "bodyBatteryDrainedValue", "bodyBatteryChargedValue",
    "sleepingSeconds",
}


class AnalysisEngine:
    """Statistical analysis functions for health data."""

    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    def _filter_summaries(self, summaries: list, metric: str) -> list:
        """Remove incomplete days if the metric is cumulative."""
        if metric in _CUMULATIVE_METRICS:
            return [s for s in summaries if s.get("is_complete", True)]
        return summaries

    def find_matching_behavior(self, target: str, available_behaviors: list[str]) -> str | None:
        """Find the best match for a behavior name (case-insensitive, fuzzy)."""
        target_lower = target.lower()
        
        # 1. Exact match (case-insensitive)
        for b in available_behaviors:
            if b.lower() == target_lower:
                return b
                
        # 2. Substring match (e.g. "caffeine" -> "Morning Caffeine")
        for b in available_behaviors:
            if target_lower in b.lower():
                return b

        # 3. Fuzzy match (spelling mistakes)
        matches = difflib.get_close_matches(target, available_behaviors, n=1, cutoff=0.7)
        return matches[0] if matches else None

    def compare_metric_with_behavior(
        self, behavior: str, metric: str, days: int = 90
    ) -> ComparisonResult | None:
        """Split days by a lifestyle behavior (on/off) and compare a metric's means.

        Uses Welch's t-test for significance. Supports fuzzy matching for behavior names.
        """
        from datetime import datetime, timedelta

        today = datetime.utcnow().strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        summaries = self._memory.get_daily_summaries_range(start, today)
        summaries = self._filter_summaries(summaries, metric)

        vals_with: list[float] = []
        vals_without: list[float] = []

        # Auto-detect lag for sleep metrics
        # Behavior on Day T affects Sleep on Day T+1
        shift_days = 0
        SLEEP_METRICS = {
            "sleepScore", "deepSleepSeconds", "remSleepSeconds", "lightSleepSeconds",
            "awakeSleepSeconds", "avgOvernightHrv", "lowestHeartRate",
            "restStressDuration", "avgStressLevel"  # Sometimes overnight stress
        }
        if metric in SLEEP_METRICS:
            shift_days = 1

        # Find the canonical behavior name from the data to ensure consistency
        # We scan all summaries to find available behaviors first
        all_behaviors = set()
        for s in summaries:
            all_behaviors.update(s.get("lifestyle", {}).keys())
        
        canonical_behavior = self.find_matching_behavior(behavior, list(all_behaviors))
        if not canonical_behavior:
            logger.warning("Behavior '%s' not found in recent data (checked %d behaviors).", 
                           behavior, len(all_behaviors))
            return None

        for i, s in enumerate(summaries):
            # Apply shift: Behavior at [i] affects Metric at [i + shift]
            metric_idx = i + shift_days
            if metric_idx >= len(summaries):
                continue
                
            metric_val = summaries[metric_idx].get(metric)
            if metric_val is None:
                continue
            
            lifestyle = s.get("lifestyle", {})
            behavior_data = lifestyle.get(canonical_behavior)
            
            # Check if behavior was present (status=1)
            if behavior_data and behavior_data.get("status") == 1:
                vals_with.append(float(metric_val))
            else:
                vals_without.append(float(metric_val))

        # We allow N=1 for descriptive stats, even if significance test is impossible
        if len(vals_with) < 1 or len(vals_without) < 1:
            return None # Cannot compare if never occurred or always occurred

        if len(vals_with) < 2 or len(vals_without) < 2:
             # Not enough for t-test, but return means for descriptive insight
             diff = None
             if vals_with and vals_without:
                 diff = float(np.mean(vals_with) - np.mean(vals_without))
                 
             return ComparisonResult(
                behavior=behavior, metric=metric,
                mean_with=np.mean(vals_with) if vals_with else None,
                mean_without=np.mean(vals_without) if vals_without else None,
                n_with=len(vals_with), n_without=len(vals_without),
                difference=diff,
                pct_change=None, p_value=None, significant=False,
            )

        mean_w = float(np.mean(vals_with))
        mean_wo = float(np.mean(vals_without))
        diff = mean_w - mean_wo
        pct = (diff / abs(mean_wo) * 100) if mean_wo != 0 else None

        t_stat, p_val = stats.ttest_ind(vals_with, vals_without, equal_var=False)

        return ComparisonResult(
            behavior=behavior, metric=metric,
            mean_with=mean_w, mean_without=mean_wo,
            n_with=len(vals_with), n_without=len(vals_without),
            difference=diff, pct_change=pct,
            p_value=float(p_val), significant=bool(p_val < 0.05),
        )

    def detect_trend(
        self, metric: str, days: int = 30
    ) -> TrendResult | None:
        """Detect a linear trend over a rolling window."""
        from datetime import datetime, timedelta

        today = datetime.utcnow().strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        summaries = self._memory.get_daily_summaries_range(start, today)
        summaries = self._filter_summaries(summaries, metric)

        values = []
        for s in summaries:
            val = s.get(metric)
            if val is not None:
                values.append(float(val))

        if len(values) < 3:
            return None

        x = np.arange(len(values), dtype=float)
        slope, intercept, r_val, p_val, std_err = stats.linregress(x, values)

        if abs(r_val) > 0.3:
            direction = "increasing" if slope > 0 else "decreasing"
        else:
            direction = "stable"

        return TrendResult(
            metric=metric,
            direction=direction,
            slope_per_day=float(slope),
            r_squared=float(r_val ** 2),
            days_analyzed=len(values),
        )

    def detect_anomalies(
        self, metric: str, days: int = 7, threshold_sigma: float = 1.5
    ) -> list[AnomalyResult]:
        """Flag recent values that deviate from the 30-day baseline."""
        baseline = self._memory.get_baseline(metric)
        if not baseline or baseline.get("avg_30d") is None or baseline.get("std_7d") is None:
            return []

        mean = baseline["avg_30d"]
        std = baseline["std_7d"]
        if std == 0 or std is None:
            return []

        from datetime import datetime, timedelta

        today = datetime.utcnow().strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        summaries = self._memory.get_daily_summaries_range(start, today)
        summaries = self._filter_summaries(summaries, metric)

        anomalies = []
        for s in summaries:
            val = s.get(metric)
            if val is None:
                continue
            z = (float(val) - mean) / std
            if abs(z) >= threshold_sigma:
                anomalies.append(AnomalyResult(
                    metric=metric,
                    date=s["date"],
                    value=float(val),
                    baseline_mean=mean,
                    baseline_std=std,
                    z_score=z,
                    direction="above" if z > 0 else "below",
                ))

        return anomalies

    def compute_correlation_matrix(
        self, metrics: list[str], days: int = 90
    ) -> dict[str, Any]:
        """Pearson correlation between multiple metrics."""
        from datetime import datetime, timedelta

        today = datetime.utcnow().strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        summaries = self._memory.get_daily_summaries_range(start, today)
        # Exclude incomplete days if any metric in the set is cumulative
        if any(m in _CUMULATIVE_METRICS for m in metrics):
            summaries = [s for s in summaries if s.get("is_complete", True)]

        data: dict[str, list[float | None]] = {m: [] for m in metrics}
        for s in summaries:
            for m in metrics:
                data[m].append(s.get(m))

        df = pd.DataFrame(data).dropna()
        if len(df) < 3:
            return {"error": "Not enough data points", "n": len(df)}

        # Off-diagonal pairs, each with a two-sided p-value. With k metrics there
        # are k(k-1)/2 pairs tested at once, so a bare r over-reports — we attach
        # a Benjamini-Hochberg FDR-corrected 'significant' flag across the set.
        pairs = []
        raw_p: list[float | None] = []
        for i, m1 in enumerate(metrics):
            for j, m2 in enumerate(metrics):
                if i < j:
                    r, p, n = pearson_r_p(df[m1].to_numpy(), df[m2].to_numpy())
                    if r is None:
                        continue
                    raw_p.append(p)
                    pairs.append({
                        "metric_a": m1,
                        "metric_b": m2,
                        "correlation": round(float(r), 3),
                        "p_value": round(p, 4) if p is not None else None,
                        "n": n,
                        "strength": (
                            "strong" if abs(r) > 0.7
                            else "moderate" if abs(r) > 0.4
                            else "weak"
                        ),
                    })
        for pair, sig in zip(pairs, benjamini_hochberg(raw_p)):
            pair["significant"] = sig

        return {
            "n_days": len(df),
            "pairs": pairs,
            "note": (
                "p_value is the two-sided Pearson p; 'significant' is "
                "Benjamini-Hochberg FDR-corrected (q=0.05) across all pairs. "
                "Correlation is not causation — treat non-significant pairs as noise."
            ),
        }

    def run_full_anomaly_scan(self) -> list[AnomalyResult]:
        """Run anomaly detection on all baselined metrics."""
        baselines = self._memory.get_baselines()
        all_anomalies = []
        for metric in baselines:
            anomalies = self.detect_anomalies(metric, days=3, threshold_sigma=1.5)
            all_anomalies.extend(anomalies)
        return all_anomalies

    # ------------------------------------------------------------------
    # Multi-signal pattern detection
    # ------------------------------------------------------------------
    def detect_illness_signature(self, days: int = 5) -> dict[str, Any]:
        """Look for the RHR↑ + HRV↓ + respiration↑ pattern that precedes illness.

        Returns a verdict + per-day signal scores for the last `days` complete days.
        Based on Quer et al., 2021 (Nature Medicine).
        """
        from datetime import datetime, timedelta

        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=days + 1)).strftime("%Y-%m-%d")

        rhr_b = self._memory.get_baseline("restingHeartRate")
        hrv_b = self._memory.get_baseline("avgOvernightHrv")
        resp_b = self._memory.get_baseline("averageRespirationValue")

        if not (rhr_b and hrv_b and resp_b):
            return {
                "verdict": "insufficient_baseline",
                "message": "Need at least 30 days of baselines for RHR, HRV, and respiration."
            }

        rhr_mean, rhr_std = rhr_b.get("avg_30d"), rhr_b.get("std_7d")
        hrv_mean, hrv_std = hrv_b.get("avg_30d"), hrv_b.get("std_7d")
        resp_mean, resp_std = resp_b.get("avg_30d"), resp_b.get("std_7d")

        if not all([rhr_mean, hrv_mean, resp_mean, rhr_std, hrv_std, resp_std]):
            return {"verdict": "insufficient_baseline", "message": "Baseline statistics incomplete."}

        summaries = self._memory.get_daily_summaries_range(start, yesterday)
        summaries = [s for s in summaries if s.get("is_complete", True)]

        per_day = []
        flagged_days = 0

        for s in summaries[-days:]:
            rhr = s.get("restingHeartRate")
            hrv = s.get("avgOvernightHrv")
            resp = s.get("averageRespirationValue")
            if rhr is None or hrv is None or resp is None:
                continue

            rhr_z = (float(rhr) - rhr_mean) / rhr_std
            hrv_z = (float(hrv) - hrv_mean) / hrv_std
            resp_z = (float(resp) - resp_mean) / resp_std

            # Illness signature: RHR up, HRV down, respiration up
            triggered = (rhr_z > 0.8 and hrv_z < -0.8 and resp_z > 0.8)
            if triggered:
                flagged_days += 1

            per_day.append({
                "date": s["date"],
                "rhr": float(rhr),
                "rhr_z": round(rhr_z, 2),
                "hrv": float(hrv),
                "hrv_z": round(hrv_z, 2),
                "respiration": float(resp),
                "resp_z": round(resp_z, 2),
                "illness_signature": triggered,
            })

        # Verdict: 2+ consecutive flagged days = strong signal
        verdict = "clear"
        if flagged_days >= 2:
            verdict = "illness_likely"
        elif flagged_days == 1:
            verdict = "watch"

        return {
            "verdict": verdict,
            "flagged_days": flagged_days,
            "days_analyzed": len(per_day),
            "per_day": per_day,
            "research_citation": "Quer et al., 2021, Nature Medicine",
        }

    def detect_social_jet_lag(self, days: int = 21) -> dict[str, Any] | None:
        """Compare weekday vs weekend sleep timing variance."""
        from datetime import datetime, timedelta

        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        summaries = self._memory.get_daily_summaries_range(start, yesterday)

        # We only have sleep duration, not start/end times — so this is approximate
        weekday_sleep = []
        weekend_sleep = []
        for s in summaries:
            sleep_secs = s.get("sleepingSeconds") or s.get("sleepTimeSeconds")
            if sleep_secs is None:
                continue
            try:
                day = datetime.strptime(s["date"], "%Y-%m-%d").weekday()
            except ValueError:
                continue
            (weekend_sleep if day >= 5 else weekday_sleep).append(float(sleep_secs))

        if len(weekday_sleep) < 3 or len(weekend_sleep) < 2:
            return None

        weekday_mean = float(np.mean(weekday_sleep))
        weekend_mean = float(np.mean(weekend_sleep))
        diff_hours = abs(weekend_mean - weekday_mean) / 3600

        return {
            "weekday_sleep_hours": round(weekday_mean / 3600, 2),
            "weekend_sleep_hours": round(weekend_mean / 3600, 2),
            "diff_hours": round(diff_hours, 2),
            "n_weekdays": len(weekday_sleep),
            "n_weekends": len(weekend_sleep),
            "social_jet_lag": diff_hours > 1.0,
            "interpretation": (
                "Significant weekday/weekend variance — possible social jet lag impact"
                if diff_hours > 1.0
                else "Sleep duration is consistent across the week"
            ),
        }
