"""SQLite query layer — returns pandas DataFrames for all Garmin measurements."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any

import pandas as pd

from garmin_insights.config import Settings

logger = logging.getLogger(__name__)


class SqliteRepo:
    """Wrapper around SQLite for Garmin data queries, replacing InfluxRepo."""

    def __init__(self, settings: Settings) -> None:
        self.db_path = settings.sqlite_db_path

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def _query(self, sql: str, params: tuple | dict = ()) -> pd.DataFrame:
        """Execute SQL and return a DataFrame."""
        logger.debug("SQL: %s", sql)
        conn = self._get_conn()
        try:
            df = pd.read_sql_query(sql, conn, params=params)
            # Standardize time index if present
            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"])
                df = df.set_index("time").sort_index()
            return df
        except Exception as e:
            logger.error(f"Query failed: {e}")
            return pd.DataFrame()
        finally:
            conn.close()

    @staticmethod
    def _date_clause(start: str, end: str) -> str:
        """Build a WHERE time clause from YYYY-MM-DD strings."""
        # SQLite compares strings lexicographically, which works for ISO8601
        return f"time >= '{start}T00:00:00' AND time <= '{end}T23:59:59'"

    @staticmethod
    def _fields_clause(fields: list[str] | None, table: str) -> str:
        if fields:
            # Map InfluxDB field names to SQLite column names if they differ
            # For now assuming 1:1 mapping based on sqlite_manager.py
            # But we might need to be careful with casing.
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
        # Map fields if necessary, or assume caller knows SQLite columns
        # The agent.py might request specific fields.
        # Let's start with basic implementation.
        cols = "*"
        if fields:
            # Simple sanitization/mapping could go here
            cols = ", ".join(fields)
        
        q = f"SELECT {cols} FROM daily_stats WHERE {self._date_clause(start, end)}"
        return self._query(q)

    def query_sleep_summary(
        self,
        start: str,
        end: str,
        fields: list[str] | None = None,
    ) -> pd.DataFrame:
        """SleepSummary — per-night sleep quality metrics."""
        cols = "*"
        if fields:
            cols = ", ".join(fields)
        q = f"SELECT {cols} FROM sleep_summary WHERE {self._date_clause(start, end)}"
        return self._query(q)

    def query_training_readiness(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT * FROM training_readiness WHERE {self._date_clause(start, end)}"
        return self._query(q)

    def query_training_status(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT * FROM training_status WHERE {self._date_clause(start, end)}"
        return self._query(q)

    def query_body_composition(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT * FROM body_composition WHERE {self._date_clause(start, end)}"
        # Note: SQLite table is body_composition, Influx was BodyComposition.
        return self._query(q)

    def query_fitness_age(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT * FROM fitness_age WHERE {self._date_clause(start, end)}"
        return self._query(q)

    def query_vo2_max(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT * FROM vo2_max WHERE {self._date_clause(start, end)}"
        return self._query(q)

    def query_endurance_score(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT * FROM endurance_score WHERE {self._date_clause(start, end)}"
        return self._query(q)

    def query_hydration(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT * FROM hydration WHERE {self._date_clause(start, end)}"
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
        # Note: In SQLite we stored date simply as YYYY-MM-DD in 'date' column for lifestyle_journal
        # But for consistency with other queries, we might want to check if we used 'time' or 'date'.
        # checking sqlite_manager.py: 
        # CREATE TABLE IF NOT EXISTS lifestyle_journal (date TEXT, behavior TEXT, ... PRIMARY KEY (date, behavior))
        # It uses 'date' column, not 'time'.
        
        where = f"date >= '{start}' AND date <= '{end}'"
        params = []
        if behavior:
            where += " AND behavior = ?"
            params.append(behavior)
        if category:
            where += " AND category = ?"
            params.append(category)
            
        q = f"SELECT * FROM lifestyle_journal WHERE {where}"
        # Execute with params
        logger.debug("SQL: %s Params: %s", q, params)
        conn = self._get_conn()
        try:
            df = pd.read_sql_query(q, conn, params=params)
            if "date" in df.columns:
                df["time"] = pd.to_datetime(df["date"])
                df = df.set_index("time").sort_index()
            return df
        except Exception as e:
            logger.error(f"Query failed: {e}")
            return pd.DataFrame()
        finally:
            conn.close()

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
        params = []
        if activity_type:
            where += " AND activity_type = ?"
            params.append(activity_type)
        
        # In SQLite we don't have the "END" marker rows like Influx had
        # so we don't need to filter activityName != 'END'
        
        q = f"SELECT * FROM activity_summary WHERE {where}"
        
        # Execute with params
        logger.debug("SQL: %s Params: %s", q, params)
        conn = self._get_conn()
        try:
            df = pd.read_sql_query(q, conn, params=params)
            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"])
                df = df.set_index("time").sort_index()
            return df
        except Exception as e:
            logger.error(f"Query failed: {e}")
            return pd.DataFrame()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Intraday (high-frequency) measurements
    # ------------------------------------------------------------------
    def query_stress_intraday(
        self, date: str, start_hour: int = 0, end_hour: int = 24
    ) -> pd.DataFrame:
        start_ts = f"{date}T{start_hour:02d}:00:00"
        end_ts = f"{date}T{min(end_hour, 23):02d}:59:59"
        q = (
            f"SELECT stress_level FROM stress_intraday "
            f"WHERE time >= '{start_ts}' AND time <= '{end_ts}'"
        )
        return self._query(q)

    def query_body_battery_intraday(self, date: str) -> pd.DataFrame:
        q = (
            f"SELECT body_battery_level FROM body_battery_intraday "
            f"WHERE {self._date_clause(date, date)}"
        )
        return self._query(q)

    def query_hrv_intraday(self, start: str, end: str) -> pd.DataFrame:
        q = (
            f"SELECT hrv_value FROM hrv_intraday " # Table name matches sqlite_manager
            f"WHERE {self._date_clause(start, end)}"
        )
        return self._query(q)

    def query_heart_rate_intraday(self, date: str) -> pd.DataFrame:
        q = (
            f"SELECT heart_rate FROM heart_rate_intraday "
            f"WHERE {self._date_clause(date, date)}"
        )
        return self._query(q)

    def query_steps_intraday(self, date: str) -> pd.DataFrame:
        q = (
            f"SELECT steps_count FROM steps_intraday "
            f"WHERE {self._date_clause(date, date)}"
        )
        return self._query(q)

    def query_breathing_rate_intraday(self, date: str) -> pd.DataFrame:
        q = (
            f"SELECT breathing_rate FROM breathing_rate_intraday "
            f"WHERE {self._date_clause(date, date)}"
        )
        return self._query(q)

    # ------------------------------------------------------------------
    # Escape-hatch for ad-hoc queries
    # ------------------------------------------------------------------
    def query_raw(self, sql: str) -> pd.DataFrame:
        """Run an arbitrary SQL query and return a DataFrame."""
        return self._query(sql)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------
    def list_measurements(self) -> list[str]:
        """Return all table names in the database."""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [row[0] for row in cursor.fetchall()]
            return tables
        finally:
            conn.close()

    def get_date_range(self) -> tuple[str, str]:
        """Return the earliest and latest dates with DailyStats data."""
        # Checking daily_stats table
        first = self._query(
            "SELECT date FROM daily_stats ORDER BY date ASC LIMIT 1"
        )
        last = self._query(
            "SELECT date FROM daily_stats ORDER BY date DESC LIMIT 1"
        )
        
        # If daily_stats uses 'date' column as string YYYY-MM-DD
        if first.empty or last.empty:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            return today, today
            
        # The query returns a DataFrame. If 'date' is cleaned up to 'time' index:
        # But wait, query_daily_stats logic for _query might try to set index if 'time' column exists.
        # daily_stats has 'time' AND 'date'. 
        
        # Let's just use raw query for this to be safe and simple
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT date FROM daily_stats ORDER BY date ASC LIMIT 1")
            min_date = cursor.fetchone()
            cursor.execute("SELECT date FROM daily_stats ORDER BY date DESC LIMIT 1")
            max_date = cursor.fetchone()
            
            if not min_date or not max_date:
                today = datetime.now().strftime("%Y-%m-%d")
                return today, today
                
            return min_date[0], max_date[0]
        finally:
            conn.close()

    def health_check(self) -> dict[str, Any]:
        """Quick connectivity + data availability check."""
        try:
            measurements = self.list_measurements()
            start, end = self.get_date_range()
            return {
                "connected": True,
                "database": self.db_path,
                "measurements": measurements,
                "date_range": {"start": start, "end": end},
                "measurement_count": len(measurements),
            }
        except Exception as e:
            return {
                "connected": False,
                "error": str(e)
            }
