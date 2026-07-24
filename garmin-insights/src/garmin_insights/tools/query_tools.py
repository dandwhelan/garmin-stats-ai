"""LLM-callable query tools — Anthropic tool definitions for health data access."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from garmin_insights.db.sqlite_repo import SqliteRepo
from garmin_insights.db.memory import MemoryStore
from garmin_insights.stats_utils import metabolic_age_years
from garmin_insights.tools.analysis_tools import AnalysisEngine

logger = logging.getLogger(__name__)


def _round_floats(obj: Any, ndigits: int = 1) -> Any:
    """Recursively round all floats in a nested dict/list to ndigits decimal
    places. Integral floats serialise as ints (52.0 → 52) — shaves the ".0"
    off thousands of values per payload at no information cost. NaN/Inf pass
    through unchanged (is_integer() is False for them)."""
    if isinstance(obj, float):
        r = round(obj, ndigits)
        return int(r) if r.is_integer() else r
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


# Lifestyle-journal categories that represent self-reported STATES / outcomes /
# confounders (things that happen TO the user — illness, injury, migraines,
# allergy/asthma symptoms, low morning energy, work incidents, travel) rather
# than interventions the user actively chose to do. Used to stop a downstream
# LLM from treating an outcome like "Asthma symptoms" as a *cause* of other
# metric deviations. LIFESTYLE / SELF_CARE / SLEEP_RELATED / TREATMENTS are
# genuine actions and stay in the behaviour bucket.
STATE_LIFESTYLE_CATEGORIES = frozenset({"LIFE_STATUS", "CUSTOM", "CUSTOM_SLEEP_RELATED"})


def split_lifestyle_by_category(
    summaries: list[dict],
    category_map: dict[str, str],
) -> list[dict]:
    """Like ``_strip_zero_lifestyle`` but partition each day's active entries
    into ``lifestyle`` (interventions the user did) and ``states_symptoms``
    (outcomes / confounders), using each behaviour's lifestyle_journal category.

    ``category_map`` maps behaviour name -> category string. Behaviours whose
    category is in :data:`STATE_LIFESTYLE_CATEGORIES` go to ``states_symptoms``;
    everything else (including unknown/missing categories) stays in
    ``lifestyle``. Zero/absent entries are dropped exactly as
    ``_strip_zero_lifestyle`` does.
    """
    result = []
    for s in summaries:
        lf = s.get("lifestyle")
        s = {k: v for k, v in s.items() if k != "lifestyle"}
        if lf:
            actions, states = [], []
            for behavior, v in lf.items():
                if v.get("status") == 0 and v.get("value") == 0.0:
                    continue
                val = v.get("value", 0.0)
                label = f"{behavior}: {val:g}" if val else behavior
                if category_map.get(behavior, "") in STATE_LIFESTYLE_CATEGORIES:
                    states.append(label)
                else:
                    actions.append(label)
            if actions:
                s["lifestyle"] = actions
            if states:
                s["states_symptoms"] = states
        result.append(s)
    return result


def aggregate_workouts(activities: list[dict]) -> list[dict]:
    """Collapse per-session workout entries into per-type totals — a compact
    training-volume digest so a portable prompt doesn't depend on the model
    summing dozens of raw sessions itself.

    Each input entry is a ``build_portable_prompt`` activity dict
    (``type`` / ``min`` / ``km`` / ``kcal``). Returns one row per activity type
    with session count and summed duration / distance / calories, sorted by
    total minutes descending. Empty distance/calorie totals are dropped.
    """
    by_type: dict[str, dict] = {}
    for a in activities:
        t = a.get("type") or "Unknown"
        agg = by_type.setdefault(
            t, {"type": t, "sessions": 0, "min": 0.0, "km": 0.0, "kcal": 0}
        )
        agg["sessions"] += 1
        if a.get("min"):
            agg["min"] += float(a["min"])
        if a.get("km"):
            agg["km"] += float(a["km"])
        if a.get("kcal"):
            agg["kcal"] += int(a["kcal"])
    out = []
    for agg in by_type.values():
        agg["min"] = round(agg["min"], 1)
        agg["km"] = round(agg["km"], 2)
        if not agg["km"]:
            agg.pop("km")
        if not agg["kcal"]:
            agg.pop("kcal")
        out.append(agg)
    out.sort(key=lambda r: r.get("min", 0), reverse=True)
    return out


def _truncate_long_lists(obj: Any, keep: int = 45) -> Any:
    """Recursively cap list lengths in a nested payload, keeping the LAST
    ``keep`` entries (analytics lists are chronological, so the tail is the
    most recent data). A marker string notes how much was dropped — dashboard
    analytics can carry 90+ per-day rows, which is pure token waste in a tool
    result."""
    if isinstance(obj, list):
        if len(obj) > keep:
            omitted = len(obj) - keep
            return [f"... {omitted} earlier entries omitted ..."] + [
                _truncate_long_lists(v, keep) for v in obj[-keep:]
            ]
        return [_truncate_long_lists(v, keep) for v in obj]
    if isinstance(obj, dict):
        return {k: _truncate_long_lists(v, keep) for k, v in obj.items()}
    return obj


def hourly_intraday_digest(df: pd.DataFrame, date: str, agg: str = "mean") -> dict | None:
    """Collapse a timestamped intraday series to an hourly digest for one
    LOCAL calendar day.

    ``df`` is time-indexed with a single ``value`` column (UTC timestamps —
    naive ones are treated as UTC). Returns hour-keyed stats plus the day's
    peak/low sample, or None when the day has no valid samples. Negative
    values are Garmin's "unmeasured" sentinels (-1/-2 in stress) and are
    dropped.
    """
    if df is None or df.empty or "value" not in df.columns:
        return None
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")
    local = idx.tz_convert(datetime.now().astimezone().tzinfo)
    frame = pd.DataFrame(
        {"value": pd.to_numeric(df["value"], errors="coerce").values}, index=local
    ).dropna()
    frame = frame[frame["value"] >= 0]
    frame = frame[frame.index.strftime("%Y-%m-%d") == date]
    if frame.empty:
        return None

    grouped = frame.groupby(frame.index.hour)["value"]
    hourly: dict[str, Any] = {}
    if agg == "sum":
        for hour, total in grouped.sum().items():
            hourly[f"{int(hour):02d}:00"] = int(round(float(total)))
        peak_hour = max(hourly, key=hourly.get)
        extremes = {"peak_hour": {"hour": peak_hour, "sum": hourly[peak_hour]}}
    else:
        stats = grouped.agg(["mean", "min", "max"])
        for hour, row in stats.iterrows():
            hourly[f"{int(hour):02d}:00"] = {
                "avg": float(row["mean"]), "min": float(row["min"]), "max": float(row["max"]),
            }
        t_peak = frame["value"].idxmax()
        t_low = frame["value"].idxmin()
        extremes = {
            "peak": {"value": float(frame["value"].max()), "time": t_peak.strftime("%H:%M")},
            "low": {"value": float(frame["value"].min()), "time": t_low.strftime("%H:%M")},
        }
    return _round_floats({
        "date": date,
        "n_samples": int(len(frame)),
        "hourly": hourly,
        **extremes,
    })


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


def _marker_series(df, value_col: str, scale: float = 1.0, ndp: int = 1, keep: int = 8,
                   transform=None) -> dict | None:
    """Collapse a time-indexed DataFrame column to a recent {date: value} dict.

    Slow-moving fitness markers update infrequently, so we keep only the most
    recent ``keep`` distinct readings (latest value per day wins).

    ``transform`` (applied before ``scale``) lets callers normalise units with
    a guard — e.g. stats_utils.to_kg — instead of an unconditional divide.
    """
    if df is None or getattr(df, "empty", True) or value_col not in df.columns:
        return None
    d = df.reset_index() if df.index.name is not None else df
    series: dict = {}
    for row in d.to_dict(orient="records"):
        v = row.get(value_col)
        if v is None:
            continue
        # Prefer a repo-computed local `date` column (body_composition rows
        # carry one — a UTC time slice mislabels post-midnight BST readings).
        date = str(row.get("date") or row.get("time", ""))[:10]
        if not date:
            continue
        try:
            fv = float(v)
            if transform is not None:
                fv = transform(fv)
            fv = round(fv * scale, ndp)
        except (TypeError, ValueError):
            continue
        # pandas rows carry NaN (not None) for missing values — e.g. body-fat
        # on a watch-entered weight-only day. NaN is invalid JSON; skip it.
        if fv != fv:
            continue
        series[date] = fv
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


# Common valid metric names, shown as examples when a proposed experiment
# names a metric that isn't in the user's baselines or recent summaries.
_EXAMPLE_METRICS = (
    "sleepScore", "deepSleepSeconds", "remSleepSeconds", "lightSleepSeconds",
    "sleepTimeSeconds", "awakeSleepSeconds", "avgOvernightHrv",
    "restingHeartRate", "bodyBatteryAtWakeTime", "averageRespirationValue",
    "avgStressLevel", "stressPercentage", "totalSteps", "bodyBatteryChange",
    "restStressDuration",
)


def _experiment_verdict(
    result: dict, expected_direction: str, min_days_per_arm: int
) -> tuple[str, str]:
    """Turn an evaluate_experiment result into a verdict + one-line rationale.

    Verdicts: inconclusive_insufficient_data (an arm is still too small),
    supported (significant in the predicted direction), opposite_effect
    (significant but the wrong way), not_supported_yet (no significant
    difference). Significance is the raw two-sided Welch p at 0.05 — n-of-1,
    so no multiple-comparison correction is applied.
    """
    n_on = result.get("n_on", 0) or 0
    n_off = result.get("n_off", 0) or 0
    if not result.get("sufficient_data"):
        need_on = max(0, min_days_per_arm - n_on)
        need_off = max(0, min_days_per_arm - n_off)
        parts = []
        if need_on:
            parts.append(f"{need_on} more day(s) WITH the behaviour")
        if need_off:
            parts.append(f"{need_off} more day(s) WITHOUT it")
        detail = (
            "Need " + " and ".join(parts) + f" (target {min_days_per_arm} per arm)."
            if parts
            else f"Need at least {min_days_per_arm} paired days per arm."
        )
        return "inconclusive_insufficient_data", detail

    p = result.get("p_value")
    diff = result.get("difference")
    if p is not None and p < 0.05 and diff is not None:
        matches = (
            (expected_direction == "increase" and diff > 0)
            or (expected_direction == "decrease" and diff < 0)
        )
        if matches:
            return "supported", (
                f"Significant change in the predicted direction "
                f"(p={p:.3g}, difference {diff:+.2f})."
            )
        return "opposite_effect", (
            f"Significant change, but OPPOSITE to the prediction "
            f"(p={p:.3g}, difference {diff:+.2f})."
        )
    p_txt = "n/a" if p is None else f"{p:.3g}"
    return "not_supported_yet", (
        f"No statistically significant difference yet (p={p_txt}); keep logging."
    )


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
        # Dashboard analytics services, built lazily on first use — most chat
        # turns never touch them, and constructing them here would couple every
        # agent start-up to the web package.
        self._lifestyle_service = None
        self._viz_service = None

    def _get_lifestyle_service(self):
        if self._lifestyle_service is None:
            from garmin_insights.web.lifestyle_viz import LifestyleService
            self._lifestyle_service = LifestyleService(self._repo.db_path)
        return self._lifestyle_service

    def _get_viz_service(self):
        if self._viz_service is None:
            from garmin_insights.web.visualizations import VisualizationService
            self._viz_service = VisualizationService(self._repo.db_path)
        return self._viz_service

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
        # session. HR zones / speed / power / running dynamics are kept too —
        # zone seconds exist on every activity and the grey_zone_training KB
        # rule is unanswerable without them (nulls are stripped, so the sparse
        # dynamics fields cost nothing on activities that lack them).
        keep_cols = [
            "activity_name", "activity_type", "average_hr", "max_hr",
            "calories", "distance", "elapsed_duration", "moving_duration",
            "average_speed", "max_speed",
            "hr_time_in_zone_1", "hr_time_in_zone_2", "hr_time_in_zone_3",
            "hr_time_in_zone_4", "hr_time_in_zone_5",
            "avg_power", "max_power", "norm_power",
            "avg_run_cadence", "avg_stride_length",
            "avg_vertical_oscillation", "avg_vertical_ratio",
            "avg_ground_contact_time",
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
        # Garmin stores masses in grams; convert to kg so the model never has
        # to guess units (the >1000 guard skips rows already in kg).
        for col in ("weight", "muscle_mass", "bone_mass"):
            if col in df.columns:
                df[col] = df[col].where(df[col] <= 1000, df[col] / 1000.0)
        # Garmin returns metabolic age as a ms duration — normalise to years
        # (guarded, so rows already stored in years pass through). NaN-safe:
        # v == v is False for NaN.
        if "metabolic_age" in df.columns:
            df["metabolic_age"] = df["metabolic_age"].apply(
                lambda v: metabolic_age_years(v) if v == v else v)
        df = df.rename(columns={"weight": "weight_kg", "muscle_mass": "muscle_mass_kg",
                                "bone_mass": "bone_mass_kg", "body_fat": "body_fat_pct",
                                "body_water": "body_water_pct",
                                "metabolic_age": "metabolic_age_years"})
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
        # environment_daily deliberately holds a few FUTURE days of Open-Meteo
        # forecast (the dashboard's pollen chart shows them as such). The
        # model has no way to tell forecast from measurement, so clamp the
        # tool to completed history — otherwise "this week" queries quantify
        # exposure on days that haven't happened yet.
        today = datetime.now().strftime("%Y-%m-%d")
        clamped = end_date > today
        if clamped:
            end_date = today
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
        payload: dict = {"available": True, "entries": by_date}
        if clamped:
            payload["note"] = ("end_date clamped to today — later rows are weather "
                               "forecasts, not measurements")
        return json.dumps(payload, default=str)

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
        end = end_date or datetime.now().strftime("%Y-%m-%d")
        if start_date:
            start = start_date
        else:
            start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

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

    def get_training_status(self, start_date: str, end_date: str) -> str:
        """Garmin training status + load: status phrase, weekly load, ACWR,
        acute/chronic load, fitness trend, and heat/altitude acclimation.
        Latest reading per day (Garmin can report several times a day)."""
        df = self._repo.query_training_status(start_date, end_date)
        if df.empty:
            return json.dumps({"message": "No training status data found — the "
                               "device/account may not report training load"})
        cols = [c for c in (
            "training_status", "training_status_feedback_phrase",
            "weekly_training_load", "fitness_trend", "acwr_percent",
            "daily_training_load_acute", "daily_training_load_chronic",
            "daily_acute_chronic_workload_ratio",
            "heat_acclimation_percentage", "altitude_acclimation_percentage",
        ) if c in df.columns]
        d = df.reset_index() if df.index.name is not None else df
        by_date: dict[str, dict] = {}
        for row in sorted(d.to_dict(orient="records"), key=lambda r: str(r.get("time", ""))):
            day = str(row.get("time", ""))[:10]
            vals = {k: row[k] for k in cols
                    if row.get(k) is not None and row.get(k) == row.get(k)}
            if day and vals:
                by_date[day] = vals
        if not by_date:
            return json.dumps({"message": "No training status values recorded for this range"})
        # 2 d.p. — ACWR lives in 0.8-1.3, where 1 d.p. erases the signal
        # (0.95 and 1.04 are different training states, both round to ~1).
        return json.dumps(_round_floats(dict(sorted(by_date.items())), ndigits=2), default=str)

    def get_intraday_data(self, date: str, metric: str = "stress") -> str:
        """Hourly digest of one day's intraday samples for a metric."""
        try:
            df = self._repo.query_intraday_series(metric, date)
        except ValueError:
            return json.dumps({
                "error": f"unknown metric '{metric}'",
                "available_metrics": sorted(SqliteRepo.INTRADAY_SERIES),
            })
        agg = SqliteRepo.INTRADAY_SERIES[metric][2]
        digest = hourly_intraday_digest(df, date, agg)
        if digest is None:
            return json.dumps({"message": f"No intraday {metric} samples recorded for {date}"})
        return json.dumps({"metric": metric, **digest}, default=str)

    def get_lifestyle_analytics(self, analytic: str, days: int = 60) -> str:
        """Run one of the dashboard's precomputed lifestyle analytics."""
        allowed = {
            "sleep_regularity", "social_jet_lag", "caffeine_cutoff",
            "behavior_dose_response", "behavior_recovery_cost",
            "stress_resilience", "body_battery_decay", "illness_radar",
            "inflammation_index", "recovery_debt", "habit_half_life",
            "step_distribution", "who_intensity_target",
            "stress_hour_fingerprint", "stress_trigger_leaderboard",
            "research_signal_scorecard",
        }
        if analytic not in allowed:
            return json.dumps({"error": f"unknown analytic '{analytic}'",
                               "available": sorted(allowed)})
        days = max(14, min(int(days), 180))
        today = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        svc = self._get_lifestyle_service()
        try:
            if analytic == "habit_half_life":
                result = svc.habit_half_life(today, lookback_days=days)
            else:
                result = getattr(svc, analytic)(start, today)
        except Exception as e:
            logger.error("Lifestyle analytic %s failed: %s", analytic, e)
            return json.dumps({"error": f"analytic '{analytic}' failed: {e}"})
        payload = _round_floats(_truncate_long_lists(result))
        return json.dumps({"analytic": analytic, "window_days": days,
                           "result": payload}, default=str)

    def get_behavior_root_cause(self, behavior: str, days: int = 180,
                                lookback_hours: int = 48) -> str:
        """Per-event root-cause scan for a logged behavior (e.g. 'Migraines')."""
        days = max(14, min(int(days), 365))
        today = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            result = self._get_viz_service().behavior_root_cause(
                behavior, start, today, lookback_hours=lookback_hours
            )
        except Exception as e:
            logger.error("behavior_root_cause failed: %s", e)
            return json.dumps({"error": f"root-cause scan failed: {e}"})
        return json.dumps(_round_floats(_truncate_long_lists(result)), default=str)

    def get_bedroom_environment(self, start_date: str, end_date: str) -> str:
        """Home Assistant daily sensor aggregates (e.g. bedroom temperature)."""
        df = self._repo.query_ha_sensors(start_date, end_date)
        if df is None or df.empty:
            return json.dumps({
                "available": False,
                "message": "No Home Assistant sensor data — HA integration not configured",
            })
        if df.index.name is not None:
            df = df.reset_index()
        sensors: dict[str, dict] = {}
        for row in df.to_dict(orient="records"):
            entity = row.get("entity_id")
            day = str(row.get("date", ""))[:10]
            if not entity or not day:
                continue
            ent = sensors.setdefault(str(entity), {"unit": row.get("unit"), "days": {}})
            vals = {label: row[col] for label, col in (
                ("overnight", "overnight_mean"), ("mean", "mean_value"),
                ("min", "min_value"), ("max", "max_value"),
            ) if row.get(col) is not None and row.get(col) == row.get(col)}
            if vals:
                ent["days"][day] = vals
        sensors = {k: v for k, v in sensors.items() if v["days"]}
        if not sensors:
            return json.dumps({"available": False,
                               "message": "No sensor values recorded for this range"})
        return json.dumps(_round_floats({
            "available": True,
            "note": "overnight = 22:00-08:00 mean — the value relevant to sleep quality",
            "sensors": sensors,
        }), default=str)

    def get_environment_forecast(self) -> str:
        """Open-Meteo FORECAST rows for today + coming days (planning only)."""
        today = datetime.now().strftime("%Y-%m-%d")
        end = (datetime.now() + timedelta(days=6)).strftime("%Y-%m-%d")
        df = self._repo.query_environment(today, end)
        if df is None or df.empty:
            return json.dumps({
                "available": False,
                "message": "No environment data — HOME_LAT/HOME_LON not configured for this user",
            })
        if df.index.name is not None:
            df = df.reset_index()
        keys = (
            "temp_max_c", "temp_min_c", "apparent_temp_max_c",
            "precipitation_mm", "uv_index_max", "european_aqi", "pm25",
            "pollen_alder", "pollen_birch", "pollen_grass",
            "pollen_mugwort", "pollen_olive", "pollen_ragweed",
        )
        by_date = {}
        for row in df.to_dict(orient="records"):
            d = str(row.get("date", ""))[:10]
            if not d:
                continue
            by_date[d] = {
                k: row[k] for k in keys
                if row.get(k) is not None and row.get(k) == row.get(k)
                and not (k.startswith("pollen_") and row[k] in (0, 0.0))
            }
        return json.dumps(_round_floats({
            "available": True,
            "note": ("FORECAST data, not measurements — use only for "
                     "planning/warnings (e.g. high-pollen or heat days ahead), "
                     "never to explain metrics that already happened. "
                     f"Rows after {today} are Open-Meteo forecasts; today's row "
                     "mixes measurement and forecast."),
            "entries": dict(sorted(by_date.items())),
        }), default=str)

    # ------------------------------------------------------------------
    # Analysis tools
    # ------------------------------------------------------------------
    def scan_all_anomalies(self) -> str:
        """Anomaly scan across every baselined metric (last 3 days vs 30-day
        baseline) — one call instead of naming metrics individually."""
        results = self._analysis.run_full_anomaly_scan()
        if not results:
            return json.dumps({"message": "No anomalies vs 30-day baselines in the last 3 days"})
        return json.dumps(_round_floats([r.to_dict() for r in results]), default=str)

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
    # Experiment tools (n-of-1 personal behaviour trials)
    # ------------------------------------------------------------------
    def _known_metric_names(self) -> set[str]:
        """Metrics the user actually has: baselined metrics + any numeric field
        seen in the last 30 days of summaries. Used to validate an experiment's
        target metric so it can't register something un-evaluable."""
        names = set(self._memory.get_baselines().keys())
        today = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        for s in self._memory.get_daily_summaries_range(start, today):
            for k, v in s.items():
                if k in ("date", "lifestyle", "note", "is_complete"):
                    continue
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    names.add(k)
        return names

    def _behaviors_seen(self, days: int = 90) -> set[str]:
        today = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        seen: set[str] = set()
        for s in self._memory.get_daily_summaries_range(start, today):
            seen.update(s.get("lifestyle", {}).keys())
        return seen

    def start_experiment(
        self,
        name: str,
        hypothesis: str,
        behavior: str,
        metric: str,
        expected_direction: str = "increase",
        lag_days: int = 1,
        min_days_per_arm: int = 5,
    ) -> str:
        """Register an n-of-1 experiment tying a lifestyle behaviour to a metric."""
        direction = (expected_direction or "").strip().lower()
        if direction not in ("increase", "decrease"):
            return json.dumps({
                "error": "expected_direction must be 'increase' or 'decrease'.",
            })

        known = self._known_metric_names()
        if metric not in known:
            examples = sorted(known)[:15] or list(_EXAMPLE_METRICS)
            return json.dumps({
                "error": f"unknown metric '{metric}' — it is not in the user's "
                         "baselines or the last 30 days of summaries.",
                "example_metrics": examples,
            })

        # Reject a duplicate name up front (the column is globally UNIQUE).
        existing = self._memory.get_experiment(name)
        if existing:
            if existing.get("status") == "active":
                return json.dumps({
                    "error": f"An active experiment named '{name}' already exists. "
                             "Evaluate or conclude it, or choose a different name.",
                    "existing": _round_floats(existing),
                })
            return json.dumps({
                "error": f"An experiment named '{name}' already exists "
                         f"(status: {existing.get('status')}). Choose a different name.",
            })

        # Resolve the behaviour against what's been logged; warn (don't fail)
        # when it hasn't been logged yet so the user can start before day one.
        canonical = self._analysis.find_matching_behavior(
            behavior, list(self._behaviors_seen(90))
        )
        resolved = canonical or behavior
        warning = None
        if not canonical:
            warning = (
                f"behavior not yet logged — log '{behavior}' in the Garmin Connect "
                "lifestyle journal on days you do it, and skip logging on days you don't"
            )

        try:
            self._memory.create_experiment(
                name=name, hypothesis=hypothesis, behavior=resolved, metric=metric,
                expected_direction=direction, lag_days=int(lag_days),
                min_days_per_arm=int(min_days_per_arm),
            )
        except sqlite3.IntegrityError:
            return json.dumps({
                "error": f"An experiment named '{name}' already exists.",
            })

        lag_txt = ("compared same-day" if int(lag_days) == 0
                   else f"paired with the metric {int(lag_days)} day(s) later "
                        "(the night it affects)")
        protocol = (
            f"Log '{resolved}' in the Garmin Connect lifestyle journal every day it "
            f"happens, and leave it unlogged on days it doesn't. Aim for at least "
            f"{int(min_days_per_arm)} days with and {int(min_days_per_arm)} days without, "
            f"then evaluate after ~2-3 weeks. Each behaviour day is {lag_txt}."
        )
        payload: dict[str, Any] = {
            "created": True,
            "experiment": _round_floats(self._memory.get_experiment(name)),
            "protocol": protocol,
        }
        if warning:
            payload["warning"] = warning
        return json.dumps(payload, default=str)

    def get_experiments(self, status: str = "active") -> str:
        """List experiments by status (active/completed/abandoned/all)."""
        status = (status or "active").strip().lower()
        if status not in ("active", "completed", "abandoned", "all"):
            return json.dumps({
                "error": "status must be one of active, completed, abandoned, all.",
            })
        rows = self._memory.get_experiments(None if status == "all" else status)
        if not rows:
            return json.dumps({"message": f"No {status} experiments."})
        today = datetime.now().date()
        for r in rows:
            if r.get("status") == "active" and r.get("started_at"):
                try:
                    started = datetime.strptime(str(r["started_at"])[:10], "%Y-%m-%d").date()
                    r["days_elapsed"] = max(0, (today - started).days)
                except ValueError:
                    pass
        return json.dumps(_round_floats(rows), default=str)

    def evaluate_experiment(
        self, name: str, conclude: bool = False, abandon: bool = False
    ) -> str:
        """Evaluate (or abandon/conclude) a registered experiment."""
        row = self._memory.get_experiment(name)
        if row is None:
            active = [r["name"] for r in self._memory.get_experiments("active")]
            return json.dumps({
                "error": f"No experiment named '{name}'.",
                "active_experiments": active,
            })

        if abandon:
            self._memory.abandon_experiment(row["name"])
            return json.dumps({
                "abandoned": True,
                "name": row["name"],
                "message": f"Experiment '{row['name']}' marked abandoned.",
            })

        min_days = int(row.get("min_days_per_arm") or 5)
        lag_days = int(row.get("lag_days") or 0)
        start_date = str(row.get("started_at", ""))[:10]
        expected_direction = row.get("expected_direction") or "increase"

        result = self._analysis.evaluate_experiment(
            row["behavior"], row["metric"], start_date,
            lag_days=lag_days, min_days_per_arm=min_days,
        )
        if result is None:
            return json.dumps({
                "name": row["name"],
                "metric": row["metric"],
                "message": (f"Metric '{row['metric']}' has no data since {start_date} "
                            "— nothing to evaluate yet."),
            })

        # Verdict from the RAW result, before rounding (2 d.p. on p is fine for
        # display but the < 0.05 test must see the unrounded value).
        verdict, detail = _experiment_verdict(result, expected_direction, min_days)
        caveats = [
            "Single n-of-1 comparison — no multiple-comparison correction applied.",
            "Non-randomized: you chose which days to do the behaviour, so day-of-week, "
            "motivation and co-occurring habits can travel with it.",
            "Confounders are not controlled — sleep debt, alcohol, stress, illness, "
            "travel and environment (heat / air quality / pollen) all move these metrics.",
            "The metric is a device estimate (e.g. Garmin sleep-stage / HRV figures), "
            "not a clinical measurement.",
            "This is an association in your own personal data, not clinical evidence.",
        ]

        # 2 d.p. — 1 d.p. would collapse p=0.04 vs 0.4 (mirrors get_training_status).
        result = _round_floats(result, ndigits=2)
        payload: dict[str, Any] = {
            "name": row["name"],
            "hypothesis": row["hypothesis"],
            "expected_direction": expected_direction,
            "verdict": verdict,
            "verdict_detail": detail,
            "result": result,
            "caveats": caveats,
        }
        if conclude:
            self._memory.conclude_experiment(row["name"], verdict, result)
            payload["concluded"] = True
        else:
            self._memory.record_experiment_result(row["name"], result)
            payload["interim"] = True
        return json.dumps(payload, default=str)


