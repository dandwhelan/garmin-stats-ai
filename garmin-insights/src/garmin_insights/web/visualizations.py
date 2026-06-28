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

from garmin_insights.stats_utils import correlate_pair, finalize_correlations

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
        return sqlite3.connect(self.db_path, timeout=10)

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
                   weekly_training_load,
                   heat_acclimation_percentage AS heat_acclimation,
                   altitude_acclimation_percentage AS altitude_acclimation,
                   heat_trend
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
        cols = "weight, bmi, body_fat, muscle_mass, body_water, visceral_fat"
        with self._conn() as conn:
            df = pd.read_sql_query(
                f"SELECT substr(time, 1, 10) AS date, {cols} FROM body_composition "
                "WHERE time >= ? AND time <= ? ORDER BY time",
                conn, params=(f"{start}T00:00:00", f"{end}T23:59:59"),
            )
            # Body composition is slow-moving and often logged irregularly (e.g.
            # only when the user steps on the scale). If the requested window has
            # no readings, fall back to the last ~year so a recent weight trend
            # still shows instead of a blank card. The frontend labels real dates,
            # so the older points stay honest.
            if df.empty:
                try:
                    year_ago = (datetime.fromisoformat(end) - timedelta(days=365)).date().isoformat()
                except ValueError:
                    year_ago = start
                df = pd.read_sql_query(
                    f"SELECT substr(time, 1, 10) AS date, {cols} FROM body_composition "
                    "WHERE time >= ? ORDER BY time",
                    conn, params=(f"{year_ago}T00:00:00",),
                )
        if df.empty:
            return []
        df = df.groupby("date", as_index=False).mean(numeric_only=True)
        # Garmin Connect stores weight in GRAMS. Convert to kg for display; the
        # >1000 guard avoids double-converting any row already in kg.
        if "weight" in df.columns:
            df["weight"] = df["weight"].where(df["weight"] <= 1000, df["weight"] / 1000.0)
        for col in ("weight", "bmi", "body_fat", "muscle_mass", "body_water", "visceral_fat"):
            if col in df.columns:
                df[col] = df[col].round(2)
        return df.to_dict(orient="records")

    # ------------------------------------------------------------------
    # 3b. Fitness trajectory — VO2 max, race predictions, endurance, hill
    # ------------------------------------------------------------------
    def fitness_trajectory(self, start: str, end: str) -> dict:
        """Slow-moving fitness markers for the Fitness Trajectory dashboard card.

        These update infrequently, so a short dashboard window (e.g. 14 days)
        would usually be blank. We extend the lookback to at least ~180 days
        from ``end`` so the VO2 max / race-prediction trend is visible, while
        still labelling real dates.
        """
        try:
            series_start = (datetime.fromisoformat(end) - timedelta(days=180)).date().isoformat()
        except ValueError:
            series_start = start
        if series_start > start:
            series_start = start
        lo, hi = f"{series_start}T00:00:00", f"{end}T23:59:59"
        out: dict = {"available": False}

        def _daily(sql: str) -> pd.DataFrame:
            # Tables can be legitimately absent on older DBs / device models that
            # don't report these markers — treat "no such table" as empty data.
            try:
                with self._conn() as conn:
                    return pd.read_sql_query(sql, conn, params=(lo, hi))
            except Exception as exc:
                logger.debug("fitness_trajectory query skipped: %s", exc)
                return pd.DataFrame()

        # VO2 max (running + cycling) — one row per day, latest reading wins
        vo2 = _daily(
            "SELECT substr(time,1,10) AS date, vo2_max_value AS running, "
            "vo2_max_value_cycling AS cycling FROM vo2_max "
            "WHERE time >= ? AND time <= ? ORDER BY time"
        )
        if not vo2.empty:
            vo2 = vo2.groupby("date", as_index=False).last()
            out["vo2_max"] = [
                {k: (round(float(v), 1) if isinstance(v, (int, float)) and pd.notna(v) else None)
                 for k, v in r.items()}
                for r in vo2.to_dict(orient="records")
            ]
            out["available"] = True

        # Race predictions (seconds) — surfaced in minutes for the chart axis
        rp = _daily(
            "SELECT substr(time,1,10) AS date, time_5k, time_10k, "
            "time_half_marathon, time_marathon FROM race_predictions "
            "WHERE time >= ? AND time <= ? ORDER BY time"
        )
        if not rp.empty:
            rp = rp.groupby("date", as_index=False).last()
            recs = []
            for r in rp.to_dict(orient="records"):
                rec = {"date": r["date"]}
                for src, dst in (("time_5k", "5k"), ("time_10k", "10k"),
                                 ("time_half_marathon", "half"), ("time_marathon", "marathon")):
                    v = r.get(src)
                    rec[dst] = round(float(v) / 60.0, 1) if v and pd.notna(v) and v > 0 else None
                recs.append(rec)
            out["race_predictions"] = recs
            out["available"] = True

        # Endurance score — single integer per day
        es = _daily(
            "SELECT substr(time,1,10) AS date, endurance_score AS value "
            "FROM endurance_score WHERE time >= ? AND time <= ? ORDER BY time"
        )
        if not es.empty:
            es = es.groupby("date", as_index=False).last()
            out["endurance"] = [
                {"date": r["date"], "value": int(r["value"])}
                for r in es.to_dict(orient="records") if pd.notna(r["value"])
            ]
            out["available"] = True

        # Hill score — latest snapshot only (overall/strength/endurance)
        hs = _daily(
            "SELECT substr(time,1,10) AS date, overall_score, strength_score, "
            "endurance_score FROM hill_score WHERE time >= ? AND time <= ? ORDER BY time"
        )
        if not hs.empty:
            last = hs.dropna(subset=["overall_score"]).tail(1)
            if not last.empty:
                row = last.iloc[0]
                out["hill_latest"] = {
                    "date": row["date"],
                    "overall": int(row["overall_score"]) if pd.notna(row["overall_score"]) else None,
                    "strength": int(row["strength_score"]) if pd.notna(row["strength_score"]) else None,
                    "endurance": int(row["endurance_score"]) if pd.notna(row["endurance_score"]) else None,
                }
                out["available"] = True

        return out

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
            SELECT date, time, sleep_start, sleep_end, sleep_time_seconds, sleep_score
            FROM sleep_summary
            WHERE date >= ? AND date <= ?
            ORDER BY date
        """
        with self._conn() as conn:
            df = pd.read_sql_query(sql, conn, params=(start, end))
        out: list[dict] = []
        for _, r in df.iterrows():
            secs = r["sleep_time_seconds"]
            try:
                # Prefer stored timestamps; fall back to deriving from wake - duration.
                if r.get("sleep_end") and not str(r["sleep_end"]) in ("", "None", "nan"):
                    wake_dt = datetime.fromisoformat(str(r["sleep_end"]).replace("Z", "").split(".")[0])
                else:
                    t = r["time"]
                    if not t or not secs:
                        continue
                    wake_dt = datetime.fromisoformat(str(t).replace("Z", "").split(".")[0])

                if r.get("sleep_start") and not str(r["sleep_start"]) in ("", "None", "nan"):
                    bed_dt = datetime.fromisoformat(str(r["sleep_start"]).replace("Z", "").split(".")[0])
                elif secs:
                    bed_dt = wake_dt - timedelta(seconds=int(secs))
                else:
                    continue
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
            score = r.get("sleep_score")
            out.append({
                "date": r["date"],
                "dow": dow,
                "bedtime": round(bed_h, 2),
                "waketime": round(wake_h, 2),
                "duration_h": round(int(secs) / 3600, 2),
                "score": int(score) if score is not None and not (isinstance(score, float) and score != score) else None,
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

    # ------------------------------------------------------------------
    # 9. Environment ↔ recovery overlay & correlations
    # ------------------------------------------------------------------
    def environment_recovery(self, start: str, end: str) -> dict:
        """Join environment_daily with daily_summaries on date and return
        date-aligned arrays for the overlay chart plus Pearson correlations
        between environmental drivers and physiological recovery markers.

        Returns `available: false` when the user has no environment_daily
        rows (HOME_LAT/HOME_LON unset) so the frontend can hide the section.
        """
        with self._conn() as conn:
            try:
                env = pd.read_sql_query(
                    "SELECT date, apparent_temp_max_c, temp_max_c, european_aqi, "
                    "pm25, o3, pollen_grass, pollen_birch, pollen_ragweed, "
                    "pollen_alder, pollen_olive, pollen_mugwort "
                    "FROM environment_daily "
                    "WHERE date >= ? AND date <= ? ORDER BY date",
                    conn, params=(start, end),
                )
            except Exception as exc:
                logger.debug("environment_recovery: env query failed: %s", exc)
                return {"available": False, "dates": [], "entries": []}
            if env.empty:
                return {"available": False, "dates": [], "entries": []}
            summaries = pd.read_sql_query(
                "SELECT date, metric_json FROM daily_summaries "
                "WHERE date >= ? AND date <= ? ORDER BY date",
                conn, params=(start, end),
            )

        # Extract physiological markers from metric_json
        recovery_keys = (
            "restingHeartRate", "avgOvernightHrv",
            "averageRespirationValue", "sleepScore",
        )
        recovery_rows = []
        for _, r in summaries.iterrows():
            try:
                m = json.loads(r["metric_json"]) if r["metric_json"] else {}
            except Exception:
                m = {}
            recovery_rows.append({"date": r["date"], **{k: m.get(k) for k in recovery_keys}})
        rec_df = pd.DataFrame(recovery_rows) if recovery_rows else pd.DataFrame(columns=("date", *recovery_keys))

        # Compose per-species pollen peak per row (max over species, ignoring None)
        pollen_cols = ["pollen_grass", "pollen_birch", "pollen_ragweed",
                       "pollen_alder", "pollen_olive", "pollen_mugwort"]
        env = env.copy()
        env["pollen_peak"] = env[pollen_cols].max(axis=1, skipna=True)

        merged = env.merge(rec_df, on="date", how="left")

        # Buekers 2023: pollen impacts RHR the *following* day.
        # Add a pollen_peak_lag1 column = previous day's pollen peak.
        merged = merged.sort_values("date").reset_index(drop=True)
        merged["pollen_peak_lag1"] = merged["pollen_peak"].shift(1)

        # Build per-date entries for the chart
        def _f(v):
            if v is None:
                return None
            try:
                if pd.isna(v):
                    return None
            except (TypeError, ValueError):
                pass
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return None

        entries = []
        for _, r in merged.iterrows():
            entries.append({
                "date": r["date"],
                "apparent_temp_max_c": _f(r.get("apparent_temp_max_c")),
                "temp_max_c": _f(r.get("temp_max_c")),
                "european_aqi": _f(r.get("european_aqi")),
                "pm25": _f(r.get("pm25")),
                "o3": _f(r.get("o3")),
                "pollen_peak": _f(r.get("pollen_peak")),
                "restingHeartRate": _f(r.get("restingHeartRate")),
                "avgOvernightHrv": _f(r.get("avgOvernightHrv")),
                "averageRespirationValue": _f(r.get("averageRespirationValue")),
                "sleepScore": _f(r.get("sleepScore")),
            })

        # Pearson correlations between env drivers and recovery markers.
        # Same-day for heat / AQ / PM2.5, next-day (lag-1) for pollen
        # (Buekers 2023: allergy load → next-day RHR).
        env_drivers = [
            ("apparent_temp_max_c", "same-day"),
            ("european_aqi",        "same-day"),
            ("pm25",                "same-day"),
            ("pollen_peak_lag1",    "next-day"),
        ]
        recovery_markers = list(recovery_keys)
        correlations: list[dict] = []
        for driver, lag_label in env_drivers:
            for marker in recovery_markers:
                if driver not in merged.columns or marker not in merged.columns:
                    continue
                pair = merged[[driver, marker]].apply(pd.to_numeric, errors="coerce").dropna()
                correlations.append(correlate_pair(
                    pair[driver], pair[marker],
                    driver=driver, marker=marker, lag=lag_label,
                ))
        # FDR-correct across all env↔recovery pairs tested in this window so the
        # UI can mark which r values survive multiple-comparison correction.
        finalize_correlations(correlations)

        return {
            "available": True,
            "start": start,
            "end": end,
            "entries": entries,
            "correlations": correlations,
            "notes": (
                "Pollen↔RHR uses next-day lag per Buekers 2023 (n=72, 2,497 days). "
                "Heat/AQ/PM2.5↔HRV use same-day per Niu 2020 meta-analysis. "
                "r values are Pearson; treat |r|<0.2 as noise, 0.2-0.4 as weak, "
                ">0.4 as moderate. 'significant' is Benjamini-Hochberg FDR-"
                "corrected (q=0.05) across all pairs; 'p' is the two-sided "
                "Pearson p-value. Unmarked pairs are within-noise."
            ),
        }

    def _logged_behavior_days(self, conn, behavior: str, start: str, end: str) -> set[str]:
        try:
            rows = conn.execute(
                "SELECT date FROM lifestyle_journal "
                "WHERE behavior = ? AND status = 1 AND date >= ? AND date <= ?",
                (behavior, start, end),
            ).fetchall()
        except sqlite3.Error:
            return set()
        return {str(r[0])[:10] for r in rows}

    def _recovery_by_date(self, conn, start: str, end: str) -> dict[str, dict]:
        rows = conn.execute(
            "SELECT date, metric_json FROM daily_summaries "
            "WHERE date >= ? AND date <= ? ORDER BY date",
            (start, end),
        ).fetchall()
        keep = ("restingHeartRate", "avgOvernightHrv",
                "averageRespirationValue", "sleepScore", "awakeCount")
        out: dict[str, dict] = {}
        for d, j in rows:
            try:
                m = json.loads(j) if j else {}
            except Exception:
                m = {}
            out[str(d)[:10]] = {k: m.get(k) for k in keep}
        return out

    def behavior_environment_impact(
        self,
        behavior: str,
        env_columns: list[str],
        start: str,
        end: str,
    ) -> dict:
        """For a logged behavior (e.g. 'Allergy Symptoms', 'Asthma symptoms'),
        return per-day env drivers + same-day recovery markers, split into
        on/off groups, plus Pearson r and mean deltas.

        Used for the pollen×allergy and AQ×asthma cross-tab charts.
        """
        with self._conn() as conn:
            logged = self._logged_behavior_days(conn, behavior, start, end)
            try:
                cols = ", ".join(env_columns)
                env = pd.read_sql_query(
                    f"SELECT date, {cols} FROM environment_daily "
                    "WHERE date >= ? AND date <= ? ORDER BY date",
                    conn, params=(start, end),
                )
            except Exception as exc:
                logger.debug("behavior_environment_impact: env query failed: %s", exc)
                return {"available": False, "behavior": behavior, "entries": []}
            if env.empty:
                return {"available": False, "behavior": behavior, "entries": []}
            recovery = self._recovery_by_date(conn, start, end)

        def _f(v):
            if v is None:
                return None
            try:
                if pd.isna(v):
                    return None
            except (TypeError, ValueError):
                pass
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return None

        entries = []
        on_rows, off_rows = [], []
        for _, r in env.iterrows():
            d = str(r["date"])[:10]
            rec = recovery.get(d, {})
            entry = {
                "date": d,
                "logged": d in logged,
                **{c: _f(r.get(c)) for c in env_columns},
                "restingHeartRate": _f(rec.get("restingHeartRate")),
                "avgOvernightHrv": _f(rec.get("avgOvernightHrv")),
                "averageRespirationValue": _f(rec.get("averageRespirationValue")),
                "sleepScore": _f(rec.get("sleepScore")),
            }
            entries.append(entry)
            (on_rows if entry["logged"] else off_rows).append(entry)

        def _mean(rows, key):
            xs = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
            return round(sum(xs) / len(xs), 2) if xs else None

        recovery_keys = ("restingHeartRate", "avgOvernightHrv",
                         "averageRespirationValue", "sleepScore")
        deltas = {}
        for k in recovery_keys:
            on_v, off_v = _mean(on_rows, k), _mean(off_rows, k)
            deltas[k] = {
                "on": on_v, "off": off_v,
                "delta": round(on_v - off_v, 2) if (on_v is not None and off_v is not None) else None,
                "n_on": sum(1 for r in on_rows if isinstance(r.get(k), (int, float))),
                "n_off": sum(1 for r in off_rows if isinstance(r.get(k), (int, float))),
            }

        # Pearson r: env driver vs recovery marker across all days
        correlations = []
        df = pd.DataFrame(entries)
        for driver in env_columns:
            for marker in recovery_keys:
                if driver not in df.columns or marker not in df.columns:
                    continue
                pair = df[[driver, marker]].apply(pd.to_numeric, errors="coerce").dropna()
                correlations.append(correlate_pair(
                    pair[driver], pair[marker], driver=driver, marker=marker,
                ))
        finalize_correlations(correlations)

        return {
            "available": True,
            "behavior": behavior,
            "start": start, "end": end,
            "entries": entries,
            "deltas": deltas,
            "correlations": correlations,
            "n_logged": len(on_rows),
            "n_unlogged": len(off_rows),
        }

    def bedroom_temp_sleep(self, start: str, end: str) -> dict:
        """Overnight bedroom temperature vs sleep score / HRV / awakenings.

        Reads ha_sensor_daily (overnight 22:00-08:00 mean) joined to
        daily_summaries on date. Returns `available: false` when no HA
        bedroom-temp entity is configured / has no data yet.
        """
        with self._conn() as conn:
            try:
                ha = pd.read_sql_query(
                    "SELECT date, entity_id, mean_value, overnight_mean "
                    "FROM ha_sensor_daily "
                    "WHERE date >= ? AND date <= ? "
                    "AND lower(entity_id) LIKE '%bedroom%' "
                    "ORDER BY date",
                    conn, params=(start, end),
                )
            except Exception as exc:
                logger.debug("bedroom_temp_sleep: HA table missing: %s", exc)
                return {"available": False, "entries": []}
            if ha.empty:
                return {"available": False, "entries": []}
            recovery = self._recovery_by_date(conn, start, end)

        entries = []
        for _, r in ha.iterrows():
            d = str(r["date"])[:10]
            rec = recovery.get(d, {})
            entries.append({
                "date": d,
                "bedroom_overnight_c": r.get("overnight_mean"),
                "bedroom_mean_c": r.get("mean_value"),
                "sleepScore": rec.get("sleepScore"),
                "avgOvernightHrv": rec.get("avgOvernightHrv"),
                "awakeCount": rec.get("awakeCount"),
                "restingHeartRate": rec.get("restingHeartRate"),
            })

        df = pd.DataFrame(entries)
        correlations = []
        for driver in ("bedroom_overnight_c", "bedroom_mean_c"):
            for marker in ("sleepScore", "avgOvernightHrv", "awakeCount", "restingHeartRate"):
                pair = df[[driver, marker]].apply(pd.to_numeric, errors="coerce").dropna()
                correlations.append(correlate_pair(
                    pair[driver], pair[marker], driver=driver, marker=marker,
                ))
        finalize_correlations(correlations)

        return {
            "available": True,
            "start": start, "end": end,
            "entries": entries,
            "correlations": correlations,
            "notes": (
                "Bedroom T from Home Assistant. Overnight mean is 22:00–08:00. "
                "Baniak 2023: cooler bedroom T (16–19°C) associated with better sleep efficiency."
            ),
        }

    def behavior_root_cause(
        self,
        behavior: str,
        start: str,
        end: str,
        lookback_hours: int = 48,
    ) -> dict:
        """For each day the user logged `behavior` (e.g. 'Migraines'), surface
        the prior ~48h confounders: other lifestyle behaviors, env extremes,
        sleep / HRV / RHR deltas.

        Returns one entry per logged day. Used by the migraine root-cause panel.
        """
        with self._conn() as conn:
            try:
                target_days = sorted(self._logged_behavior_days(conn, behavior, start, end))
            except sqlite3.Error:
                return {"available": False, "behavior": behavior, "events": []}
            if not target_days:
                return {"available": True, "behavior": behavior, "events": []}
            try:
                lj = pd.read_sql_query(
                    "SELECT date, behavior, status FROM lifestyle_journal "
                    "WHERE date >= ? AND date <= ? AND status = 1",
                    conn, params=(start, end),
                )
            except sqlite3.Error:
                lj = pd.DataFrame(columns=("date", "behavior", "status"))
            try:
                env = pd.read_sql_query(
                    "SELECT date, apparent_temp_max_c, european_aqi, pm25, "
                    "humidity_mean, precipitation_mm FROM environment_daily "
                    "WHERE date >= ? AND date <= ?",
                    conn, params=(start, end),
                )
            except Exception:
                env = pd.DataFrame()
            recovery = self._recovery_by_date(conn, start, end)

        from datetime import datetime, timedelta
        env_by_date = {str(r["date"])[:10]: r for _, r in env.iterrows()} if not env.empty else {}
        lj_by_date: dict[str, list[str]] = {}
        for _, r in lj.iterrows():
            d = str(r["date"])[:10]
            if r["behavior"] == behavior:
                continue
            lj_by_date.setdefault(d, []).append(r["behavior"])

        days_back = max(1, lookback_hours // 24)
        events = []
        for d in target_days:
            try:
                d0 = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                continue
            window: list[str] = []
            for offset in range(days_back, 0, -1):
                wd = (d0 - timedelta(days=offset)).isoformat()
                for b in lj_by_date.get(wd, []):
                    window.append(f"{wd}: {b}")
            same_day = lj_by_date.get(d, [])
            envrow = env_by_date.get(d, {})
            rec_today = recovery.get(d, {})
            rec_prev = recovery.get((d0 - timedelta(days=1)).isoformat(), {})

            def _g(row, k):
                v = row.get(k) if hasattr(row, "get") else None
                try:
                    return None if v is None or pd.isna(v) else round(float(v), 1)
                except (TypeError, ValueError):
                    return None

            events.append({
                "date": d,
                "prior_behaviors": window,
                "same_day_behaviors": same_day,
                "env": {
                    "apparent_temp_max_c": _g(envrow, "apparent_temp_max_c"),
                    "european_aqi": _g(envrow, "european_aqi"),
                    "pm25": _g(envrow, "pm25"),
                    "humidity_mean": _g(envrow, "humidity_mean"),
                    "precipitation_mm": _g(envrow, "precipitation_mm"),
                },
                "recovery_today": rec_today,
                "recovery_prev_day": rec_prev,
            })

        return {
            "available": True,
            "behavior": behavior,
            "lookback_hours": lookback_hours,
            "events": events,
        }
