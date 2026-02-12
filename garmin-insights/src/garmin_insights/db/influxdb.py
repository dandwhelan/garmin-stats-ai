"""InfluxDB v1 query layer — returns pandas DataFrames for all Garmin measurements."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from influxdb import InfluxDBClient

from garmin_insights.config import Settings

logger = logging.getLogger(__name__)


class InfluxRepo:
    """Thin wrapper around the InfluxDB v1 client for Garmin data queries."""

    def __init__(self, settings: Settings) -> None:
        self._client = InfluxDBClient(
            host=settings.influxdb_host,
            port=settings.influxdb_port,
            username=settings.influxdb_username or None,
            password=settings.influxdb_password or None,
        )
        self._db = settings.influxdb_database
        self._client.switch_database(self._db)

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def _query(self, influxql: str) -> pd.DataFrame:
        """Execute InfluxQL and return a DataFrame."""
        logger.debug("InfluxQL: %s", influxql)
        result = self._client.query(influxql, database=self._db)
        points = list(result.get_points())
        if not points:
            return pd.DataFrame()
        df = pd.DataFrame(points)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"])
            df = df.set_index("time").sort_index()
        return df

    @staticmethod
    def _date_clause(start: str, end: str) -> str:
        """Build a WHERE time clause from YYYY-MM-DD strings."""
        return f"time >= '{start}T00:00:00Z' AND time <= '{end}T23:59:59Z'"

    @staticmethod
    def _fields_clause(fields: list[str] | None) -> str:
        if fields:
            return ", ".join(f'"{f}"' for f in fields)
        return "*"

    # ------------------------------------------------------------------
    # Daily-granularity measurements
    # ------------------------------------------------------------------
    def query_daily_stats(
        self,
        start: str,
        end: str,
        fields: list[str] | None = None,
    ) -> pd.DataFrame:
        """DailyStats — RHR, stress, body battery, steps, etc."""
        q = (
            f"SELECT {self._fields_clause(fields)} FROM DailyStats "
            f"WHERE {self._date_clause(start, end)}"
        )
        return self._query(q)

    def query_sleep_summary(
        self,
        start: str,
        end: str,
        fields: list[str] | None = None,
    ) -> pd.DataFrame:
        """SleepSummary — per-night sleep quality metrics."""
        q = (
            f"SELECT {self._fields_clause(fields)} FROM SleepSummary "
            f"WHERE {self._date_clause(start, end)}"
        )
        return self._query(q)

    def query_training_readiness(self, start: str, end: str) -> pd.DataFrame:
        q = (
            f"SELECT * FROM TrainingReadiness "
            f"WHERE {self._date_clause(start, end)}"
        )
        return self._query(q)

    def query_training_status(self, start: str, end: str) -> pd.DataFrame:
        q = (
            f"SELECT * FROM TrainingStatus "
            f"WHERE {self._date_clause(start, end)}"
        )
        return self._query(q)

    def query_body_composition(self, start: str, end: str) -> pd.DataFrame:
        q = (
            f"SELECT * FROM BodyComposition "
            f"WHERE {self._date_clause(start, end)}"
        )
        return self._query(q)

    def query_fitness_age(self, start: str, end: str) -> pd.DataFrame:
        q = (
            f"SELECT * FROM FitnessAge "
            f"WHERE {self._date_clause(start, end)}"
        )
        return self._query(q)

    def query_vo2_max(self, start: str, end: str) -> pd.DataFrame:
        q = (
            f"SELECT * FROM VO2_Max "
            f"WHERE {self._date_clause(start, end)}"
        )
        return self._query(q)

    def query_endurance_score(self, start: str, end: str) -> pd.DataFrame:
        q = (
            f"SELECT * FROM EnduranceScore "
            f"WHERE {self._date_clause(start, end)}"
        )
        return self._query(q)

    def query_hydration(self, start: str, end: str) -> pd.DataFrame:
        q = (
            f"SELECT * FROM Hydration "
            f"WHERE {self._date_clause(start, end)}"
        )
        return self._query(q)

    # ------------------------------------------------------------------
    # Lifestyle journal (behaviour tags)
    # ------------------------------------------------------------------
    def query_lifestyle_journal(
        self,
        start: str,
        end: str,
        behavior: str | None = None,
        category: str | None = None,
    ) -> pd.DataFrame:
        """LifestyleJournal — behaviour status & value by day."""
        where = self._date_clause(start, end)
        if behavior:
            where += f" AND (\"Behavior\" = '{behavior}' OR \"behavior\" = '{behavior}')"
        if category:
            where += f" AND (\"Category\" = '{category}' OR \"category\" = '{category}')"
        q = f"SELECT * FROM LifestyleJournal WHERE {where}"
        return self._query(q)

    # ------------------------------------------------------------------
    # Activity summaries
    # ------------------------------------------------------------------
    def query_activity_summary(
        self,
        start: str,
        end: str,
        activity_type: str | None = None,
    ) -> pd.DataFrame:
        where = self._date_clause(start, end)
        if activity_type:
            where += f" AND \"activityType\" = '{activity_type}'"
        # Exclude the synthetic END markers
        where += " AND \"activityName\" != 'END'"
        q = f"SELECT * FROM ActivitySummary WHERE {where}"
        return self._query(q)

    # ------------------------------------------------------------------
    # Intraday (high-frequency) measurements
    # ------------------------------------------------------------------
    def query_stress_intraday(
        self, date: str, start_hour: int = 0, end_hour: int = 24
    ) -> pd.DataFrame:
        start_ts = f"{date}T{start_hour:02d}:00:00Z"
        end_ts = f"{date}T{min(end_hour, 23):02d}:59:59Z"
        q = (
            f"SELECT stressLevel FROM StressIntraday "
            f"WHERE time >= '{start_ts}' AND time <= '{end_ts}'"
        )
        return self._query(q)

    def query_body_battery_intraday(self, date: str) -> pd.DataFrame:
        q = (
            f"SELECT BodyBatteryLevel FROM BodyBatteryIntraday "
            f"WHERE {self._date_clause(date, date)}"
        )
        return self._query(q)

    def query_hrv_intraday(self, start: str, end: str) -> pd.DataFrame:
        q = (
            f"SELECT hrvValue FROM HRV_Intraday "
            f"WHERE {self._date_clause(start, end)}"
        )
        return self._query(q)

    def query_heart_rate_intraday(self, date: str) -> pd.DataFrame:
        q = (
            f"SELECT HeartRate FROM HeartRateIntraday "
            f"WHERE {self._date_clause(date, date)}"
        )
        return self._query(q)

    def query_steps_intraday(self, date: str) -> pd.DataFrame:
        q = (
            f"SELECT StepsCount FROM StepsIntraday "
            f"WHERE {self._date_clause(date, date)}"
        )
        return self._query(q)

    def query_breathing_rate_intraday(self, date: str) -> pd.DataFrame:
        q = (
            f"SELECT BreathingRate FROM BreathingRateIntraday "
            f"WHERE {self._date_clause(date, date)}"
        )
        return self._query(q)

    # ------------------------------------------------------------------
    # Escape-hatch for ad-hoc queries
    # ------------------------------------------------------------------
    def query_raw(self, influxql: str) -> pd.DataFrame:
        """Run an arbitrary InfluxQL query and return a DataFrame."""
        return self._query(influxql)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------
    def list_measurements(self) -> list[str]:
        """Return all measurement names in the database."""
        result = self._client.query("SHOW MEASUREMENTS", database=self._db)
        return [m["name"] for m in result.get_points()]

    def get_date_range(self) -> tuple[str, str]:
        """Return the earliest and latest dates with DailyStats data."""
        first = self._query(
            "SELECT * FROM DailyStats ORDER BY time ASC LIMIT 1"
        )
        last = self._query(
            "SELECT * FROM DailyStats ORDER BY time DESC LIMIT 1"
        )
        if first.empty or last.empty:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            return today, today
        return (
            first.index[0].strftime("%Y-%m-%d"),
            last.index[0].strftime("%Y-%m-%d"),
        )

    def health_check(self) -> dict[str, Any]:
        """Quick connectivity + data availability check."""
        measurements = self.list_measurements()
        start, end = self.get_date_range()
        return {
            "connected": True,
            "database": self._db,
            "measurements": measurements,
            "date_range": {"start": start, "end": end},
            "measurement_count": len(measurements),
        }
