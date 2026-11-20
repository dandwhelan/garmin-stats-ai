"""Smart cache builder — computes daily summaries and baselines from SQLite.

The key design principle: heavy raw-table queries happen here (pure Python,
no LLM cost).  The LLM only ever sees the compact cached summaries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from garmin_insights.db.sqlite_repo import (
    SqliteRepo,
    _DAILY_STATS_COLS as _DAILY_STATS_COLUMN_MAP,
    _SLEEP_COLS as _SLEEP_COLUMN_MAP,
)
from garmin_insights.db.memory import MemoryStore
from garmin_insights.stats_utils import metabolic_age_years, to_kg

logger = logging.getLogger(__name__)

# Below this much recorded sleep the night wasn't captured by the watch.
_MIN_RECORDED_SLEEP_S = 3600

# Key metrics we track baselines for
_BASELINE_METRICS = [
    "restingHeartRate", "stressPercentage", "highStressPercentage",
    "bodyBatteryHighestValue", "bodyBatteryLowestValue", "bodyBatteryAtWakeTime",
    "totalSteps", "averageSpo2",
    "sleepScore", "deepSleepSeconds", "remSleepSeconds",
    "avgOvernightHrv", "avgSleepStress",
    "bodyBatteryChange", "awakeCount",
    # Required by detect_illness_signature and the composite strain triad
    # (RHR / HRV / respiration) — without a respiration baseline both
    # detectors were permanently blind to elevated respiration.
    "averageRespirationValue",
    # Body composition — smart-scale readings can land daily, and the trend /
    # anomaly / correlation tools only see metrics that live in the summaries
    # and baselines. visceral_fat is required by the visceral_fat_hrv KB rule.
    "weight_kg", "body_fat_pct", "visceral_fat",
    # overnight_spo2_disordered_breathing keys on overnight SpO2 dips;
    # sedentary_stress_coupling needs sedentary time. Neither rule could fire
    # while these lived only in the raw tables.
    "lowestSpo2Value", "sedentarySeconds",
]

# body_composition db column → summary key. Masses are normalised to kg via
# to_kg(); metabolic_age via metabolic_age_years() (Garmin returns a ms
# duration); percentages / ratings pass through unchanged.
_BODY_COMP_MAP = {
    "weight": "weight_kg",
    "bmi": "bmi",
    "body_fat": "body_fat_pct",
    "body_water": "body_water_pct",
    "muscle_mass": "muscle_mass_kg",
    "bone_mass": "bone_mass_kg",
    "visceral_fat": "visceral_fat",
    "metabolic_age": "metabolic_age",
}
_BODY_COMP_MASS_COLS = {"weight", "muscle_mass", "bone_mass"}


class CacheBuilder:
    """Orchestrates daily summary generation and caching."""

    def __init__(self, repo: SqliteRepo, memory: MemoryStore) -> None:
        self._repo = repo
        self._memory = memory
        # Which local calendar day last got its trailing-window force rebuild
        # (see refresh) — in-memory is fine; a restart just rebuilds once more.
        self._last_trailing_rebuild_day: str | None = None
        # Content fingerprint of the last baseline computation's input — the
        # dashboard refreshes every 60 s, but the 30 completed days behind the
        # baselines rarely change, so most refreshes can skip the recompute.
        self._baseline_fp: str | None = None
        self._baseline_result: dict[str, dict[str, float | None]] | None = None

    # ------------------------------------------------------------------
    # Raw-data loading — one query per table for a whole date range
    # ------------------------------------------------------------------
    @staticmethod
    def _first_per_day(df: pd.DataFrame, last: bool = False) -> dict[str, pd.Series]:
        """Map stored calendar date → first (or last) row, for a time-indexed frame.

        Mirrors the per-day path, which filtered ``time`` by the stored date's
        string prefix and took ``iloc[0]`` of the time-sorted result.
        """
        if df is None or df.empty:
            return {}
        days = df.index.strftime("%Y-%m-%d")
        out: dict[str, pd.Series] = {}
        for pos, day in enumerate(days):
            if last or day not in out:
                out[day] = df.iloc[pos]
        return out

    @staticmethod
    def _by_date_col(df: pd.DataFrame) -> dict[str, pd.Series]:
        if df is None or df.empty or "date" not in df.columns:
            return {}
        return {str(row["date"]): row for _, row in df.iterrows()}

    def _load_range(self, start: str, end: str) -> dict[str, Any]:
        """Read every raw table the summary needs for [start, end] in one pass.

        The old per-day build opened ~8 connections per date (≈720 for the
        90-day startup refresh on the Pi); this is 8 per range.
        """
        next_day = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        repo = self._repo

        daily = repo.query_daily_stats(
            start, next_day, ["date", *_DAILY_STATS_COLUMN_MAP.values()]
        )
        sleep = repo.query_sleep_summary(
            start, next_day, ["date", *_SLEEP_COLUMN_MAP.values()]
        )
        try:
            bc = repo.query_body_composition(start, end)
        except Exception as e:
            logger.debug("Body composition fetch failed for %s..%s: %s", start, end, e)
            bc = pd.DataFrame()
        bc_by_day: dict[str, pd.Series] = {}
        if not bc.empty:
            bc = bc.reset_index() if bc.index.name is not None else bc
            if "time" in bc.columns:
                bc = bc.sort_values("time", kind="stable")
            for _, row in bc.iterrows():
                bc_by_day[str(row["date"])] = row  # later rows win → latest reading

        try:
            scale_rows = self._memory.get_scale_readings(start, end)
        except Exception as e:
            logger.debug("Scale reading fetch failed for %s..%s: %s", start, end, e)
            scale_rows = []
        scale_by_day: dict[str, dict] = {}
        for r in scale_rows:  # ordered by taken_at → last wins
            scale_by_day[str(r.get("date"))] = r

        lj = repo.query_lifestyle_journal(start, end)
        lj_by_day: dict[str, list[pd.Series]] = {}
        if not lj.empty and "date" in lj.columns:
            for _, row in lj.iterrows():
                d = row["date"]
                if isinstance(d, pd.Series):
                    d = d.iloc[0]
                lj_by_day.setdefault(str(d), []).append(row)

        return {
            "daily": self._by_date_col(daily),
            "sleep": self._by_date_col(sleep),
            "tr": self._first_per_day(repo.query_training_readiness(start, end)),
            "ts": self._first_per_day(repo.query_training_status(start, end)),
            "bc": bc_by_day,
            "scale": scale_by_day,
            "lj": lj_by_day,
        }

    @staticmethod
    def _num(val: Any) -> Any:
        return float(val) if isinstance(val, (int, float, np.number)) else val

    @staticmethod
    def _missing(val: Any) -> bool:
        if val is None:
            return True
        try:
            return bool(pd.isna(val))
        except (TypeError, ValueError):
            return False

    def _compose(
        self, date: str, is_complete: bool, data: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build one day's (summary, lifestyle) from pre-loaded range data."""
        summary: dict[str, Any] = {"date": date, "is_complete": is_complete}

        # -- DailyStats --
        row = data["daily"].get(date)
        if row is not None:
            for logical, db_col in _DAILY_STATS_COLUMN_MAP.items():
                val = row.get(db_col)
                if not self._missing(val):
                    summary[logical] = self._num(val)

        # -- SleepSummary (keyed to the wake date) --
        row = data["sleep"].get(date)
        if row is not None:
            for logical, db_col in _SLEEP_COLUMN_MAP.items():
                val = row.get(db_col)
                if not self._missing(val):
                    key = logical if logical not in summary else f"sleep_{logical}"
                    summary[key] = self._num(val)

        # A night the watch didn't record (off the wrist / charging) leaves
        # sleepingSeconds ~0 and Garmin books the whole night as sedentary —
        # ~21 h "sedentary" days that inflate the sedentarySeconds baseline and
        # fire false sedentary_stress_coupling findings. Neither value measures
        # anything on such a day, so drop both rather than cache them.
        sleeping = summary.get("sleepingSeconds")
        if is_complete and (sleeping is None or sleeping < _MIN_RECORDED_SLEEP_S):
            summary.pop("sleepingSeconds", None)
            summary.pop("sedentarySeconds", None)

        # -- TrainingReadiness --
        row = data["tr"].get(date)
        if row is not None:
            _tr_map = {
                "score": "score",
                "level": "level",
                "sleepScore": "sleep_score",
                "recoveryTime": "recovery_time",
                "hrvFactorPercent": "hrv_factor_percent",
            }
            for logical, db_col in _tr_map.items():
                val = row.get(db_col)
                if not self._missing(val):
                    summary[f"training_{logical}"] = self._num(val)

        # -- Heat / Altitude Acclimation (rides in the training_status table) --
        row = data["ts"].get(date)
        if row is not None:
            _acc_map = {
                "heatAcclimationPercentage": "heat_acclimation_percentage",
                "altitudeAcclimationPercentage": "altitude_acclimation_percentage",
                "heatTrend": "heat_trend",
                "altitudeTrend": "altitude_trend",
            }
            for logical, db_col in _acc_map.items():
                val = row.get(db_col)
                if not self._missing(val):
                    summary[logical] = self._num(val)

        # -- BodyComposition (smart-scale / manual weigh-ins) --
        # Latest reading of the day wins; without this merge, weight and body
        # fat never reach the daily summaries the agent queries first.
        row = data["bc"].get(date)
        if row is not None:
            for db_col, key in _BODY_COMP_MAP.items():
                val = row.get(db_col)
                if self._missing(val):
                    continue
                val = float(val)
                if db_col in _BODY_COMP_MASS_COLS:
                    val = to_kg(val)
                elif db_col == "metabolic_age":
                    # Garmin returns a ms duration, not years — see stats_utils.
                    val = metabolic_age_years(val)
                summary[key] = val

        # -- Scale readings fallback (local-first Fitdays/scale storage) --
        # Garmin's own synced body_composition value wins once it lands; this
        # only closes the gap between stepping off the scale and the next
        # fetch. scale_readings already stores kg/years — no conversion.
        if not any(k in summary for k in _BODY_COMP_MAP.values()):
            scale_reading = data["scale"].get(date)
            if scale_reading:
                for key in _BODY_COMP_MAP.values():
                    val = scale_reading.get(key)
                    if val is not None:
                        summary[key] = val

        # -- LifestyleJournal --
        lifestyle: dict[str, Any] = {}
        for row in data["lj"].get(date, []):
            # Handle both 'behavior' and 'Behavior' columns; duplicate columns
            # come back as a Series — take the first value.
            b1 = row.get("behavior")
            b2 = row.get("Behavior")
            if isinstance(b1, pd.Series): b1 = b1.iloc[0]
            if isinstance(b2, pd.Series): b2 = b2.iloc[0]
            behavior = b1 if pd.notna(b1) else b2
            if pd.isna(behavior) or behavior == "unknown":
                continue
            status = row.get("status", 0)
            if isinstance(status, pd.Series): status = status.iloc[0]
            value = row.get("value", 0)
            if isinstance(value, pd.Series): value = value.iloc[0]
            lifestyle[behavior] = {"status": int(status), "value": float(value)}

        return summary, lifestyle

    def _build_dates(self, dates: list[tuple[str, bool]]) -> list[dict[str, Any]]:
        """Build + persist summaries for (date, is_complete) pairs in one pass."""
        if not dates:
            return []
        data = self._load_range(min(d for d, _ in dates), max(d for d, _ in dates))
        rows, summaries = [], []
        for date, is_complete in dates:
            try:
                summary, lifestyle = self._compose(date, is_complete, data)
            except Exception as e:
                logger.warning("Failed to build summary for %s: %s", date, e)
                continue
            rows.append((date, summary, lifestyle or None))
            summaries.append(summary)
        self._memory.upsert_daily_summaries(rows)
        logger.info("Cached %d daily summaries (%s..%s)", len(rows), dates[0][0], dates[-1][0])
        return summaries

    def build_daily_summary(self, date: str, is_complete: bool = True) -> dict[str, Any]:
        """Compute and cache a full daily metric snapshot for one date.

        Args:
            date: Date in YYYY-MM-DD format.
            is_complete: False if this is today (data still accumulating).
        """
        built = self._build_dates([(date, is_complete)])
        return built[0] if built else {"date": date, "is_complete": is_complete}

    def build_range(self, start: str, end: str, force: bool = False) -> int:
        """Build summaries for all uncached dates in the range. Returns count built."""
        if force:
            d = datetime.strptime(start, "%Y-%m-%d").date()
            d_end = datetime.strptime(end, "%Y-%m-%d").date()
            dates = []
            while d <= d_end:
                dates.append(d.isoformat())
                d += timedelta(days=1)
        else:
            dates = self._memory.get_uncached_dates(start, end)

        if not dates:
            logger.info("All dates in range %s to %s are already cached.", start, end)
            return 0

        logger.info("Building daily summaries for %d uncached dates...", len(dates))
        self._build_dates([(d, True) for d in dates])
        return len(dates)

    def update_baselines(self) -> dict[str, dict[str, float | None]]:
        """Compute rolling 7d/30d baselines from cached daily summaries.

        IMPORTANT: excludes today (incomplete day) from baseline computation.
        Baselines should only reflect fully completed days.
        """
        # Local calendar days — daily rows are keyed by local date; a UTC
        # "today" points at yesterday during the BST window after local midnight.
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        start_30d = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        start_7d = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        # Only use completed days (up to yesterday, not today)
        summaries_30d = self._memory.get_daily_summaries_range(start_30d, yesterday)
        summaries_30d = [s for s in summaries_30d if s.get("is_complete", True)]
        summaries_7d = [s for s in summaries_30d if s["date"] >= start_7d]

        fp = hashlib.sha1(
            json.dumps([yesterday, summaries_30d], sort_keys=True, default=str).encode()
        ).hexdigest()
        if fp == self._baseline_fp and self._baseline_result is not None:
            logger.debug("Baselines unchanged since last refresh; skipping recompute.")
            return self._baseline_result

        baselines = {}
        rows: list[tuple] = []
        for metric in _BASELINE_METRICS:
            vals_30d = [s[metric] for s in summaries_30d if metric in s and s[metric] is not None]
            vals_7d = [s[metric] for s in summaries_7d if metric in s and s[metric] is not None]

            avg_7d = float(np.mean(vals_7d)) if vals_7d else None
            avg_30d = float(np.mean(vals_30d)) if vals_30d else None
            # Sample SD (ddof=1) — matches the sample statistics used in
            # analysis_tools; population SD understates spread on 7-point windows.
            std_7d = float(np.std(vals_7d, ddof=1)) if len(vals_7d) > 1 else None
            std_30d = float(np.std(vals_30d, ddof=1)) if len(vals_30d) > 1 else None
            min_30d = float(np.min(vals_30d)) if vals_30d else None
            max_30d = float(np.max(vals_30d)) if vals_30d else None
            latest = vals_7d[-1] if vals_7d else None

            rows.append(
                (metric, avg_7d, avg_30d, std_7d, std_30d, min_30d, max_30d, latest)
            )
            baselines[metric] = {
                "avg_7d": avg_7d, "avg_30d": avg_30d,
                "std_7d": std_7d, "std_30d": std_30d,
                "latest": latest,
            }

        self._memory.upsert_baselines(rows)
        self._baseline_fp, self._baseline_result = fp, baselines
        logger.info("Updated baselines for %d metrics (today excluded).", len(baselines))
        return baselines

    def refresh(self, days: int = 30) -> None:
        """Full refresh: build missing summaries then update baselines."""
        # Local dates, not UTC — see update_baselines.
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        # Always rebuild today (data still accumulating) — marked incomplete
        self.build_daily_summary(today, is_complete=False)
        # Re-promote past days still cached as incomplete. Mid-day builds leave
        # is_complete=False; without this, that partial snapshot (e.g. 107 steps
        # logged by 09:43) is never replaced with the day's final totals once the
        # day ends, because build_range skips dates that already have a row.
        stale = [
            (entry["date"], True)
            for entry in self._memory.get_daily_summaries_range(start, yesterday)
            if not entry.get("is_complete", True)
        ]
        self._build_dates(stale)
        # Build any other missing days (these are complete)
        self.build_range(start, yesterday)
        # Raw data lands AFTER a day is cached complete all the time: the
        # fetcher's trailing resync picks up retroactive Garmin edits, a
        # weigh-in flows back on the next watch sync, sleep syncs late after
        # a travel day. Once per calendar day, force-rebuild the same trailing
        # window the fetcher re-scans so those late arrivals reach the cache
        # (cheap: ~7 rebuilds), instead of freezing the day's first snapshot
        # forever.
        if self._last_trailing_rebuild_day != today:
            self._last_trailing_rebuild_day = today
            resync_days = int(os.getenv("RESYNC_WINDOW_DAYS", "7") or 7)
            trail_start = (datetime.now() - timedelta(days=resync_days)).strftime("%Y-%m-%d")
            self.build_range(trail_start, yesterday, force=True)
        # Update baselines (excludes today)
        self.update_baselines()
