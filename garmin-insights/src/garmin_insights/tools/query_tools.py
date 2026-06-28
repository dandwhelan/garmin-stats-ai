"""LLM-callable query tools — Anthropic tool definitions for health data access."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from garmin_insights.db.sqlite_repo import SqliteRepo
from garmin_insights.db.memory import MemoryStore
from garmin_insights.tools.analysis_tools import AnalysisEngine

logger = logging.getLogger(__name__)


def _round_floats(obj: Any, ndigits: int = 1) -> Any:
    """Recursively round all floats in a nested dict/list to ndigits decimal places."""
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def _clean_records(records: list[dict]) -> list[dict]:
    """Strip None/NaN values and round floats before JSON serialisation.

    Null fields add tokens without informing Claude — omitting them cuts
    payload size by 20-30% on sparse tables. Floats are rounded to 1 d.p.;
    the model needs no more precision than that for health analytics.
    """
    return [
        _round_floats({k: v for k, v in row.items() if v is not None})
        for row in records
    ]


def _strip_zero_lifestyle(summaries: list[dict]) -> list[dict]:
    """Convert lifestyle dicts to a compact string list, dropping zero entries.

    {"Caffeine": {"status": 1, "value": 2.0}, "Alcohol": {"status": 0, "value": 0.0}}
    becomes ["Caffeine: 2"] — status=0/value=0 entries are dropped entirely.
    Binary occurrences (value=0 but status=1) become just the behavior name.
    """
    result = []
    for s in summaries:
        lf = s.get("lifestyle")
        if lf:
            items = []
            for behavior, v in lf.items():
                if v.get("status") == 0 and v.get("value") == 0.0:
                    continue
                val = v.get("value", 0.0)
                items.append(f"{behavior}: {val:g}" if val else behavior)
            s = {k: v for k, v in s.items() if k != "lifestyle"}
            if items:
                s["lifestyle"] = items
        result.append(s)
    return result


def _df_to_clean_json(df) -> str:
    """Convert a DataFrame to compact JSON with nulls stripped and floats rounded."""
    records = json.loads(df.to_json(orient="records"))
    return json.dumps(_clean_records(records))


def _fmt_race_time(seconds: float | int | None) -> str | None:
    """Format a race-prediction time (stored in seconds) as H:MM:SS / M:SS."""
    if seconds is None or seconds <= 0:
        return None
    s = int(round(float(seconds)))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _marker_series(df, value_col: str, scale: float = 1.0, ndp: int = 1, keep: int = 8) -> dict | None:
    """Collapse a time-indexed DataFrame column to a recent {date: value} dict.

    Slow-moving fitness markers update infrequently, so we keep only the most
    recent ``keep`` distinct readings (latest value per day wins).
    """
    if df is None or getattr(df, "empty", True) or value_col not in df.columns:
        return None
    d = df.reset_index() if df.index.name is not None else df
    series: dict = {}
    for row in d.to_dict(orient="records"):
        v = row.get(value_col)
        if v is None:
            continue
        date = str(row.get("time", ""))[:10]
        if not date:
            continue
        try:
            series[date] = round(float(v) * scale, ndp)
        except (TypeError, ValueError):
            continue
    if not series:
        return None
    return dict(sorted(series.items())[-keep:])


def _night_label(wake_date: str) -> str:
    """Sleep is keyed to the wake-up date — a record dated X covers the night that
    ENDED on the morning of X. Return a '<prev>→<wake>' span so the model never
    misattributes a sleep record to the wrong night (e.g. presenting an earlier
    night as 'last night')."""
    try:
        d = datetime.strptime(wake_date[:10], "%Y-%m-%d").date()
        return f"{(d - timedelta(days=1)).isoformat()}→{d.isoformat()}"
    except (TypeError, ValueError):
        return wake_date


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
        cleaned = _clean_records(_strip_zero_lifestyle(summaries))
        by_date = {s["date"]: {k: v for k, v in s.items() if k != "date"}
                   for s in cleaned if "date" in s}
        return json.dumps(by_date, default=str)

    def get_sleep_data(self, start_date: str, end_date: str) -> str:
        df = self._repo.query_sleep_summary(start_date, end_date)
        if df.empty:
            return json.dumps({"message": "No sleep data found for this range"})
        df = df.reset_index()
        df["time"] = df["time"].astype(str)
        records = json.loads(df.to_json(orient="records"))
        # Tag each night with the span it covers. The 'date' column is the wake-up
        # date, so "last night" is the record dated today — this label keeps the
        # model from shifting a record onto the wrong night.
        for r in records:
            d = r.get("date")
            if isinstance(d, str) and len(d) >= 10:
                r["night_of"] = _night_label(d)
        return json.dumps(_clean_records(records))

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
        # SQLite columns are snake_case (the Influx schema was camelCase). Using
        # the old camelCase names here silently dropped everything except
        # calories/distance, so the agent could not tell a run from a strength
        # session.
        keep_cols = [
            "activity_name", "activity_type", "average_hr", "max_hr",
            "calories", "distance", "elapsed_duration", "moving_duration",
        ]
        available = [c for c in keep_cols if c in df.columns]
        df = df[available].reset_index()
        df["time"] = df["time"].astype(str)
        return _df_to_clean_json(df)

    def get_body_composition(self, start_date: str, end_date: str) -> str:
        df = self._repo.query_body_composition(start_date, end_date)
        if df.empty:
            return json.dumps({"message": "No body composition data found"})
        df = df.reset_index()
        df["time"] = df["time"].astype(str)
        return _df_to_clean_json(df)

    def get_training_readiness(self, start_date: str, end_date: str) -> str:
        df = self._repo.query_training_readiness(start_date, end_date)
        if df.empty:
            return json.dumps({"message": "No training readiness data found"})
        df = df.reset_index()
        df["time"] = df["time"].astype(str)
        return _df_to_clean_json(df)

    def get_heat_acclimation(self, start_date: str, end_date: str) -> str:
        """Garmin heat (and altitude) acclimation percentage over a date range.

        Acclimation is how adapted the body is to training in heat — it builds
        with heat exposure and decays over ~1-2 weeks without it. Returns the
        heat/altitude acclimation percentage and trend per day. Sourced from the
        training_status table; empty if the device/account doesn't report it.
        """
        df = self._repo.query_training_status(start_date, end_date)
        if df.empty:
            return json.dumps({"message": "No training status data found"})
        cols = [c for c in (
            "heat_acclimation_percentage", "altitude_acclimation_percentage",
            "heat_trend", "altitude_trend", "current_altitude",
        ) if c in df.columns]
        if not cols:
            return json.dumps({"message": "No heat acclimation data available"})
        df = df.reset_index()
        df["time"] = df["time"].astype(str)
        keep = ["time"] + cols
        df = df[keep]
        # Drop rows where every acclimation field is null
        df = df.dropna(subset=cols, how="all")
        if df.empty:
            return json.dumps({"message": "No heat acclimation data recorded for this range"})
        return _df_to_clean_json(df)

    def get_menstrual_cycle(self, start_date: str, end_date: str) -> str:
        df = self._repo.query_menstrual_cycle(start_date, end_date)
        if df.empty:
            return json.dumps({"message": "No menstrual cycle data tracked for this window"})
        return _df_to_clean_json(df)

    def get_environment_data(self, start_date: str, end_date: str) -> str:
        """Daily weather + air quality + pollen for the user's home location.

        Returns an `available: false` envelope when the user has no
        environment_daily rows (HOME_LAT / HOME_LON not configured).
        Otherwise returns a date-keyed dict — each value has temperature,
        precipitation, humidity, UV, AQI, PM2.5, PM10, ozone, NO2 and the
        per-species pollen counts. Use this to explain RHR / HRV /
        respiration / sleep deviations as environmentally driven when
        heat (>28°C apparent), poor air quality (EU AQI >60 or PM2.5
        >25 µg/m³), or high pollen (>50 grains/m³) coincide with them —
        see the `*_environmental_*`, `heat_recovery_confounder`,
        `air_quality_recovery_confounder` and `allergy_next_day_rhr_systemic`
        rules in the knowledge base.
        """
        df = self._repo.query_environment(start_date, end_date)
        if df is None or df.empty:
            return json.dumps({
                "available": False,
                "message": "No environment data — HOME_LAT/HOME_LON not configured for this user",
            })
        # _query() may set a time index; reset so we serialise cleanly.
        if df.index.name is not None:
            df = df.reset_index()
        records = json.loads(df.to_json(orient="records"))
        cleaned = _clean_records(records)
        by_date = {
            r["date"]: {k: v for k, v in r.items()
                        if k not in ("date", "latitude", "longitude", "fetched_at")}
            for r in cleaned if "date" in r
        }
        return json.dumps({"available": True, "entries": by_date}, default=str)

    def get_fitness_markers(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        """Slow-moving fitness markers: VO2 max, fitness age, race predictions,
        endurance score and hill score.

        These update infrequently (the latest reading may be weeks old), so when
        no range is given we look back a full year and return the most recent
        trend per marker. Use this for questions about cardiorespiratory fitness,
        running-performance trajectory, VO2 max plateaus, fitness vs chronological
        age, and the grey-zone/endurance training rules in the knowledge base —
        none of which are in get_daily_metrics.
        """
        end = end_date or datetime.utcnow().strftime("%Y-%m-%d")
        if start_date:
            start = start_date
        else:
            start = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%d")

        out: dict[str, Any] = {}

        vo2_run = _marker_series(self._repo.query_vo2_max(start, end), "vo2_max_value")
        if vo2_run:
            out["vo2_max_running"] = vo2_run
        vo2_cyc = _marker_series(self._repo.query_vo2_max(start, end), "vo2_max_value_cycling")
        if vo2_cyc:
            out["vo2_max_cycling"] = vo2_cyc

        fa_df = self._repo.query_fitness_age(start, end)
        fa = _marker_series(fa_df, "fitness_age")
        if fa:
            out["fitness_age"] = fa
            # Latest snapshot with chronological + achievable for context
            d = fa_df.reset_index() if fa_df.index.name is not None else fa_df
            latest = d.sort_values("time").iloc[-1] if "time" in d.columns and not d.empty else None
            if latest is not None:
                snap = {k: latest.get(k) for k in
                        ("fitness_age", "chronological_age", "achievable_fitness_age")
                        if latest.get(k) is not None}
                if snap:
                    out["fitness_age_latest"] = _round_floats(snap)

        endurance = _marker_series(self._repo.query_endurance_score(start, end), "endurance_score", ndp=0)
        if endurance:
            out["endurance_score"] = endurance

        hill_df = self._repo.query_hill_score(start, end)
        if hill_df is not None and not hill_df.empty:
            hill = {}
            for col, label in (("overall_score", "overall"),
                               ("strength_score", "strength"),
                               ("endurance_score", "endurance")):
                s = _marker_series(hill_df, col, ndp=0)
                if s:
                    hill[label] = s
            if hill:
                out["hill_score"] = hill

        rp_df = self._repo.query_race_predictions(start, end)
        if rp_df is not None and not rp_df.empty:
            d = rp_df.reset_index() if rp_df.index.name is not None else rp_df
            preds: dict = {}
            for row in d.to_dict(orient="records"):
                date = str(row.get("time", ""))[:10]
                if not date:
                    continue
                day = {
                    "5k": _fmt_race_time(row.get("time_5k")),
                    "10k": _fmt_race_time(row.get("time_10k")),
                    "half": _fmt_race_time(row.get("time_half_marathon")),
                    "marathon": _fmt_race_time(row.get("time_marathon")),
                }
                day = {k: v for k, v in day.items() if v is not None}
                if day:
                    preds[date] = day
            if preds:
                out["race_predictions"] = dict(sorted(preds.items())[-8:])

        if not out:
            return json.dumps({"message": "No fitness-marker data (VO2 max / race "
                               "predictions / endurance / hill) for this range"})
        return json.dumps(out, default=str)

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
        # Strip null sub-fields and drop metrics where everything is null
        clean = {m: {k: v for k, v in vals.items() if v is not None}
                 for m, vals in baselines.items()}
        clean = {m: v for m, v in clean.items() if v}
        return json.dumps(_round_floats(clean), default=str)

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

    def save_daily_note(self, date: str, note: str) -> str:
        """Append to the user's free-text note for a day (never overwrites)."""
        self._memory.append_daily_note(date, note)
        return json.dumps({"saved": True, "date": date})

    def get_daily_notes(self, start_date: str, end_date: str) -> str:
        notes = self._memory.get_daily_notes_range(start_date, end_date)
        if not notes:
            return json.dumps({"message": "No daily notes recorded for this range"})
        return json.dumps(notes, default=str)


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
            "name": "get_heat_acclimation",
            "description": (
                "Query Garmin heat (and altitude) acclimation for a date range. "
                "Acclimation is how adapted the body is to exercising in heat — it "
                "is reported as a percentage that builds with heat exposure and "
                "decays over ~1-2 weeks without it (this is what 'how long it takes "
                "to adjust to outside temperature' refers to). Returns per-day "
                "heat/altitude acclimation percentage and trend. Empty if the "
                "user's device/account does not report acclimation."
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
            "name": "get_menstrual_cycle",
            "description": (
                "Query menstrual cycle data for a date range: cycle phase (menstrual/follicular/"
                "ovulation/luteal), day of cycle, flow level, symptoms, mood, and cycle length. "
                "Useful for correlating cycle phase with sleep, HRV, energy and training response. "
                "Returns 'No menstrual cycle data tracked' if the user doesn't use this feature."
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
            "name": "get_environment_data",
            "description": (
                "Query daily weather, air quality and pollen at the user's home "
                "location (Open-Meteo) for a date range. Returns date-keyed dict "
                "with temp_max/min/mean_c, apparent_temp_max_c, precipitation_mm, "
                "humidity_mean, uv_index_max, european_aqi, pm25, pm10, o3, no2, "
                "and per-species pollen (alder/birch/grass/mugwort/olive/ragweed). "
                "Returns `available: false` if the user has not configured "
                "HOME_LAT/HOME_LON. "
                "Call this when investigating recovery deviations — heat (>28°C "
                "apparent), poor air quality (EU AQI >60 or PM2.5 >25 µg/m³), and "
                "high pollen (>50 grains/m³) are research-validated confounders "
                "for RHR↑, HRV↓, respiration↑ and sleep fragmentation. See the "
                "`heat_recovery_confounder`, `air_quality_recovery_confounder`, "
                "`high_pollen_sleep_confounder`, `allergy_next_day_rhr_systemic` "
                "and `asthma_environmental_hr_marker` rules in the knowledge base."
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
        {
            "name": "save_daily_note",
            "description": (
                "Record the user's own free-text note about what they did on a "
                "specific day (e.g. 'hard 10k run, two coffees, poor sleep, "
                "stressful work deadline'). Use this whenever the user tells you "
                "what happened on a given day so it's attached to that date and "
                "available in future analysis. This APPENDS to the day's note — "
                "it never overwrites or erases text the user wrote by hand. "
                "Identical text won't be added twice."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {**_DATE_PROP, "description": "The day the note is about, YYYY-MM-DD."},
                    "note": {"type": "string", "description": "The free-text note content."},
                },
                "required": ["date", "note"],
            },
        },
        {
            "name": "get_fitness_markers",
            "description": (
                "Query slow-moving fitness markers Garmin updates infrequently: "
                "VO2 max (running + cycling), fitness age (vs chronological and "
                "achievable), race-time predictions (5k/10k/half/marathon), "
                "endurance score and hill score. Both date arguments are OPTIONAL "
                "— omit them to look back a full year and get the latest trend "
                "(the most recent reading may be weeks old, so a short window can "
                "return nothing). These metrics are NOT in get_daily_metrics. Use "
                "for cardiorespiratory-fitness questions, VO2 max plateaus "
                "(vo2_max_plateau rule), fitness-vs-chronological age "
                "(fitness_age_vs_chronological rule), and running-performance "
                "trajectory."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_date": {**_DATE_PROP, "description": "Optional start date YYYY-MM-DD. Defaults to 365 days ago."},
                    "end_date": {**_DATE_PROP, "description": "Optional end date YYYY-MM-DD. Defaults to today."},
                },
                "required": [],
            },
        },
        {
            "name": "get_daily_notes",
            "description": (
                "Retrieve the user's own free-text daily notes for a date range "
                "as a {date: note} map. These are the user's words about what they "
                "actually did each day — read them when interpreting metric "
                "deviations. (Notes are also merged inline into get_daily_metrics "
                "under a 'note' key.)"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_date": {**_DATE_PROP, "description": "Start date in YYYY-MM-DD format."},
                    "end_date": {**_DATE_PROP, "description": "End date in YYYY-MM-DD format."},
                },
                "required": ["start_date", "end_date"],
            },
            # Cache the entire tool definitions list — it never changes at runtime
            # and Anthropic charges for these ~2,500 tokens on every round otherwise.
            "cache_control": {"type": "ephemeral"},
        },
    ]
