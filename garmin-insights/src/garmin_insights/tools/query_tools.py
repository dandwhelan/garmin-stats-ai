"""LLM-callable query tools — Anthropic tool definitions for health data access."""

from __future__ import annotations

import json
import logging
from typing import Any

from garmin_insights.db.sqlite_repo import SqliteRepo
from garmin_insights.db.memory import MemoryStore
from garmin_insights.tools.analysis_tools import AnalysisEngine

logger = logging.getLogger(__name__)


class QueryToolHandler:
    """Dispatches Claude tool calls to the appropriate data functions."""

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
        summaries = self._memory.get_daily_summaries_range(start_date, end_date)
        if metrics:
            summaries = [
                {k: v for k, v in s.items() if k in metrics or k == "date"}
                for s in summaries
            ]
        return json.dumps(summaries, default=str)

    def get_sleep_data(self, start_date: str, end_date: str) -> str:
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
        summaries = self._memory.get_daily_summaries_range(start_date, end_date)

        target_behavior = behavior
        if behavior:
            all_behaviors: set[str] = set()
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
                    results.append({"date": s["date"], "behavior": target_behavior, **lifestyle[target_behavior]})
            else:
                for b, data in lifestyle.items():
                    results.append({"date": s["date"], "behavior": b, **data})
        return json.dumps(results, default=str)

    def get_activity_history(
        self,
        start_date: str,
        end_date: str,
        activity_type: str | None = None,
    ) -> str:
        df = self._repo.query_activity_summary(start_date, end_date, activity_type)
        if df.empty:
            return json.dumps({"message": "No activities found for this range"})
        keep_cols = [
            "activityName", "activityType", "averageHR", "maxHR",
            "calories", "distance", "elapsedDuration",
        ]
        available = [c for c in keep_cols if c in df.columns]
        df = df[available].reset_index()
        df["time"] = df["time"].astype(str)
        return df.to_json(orient="records")

    def get_body_composition(self, start_date: str, end_date: str) -> str:
        df = self._repo.query_body_composition(start_date, end_date)
        if df.empty:
            return json.dumps({"message": "No body composition data found"})
        df = df.reset_index()
        df["time"] = df["time"].astype(str)
        return df.to_json(orient="records")

    def get_training_readiness(self, start_date: str, end_date: str) -> str:
        df = self._repo.query_training_readiness(start_date, end_date)
        if df.empty:
            return json.dumps({"message": "No training readiness data found"})
        df = df.reset_index()
        df["time"] = df["time"].astype(str)
        return df.to_json(orient="records")

    # ------------------------------------------------------------------
    # Analysis tools
    # ------------------------------------------------------------------
    def compare_behavior_impact(self, behavior: str, metric: str, days: int = 30) -> str:
        result = self._analysis.compare_metric_with_behavior(behavior, metric, days)
        if result:
            return json.dumps(result.to_dict())
        return json.dumps({"message": "Not enough data for comparison"})

    def detect_metric_trend(self, metric: str, days: int = 14) -> str:
        result = self._analysis.detect_trend(metric, days)
        if result:
            return json.dumps(result.to_dict())
        return json.dumps({"message": "Not enough data for trend detection"})

    def find_anomalies(self, metric: str, days: int = 7) -> str:
        results = self._analysis.detect_anomalies(metric, days)
        return json.dumps([r.to_dict() for r in results])

    def get_metric_correlations(self, metrics: list[str], days: int = 30) -> str:
        result = self._analysis.compute_correlation_matrix(metrics, days)
        return json.dumps(result, default=str)

    def detect_illness_signature(self, days: int = 5) -> str:
        result = self._analysis.detect_illness_signature(days)
        return json.dumps(result, default=str)

    def detect_social_jet_lag(self, days: int = 21) -> str:
        result = self._analysis.detect_social_jet_lag(days)
        if result is None:
            return json.dumps({"message": "Not enough data to compute sleep timing variance"})
        return json.dumps(result, default=str)

    # ------------------------------------------------------------------
    # Memory tools
    # ------------------------------------------------------------------
    def get_my_baselines(self) -> str:
        baselines = self._memory.get_baselines()
        return json.dumps(baselines, default=str)

    def get_recent_insights(self, hours: int = 168) -> str:
        insights = self._memory.get_recent_insights(hours)
        return json.dumps(insights, default=str)

    def get_last_session_summary(self) -> str:
        session = self._memory.get_last_session()
        return json.dumps(session, default=str) if session else json.dumps({"message": "No previous sessions found"})

    def save_user_note(self, key: str, value: str) -> str:
        self._memory.set_profile(key, value)
        return json.dumps({"saved": True, "key": key})

    def get_user_profile(self) -> str:
        profile = self._memory.get_all_profile()
        return json.dumps(profile, default=str)


