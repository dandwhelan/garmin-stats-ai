"""Lifestyle-focused visualizations.

Research-backed views built from `lifestyle_journal` plus the existing metrics
tables. Each method returns JSON-serialisable structures.

References cited inline: Pietilä 2018 (alcohol/HRV), Drake 2013 (caffeine cutoff),
Windred 2024 (sleep regularity & mortality), Quer 2021 (illness signature),
Kellmann 2018 (overtraining).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class LifestyleService:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _load_summaries(self, start: str, end: str) -> pd.DataFrame:
        with self._conn() as conn:
            df = pd.read_sql_query(
                "SELECT date, metric_json FROM daily_summaries "
                "WHERE date >= ? AND date <= ? ORDER BY date",
                conn, params=(start, end),
            )
        if df.empty:
            return pd.DataFrame()
        records = []
        for _, r in df.iterrows():
            try:
                m = json.loads(r["metric_json"]) if r["metric_json"] else {}
            except Exception:
                m = {}
            m["date"] = r["date"]
            records.append(m)
        return pd.DataFrame(records)

    def _load_journal(self, start: str, end: str) -> pd.DataFrame:
        with self._conn() as conn:
            return pd.read_sql_query(
                "SELECT date, behavior, category, status, value "
                "FROM lifestyle_journal WHERE date >= ? AND date <= ?",
                conn, params=(start, end),
            )

    # ------------------------------------------------------------------
    # 1. Behavior dose-response (behaviors logged with numeric value)
    # ------------------------------------------------------------------
    def behavior_dose_response(self, start: str, end: str) -> dict:
        lj = self._load_journal(start, end)
        ds = self._load_summaries(start, end)
        if lj.empty or ds.empty:
            return {"behaviors": []}
        lj = lj[lj["status"] == 1].dropna(subset=["value"])
        if lj.empty:
            return {"behaviors": []}

        ds = ds.set_index("date")
        out = []
        for behavior, group in lj.groupby("behavior"):
            if len(group) < 5:
                continue
            points: list[dict] = []
            for _, r in group.iterrows():
                row = ds.loc[r["date"]] if r["date"] in ds.index else None
                if row is None:
                    continue
                points.append({
                    "date": r["date"],
                    "value": float(r["value"]),
                    "sleepScore": _safe(row.get("sleepScore")),
                    "deepSleepHours": _safe_div(row.get("deepSleepSeconds"), 3600),
                    "hrv": _safe(row.get("avgOvernightHrv")),
                    "rhr": _safe(row.get("restingHeartRate")),
                })
            if points:
                out.append({"behavior": behavior, "n": len(points), "points": points})
        out.sort(key=lambda b: b["n"], reverse=True)
        return {"behaviors": out}

    # ------------------------------------------------------------------
    # 2. Caffeine cutoff — Late vs early vs none
    # ------------------------------------------------------------------
    def caffeine_cutoff(self, start: str, end: str) -> dict:
        lj = self._load_journal(start, end)
        ds = self._load_summaries(start, end)
        if lj.empty or ds.empty:
            return {"groups": []}
        lj = lj[lj["status"] == 1]

        late_dates = set(lj[lj["behavior"].str.contains("Late Caffeine", case=False, na=False)]["date"])
        any_dates = set(lj[lj["behavior"].str.contains("Caffeine", case=False, na=False)]["date"])
        early_dates = any_dates - late_dates

        def _stats(dates: set[str], label: str) -> dict:
            sub = ds[ds["date"].isin(dates)]
            row = {"group": label, "n": int(len(sub))}
            for k, label_k in (("sleepScore", "sleep_score"),
                                ("deepSleepSeconds", "deep_sleep_h"),
                                ("avgOvernightHrv", "hrv"),
                                ("awakeCount", "awakenings")):
                vals = pd.to_numeric(sub[k], errors="coerce").dropna() if k in sub.columns else pd.Series([], dtype=float)
                if not len(vals):
                    row[label_k] = None
                    continue
                if k == "deepSleepSeconds":
                    row[label_k] = round(float(vals.mean()) / 3600, 2)
                else:
                    row[label_k] = round(float(vals.mean()), 1)
            return row

        groups = [
            _stats(late_dates, "Late caffeine"),
            _stats(early_dates, "Early-only"),
            _stats(set(ds["date"]) - any_dates, "No caffeine"),
        ]
        return {"groups": groups}

    # ------------------------------------------------------------------
    # 3. Sleep regularity index (proxy)
    # ------------------------------------------------------------------
    def sleep_regularity(self, start: str, end: str) -> dict:
        """Proxy SRI: 100 - (std of sleep midpoint in hours over a 7-day window) * 25.

        Perfectly regular sleeper scores 100; every hour of variance ~ -25 pts.
        """
        with self._conn() as conn:
            df = pd.read_sql_query(
                "SELECT date, time, sleep_time_seconds FROM sleep_summary "
                "WHERE date >= ? AND date <= ? ORDER BY date",
                conn, params=(start, end),
            )
        if df.empty:
            return {"series": [], "current": None}

        midpoints = []
        for _, r in df.iterrows():
            try:
                wake = datetime.fromisoformat(str(r["time"]).replace("Z", "").split(".")[0])
                bed = wake - timedelta(seconds=int(r["sleep_time_seconds"] or 0))
                mid = bed + (wake - bed) / 2
                ref = mid.replace(hour=12, minute=0, second=0, microsecond=0)
                if mid < ref:
                    ref -= timedelta(days=1)
                midpoints.append({"date": r["date"], "mid_h": (mid - ref).total_seconds() / 3600})
            except Exception:
                continue

        if not midpoints:
            return {"series": [], "current": None}

        m = pd.DataFrame(midpoints)
        m["sri"] = 100 - (m["mid_h"].rolling(7, min_periods=3).std() * 25)
        m["sri"] = m["sri"].clip(lower=0, upper=100).round(1)
        series = [{"date": r["date"], "sri": (None if pd.isna(r["sri"]) else float(r["sri"]))}
                  for _, r in m.iterrows()]
        current = float(m["sri"].iloc[-1]) if not pd.isna(m["sri"].iloc[-1]) else None
        return {"series": series, "current": current}

    # ------------------------------------------------------------------
    # 4. Social jet lag — weekday vs weekend midpoint
    # ------------------------------------------------------------------
    def social_jet_lag(self, start: str, end: str) -> dict:
        with self._conn() as conn:
            df = pd.read_sql_query(
                "SELECT date, time, sleep_time_seconds FROM sleep_summary "
                "WHERE date >= ? AND date <= ? ORDER BY date",
                conn, params=(start, end),
            )
        weekday_mids: list[float] = []
        weekend_mids: list[float] = []
        for _, r in df.iterrows():
            try:
                wake = datetime.fromisoformat(str(r["time"]).replace("Z", "").split(".")[0])
                bed = wake - timedelta(seconds=int(r["sleep_time_seconds"] or 0))
                mid = bed + (wake - bed) / 2
                h = mid.hour + mid.minute / 60
                if h < 12:
                    h += 24
                dow = datetime.fromisoformat(r["date"]).weekday()
                (weekend_mids if dow >= 5 else weekday_mids).append(h)
            except Exception:
                continue
        weekday_mid = round(float(np.mean(weekday_mids)), 2) if weekday_mids else None
        weekend_mid = round(float(np.mean(weekend_mids)), 2) if weekend_mids else None
        delta = (round(abs(weekday_mid - weekend_mid), 2)
                 if weekday_mid is not None and weekend_mid is not None else None)
        return {
            "weekday_midpoint_h": weekday_mid,
            "weekend_midpoint_h": weekend_mid,
            "delta_h": delta,
            "weekday_n": len(weekday_mids),
            "weekend_n": len(weekend_mids),
        }

    # ------------------------------------------------------------------
    # 5. Behavior recovery cost — days for HRV to return to baseline
    # ------------------------------------------------------------------
    def behavior_recovery_cost(self, start: str, end: str) -> list[dict]:
        lj = self._load_journal(start, end)
        ds = self._load_summaries(start, end)
        if lj.empty or ds.empty:
            return []
        lj = lj[lj["status"] == 1]

        ds["hrv"] = pd.to_numeric(ds.get("avgOvernightHrv"), errors="coerce")
        baseline_hrv = ds["hrv"].rolling(30, min_periods=7).mean()
        std_hrv = ds["hrv"].rolling(30, min_periods=7).std()
        ds = ds.assign(b_hrv=baseline_hrv, s_hrv=std_hrv).set_index("date")

        results = []
        for behavior, group in lj.groupby("behavior"):
            recoveries: list[int] = []
            for _, r in group.iterrows():
                try:
                    start_idx = list(ds.index).index(r["date"])
                except ValueError:
                    continue
                for offset in range(1, 8):
                    if start_idx + offset >= len(ds.index):
                        break
                    row = ds.iloc[start_idx + offset]
                    if pd.isna(row["hrv"]) or pd.isna(row["b_hrv"]) or pd.isna(row["s_hrv"]):
                        continue
                    if row["hrv"] >= row["b_hrv"] - 0.5 * row["s_hrv"]:
                        recoveries.append(offset)
                        break
            if recoveries:
                results.append({
                    "behavior": behavior,
                    "n_events": len(recoveries),
                    "median_recovery_days": float(np.median(recoveries)),
                    "max_recovery_days": int(max(recoveries)),
                })
        results.sort(key=lambda x: x["median_recovery_days"], reverse=True)
        return results

    # ------------------------------------------------------------------
    # 6. Stress resilience — inverted stress z-score on a 0–100 scale
    # ------------------------------------------------------------------
    def stress_resilience(self, start: str, end: str) -> list[dict]:
        ds = self._load_summaries(start, end)
        if ds.empty:
            return []
        s = pd.to_numeric(ds.get("stressPercentage"), errors="coerce")
        roll = s.rolling(30, min_periods=7)
        z = (s - roll.mean()) / roll.std()
        score = (50 - 15 * z).clip(lower=0, upper=100)
        return [
            {"date": d, "resilience": (None if pd.isna(v) else round(float(v), 1))}
            for d, v in zip(ds["date"], score)
        ]

    # ------------------------------------------------------------------
    # 7. Body Battery decay slope per day
    # ------------------------------------------------------------------
    def body_battery_decay(self, start: str, end: str) -> list[dict]:
        sql = """
            SELECT substr(time, 1, 10) AS date,
                   time, body_battery_level
            FROM body_battery_intraday
            WHERE time >= ? AND time <= ? AND body_battery_level IS NOT NULL
            ORDER BY time
        """
        with self._conn() as conn:
            df = pd.read_sql_query(sql, conn, params=(f"{start}T00:00:00", f"{end}T23:59:59"))
        if df.empty:
            return []
        df["ts"] = pd.to_datetime(df["time"])
        out = []
        for date, group in df.groupby("date"):
            g = group.dropna(subset=["body_battery_level"]).sort_values("ts")
            if len(g) < 4:
                continue
            x = (g["ts"] - g["ts"].iloc[0]).dt.total_seconds().values / 3600
            y = g["body_battery_level"].astype(float).values
            if x[-1] - x[0] < 4:
                continue
            slope, _intercept = np.polyfit(x, y, 1)
            out.append({
                "date": date,
                "decay_per_hour": round(float(slope), 2),
                "peak": float(y.max()),
                "trough": float(y.min()),
            })
        return out

    # ------------------------------------------------------------------
    # 8. Pre-symptom illness radar (Quer 2021 multi-signal)
    # ------------------------------------------------------------------
    def illness_radar(self, start: str, end: str) -> dict:
        ds = self._load_summaries(start, end)
        if ds.empty:
            return {"series": [], "alerts": []}
        with self._conn() as conn:
            sleep = pd.read_sql_query(
                "SELECT date, average_respiration_value AS resp FROM sleep_summary "
                "WHERE date >= ? AND date <= ? ORDER BY date",
                conn, params=(start, end),
            )
        ds = ds.merge(sleep, on="date", how="left")
        ds["rhr"] = pd.to_numeric(ds.get("restingHeartRate"), errors="coerce")
        ds["hrv"] = pd.to_numeric(ds.get("avgOvernightHrv"), errors="coerce")
        ds["resp"] = pd.to_numeric(ds.get("resp"), errors="coerce")

        def zscore(col: pd.Series) -> pd.Series:
            roll = col.rolling(30, min_periods=7)
            return (col - roll.mean()) / roll.std()

        ds["z_rhr"] = zscore(ds["rhr"])
        ds["z_hrv"] = -zscore(ds["hrv"])
        ds["z_resp"] = zscore(ds["resp"])

        ds["composite"] = ds[["z_rhr", "z_hrv", "z_resp"]].mean(axis=1)

        series = []
        alerts = []
        for _, r in ds.iterrows():
            series.append({
                "date": r["date"],
                "z_rhr": _round(r["z_rhr"]),
                "z_hrv_inv": _round(r["z_hrv"]),
                "z_resp": _round(r["z_resp"]),
                "composite": _round(r["composite"]),
            })
            if (pd.notna(r["z_rhr"]) and pd.notna(r["z_hrv"]) and pd.notna(r["z_resp"])
                    and r["z_rhr"] >= 1 and r["z_hrv"] >= 1 and r["z_resp"] >= 1):
                alerts.append({
                    "date": r["date"],
                    "composite": _round(r["composite"]),
                    "note": "All 3 illness signals elevated",
                })
        return {"series": series, "alerts": alerts}

    # ------------------------------------------------------------------
    # 9. Inflammation index — RHR + respiration + (1 - sleep efficiency)
    # ------------------------------------------------------------------
    def inflammation_index(self, start: str, end: str) -> list[dict]:
        ds = self._load_summaries(start, end)
        if ds.empty:
            return []
        with self._conn() as conn:
            sleep = pd.read_sql_query(
                "SELECT date, average_respiration_value AS resp, "
                "       deep_sleep_seconds, light_sleep_seconds, rem_sleep_seconds, awake_sleep_seconds "
                "FROM sleep_summary WHERE date >= ? AND date <= ?",
                conn, params=(start, end),
            )
        ds = ds.merge(sleep, on="date", how="left")
        ds["rhr"] = pd.to_numeric(ds.get("restingHeartRate"), errors="coerce")
        ds["resp"] = pd.to_numeric(ds.get("resp"), errors="coerce")
        total = ds[["deep_sleep_seconds", "light_sleep_seconds", "rem_sleep_seconds", "awake_sleep_seconds"]].sum(axis=1)
        sleep_eff = (total - ds["awake_sleep_seconds"].fillna(0)) / total.replace(0, np.nan)

        def z(col: pd.Series) -> pd.Series:
            roll = col.rolling(30, min_periods=7)
            return (col - roll.mean()) / roll.std()

        z_total = z(ds["rhr"]) + z(ds["resp"]) + (-z(sleep_eff))
        return [
            {"date": d, "index": _round(v)}
            for d, v in zip(ds["date"], z_total)
        ]

    # ------------------------------------------------------------------
    # 10. Recovery debt accumulator
    # ------------------------------------------------------------------
    def recovery_debt(self, start: str, end: str, target: int = 75) -> list[dict]:
        ds = self._load_summaries(start, end)
        if ds.empty:
            return []
        bb = pd.to_numeric(ds.get("bodyBatteryAtWakeTime"), errors="coerce")
        deficit = (target - bb).fillna(0)
        cumulative = deficit.cumsum()
        return [
            {"date": d, "wake_battery": _round(b), "daily_deficit": _round(deficit_v),
             "cumulative_debt": _round(c)}
            for d, b, deficit_v, c in zip(ds["date"], bb, deficit, cumulative)
        ]

    # ------------------------------------------------------------------
    # 11. Behavior streak calendar
    # ------------------------------------------------------------------
    def behavior_streak_calendar(self, start: str, end: str) -> dict:
        lj = self._load_journal(start, end)
        if lj.empty:
            return {"behaviors": [], "dates": []}
        lj = lj[lj["status"] == 1]
        d_start = datetime.fromisoformat(start).date()
        d_end = datetime.fromisoformat(end).date()
        dates = []
        d = d_start
        while d <= d_end:
            dates.append(d.isoformat())
            d += timedelta(days=1)

        out = []
        counts = lj["behavior"].value_counts().head(12)
        for behavior in counts.index:
            sub = lj[lj["behavior"] == behavior]
            cells = []
            seen = set(sub["date"])
            for d in dates:
                v = sub[sub["date"] == d]["value"].mean() if d in seen else None
                cells.append(None if pd.isna(v) else (1.0 if v is None else round(float(v), 2)))
            out.append({
                "behavior": behavior,
                "count": int(counts[behavior]),
                "cells": cells,
            })
        return {"behaviors": out, "dates": dates}

    # ------------------------------------------------------------------
    # 12. Habit half-life — days since last logged
    # ------------------------------------------------------------------
    def habit_half_life(self, end: str, lookback_days: int = 90) -> list[dict]:
        d_end = datetime.fromisoformat(end).date()
        start = (d_end - timedelta(days=lookback_days)).isoformat()
        lj = self._load_journal(start, end)
        if lj.empty:
            return []
        lj = lj[lj["status"] == 1]
        out = []
        for behavior, group in lj.groupby("behavior"):
            last = max(group["date"])
            try:
                days_since = (d_end - datetime.fromisoformat(last).date()).days
            except Exception:
                continue
            out.append({
                "behavior": behavior,
                "last_logged": last,
                "days_since": days_since,
                "frequency_30d": int(len(group[group["date"] >= (d_end - timedelta(days=30)).isoformat()])),
                "frequency_90d": int(len(group)),
            })
        out.sort(key=lambda x: x["days_since"])
        return out

    # ------------------------------------------------------------------
    # 13. Behavior co-occurrence matrix
    # ------------------------------------------------------------------
    def behavior_cooccurrence(self, start: str, end: str) -> dict:
        lj = self._load_journal(start, end)
        if lj.empty:
            return {"behaviors": [], "matrix": []}
        lj = lj[lj["status"] == 1]
        per_day: dict[str, set[str]] = defaultdict(set)
        for _, r in lj.iterrows():
            per_day[r["date"]].add(r["behavior"])

        top = [b for b, _ in Counter(lj["behavior"]).most_common(10)]
        matrix = [[0] * len(top) for _ in top]
        for behaviors in per_day.values():
            for i, a in enumerate(top):
                if a not in behaviors:
                    continue
                for j, b in enumerate(top):
                    if b in behaviors:
                        matrix[i][j] += 1
        return {"behaviors": top, "matrix": matrix}

    # ------------------------------------------------------------------
    # 14. Stress hour-of-day fingerprint
    # ------------------------------------------------------------------
    def stress_hour_fingerprint(self, start: str, end: str) -> dict:
        sql = """
            SELECT substr(time, 1, 10) AS date,
                   CAST(substr(time, 12, 2) AS INTEGER) AS hour,
                   AVG(stress_level) AS stress
            FROM stress_intraday
            WHERE time >= ? AND time <= ? AND stress_level IS NOT NULL AND stress_level >= 0
            GROUP BY date, hour
        """
        with self._conn() as conn:
            df = pd.read_sql_query(sql, conn, params=(f"{start}T00:00:00", f"{end}T23:59:59"))
        if df.empty:
            return {"hours": list(range(24)), "weekday": [None] * 24, "weekend": [None] * 24}
        df["dow"] = pd.to_datetime(df["date"]).dt.weekday
        df["weekend"] = df["dow"] >= 5
        weekday_avg = df[~df["weekend"]].groupby("hour")["stress"].mean().reindex(range(24))
        weekend_avg = df[df["weekend"]].groupby("hour")["stress"].mean().reindex(range(24))
        return {
            "hours": list(range(24)),
            "weekday": [_round(v) for v in weekday_avg.tolist()],
            "weekend": [_round(v) for v in weekend_avg.tolist()],
        }

    # ------------------------------------------------------------------
    # 15. Stress trigger leaderboard
    # ------------------------------------------------------------------
    def stress_trigger_leaderboard(self, start: str, end: str) -> dict:
        ds = self._load_summaries(start, end)
        lj = self._load_journal(start, end)
        if ds.empty or lj.empty:
            return {"top_quintile_threshold": None, "triggers": []}
        stress = pd.to_numeric(ds.get("stressPercentage"), errors="coerce").dropna()
        if not len(stress):
            return {"top_quintile_threshold": None, "triggers": []}
        threshold = stress.quantile(0.8)
        high_dates = set(ds.loc[ds["stressPercentage"].astype(float, errors="ignore") >= threshold, "date"])
        lj = lj[lj["status"] == 1]
        on_high = lj[lj["date"].isin(high_dates)]
        on_low = lj[~lj["date"].isin(high_dates)]
        n_high = max(1, len(high_dates))
        n_low = max(1, len(set(ds["date"]) - high_dates))
        triggers = []
        for behavior in on_high["behavior"].dropna().unique():
            high_freq = (on_high["behavior"] == behavior).sum() / n_high
            low_freq = (on_low["behavior"] == behavior).sum() / n_low
            triggers.append({
                "behavior": behavior,
                "high_stress_freq": round(float(high_freq), 3),
                "normal_stress_freq": round(float(low_freq), 3),
                "lift": round(float(high_freq - low_freq), 3),
                "count_on_high": int((on_high["behavior"] == behavior).sum()),
            })
        triggers.sort(key=lambda x: x["lift"], reverse=True)
        return {
            "top_quintile_threshold": round(float(threshold), 1),
            "triggers": triggers[:12],
        }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _safe(v):
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def _safe_div(v, d):
    s = _safe(v)
    return None if s is None else round(s / d, 2)


def _round(v, n=2):
    try:
        if v is None or pd.isna(v):
            return None
        return round(float(v), n)
    except Exception:
        return None
