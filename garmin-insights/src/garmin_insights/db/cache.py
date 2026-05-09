"""Smart cache builder — computes daily summaries and baselines from InfluxDB.

The key design principle: heavy InfluxDB queries happen here (pure Python,
no LLM cost).  The LLM only ever sees the compact cached summaries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from garmin_insights.db.sqlite_repo import SqliteRepo
from garmin_insights.db.memory import MemoryStore

logger = logging.getLogger(__name__)

# Metrics we extract from each measurement for the daily snapshot
_DAILY_STATS_FIELDS = [
    "restingHeartRate", "minHeartRate", "maxHeartRate",
    "stressPercentage", "highStressPercentage",
    "bodyBatteryHighestValue", "bodyBatteryLowestValue",
    "bodyBatteryChargedValue", "bodyBatteryDrainedValue",
    "bodyBatteryAtWakeTime",
    "totalSteps", "totalDistanceMeters",
    "activeKilocalories",
    "sleepingSeconds",
    "moderateIntensityMinutes", "vigorousIntensityMinutes",
    "averageSpo2",
]

_SLEEP_FIELDS = [
    "sleepScore", "sleepTimeSeconds",
    "deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds", "awakeSleepSeconds",
    "avgSleepStress", "avgOvernightHrv",
    "bodyBatteryChange", "restingHeartRate",
    "averageSpO2Value", "awakeCount", "restlessMomentsCount",
    "averageRespirationValue",
]

# Key metrics we track baselines for
_BASELINE_METRICS = [
    "restingHeartRate", "stressPercentage", "highStressPercentage",
    "bodyBatteryHighestValue", "bodyBatteryLowestValue", "bodyBatteryAtWakeTime",
    "totalSteps", "averageSpo2",
    "sleepScore", "deepSleepSeconds", "remSleepSeconds",
    "avgOvernightHrv", "avgSleepStress",
    "bodyBatteryChange", "awakeCount",
]


class CacheBuilder:
    """Orchestrates daily summary generation and caching."""

    def __init__(self, repo: SqliteRepo, memory: MemoryStore) -> None:
        self._repo = repo
        self._memory = memory

    def build_daily_summary(self, date: str, is_complete: bool = True) -> dict[str, Any]:
        """Compute and cache a full daily metric snapshot for one date.

        Args:
            date: Date in YYYY-MM-DD format.
            is_complete: False if this is today (data still accumulating).
        """
        summary: dict[str, Any] = {"date": date, "is_complete": is_complete}
        
        # Calculate next day for query ranges [start, end)
        from datetime import datetime, timedelta
        date_obj = datetime.strptime(date, "%Y-%m-%d")
        next_day_str = (date_obj + timedelta(days=1)).strftime("%Y-%m-%d")

        # -- DailyStats --
        df_daily = self._repo.query_daily_stats(date, next_day_str, _DAILY_STATS_FIELDS)
        if not df_daily.empty:
            row = df_daily.iloc[0]
            for f in _DAILY_STATS_FIELDS:
                val = row.get(f)
                if val is not None and not (isinstance(val, float) and np.isnan(val)):
                    summary[f] = float(val) if isinstance(val, (int, float, np.number)) else val

        # -- SleepSummary --
        df_sleep = self._repo.query_sleep_summary(date, next_day_str, _SLEEP_FIELDS)
        if not df_sleep.empty:
            row = df_sleep.iloc[0]
            for f in _SLEEP_FIELDS:
                val = row.get(f)
                if val is not None and not (isinstance(val, float) and np.isnan(val)):
                    # Prefer original name 'f' if not taken. Collision -> 'sleep_f'
                    key = f if f not in summary else f"sleep_{f}"
                    summary[key] = float(val) if isinstance(val, (int, float, np.number)) else val

        # -- TrainingReadiness --
        df_tr = self._repo.query_training_readiness(date, date)
        if not df_tr.empty:
            row = df_tr.iloc[0]
            for f in ["score", "level", "sleepScore", "recoveryTime", "hrvFactorPercent"]:
                val = row.get(f)
                if val is not None and not (isinstance(val, float) and np.isnan(val)):
                    summary[f"training_{f}"] = float(val) if isinstance(val, (int, float, np.number)) else val

        # -- LifestyleJournal --
        df_lj = self._repo.query_lifestyle_journal(date, date)
        lifestyle: dict[str, Any] = {}
        if not df_lj.empty:
            # Iterate rows directly to avoid index alignment issues with combine_first
            for _, row in df_lj.iterrows():
                # Handle both 'behavior' and 'Behavior' columns
                # Use robust checking incase of duplicate columns (returns Series)
                b1 = row.get("behavior") 
                b2 = row.get("Behavior")

                # If returns Series (duplicate cols), take first valid value
                if isinstance(b1, pd.Series): b1 = b1.iloc[0]
                if isinstance(b2, pd.Series): b2 = b2.iloc[0]

                behavior = b1 if pd.notna(b1) else b2
                
                # Check for explicit unknown/None values
                if pd.isna(behavior) or behavior == "unknown":
                    continue

                status = row.get("status", 0)
                if isinstance(status, pd.Series): status = status.iloc[0]
                
                value = row.get("value", 0)
                if isinstance(value, pd.Series): value = value.iloc[0]

                lifestyle[behavior] = {"status": int(status), "value": float(value)}

        # -- Save to MariaDB --
        self._memory.upsert_daily_summary(date, summary, lifestyle if lifestyle else None)

        status_label = "incomplete" if not is_complete else "complete"
        logger.info("Cached daily summary for %s [%s] (%d metrics, %d behaviors)",
                     date, status_label, len(summary), len(lifestyle))
        return summary

    def build_range(self, start: str, end: str, force: bool = False) -> int:
        """Build summaries for all uncached dates in the range. Returns count built."""
        if force:
            # Build all dates
            from datetime import date as dt_date
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
        for date in dates:
            try:
                self.build_daily_summary(date)
            except Exception as e:
                logger.warning("Failed to build summary for %s: %s", date, e)
        return len(dates)

    def update_baselines(self) -> dict[str, dict[str, float | None]]:
        """Compute rolling 7d/30d baselines from cached daily summaries.

        IMPORTANT: excludes today (incomplete day) from baseline computation.
        Baselines should only reflect fully completed days.
        """
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        start_30d = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        start_7d = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")

        # Only use completed days (up to yesterday, not today)
        summaries_30d = self._memory.get_daily_summaries_range(start_30d, yesterday)
        summaries_30d = [s for s in summaries_30d if s.get("is_complete", True)]
        summaries_7d = [s for s in summaries_30d if s["date"] >= start_7d]

        baselines = {}
        for metric in _BASELINE_METRICS:
            vals_30d = [s[metric] for s in summaries_30d if metric in s and s[metric] is not None]
            vals_7d = [s[metric] for s in summaries_7d if metric in s and s[metric] is not None]

            avg_7d = float(np.mean(vals_7d)) if vals_7d else None
            avg_30d = float(np.mean(vals_30d)) if vals_30d else None
            std_7d = float(np.std(vals_7d)) if len(vals_7d) > 1 else None
            std_30d = float(np.std(vals_30d)) if len(vals_30d) > 1 else None
            min_30d = float(np.min(vals_30d)) if vals_30d else None
            max_30d = float(np.max(vals_30d)) if vals_30d else None
            latest = vals_7d[-1] if vals_7d else None

            self._memory.upsert_baseline(
                metric, avg_7d, avg_30d, std_7d, std_30d, min_30d, max_30d, latest
            )
            baselines[metric] = {
                "avg_7d": avg_7d, "avg_30d": avg_30d,
                "std_7d": std_7d, "std_30d": std_30d,
                "latest": latest,
            }

        logger.info("Updated baselines for %d metrics (today excluded).", len(baselines))
        return baselines

    def refresh(self, days: int = 30) -> None:
        """Full refresh: build missing summaries then update baselines."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        # Always rebuild today (data still accumulating) — marked incomplete
        self.build_daily_summary(today, is_complete=False)
        # Build any other missing days (these are complete)
        self.build_range(start, yesterday)
        # Update baselines (excludes today)
        self.update_baselines()
