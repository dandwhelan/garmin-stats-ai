"""LLM-callable query tools — Gemini function declarations for InfluxDB data access."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from garmin_insights.db.influxdb import InfluxRepo
from garmin_insights.db.memory import MemoryStore
from garmin_insights.tools.analysis_tools import AnalysisEngine

logger = logging.getLogger(__name__)


class QueryToolHandler:
    """Dispatches Gemini tool calls to the appropriate data functions.

    Each public method corresponds to a tool the LLM can call.
    The method signature + docstring is used by the Gemini SDK to auto-generate
    the function declaration schema.
    """

    def __init__(
        self,
        repo: SqliteRepo,
        memory: MemoryStore,
        analysis: AnalysisEngine,
    ) -> None:
        self._repo = repo
        self._memory = memory
        self._analysis = analysis

    # ------------------------------------------------------------------
    # Data query tools
    # ------------------------------------------------------------------
    def get_daily_metrics(
        self,
        start_date: str,
        end_date: str,
        metrics: list[str] | None = None,
    ) -> str:
        """Query daily health metrics (RHR, stress, body battery, steps, sleep, etc.) for a date range.

        Args:
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format.
            metrics: Optional list of specific metric names to return. If not given, returns all available.

        Returns:
            JSON string of daily metric values.
        """
        summaries = self._memory.get_daily_summaries_range(start_date, end_date)
        if metrics:
            summaries = [
                {k: v for k, v in s.items() if k in metrics or k == "date"}
                for s in summaries
            ]
        return json.dumps(summaries, default=str)

    def get_sleep_data(
        self,
        start_date: str,
        end_date: str,
    ) -> str:
        """Query sleep metrics (sleep score, deep/REM/light sleep, HRV, stress, SpO2) for a date range.

        Args:
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format.

        Returns:
            JSON string of sleep data per night.
        """
        df = self._repo.query_sleep_summary(start_date, end_date)
        if df.empty:
            return json.dumps({"message": "No sleep data found for this range"})
        df = df.reset_index()
        df["time"] = df["time"].astype(str)
        return df.to_json(orient="records")

    def get_lifestyle_behaviors(
        self,
        start_date: str,
        end_date: str,
        behavior: str | None = None,
    ) -> str:
        """Query lifestyle journal entries (caffeine, alcohol, exercise, meals, etc.) for a date range.

        Args:
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format.
            behavior: Optional specific behavior name to filter by.

        Returns:
            JSON string of lifestyle entries.
        """
        summaries = self._memory.get_daily_summaries_range(start_date, end_date)
        
        # If behavior is requested, resolve it using fuzzy matching first
        target_behavior = behavior
        if behavior:
            all_behaviors = set()
            for s in summaries:
                all_behaviors.update(s.get("lifestyle", {}).keys())
            
            canonical = self._analysis.find_matching_behavior(behavior, list(all_behaviors))
            if canonical:
                target_behavior = canonical
            else:
                return json.dumps({"message": f"Behavior '{behavior}' not found in data"})

        results = []
        for s in summaries:
            lifestyle = s.get("lifestyle", {})
            if target_behavior:
                if target_behavior in lifestyle:
                    results.append({
                        "date": s["date"],
                        "behavior": target_behavior,
                        **lifestyle[target_behavior],
                    })
            else:
                for b, data in lifestyle.items():
                    results.append({
                        "date": s["date"],
                        "behavior": b,
                        **data,
                    })
        return json.dumps(results, default=str)

    def get_activity_history(
        self,
        start_date: str,
        end_date: str,
        activity_type: str | None = None,
    ) -> str:
        """Query exercise/activity summaries (type, HR, calories, distance, duration) for a date range.

        Args:
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format.
            activity_type: Optional activity type to filter (e.g., 'running', 'cycling').

        Returns:
            JSON string of activity summaries.
        """
        df = self._repo.query_activity_summary(start_date, end_date, activity_type)
        if df.empty:
            return json.dumps({"message": "No activities found for this range"})
        # Keep only essential columns to minimize tokens
        keep_cols = [
            "activityName", "activityType", "averageHR", "maxHR",
            "calories", "distance", "elapsedDuration",
        ]
        available = [c for c in keep_cols if c in df.columns]
        df = df[available].reset_index()
        df["time"] = df["time"].astype(str)
        return df.to_json(orient="records")

    def get_body_composition(
        self,
        start_date: str,
        end_date: str,
    ) -> str:
        """Query body composition data (weight, body fat, muscle mass, BMI) for a date range.

        Args:
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format.

        Returns:
            JSON string of body composition readings.
        """
        df = self._repo.query_body_composition(start_date, end_date)
        if df.empty:
            return json.dumps({"message": "No body composition data found"})
        df = df.reset_index()
        df["time"] = df["time"].astype(str)
        return df.to_json(orient="records")

    def get_training_readiness(
        self,
        start_date: str,
        end_date: str,
    ) -> str:
        """Query training readiness scores and contributing factors for a date range.

        Args:
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format.

        Returns:
            JSON string of training readiness data.
        """
        df = self._repo.query_training_readiness(start_date, end_date)
        if df.empty:
            return json.dumps({"message": "No training readiness data found"})
        df = df.reset_index()
        df["time"] = df["time"].astype(str)
        return df.to_json(orient="records")

    # ------------------------------------------------------------------
    # Analysis tools
    # ------------------------------------------------------------------
    def compare_behavior_impact(
        self,
        behavior: str,
        metric: str,
        days: int = 30,
    ) -> str:
        """Compare a health metric on days with vs without a specific lifestyle behavior.

        Uses statistical t-test to determine if the difference is significant.

        Args:
            behavior: Lifestyle behavior name (e.g., 'Alcohol', 'Late Caffeine').
            metric: Health metric to compare (e.g., 'sleepScore', 'restingHeartRate').
            days: Number of days to analyze (default 30).

        Returns:
            JSON with mean values, difference, p-value, and significance.
        """
        result = self._analysis.compare_metric_with_behavior(behavior, metric, days)
        if result:
            return json.dumps(result.to_dict())
        return json.dumps({"message": "Not enough data for comparison"})

    def detect_metric_trend(
        self,
        metric: str,
        days: int = 14,
    ) -> str:
        """Detect whether a health metric is trending up, down, or stable.

        Uses linear regression over the specified window.

        Args:
            metric: Health metric to analyze (e.g., 'restingHeartRate', 'avgOvernightHrv').
            days: Number of days to analyze (default 14).

        Returns:
            JSON with trend direction, slope per day, and R-squared.
        """
        result = self._analysis.detect_trend(metric, days)
        if result:
            return json.dumps(result.to_dict())
        return json.dumps({"message": "Not enough data for trend detection"})

    def find_anomalies(
        self,
        metric: str,
        days: int = 7,
    ) -> str:
        """Find recent anomalous values for a metric compared to the 30-day baseline.

        Args:
            metric: Health metric to check (e.g., 'restingHeartRate', 'sleepScore').
            days: Number of recent days to scan (default 7).

        Returns:
            JSON array of anomalies with z-scores and directions.
        """
        results = self._analysis.detect_anomalies(metric, days)
        return json.dumps([r.to_dict() for r in results])

    def get_metric_correlations(
        self,
        metrics: list[str],
        days: int = 30,
    ) -> str:
        """Compute Pearson correlations between multiple health metrics.

        Args:
            metrics: List of metric names to correlate (e.g., ['sleepScore', 'stressPercentage', 'totalSteps']).
            days: Number of days to analyze (default 30).

        Returns:
            JSON with correlation pairs and strength assessments.
        """
        result = self._analysis.compute_correlation_matrix(metrics, days)
        return json.dumps(result, default=str)

    # ------------------------------------------------------------------
    # Memory tools
    # ------------------------------------------------------------------
    def get_my_baselines(self) -> str:
        """Retrieve the user's current baseline values for all tracked health metrics.

        Returns:
            JSON of baselines including 7-day and 30-day averages, standard deviations,
            and latest values for metrics like RHR, HRV, sleep score, stress, body battery.
        """
        baselines = self._memory.get_baselines()
        return json.dumps(baselines, default=str)

    def get_recent_insights(self, hours: int = 168) -> str:
        """Get previously discovered health insights from the last N hours.

        Args:
            hours: How far back to look (default 168 = 1 week).

        Returns:
            JSON array of recent insights with descriptions and dates.
        """
        insights = self._memory.get_recent_insights(hours)
        return json.dumps(insights, default=str)

    def get_last_session_summary(self) -> str:
        """Recall what was discussed in the previous conversation session.

        Returns:
            JSON with conversation summary and key findings.
        """
        session = self._memory.get_last_session()
        return json.dumps(session, default=str) if session else json.dumps({"message": "No previous sessions found"})

    def save_user_note(self, key: str, value: str) -> str:
        """Save a personal note, sensitivity, or preference to the user's profile.

        Args:
            key: What this note is about (e.g., 'caffeine_sensitivity', 'sleep_goal').
            value: The note content.

        Returns:
            Confirmation message.
        """
        self._memory.set_profile(key, value)
        return json.dumps({"saved": True, "key": key})

    def get_user_profile(self) -> str:
        """Retrieve all saved user preferences, sensitivities, and notes.

        Returns:
            JSON of all user profile entries.
        """
        profile = self._memory.get_all_profile()
        return json.dumps(profile, default=str)


def get_all_tools(handler: QueryToolHandler) -> list[callable]:
    """Return all tool methods for registration with the Gemini SDK."""
    return [
        handler.get_daily_metrics,
        handler.get_sleep_data,
        handler.get_lifestyle_behaviors,
        handler.get_activity_history,
        handler.get_body_composition,
        handler.get_training_readiness,
        handler.compare_behavior_impact,
        handler.detect_metric_trend,
        handler.find_anomalies,
        handler.get_metric_correlations,
        handler.get_my_baselines,
        handler.get_recent_insights,
        handler.get_last_session_summary,
        handler.save_user_note,
        handler.get_user_profile,
    ]