# ------------------------------------------------------------------
# Anthropic tool definitions
# ------------------------------------------------------------------

_DATE_PROP = {"type": "string", "description": "Date in YYYY-MM-DD format."}


def get_all_tools_anthropic(handler: QueryToolHandler) -> list[dict]:
    """Return all tools as Anthropic-format tool definitions."""
    return [
        {
            "name": "get_daily_metrics",
            "description": (
                "Query daily health metrics (RHR, stress, body battery, steps, sleep score, etc.) "
                "for a date range. Uses the fast cached summaries — call this first."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_date": {**_DATE_PROP, "description": "Start date in YYYY-MM-DD format."},
                    "end_date": {**_DATE_PROP, "description": "End date in YYYY-MM-DD format."},
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of specific metric names to return. If omitted, returns all.",
                    },
                },
                "required": ["start_date", "end_date"],
            },
        },
        {
            "name": "get_sleep_data",
            "description": (
                "Query detailed sleep metrics (sleep score, deep/REM/light sleep seconds, "
                "HRV, overnight stress, SpO2) for a date range."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_date": {**_DATE_PROP, "description": "Start date in YYYY-MM-DD format."},
                    "end_date": {**_DATE_PROP, "description": "End date in YYYY-MM-DD format."},
                },
                "required": ["start_date", "end_date"],
            },
        },
        {
            "name": "get_lifestyle_behaviors",
            "description": (
                "Query lifestyle journal entries (caffeine, alcohol, exercise, meals, etc.) "
                "for a date range. Optionally filter to a specific behavior."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_date": {**_DATE_PROP, "description": "Start date in YYYY-MM-DD format."},
                    "end_date": {**_DATE_PROP, "description": "End date in YYYY-MM-DD format."},
                    "behavior": {
                        "type": "string",
                        "description": "Optional behavior name to filter by (e.g. 'Alcohol', 'Late Caffeine'). Supports fuzzy matching.",
                    },
                },
                "required": ["start_date", "end_date"],
            },
        },
        {
            "name": "get_activity_history",
            "description": (
                "Query exercise/activity summaries (type, avg HR, max HR, calories, distance, duration) "
                "for a date range."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_date": {**_DATE_PROP, "description": "Start date in YYYY-MM-DD format."},
                    "end_date": {**_DATE_PROP, "description": "End date in YYYY-MM-DD format."},
                    "activity_type": {
                        "type": "string",
                        "description": "Optional activity type to filter (e.g. 'running', 'cycling', 'strength_training').",
                    },
                },
                "required": ["start_date", "end_date"],
            },
        },
        {
            "name": "get_body_composition",
            "description": "Query body composition data (weight, body fat %, muscle mass, BMI) for a date range.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_date": {**_DATE_PROP, "description": "Start date in YYYY-MM-DD format."},
                    "end_date": {**_DATE_PROP, "description": "End date in YYYY-MM-DD format."},
                },
                "required": ["start_date", "end_date"],
            },
        },
        {
            "name": "get_training_readiness",
            "description": "Query Garmin training readiness scores and contributing factors for a date range.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_date": {**_DATE_PROP, "description": "Start date in YYYY-MM-DD format."},
                    "end_date": {**_DATE_PROP, "description": "End date in YYYY-MM-DD format."},
                },
                "required": ["start_date", "end_date"],
            },
        },
        {
            "name": "compare_behavior_impact",
            "description": (
                "Compare a health metric on days with vs without a specific lifestyle behavior. "
                "Uses a statistical t-test to determine significance. "
                "Example: compare sleep score on alcohol vs non-alcohol nights."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "behavior": {
                        "type": "string",
                        "description": "Lifestyle behavior name (e.g. 'Alcohol', 'Late Caffeine', 'Stretching').",
                    },
                    "metric": {
                        "type": "string",
                        "description": "Health metric to compare (e.g. 'sleepScore', 'restingHeartRate', 'avgOvernightHrv').",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Number of days to analyze. Default 30.",
                    },
                },
                "required": ["behavior", "metric"],
            },
        },
        {
            "name": "detect_metric_trend",
            "description": (
                "Detect whether a health metric is trending up, down, or stable using linear regression. "
                "Returns slope per day and R-squared."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "description": "Health metric to analyze (e.g. 'restingHeartRate', 'avgOvernightHrv', 'sleepScore').",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Number of days to analyze. Default 14.",
                    },
                },
                "required": ["metric"],
            },
        },
        {
            "name": "find_anomalies",
            "description": (
                "Find recent anomalous values for a metric compared to the 30-day baseline. "
                "Returns z-scores and direction of each anomaly."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "description": "Health metric to check (e.g. 'restingHeartRate', 'sleepScore', 'bodyBatteryAtWakeTime').",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Number of recent days to scan for anomalies. Default 7.",
                    },
                },
                "required": ["metric"],
            },
        },
        {
            "name": "get_metric_correlations",
            "description": (
                "Compute Pearson correlations between multiple health metrics. "
                "Returns correlation pairs with strength assessments (strong/moderate/weak)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of metric names to correlate (e.g. ['sleepScore', 'stressPercentage', 'totalSteps']).",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Number of days to analyze. Default 30.",
                    },
                },
                "required": ["metrics"],
            },
        },
        {
            "name": "detect_illness_signature",
            "description": (
                "Check the last several days for the multi-signal illness pattern: "
                "elevated resting heart rate + depressed overnight HRV + elevated respiration "
                "rate (Quer et al. 2021). Returns a verdict ('clear', 'watch', or 'illness_likely') "
                "with per-day z-scores. Use this for 'am I getting sick?' or recovery questions."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of recent days to scan. Default 5.",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "detect_social_jet_lag",
            "description": (
                "Compare weekday vs weekend sleep duration to detect circadian misalignment "
                "('social jet lag'). Returns mean sleep duration for each and the variance "
                "in hours. Variance >1h is considered metabolically significant."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of recent days to analyse. Default 21.",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "get_my_baselines",
            "description": (
                "Retrieve the user's current baseline values for all tracked health metrics. "
                "Returns 7-day and 30-day averages, standard deviations, and latest values. "
                "Call this before making claims about whether a value is 'high' or 'low'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "get_recent_insights",
            "description": "Get previously discovered health insights saved in memory from the last N hours.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "How far back to look in hours. Default 168 (1 week).",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "get_last_session_summary",
            "description": "Recall what was discussed in the previous conversation session for continuity.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "save_user_note",
            "description": "Save a personal note, sensitivity, or preference to the user's profile for future reference.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "What this note is about (e.g. 'caffeine_sensitivity', 'sleep_goal', 'training_plan').",
                    },
                    "value": {
                        "type": "string",
                        "description": "The note content.",
                    },
                },
                "required": ["key", "value"],
            },
        },
        {
            "name": "get_user_profile",
            "description": "Retrieve all saved user preferences, sensitivities, and notes.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    ]
