"""Data-shaping for dashboard visualizations.

Each method returns plain JSON-serialisable dicts/lists ready for the frontend.
Heavy lifting (SQL aggregation, rolling stats) is done here so the frontend
only renders.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


_INTRADAY_METRICS = {
    "stress":       ("stress_intraday",       "stress_level",       "AVG"),
    "body_battery": ("body_battery_intraday", "body_battery_level", "AVG"),
    "heart_rate":   ("heart_rate_intraday",   "heart_rate",         "AVG"),
    "steps":        ("steps_intraday",        "steps_count",        "SUM"),
}


class VisualizationService:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    # ------------------------------------------------------------------
    # 1. Intraday heatmap — 24h × N-day matrix
    # ------------------------------------------------------------------
    def intraday_heatmap(self, metric: str, days: int = 14) -> dict:
        if metric not in _INTRADAY_METRICS:
            return {"error": f"unknown metric '{metric}'", "available": list(_INTRADAY_METRICS)}
        table, col, agg = _INTRADAY_METRICS[metric]
        end = datetime.utcnow().date()
        start = end - timedelta(days=days - 1)
        sql = f"""
            SELECT substr(time, 1, 10) AS date,
                   CAST(substr(time, 12, 2) AS INTEGER) AS hour,
                   {agg}({col}) AS value
            FROM {table}
            WHERE time >= ? AND time <= ? AND {col} IS NOT NULL AND {col} >= 0
            GROUP BY date, hour
            ORDER BY date, hour
        """
        with self._conn() as conn:
            df = pd.read_sql_query(
                sql, conn,
                params=(f"{start.isoformat()}T00:00:00", f"{end.isoformat()}T23:59:59"),
            )
        if df.empty:
            return {"metric": metric, "dates": [], "hours": list(range(24)), "matrix": []}

        dates = sorted(df["date"].unique().tolist())
        index = {d: i for i, d in enumerate(dates)}
        matrix = [[None] * 24 for _ in dates]
        for _, r in df.iterrows():
            h = int(r["hour"])
            if 0 <= h < 24:
                matrix[index[r["date"]]][h] = round(float(r["value"]), 1)
        return {"metric": metric, "dates": dates, "hours": list(range(24)), "matrix": matrix}

    # ------------------------------------------------------------------
    # 2. Training load — ACWR series + readiness factor breakdown
    # ------------------------------------------------------------------
    def training(self, start: str, end: str) -> dict:
        ts_sql = """
            SELECT substr(time, 1, 10) AS date,
                   acwr_percent,
                   daily_training_load_acute AS acute_load,
                   daily_training_load_chronic AS chronic_load,
                   training_status,
                   weekly_training_load
            FROM training_status
            WHERE time >= ? AND time <= ?
            ORDER BY time
        """
        tr_sql = """
            SELECT substr(time, 1, 10) AS date,
                   score,
                   sleep_score_factor_percent       AS f_sleep,
                   recovery_time_factor_percent    AS f_recovery,
                   acwr_factor_percent             AS f_acwr,
                   stress_history_factor_percent   AS f_stress,
                   hrv_factor_percent              AS f_hrv
            FROM training_readiness
            WHERE time >= ? AND time <= ?
            ORDER BY time
        """
        params = (f"{start}T00:00:00", f"{end}T23:59:59")
        with self._conn() as conn:
            ts = pd.read_sql_query(ts_sql, conn, params=params)
            tr = pd.read_sql_query(tr_sql, conn, params=params)

        # Keep latest row per date
        if not ts.empty:
            ts = ts.groupby("date").last().reset_index()
        if not tr.empty:
            tr = tr.groupby("date").last().reset_index()

        return {
            "training_status": ts.to_dict(orient="records"),
            "training_readiness": tr.to_dict(orient="records"),
        }

    # ------------------------------------------------------------------
    # 3. Body composition — weight + body fat % + muscle mass trend
    # ------------------------------------------------------------------
    def body_composition(self, start: str, end: str) -> list[dict]:
        sql = """
            SELECT substr(time, 1, 10) AS date,
                   weight, bmi, body_fat, muscle_mass, body_water, visceral_fat
            FROM body_composition
            WHERE time >= ? AND time <= ?
            ORDER BY time
        """
        with self._conn() as conn:
            df = pd.read_sql_query(sql, conn, params=(f"{start}T00:00:00", f"{end}T23:59:59"))
        if df.empty:
            return []
        df = df.groupby("date", as_index=False).mean(numeric_only=True)
        for col in ("weight", "bmi", "body_fat", "muscle_mass", "body_water", "visceral_fat"):
            if col in df.columns:
                df[col] = df[col].round(2)
        return df.to_dict(orient="records")

    # ------------------------------------------------------------------
    # 4. HR-zone distribution by activity type
    # ------------------------------------------------------------------
    def hr_zones(self, start: str, end: str) -> dict:
        sql = """
            SELECT COALESCE(activity_type, 'unknown') AS activity_type,
                   SUM(COALESCE(hr_time_in_zone_1, 0)) AS z1,
                   SUM(COALESCE(hr_time_in_zone_2, 0)) AS z2,
                   SUM(COALESCE(hr_time_in_zone_3, 0)) AS z3,
                   SUM(COALESCE(hr_time_in_zone_4, 0)) AS z4,
                   SUM(COALESCE(hr_time_in_zone_5, 0)) AS z5,
                   COUNT(*) AS activity_count
            FROM activity_summary
            WHERE time >= ? AND time <= ?
            GROUP BY activity_type
            HAVING (z1 + z2 + z3 + z4 + z5) > 0
            ORDER BY (z1 + z2 + z3 + z4 + z5) DESC
            LIMIT 8
        """
        with self._conn() as conn:
            df = pd.read_sql_query(sql, conn, params=(f"{start}T00:00:00", f"{end}T23:59:59"))
        # Convert seconds → minutes for display
        for c in ("z1", "z2", "z3", "z4", "z5"):
            if c in df.columns:
                df[c] = (df[c].fillna(0) / 60.0).round(1)
        return {"by_type": df.to_dict(orient="records")}

    # ------------------------------------------------------------------
    # 5. Behavior-impact comparison
    # ------------------------------------------------------------------
    def behavior_impact(self, days: int = 90, min_occurrences: int = 3) -> list[dict]:
        end = datetime.utcnow().date()
        start = end - timedelta(days=days)
        with self._conn() as conn:
            lj = pd.read_sql_query(
                "SELECT date, behavior FROM lifestyle_journal "
                "WHERE date >= ? AND date <= ? AND status = 1",
                conn, params=(start.isoformat(), end.isoformat()),
            )
            ds = pd.read_sql_query(
                "SELECT date, metric_json FROM daily_summaries "
                "WHERE date >= ? AND date <= ?",
                conn, params=(start.isoformat(), end.isoformat()),
            )
        if ds.empty or lj.empty:
            return []

        def _val(j, k):
            try:
                return json.loads(j).get(k) if j else None
            except Exception:
                return None

        for k in ("sleepScore", "avgOvernightHrv", "restingHeartRate"):
            ds[k] = ds["metric_json"].apply(lambda j, kk=k: _val(j, kk))

        results = []
        for behavior in sorted(lj["behavior"].dropna().unique().tolist()):
            with_dates = set(lj[lj["behavior"] == behavior]["date"])
            if len(with_dates) < min_occurrences:
                continue
            ds_with = ds[ds["date"].isin(with_dates)]
            ds_without = ds[~ds["date"].isin(with_dates)]
            row: dict = {
                "behavior": behavior,
                "n_with": int(len(ds_with)),
                "n_without": int(len(ds_without)),
            }
            for k, label in (("sleepScore", "sleep"),
                              ("avgOvernightHrv", "hrv"),
                              ("restingHeartRate", "rhr")):
                w = ds_with[k].dropna().astype(float)
                wo = ds_without[k].dropna().astype(float)
                w_mean = round(float(w.mean()), 1) if len(w) else None
                wo_mean = round(float(wo.mean()), 1) if len(wo) else None
                row[f"{label}_with"] = w_mean
                row[f"{label}_without"] = wo_mean
                row[f"{label}_delta"] = (
                    round(w_mean - wo_mean, 1)
                    if w_mean is not None and wo_mean is not None
                    else None
                )
            results.append(row)
        results.sort(key=lambda r: abs(r.get("sleep_delta") or 0), reverse=True)
        return results

    # ------------------------------------------------------------------
    # 6. Correlation matrix between core daily metrics
    # ------------------------------------------------------------------
    def correlations(self, start: str, end: str) -> dict:
        keys = [
            "sleepScore", "avgOvernightHrv", "restingHeartRate",
            "stressPercentage", "bodyBatteryAtWakeTime", "totalSteps",
            "deepSleepSeconds", "remSleepSeconds",
            "moderateIntensityMinutes", "vigorousIntensityMinutes",
        ]
        with self._conn() as conn:
            rows = pd.read_sql_query(
                "SELECT date, metric_json FROM daily_summaries "
                "WHERE date >= ? AND date <= ? ORDER BY date",
                conn, params=(start, end),
            )
        if rows.empty:
            return {"keys": keys, "matrix": [[0] * len(keys) for _ in keys]}

        records = []
        for _, r in rows.iterrows():
            try:
                m = json.loads(r["metric_json"]) if r["metric_json"] else {}
            except Exception:
                m = {}
            records.append({k: m.get(k) for k in keys})
        df = pd.DataFrame(records)
        corr = df.corr(numeric_only=True).reindex(index=keys, columns=keys)
        # Replace NaN with 0 for serialization (insufficient data → no correlation)
        corr = corr.fillna(0).round(2)
        return {"keys": keys, "matrix": corr.values.tolist()}

    # ------------------------------------------------------------------
    # 7. Sleep timeline — bedtime/waketime drift
    # ------------------------------------------------------------------
    def sleep_timeline(self, start: str, end: str) -> list[dict]:
        sql = """
            SELECT date, time, sleep_time_seconds
            FROM sleep_summary
            WHERE date >= ? AND date <= ?
            ORDER BY date
        """
        with self._conn() as conn:
            df = pd.read_sql_query(sql, conn, params=(start, end))
        out: list[dict] = []
        for _, r in df.iterrows():
            t = r["time"]
            secs = r["sleep_time_seconds"]
            if not t or not secs:
                continue
            try:
                wake_dt = datetime.fromisoformat(str(t).replace("Z", "").split(".")[0])
                bed_dt = wake_dt - timedelta(seconds=int(secs))
            except Exception:
                continue
            bed_h = bed_dt.hour + bed_dt.minute / 60
            wake_h = wake_dt.hour + wake_dt.minute / 60
            # Normalise: if bed_h is in the morning (after midnight), shift +24
            # so the bedtime line plots continuously around midnight.
            if bed_h < 12:
                bed_h += 24
            # Day-of-week: 0=Mon..6=Sun
            try:
                dow = datetime.fromisoformat(r["date"]).weekday()
            except Exception:
                dow = None
            out.append({
                "date": r["date"],
                "dow": dow,
                "bedtime": round(bed_h, 2),
                "waketime": round(wake_h, 2),
                "duration_h": round(int(secs) / 3600, 2),
            })
        return out

    # ------------------------------------------------------------------
    # 8. Anomaly calendar — z-scores per metric per day
    # ------------------------------------------------------------------
    def anomaly_calendar(self, start: str, end: str) -> dict:
        keys = [
            "sleepScore", "avgOvernightHrv", "restingHeartRate",
            "stressPercentage", "bodyBatteryAtWakeTime",
        ]
        # Pull a 30-day buffer before `start` so rolling stats are populated
        d_start = datetime.fromisoformat(start).date()
        buffered_start = (d_start - timedelta(days=30)).isoformat()

        with self._conn() as conn:
            rows = pd.read_sql_query(
                "SELECT date, metric_json FROM daily_summaries "
                "WHERE date >= ? AND date <= ? ORDER BY date",
                conn, params=(buffered_start, end),
            )
        if rows.empty:
            return {"dates": [], "keys": keys, "matrix": [[None] * 0 for _ in keys]}

        records = []
        for _, r in rows.iterrows():
            try:
                m = json.loads(r["metric_json"]) if r["metric_json"] else {}
            except Exception:
                m = {}
            records.append({"date": r["date"], **{k: m.get(k) for k in keys}})
        df = pd.DataFrame(records)

        z_per_key: list[list[float | None]] = []
        for k in keys:
            s = pd.to_numeric(df[k], errors="coerce")
            roll_mean = s.rolling(30, min_periods=7).mean()
            roll_std = s.rolling(30, min_periods=7).std()
            z = (s - roll_mean) / roll_std
            z_per_key.append(z.tolist())

        # Trim to the requested window (keep z-scores aligned to dates)
        mask = (df["date"] >= start).tolist()
        dates = [d for d, m in zip(df["date"].tolist(), mask) if m]
        matrix = []
        for z_row in z_per_key:
            trimmed = [
                None if v is None or (isinstance(v, float) and np.isnan(v))
                else round(float(v), 2)
                for v, m in zip(z_row, mask) if m
            ]
            matrix.append(trimmed)
        return {"dates": dates, "keys": keys, "matrix": matrix}