# ------------------------------------------------------------------
# Anthropic tool definitions
# ------------------------------------------------------------------

_DATE_PROP = {"type": "string", "description": "Date in YYYY-MM-DD format."}


def get_all_tools_anthropic(handler: QueryToolHandler) -> list[dict]:
    """Return all tools as Anthropic-format tool definitions.

    The LAST definition gets a cache_control marker (added programmatically at
    the end of this function) so the whole list is prompt-cached — Anthropic
    bills these ~4k tokens on every round otherwise.
    """
    tools = [
        {
            "name": "get_daily_metrics",
            "description": (
                "Query daily health metrics (RHR, stress, body battery, steps, sleep score, etc.) "
                "for a date range. Uses the fast cached summaries — call this first. "
                "Note: averageSpo2 = daytime average SpO2 %; averageSpO2Value = overnight "
                "(sleep) average SpO2 % — similar names, different measurement windows."
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
                "Query exercise/activity summaries for a date range: type, avg/max "
                "HR, calories, distance, durations, avg/max speed (m/s), "
                "hr_time_in_zone_1..5 (seconds per HR zone — use for intensity "
                "distribution, 80/20 polarization and grey-zone questions: "
                "zones 1-2 = easy, zone 3 = grey zone, zones 4-5 = hard), and "
                "when present: power (avg/max/norm) and running dynamics "
                "(cadence, stride length, vertical oscillation/ratio, ground "
                "contact time). Null fields are omitted."
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
            "description": "Query body composition readings for a date range: weight_kg, BMI, "
                           "body_fat_pct, body_water_pct, muscle_mass_kg, bone_mass_kg, "
                           "visceral_fat (rating), metabolic_age_years. Smart-scale entries "
                           "carry all fields; "
                           "watch-entered manual weights carry weight only. Readings are "
                           "sparse (only on weigh-in days) and the composition figures are "
                           "bio-impedance estimates — prefer multi-week trends over single "
                           "readings.",
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
        },
        {
            "name": "get_training_status",
            "description": (
                "Query Garmin training status & load for a date range: status "
                "phrase (productive/maintaining/strained...), weekly training "
                "load, acute & chronic load, ACWR (acute:chronic workload "
                "ratio — a load-spike context signal, NOT an injury "
                "predictor), fitness trend, and heat/altitude acclimation. "
                "Returns the latest reading per day, date-keyed. Use for "
                "training-load balance, overreaching and acwr_injury_risk "
                "rule questions. Empty if the device doesn't report "
                "training status."
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
            "name": "get_intraday_data",
            "description": (
                "Hourly digest of one day's intraday samples for a metric: "
                "per-local-hour avg/min/max (sum for steps), total sample "
                "count, and the day's peak/low sample with clock time. Use "
                "this to answer WHEN something happened within a day — 'when "
                "did my stress spike?', 'what time did body battery crash?', "
                "'was my heart rate elevated overnight?'. Metrics: "
                "heart_rate, stress, body_battery, steps, breathing_rate."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {**_DATE_PROP, "description": "The day to analyse, YYYY-MM-DD."},
                    "metric": {
                        "type": "string",
                        "enum": ["heart_rate", "stress", "body_battery", "steps", "breathing_rate"],
                        "description": "Which intraday series to digest. Default 'stress'.",
                    },
                },
                "required": ["date"],
            },
        },
        {
            "name": "get_lifestyle_analytics",
            "description": (
                "Run one of the dashboard's precomputed lifestyle analytics "
                "and return its result — the same numbers the user sees on "
                "their dashboard, so prefer this over re-deriving. Options: "
                "sleep_regularity (SRI proxy), social_jet_lag, "
                "caffeine_cutoff (late vs early vs none), "
                "behavior_dose_response, behavior_recovery_cost (days for "
                "HRV to recover per behavior), stress_resilience, "
                "body_battery_decay, illness_radar, inflammation_index "
                "(composite strain z-score, NOT biomarkers), recovery_debt, "
                "habit_half_life, step_distribution, who_intensity_target, "
                "stress_hour_fingerprint, stress_trigger_leaderboard, "
                "research_signal_scorecard."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "analytic": {
                        "type": "string",
                        "enum": [
                            "sleep_regularity", "social_jet_lag", "caffeine_cutoff",
                            "behavior_dose_response", "behavior_recovery_cost",
                            "stress_resilience", "body_battery_decay", "illness_radar",
                            "inflammation_index", "recovery_debt", "habit_half_life",
                            "step_distribution", "who_intensity_target",
                            "stress_hour_fingerprint", "stress_trigger_leaderboard",
                            "research_signal_scorecard",
                        ],
                        "description": "Which analytic to run.",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Look-back window in days (14-180). Default 60.",
                    },
                },
                "required": ["analytic"],
            },
        },
        {
            "name": "get_behavior_root_cause",
            "description": (
                "Root-cause scan for a logged behavior/symptom (e.g. "
                "'Migraines', 'Asthma symptoms'): for each day it was logged, "
                "returns the prior ~48h of other logged behaviors, same-day "
                "environmental extremes, and the day's recovery-marker deltas "
                "(sleep / HRV / RHR). Use when the user asks WHY a recurring "
                "symptom happens. Treat output as candidate confounders to "
                "rank, never a single proven cause."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "behavior": {
                        "type": "string",
                        "description": "The logged behavior/symptom name (fuzzy matching NOT applied — use the exact journal name).",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Look-back window in days (14-365). Default 180.",
                    },
                    "lookback_hours": {
                        "type": "integer",
                        "description": "How many hours before each event to scan for confounders. Default 48.",
                    },
                },
                "required": ["behavior"],
            },
        },
        {
            "name": "get_bedroom_environment",
            "description": (
                "Home Assistant daily sensor aggregates for a date range — "
                "e.g. bedroom temperature/humidity. Each sensor returns "
                "per-day overnight (22:00-08:00), mean, min and max values. "
                "Use to check bedroom-climate effects on sleep (Baniassadi "
                "2023: sleep efficiency drops as overnight bedroom "
                "temperature rises above ~25°C). Returns available:false "
                "when no HA integration is configured."
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
            "name": "get_environment_forecast",
            "description": (
                "Open-Meteo FORECAST for today + the next ~6 days at the "
                "user's home location: temperature, precipitation, UV, air "
                "quality (EU AQI / PM2.5) and per-species pollen. Use ONLY "
                "for forward-looking planning and warnings (e.g. 'high grass "
                "pollen tomorrow — expect possible next-day RHR elevation, "
                "Buekers 2023'; 'apparent 30°C Thursday — hydrate and expect "
                "heat strain'). NEVER cite forecast rows to explain metrics "
                "that already happened — use get_environment_data for "
                "measured history."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "scan_all_anomalies",
            "description": (
                "Run anomaly detection across EVERY baselined metric at once "
                "(last 3 days vs 30-day baselines, |z| >= 1.5). Returns all "
                "deviations with z-scores and directions. Prefer this over "
                "multiple find_anomalies calls when doing a broad health "
                "check or scan."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "start_experiment",
            "description": (
                "Register a personal n-of-1 experiment when the user states a "
                "TESTABLE hypothesis linking something they can log to a Garmin "
                "metric (e.g. 'does magnesium improve my deep sleep?', 'I think "
                "a late coffee wrecks my HRV'). It ties a lifestyle-journal "
                "behaviour to a target daily metric so you can later measure "
                "behaviour-days vs non-behaviour-days. The behaviour MUST be "
                "logged by the user in the Garmin Connect lifestyle journal on "
                "the days it happens (and left unlogged otherwise) — tell them "
                "so. Offer this proactively whenever a user proposes a cause→"
                "effect they want to test. Returns the created experiment plus a "
                "protocol; a `warning` field appears if the behaviour isn't "
                "logged yet."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short unique name for the experiment (e.g. 'magnesium-deep-sleep').",
                    },
                    "hypothesis": {
                        "type": "string",
                        "description": "The testable hypothesis in the user's words (e.g. 'Magnesium before bed increases my deep sleep').",
                    },
                    "behavior": {
                        "type": "string",
                        "description": "The lifestyle-journal behaviour to split on (e.g. 'Magnesium'). Fuzzy-matched to logged behaviours.",
                    },
                    "metric": {
                        "type": "string",
                        "description": "Target daily metric (e.g. 'deepSleepSeconds', 'avgOvernightHrv', 'restingHeartRate').",
                    },
                    "expected_direction": {
                        "type": "string",
                        "enum": ["increase", "decrease"],
                        "description": "Whether the behaviour is expected to raise or lower the metric. Default 'increase'.",
                    },
                    "lag_days": {
                        "type": "integer",
                        "description": "Days between the behaviour and the metric it affects. Default 1 (evening behaviour → next morning's overnight metric). Use 0 for same-day metrics like daytime stress or steps.",
                    },
                    "min_days_per_arm": {
                        "type": "integer",
                        "description": "Minimum behaviour-days AND non-behaviour-days needed before a verdict is trustworthy. Default 5.",
                    },
                },
                "required": ["name", "hypothesis", "behavior", "metric"],
            },
        },
        {
            "name": "get_experiments",
            "description": (
                "List the user's registered n-of-1 experiments. Use when the "
                "user asks what experiments are running, and in weekly scans to "
                "check for active experiments worth reporting on. Active rows "
                "include days_elapsed; each row carries the last stored result."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "completed", "abandoned", "all"],
                        "description": "Which experiments to list. Default 'active'.",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "evaluate_experiment",
            "description": (
                "Evaluate a registered experiment: compares the target metric on "
                "days-with-behaviour vs days-without since it started, with a "
                "Welch t-test + Cohen's d, and returns a verdict (supported / "
                "not_supported_yet / opposite_effect / "
                "inconclusive_insufficient_data) with honest n-of-1 caveats. Use "
                "this when the user asks how an experiment is going ('how's my "
                "magnesium experiment?') — leave conclude=false for an interim "
                "read. Set conclude=true only once BOTH arms have enough days and "
                "the user wants to close it (persists the verdict). Set "
                "abandon=true to drop an experiment. Present the verdict WITH its "
                "caveats — it is the user's own personal evidence, not clinical proof."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The experiment name to evaluate.",
                    },
                    "conclude": {
                        "type": "boolean",
                        "description": "If true, mark the experiment completed and persist the verdict. Default false (interim evaluation).",
                    },
                    "abandon": {
                        "type": "boolean",
                        "description": "If true, mark the experiment abandoned without evaluating. Default false.",
                    },
                },
                "required": ["name"],
            },
        },
    ]
    # Cache the entire tool definitions list — it never changes at runtime and
    # Anthropic bills these tokens on every round otherwise. Applied to the
    # last element programmatically so reordering/appending tools can't
    # silently strand the marker mid-list.
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools
