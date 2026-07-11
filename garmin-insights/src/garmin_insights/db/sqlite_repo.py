"""SQLite query layer — returns pandas DataFrames for all Garmin measurements."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from garmin_insights.config import Settings

logger = logging.getLogger(__name__)


def utc_to_local_day(ts) -> str:
    """Local calendar day (YYYY-MM-DD) for a UTC-stored timestamp.

    Intraday tables store UTC timestamps; during BST a post-midnight reading
    carries the previous UTC date, so slicing the raw string mislabels the
    day. ``datetime.astimezone()`` applies the system zone's DST rule for
    the timestamp's own date (not today's offset).
    """
    dt = pd.Timestamp(ts).to_pydatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d")

# Maps the camelCase field names used in cache.py / _DAILY_STATS_FIELDS to the
# snake_case column names stored in SQLite by garmin-grafana's sqlite_manager.py.
# Used to build "snake_col AS camelField" aliases so callers get back the names
# they expect without knowing the DB schema's naming convention.
_DAILY_STATS_COLS: dict[str, str] = {
    "restingHeartRate":        "resting_heart_rate",
    "minHeartRate":            "min_heart_rate",
    "maxHeartRate":            "max_heart_rate",
    "stressPercentage":        "stress_percentage",
    "highStressPercentage":    "high_stress_percentage",
    "bodyBatteryHighestValue": "body_battery_highest_value",
    "bodyBatteryLowestValue":  "body_battery_lowest_value",
    "bodyBatteryChargedValue": "body_battery_charged_value",
    "bodyBatteryDrainedValue": "body_battery_drained_value",
    "bodyBatteryAtWakeTime":   "body_battery_at_wake_time",
    "totalSteps":              "total_steps",
    "totalDistanceMeters":     "total_distance_meters",
    "activeKilocalories":      "active_kilocalories",
    "sleepingSeconds":         "sleeping_seconds",
    "moderateIntensityMinutes":"moderate_intensity_minutes",
    "vigorousIntensityMinutes":"vigorous_intensity_minutes",
    "averageSpo2":             "average_spo2",
}

_SLEEP_COLS: dict[str, str] = {
    "sleepScore":              "sleep_score",
    "sleepTimeSeconds":        "sleep_time_seconds",
    "deepSleepSeconds":        "deep_sleep_seconds",
    "lightSleepSeconds":       "light_sleep_seconds",
    "remSleepSeconds":         "rem_sleep_seconds",
    "awakeSleepSeconds":       "awake_sleep_seconds",
    "avgSleepStress":          "avg_sleep_stress",
    "avgOvernightHrv":         "avg_overnight_hrv",
    "bodyBatteryChange":       "body_battery_change",
    "restingHeartRate":        "resting_heart_rate",
    "averageSpO2Value":        "average_spo2_value",
    "awakeCount":              "awake_count",
    "restlessMomentsCount":    "restless_moments_count",
    "averageRespirationValue": "average_respiration_value",
}


class SqliteRepo:
    """Wrapper around SQLite for Garmin data queries, replacing InfluxRepo."""

    def __init__(self, settings: Settings) -> None:
        self.db_path = settings.sqlite_db_path

    def _get_conn(self):
        # timeout=10 waits out the fetcher's write locks instead of failing
        # immediately with "database is locked".
        return sqlite3.connect(self.db_path, timeout=10)

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def _query(self, sql: str, params: tuple | dict = ()) -> pd.DataFrame:
        """Execute SQL and return a DataFrame."""
        logger.debug("SQL: %s", sql)
        conn = self._get_conn()
        try:
            df = pd.read_sql_query(sql, conn, params=params)
            # Standardize time index if present. format="ISO8601" tolerates
            # mixed-precision timestamps (some rows carry microseconds, some
            # don't) — the default parser infers one format from the first row
            # and then raises on any row of differing precision, which silently
            # emptied tables like body_composition / vo2_max.
            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"], format="ISO8601")
                df = df.set_index("time").sort_index()
            return df
        except Exception as e:
            return self._handle_query_error(e)
        finally:
            conn.close()

    @staticmethod
    def _handle_query_error(e: Exception) -> pd.DataFrame:
        """Swallow query errors as an empty frame — except lock contention.

        A locked database must surface to the caller: returning an empty
        DataFrame there renders as blank charts with no visible error.
        """
        if isinstance(e, sqlite3.OperationalError) and (
            "locked" in str(e).lower() or "busy" in str(e).lower()
        ):
            logger.error("Query hit lock contention: %s", e)
            raise e
        logger.error(f"Query failed: {e}")
        return pd.DataFrame()

    # Date bounds are parameterized (never interpolated into SQL) — they arrive
    # from unvalidated HTTP query params and LLM tool-call arguments.
    _TIME_RANGE = "time >= ? AND time <= ?"
    _DATE_RANGE_EXCL = "date >= ? AND date < ?"

    @staticmethod
    def _time_params(start: str, end: str) -> tuple[str, str]:
        """Bind params for a WHERE time clause (intraday / timestamped tables).

        SQLite compares strings lexicographically, which works for ISO8601.
        """
        return (f"{start}T00:00:00", f"{end}T23:59:59")

    # ------------------------------------------------------------------
    # Daily-granularity measurements
    # ------------------------------------------------------------------
    def query_daily_stats(
        self,
        start: str,
        end: str,
        fields: list[str] | None = None,
    ) -> pd.DataFrame:
        """DailyStats — RHR, stress, body battery, steps, etc.

        ``fields`` may be given as camelCase names (as used in cache.py).
        They are automatically mapped to the snake_case SQLite column names and
        aliased back so the returned DataFrame always has camelCase columns.
        """
        if fields:
            # snake_col AS "camelField" so the DataFrame uses the caller's names
            cols = ", ".join(
                f'{_DAILY_STATS_COLS.get(f, f)} AS "{f}"' for f in fields
            )
        else:
            cols = "*"
        # Filter by the 'date' column (not 'time') to avoid noon-UTC timestamps
        # from neighbouring days bleeding into the wrong day's summary. The end
        # is exclusive (date < end) so callers can pass next_day_str safely.
        q = f"SELECT {cols} FROM daily_stats WHERE {self._DATE_RANGE_EXCL}"
        return self._query(q, (start, end))

    def query_sleep_summary(
        self,
        start: str,
        end: str,
        fields: list[str] | None = None,
    ) -> pd.DataFrame:
        """SleepSummary — per-night sleep quality metrics."""
        if fields:
            cols = ", ".join(
                f'{_SLEEP_COLS.get(f, f)} AS "{f}"' for f in fields
            )
        else:
            cols = "*"
        q = f"SELECT {cols} FROM sleep_summary WHERE {self._DATE_RANGE_EXCL}"
        return self._query(q, (start, end))

    def query_training_readiness(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT * FROM training_readiness WHERE {self._TIME_RANGE}"
        return self._query(q, self._time_params(start, end))

    def query_training_status(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT * FROM training_status WHERE {self._TIME_RANGE}"
        return self._query(q, self._time_params(start, end))

    def query_body_composition(self, start: str, end: str) -> pd.DataFrame:
        """Rows whose LOCAL calendar day falls within [start, end].

        Weigh-in timestamps are stored in UTC; during BST a post-midnight
        weigh-in carries the previous UTC date, so filtering/keying on the raw
        timestamp shifted late-night readings to the wrong day. Widen the raw
        time filter by a day each side, then key on the local calendar day —
        exposed to callers as a ``date`` column.
        """
        q = f"SELECT * FROM body_composition WHERE {self._TIME_RANGE}"
        try:
            start_wide = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            end_wide = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        except ValueError:
            start_wide, end_wide = start, end
        df = self._query(q, self._time_params(start_wide, end_wide))
        if df.empty:
            return df
        df = df.assign(date=[utc_to_local_day(t) for t in df.index])
        return df[(df["date"] >= start) & (df["date"] <= end)]

    def query_fitness_age(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT * FROM fitness_age WHERE {self._TIME_RANGE}"
        return self._query(q, self._time_params(start, end))

    def query_vo2_max(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT * FROM vo2_max WHERE {self._TIME_RANGE}"
        return self._query(q, self._time_params(start, end))

    def query_endurance_score(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT * FROM endurance_score WHERE {self._TIME_RANGE}"
        return self._query(q, self._time_params(start, end))

    def query_race_predictions(self, start: str, end: str) -> pd.DataFrame:
        """Garmin race-time predictions (5k/10k/half/marathon, in seconds)."""
        q = f"SELECT * FROM race_predictions WHERE {self._TIME_RANGE}"
        return self._query(q, self._time_params(start, end))

    def query_hill_score(self, start: str, end: str) -> pd.DataFrame:
        """Garmin Hill Score — strength / endurance / overall climbing fitness."""
        q = f"SELECT * FROM hill_score WHERE {self._TIME_RANGE}"
        return self._query(q, self._time_params(start, end))

    def query_activities_with_gps(self, start: str, end: str) -> pd.DataFrame:
        """Activities in the window that have GPS track points stored."""
        q = """
            SELECT s.activity_id, s.time, s.activity_name, s.activity_type,
                   s.distance, s.elapsed_duration, s.average_hr, s.calories,
                   s.location_name,
                   (SELECT COUNT(*) FROM activity_gps g WHERE g.activity_id = s.activity_id) AS point_count
            FROM activity_summary s
            WHERE s.time BETWEEN :start AND :end
              AND COALESCE(s.activity_name, '') != 'END'
              AND COALESCE(s.activity_type, '') != 'No Activity'
              AND EXISTS (SELECT 1 FROM activity_gps g WHERE g.activity_id = s.activity_id)
            ORDER BY s.time DESC
        """
        try:
            return self._query(
                q,
                {"start": f"{start}T00:00:00", "end": f"{end}T23:59:59"},
            )
        except Exception as e:
            logger.error("query_activities_with_gps failed: %s", e)
            return pd.DataFrame()

    def query_activity_gps(self, activity_id: int) -> pd.DataFrame:
        """GPS track + per-point metrics for a single activity."""
        q = """
            SELECT time, latitude, longitude, altitude, distance, heart_rate,
                   speed, cadence, power, temperature
            FROM activity_gps
            WHERE activity_id = :aid
              AND latitude IS NOT NULL AND longitude IS NOT NULL
            ORDER BY time
        """
        try:
            return self._query(q, {"aid": activity_id})
        except Exception as e:
            logger.error("query_activity_gps failed: %s", e)
            return pd.DataFrame()

    def query_activity_export(self, activity_id: int) -> dict:
        """All stats for one activity — summary row + GPS-derived aggregates.

        Returns a dict with keys:
          summary  — full activity_summary row as a dict
          gps      — GPS-derived aggregates (elevation, cadence, power, temp)
                     only populated for fields that have non-null values
        """
        import numpy as np

        # Full summary row
        sq = "SELECT * FROM activity_summary WHERE activity_id = :aid LIMIT 1"
        summary_df = self._query(sq, {"aid": activity_id})
        if summary_df.empty:
            return {}
        # reset_index to bring time back as a column (it may be the index)
        summary_df = summary_df.reset_index()
        row = summary_df.iloc[0].where(summary_df.iloc[0].notna(), other=None).to_dict()

        # GPS aggregates (no lat/lon)
        gq = """
            SELECT altitude, cadence, power, temperature
            FROM activity_gps
            WHERE activity_id = :aid
            ORDER BY time
        """
        gps_df = self._query(gq, {"aid": activity_id})
        gps: dict = {}
        if not gps_df.empty:
            alt = gps_df["altitude"].dropna()
            if not alt.empty:
                gains = alt.diff().clip(lower=0).sum()
                losses = alt.diff().clip(upper=0).sum()
                gps["elevation_gain_m"] = round(float(gains), 1)
                gps["elevation_loss_m"] = round(float(losses), 1)
            for col, label in [("cadence", "cadence_spm"), ("power", "power_w"), ("temperature", "temp_c")]:
                series = gps_df[col].dropna() if col in gps_df.columns else pd.Series([], dtype=float)
                if not series.empty and series.max() > 0:
                    gps[f"avg_{label}"] = round(float(series.mean()), 1)
                    gps[f"max_{label}"] = round(float(series.max()), 1)

        return {"summary": row, "gps": gps}

    def query_environment(self, start: str, end: str) -> pd.DataFrame:
        """Daily weather + air-quality + pollen rows from environment_daily.

        Returns an empty DataFrame (no rows, no error) when the table is
        missing or the user hasn't configured HOME_LAT/HOME_LON. Callers can
        check df.empty without try/except.
        """
        try:
            q = (
                "SELECT * FROM environment_daily "
                "WHERE date >= ? AND date <= ? ORDER BY date"
            )
            return self._query(q, (start, end))
        except Exception as exc:
            logger.debug("environment_daily query failed (table may not exist): %s", exc)
            return pd.DataFrame()

    def query_ha_sensors(self, start: str, end: str) -> pd.DataFrame:
        """Daily HA sensor aggregates from ha_sensor_daily."""
        try:
            q = (
                "SELECT date, entity_id, mean_value, min_value, max_value, "
                "overnight_mean, unit FROM ha_sensor_daily "
                "WHERE date >= ? AND date <= ? ORDER BY date, entity_id"
            )
            return self._query(q, (start, end))
        except Exception as exc:
            logger.debug("ha_sensor_daily query failed (table may not exist): %s", exc)
            return pd.DataFrame()

    def query_menstrual_cycle(self, start: str, end: str) -> pd.DataFrame:
        q = (
            "SELECT date, cycle_start_date, current_day_of_cycle, current_cycle_phase, "
            "cycle_length, predicted_cycle_length, period_length, menstrual_flow, "
            "pregnancy_status, symptoms, mood, notes "
            f"FROM menstrual_cycle WHERE date BETWEEN :start AND :end ORDER BY date"
        )
        try:
            return self._query(q, {"start": start, "end": end})
        except Exception:
            # Table may not exist yet if the fetcher hasn't been re-run since the upgrade.
            return pd.DataFrame()

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
        """LifestyleJournal — behaviour status & value by day.

        lifestyle_journal is keyed by a 'date' TEXT column (YYYY-MM-DD), not
        'time' — see sqlite_manager.py.
        """
        where = "date >= ? AND date <= ?"
        params = [start, end]
        if behavior:
            where += " AND behavior = ?"
            params.append(behavior)
        if category:
            where += " AND category = ?"
            params.append(category)

        q = f"SELECT * FROM lifestyle_journal WHERE {where}"
        logger.debug("SQL: %s Params: %s", q, params)
        conn = self._get_conn()
        try:
            df = pd.read_sql_query(q, conn, params=params)
            if "date" in df.columns:
                df["time"] = pd.to_datetime(df["date"])
                df = df.set_index("time").sort_index()
            return df
        except Exception as e:
            return self._handle_query_error(e)
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
        where = self._TIME_RANGE
        params = list(self._time_params(start, end))
        if activity_type:
            where += " AND activity_type = ?"
            params.append(activity_type)

        q = f"SELECT * FROM activity_summary WHERE {where}"
        logger.debug("SQL: %s Params: %s", q, params)
        conn = self._get_conn()
        try:
            df = pd.read_sql_query(q, conn, params=params)
            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"], format="ISO8601")
                df = df.set_index("time").sort_index()
            return df
        except Exception as e:
            return self._handle_query_error(e)
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
        q = f"SELECT stress_level FROM stress_intraday WHERE {self._TIME_RANGE}"
        return self._query(q, (start_ts, end_ts))

    def query_body_battery_intraday(self, date: str) -> pd.DataFrame:
        q = (
            f"SELECT body_battery_level FROM body_battery_intraday "
            f"WHERE {self._TIME_RANGE}"
        )
        return self._query(q, self._time_params(date, date))

    def query_hrv_intraday(self, start: str, end: str) -> pd.DataFrame:
        q = f"SELECT hrv_value FROM hrv_intraday WHERE {self._TIME_RANGE}"
        return self._query(q, self._time_params(start, end))

    def query_heart_rate_intraday(self, date: str) -> pd.DataFrame:
        q = (
            f"SELECT heart_rate FROM heart_rate_intraday "
            f"WHERE {self._TIME_RANGE}"
        )
        return self._query(q, self._time_params(date, date))

    def query_steps_intraday(self, date: str) -> pd.DataFrame:
        q = (
            f"SELECT steps_count FROM steps_intraday "
            f"WHERE {self._TIME_RANGE}"
        )
        return self._query(q, self._time_params(date, date))

    def query_breathing_rate_intraday(self, date: str) -> pd.DataFrame:
        q = (
            f"SELECT breathing_rate FROM breathing_rate_intraday "
            f"WHERE {self._TIME_RANGE}"
        )
        return self._query(q, self._time_params(date, date))

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
        """Return the earliest and latest dates with daily_stats data."""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT MIN(date), MAX(date) FROM daily_stats")
            min_date, max_date = cursor.fetchone()
            if not min_date or not max_date:
                today = datetime.now().strftime("%Y-%m-%d")
                return today, today
            return min_date, max_date
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
