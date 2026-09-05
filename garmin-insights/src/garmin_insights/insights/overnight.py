"""Overnight physiology — per-night analytics derived from `sleep_intraday`.

The fetcher records a per-sample overnight series (sleep HR, HRV, SpO2,
respiration, stress, body battery, sleep stage, movement, restlessness) for
every night, but every other analytic in the app reads only the one-row-per-
night `sleep_summary` aggregate. That aggregate carries means and extremes;
it carries no *shape*. The shape is where the physiology is:

* **How far heart rate falls** overnight, and **when** it bottoms out. The
  fall from sleep-onset HR to the overnight nadir is the autonomic recovery
  signal; a nadir arriving late in the night is the signature of a body that
  spent the first half of the night still processing something (alcohol, a
  late meal, late training).
* **Which way HRV travels across the night.** A mean is blind to a night that
  started high and decayed versus one that recovered as it went.
* **Desaturation burden**, not just the minimum SpO2 — one artefactual dip and
  a night of repeated dips produce the same `lowest_spo2_value`.
* **Fragmentation** — how often sleep stage changes, and how much wake time
  falls between sleep onset and final awakening (WASO).

Everything here is computed against the user's own nights. Thresholds are
included only where a published reference point exists, and each one is
labelled as a screening/context signal rather than a diagnosis — Garmin's
overnight sampling is sparse and unvalidated against polysomnography.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# Garmin encodes sleep stage in `sleepLevels[].activityLevel`, which the
# fetcher stores verbatim as `stage_level`: 0 deep, 1 light, 2 REM, 3 awake.
_STAGE_DEEP, _STAGE_LIGHT, _STAGE_REM, _STAGE_AWAKE = 0, 1, 2, 3
_STAGE_NAMES = {_STAGE_DEEP: "deep", _STAGE_LIGHT: "light",
                _STAGE_REM: "rem", _STAGE_AWAKE: "awake"}

# A night's samples are split at local noon: anything from noon onwards belongs
# to the night that ENDS the following morning. This matches the convention the
# rest of the app uses — `sleep_summary` rows are stamped with the wake date, so
# "last night's sleep" lives on today's date.
_NIGHT_SPLIT_HOUR = 12

# Minimum samples of a given series before its derived statistics are trusted.
# Below this a single artefact dominates, so the metric is reported as None
# rather than as a confident-looking number.
_MIN_SAMPLES = 20
# Stage rows are segments, not fixed-interval samples — a whole night is
# typically 20-60 of them, so fragmentation needs a lower floor.
_MIN_STAGE_ROWS = 6

# Window (in samples) for the rolling median that defines the local SpO2
# baseline, and the drop below it that counts as a desaturation.
_SPO2_BASELINE_WINDOW = 10
_SPO2_DESAT_DROP = 3.0
# Below this the sample is counted toward the low-saturation burden. 90% is the
# conventional cut-off for "significant" desaturation in sleep medicine.
_SPO2_LOW_THRESHOLD = 90.0

# Reference points used only to LABEL a night, never to diagnose one.
# A nocturnal HR fall under ~10% is the "non-dipping" pattern; we use a
# deliberately conservative 8% so ordinary variation doesn't trip it.
_HR_DIP_BLUNTED_PCT = 8.0
# Desaturation indices are conventionally graded from 5/hour upward. Garmin's
# sparse pulse-ox sampling cannot produce a clinical ODI, so the same number is
# used purely as the point at which the pattern is worth raising with a
# clinician alongside symptoms.
_DESAT_INDEX_SCREEN = 5.0


def _local_tz():
    """System local timezone, resolved per call so DST is applied correctly."""
    return datetime.now().astimezone().tzinfo


def _f(value) -> float | None:
    """Round to 1 d.p., mapping NaN/inf/None to None so JSON stays clean."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return round(f, 1)


def _series(df: pd.DataFrame, column: str) -> pd.Series:
    """Numeric, NaN-dropped, time-ordered view of one column.

    Garmin uses -1 as a sentinel for "no reading" on the movement/stage
    columns; treating it as a real value would drag every mean downward.
    """
    if column not in df.columns:
        return pd.Series(dtype="float64")
    s = pd.to_numeric(df[column], errors="coerce").dropna()
    return s[s >= 0].sort_index()


class OvernightService:
    """Per-night overnight-physiology analytics over `sleep_intraday`."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def _load(self, start: str, end: str) -> pd.DataFrame:
        """Raw samples for every night ENDING in [start, end].

        The night ending on `start` begins the previous evening, so the SQL
        window is widened by a day at the front; `night_of` then does the real
        partitioning.
        """
        try:
            lo = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        except ValueError:
            lo = start
        sql = (
            "SELECT time, spo2_reading, respiration_value, heart_rate, "
            "stress_value, body_battery, hrv_value, stage_level, stage_seconds, "
            "activity_level, restless_value "
            "FROM sleep_intraday WHERE time >= ? AND time <= ? ORDER BY time"
        )
        try:
            conn = self._conn()
        except sqlite3.Error as exc:
            logger.warning("overnight: cannot open database: %s", exc)
            return pd.DataFrame()
        try:
            df = pd.read_sql_query(
                sql, conn, params=(f"{lo}T00:00:00", f"{end}T23:59:59")
            )
        except Exception as exc:
            # Table absent (a DB written before the SleepIntraday handler
            # existed) is the common case and is not an error worth raising.
            logger.info("overnight: sleep_intraday unavailable: %s", exc)
            return pd.DataFrame()
        finally:
            conn.close()
        if df.empty:
            return df
        df["time"] = pd.to_datetime(df["time"], format="ISO8601", utc=True)
        df = df.dropna(subset=["time"]).set_index("time").sort_index()
        local = df.index.tz_convert(_local_tz())
        # Samples from local noon onward belong to the next morning's night.
        shift = pd.to_timedelta((local.hour >= _NIGHT_SPLIT_HOUR).astype(int), unit="D")
        df["night_of"] = (local + shift).strftime("%Y-%m-%d")
        return df

    # ------------------------------------------------------------------
    # Per-night metrics
    # ------------------------------------------------------------------
    def _heart_rate(self, df: pd.DataFrame, night_minutes: float | None) -> dict:
        """Overnight HR fall and the timing of the nadir.

        `hr_dip_pct` is the fall from the sleep-onset HR (mean of the first 30
        minutes of samples) to the night's nadir. The nadir is taken from a
        5-sample rolling median so one dropped beat doesn't define it.
        """
        hr = _series(df, "heart_rate")
        out: dict = {"hr_samples": int(hr.size)}
        if hr.size < _MIN_SAMPLES:
            return out
        out["hr_mean"] = _f(hr.mean())
        onset_window = hr[hr.index <= hr.index[0] + timedelta(minutes=30)]
        onset = float(onset_window.mean()) if onset_window.size else float(hr.iloc[0])
        smoothed = hr.rolling(5, center=True, min_periods=1).median()
        nadir = float(smoothed.min())
        nadir_at = smoothed.idxmin()
        out["hr_onset"] = _f(onset)
        out["hr_nadir"] = _f(nadir)
        if onset > 0:
            out["hr_dip_pct"] = _f((onset - nadir) / onset * 100.0)
        minutes_in = (nadir_at - hr.index[0]).total_seconds() / 60.0
        out["hr_nadir_minutes_from_onset"] = _f(minutes_in)
        if night_minutes and night_minutes > 0:
            out["hr_nadir_pct_of_night"] = _f(minutes_in / night_minutes * 100.0)
        return out

    def _hrv(self, df: pd.DataFrame) -> dict:
        """Which way HRV travelled across the night (first vs last third)."""
        hrv = _series(df, "hrv_value")
        out: dict = {"hrv_samples": int(hrv.size)}
        if hrv.size < _MIN_SAMPLES:
            return out
        out["hrv_mean"] = _f(hrv.mean())
        third = hrv.size // 3
        first, last = hrv.iloc[:third], hrv.iloc[-third:]
        first_mean, last_mean = float(first.mean()), float(last.mean())
        out["hrv_first_third"] = _f(first_mean)
        out["hrv_last_third"] = _f(last_mean)
        if first_mean > 0:
            out["hrv_trend_pct"] = _f((last_mean - first_mean) / first_mean * 100.0)
        return out

    def _spo2(self, df: pd.DataFrame, night_hours: float | None) -> dict:
        """Desaturation burden, not just the night's minimum.

        Events are drops of >=3 percentage points below a rolling-median local
        baseline, collapsed so one sustained dip counts once. Garmin's pulse-ox
        sampling is sparse and unvalidated against polysomnography, so this is
        a device-estimated burden index, NOT a clinical ODI.
        """
        spo2 = _series(df, "spo2_reading")
        out: dict = {"spo2_samples": int(spo2.size)}
        if spo2.size < _MIN_SAMPLES:
            return out
        out["spo2_mean"] = _f(spo2.mean())
        out["spo2_min"] = _f(spo2.min())
        below = spo2 < _SPO2_LOW_THRESHOLD
        out["spo2_pct_below_90"] = _f(below.mean() * 100.0)
        baseline = spo2.rolling(_SPO2_BASELINE_WINDOW, min_periods=3).median()
        in_desat = (spo2 <= baseline - _SPO2_DESAT_DROP).fillna(False)
        # Count only the transitions into a desaturation, so a run of samples
        # below baseline is one event rather than one event per sample.
        events = int((in_desat & ~in_desat.shift(1, fill_value=False)).sum())
        out["desat_events"] = events
        if night_hours and night_hours > 0:
            out["desat_index_per_hour"] = _f(events / night_hours)
        return out

    def _respiration(self, df: pd.DataFrame) -> dict:
        """Mean and coefficient of variation of overnight breathing rate."""
        resp = _series(df, "respiration_value")
        out: dict = {"respiration_samples": int(resp.size)}
        if resp.size < _MIN_SAMPLES:
            return out
        mean = float(resp.mean())
        out["respiration_mean"] = _f(mean)
        if mean > 0:
            out["respiration_cv"] = _f(float(resp.std(ddof=1)) / mean * 100.0)
        return out

    def _body_battery(self, df: pd.DataFrame, night_hours: float | None) -> dict:
        """How fast the overnight recharge actually accrued."""
        bb = _series(df, "body_battery")
        out: dict = {"body_battery_samples": int(bb.size)}
        if bb.size < 2:
            return out
        gain = float(bb.iloc[-1] - bb.iloc[0])
        out["body_battery_start"] = _f(bb.iloc[0])
        out["body_battery_end"] = _f(bb.iloc[-1])
        out["body_battery_gain"] = _f(gain)
        if night_hours and night_hours > 0:
            out["body_battery_per_hour"] = _f(gain / night_hours)
        return out

    def _fragmentation(self, df: pd.DataFrame) -> dict:
        """Stage transitions per hour, and wake time after sleep onset.

        WASO counts only awake segments that fall BETWEEN the first and the
        last asleep segment — the awake time bracketing the night is the user
        settling down and getting up, not fragmented sleep.
        """
        # Stage rows are paired columns on the same row, so they are extracted
        # together rather than aligned by timestamp — several sample types (and
        # several devices) can legitimately share a timestamp, and reindexing
        # against a duplicated index raises.
        levels = pd.to_numeric(df.get("stage_level"), errors="coerce")
        if levels is None:
            return {"stage_rows": 0}
        stages = pd.DataFrame({
            "level": levels,
            "seconds": pd.to_numeric(df.get("stage_seconds"), errors="coerce"),
        }).dropna(subset=["level"]).sort_index()
        stages = stages[stages["level"] >= 0]
        out: dict = {"stage_rows": int(len(stages))}
        if len(stages) < _MIN_STAGE_ROWS:
            return out
        stages["level"] = stages["level"].astype(int)

        asleep = stages[stages["level"] != _STAGE_AWAKE]
        if asleep.empty:
            return out
        first_asleep, last_asleep = asleep.index[0], asleep.index[-1]
        span = stages[(stages.index >= first_asleep) & (stages.index <= last_asleep)]

        transitions = int((span["level"].diff().fillna(0) != 0).sum())
        out["stage_transitions"] = transitions
        period_hours = (last_asleep - first_asleep).total_seconds() / 3600.0
        if period_hours > 0:
            out["fragmentation_index"] = _f(transitions / period_hours)
        awake = span[span["level"] == _STAGE_AWAKE]
        out["waso_minutes"] = _f(float(awake["seconds"].sum(skipna=True)) / 60.0)
        out["waso_episodes"] = int(len(awake))

        by_stage = span.groupby("level")["seconds"].sum(min_count=1)
        if float(by_stage.sum(skipna=True)) > 0:
            out["stage_minutes"] = {
                _STAGE_NAMES.get(int(level), str(int(level))): _f(float(secs) / 60.0)
                for level, secs in by_stage.items()
                if pd.notna(secs)
            }
        return out

    def _movement(self, df: pd.DataFrame) -> dict:
        move = _series(df, "activity_level")
        restless = _series(df, "restless_value")
        out: dict = {}
        if move.size:
            out["movement_mean"] = _f(move.mean())
        if restless.size:
            out["restless_events"] = int(restless.size)
        return out

    def _one_night(self, night: str, df: pd.DataFrame) -> dict:
        span_start, span_end = df.index[0], df.index[-1]
        night_minutes = (span_end - span_start).total_seconds() / 60.0
        night_hours = night_minutes / 60.0 if night_minutes > 0 else None
        local_start = span_start.tz_convert(_local_tz())
        local_end = span_end.tz_convert(_local_tz())
        entry: dict = {
            "night_of": night,
            "span_start": local_start.strftime("%Y-%m-%dT%H:%M"),
            "span_end": local_end.strftime("%Y-%m-%dT%H:%M"),
            "span_hours": _f(night_hours),
            "samples": int(len(df)),
        }
        entry.update(self._heart_rate(df, night_minutes))
        entry.update(self._hrv(df))
        entry.update(self._spo2(df, night_hours))
        entry.update(self._respiration(df))
        entry.update(self._body_battery(df, night_hours))
        entry.update(self._fragmentation(df))
        entry.update(self._movement(df))
        return entry

    def nights(self, start: str, end: str) -> list[dict]:
        """One metrics dict per night ending in [start, end], oldest first."""
        df = self._load(start, end)
        if df.empty:
            return []
        out: list[dict] = []
        for night, group in df.groupby("night_of", sort=True):
            if night < start or night > end:
                continue
            group = group.drop(columns=["night_of"])
            try:
                out.append(self._one_night(str(night), group))
            except Exception as exc:  # one malformed night must not sink the rest
                logger.warning("overnight: night %s failed: %s", night, exc)
        return out

    # ------------------------------------------------------------------
    # Window summary — personal baselines, deviations, screening labels
    # ------------------------------------------------------------------
    def summary(self, start: str, end: str) -> dict:
        """Nights in the window plus the user's own baselines and any flags.

        `deviations` are relative to the user's own nights (robust median/MAD),
        and only in the direction that is a concern for that metric — a night
        whose heart rate fell unusually FAR is a good night, not a finding.
        `screening` carries the two published reference points, both framed as
        conversation starters rather than diagnoses.
        """
        nights = self.nights(start, end)
        if not nights:
            return {
                "available": False,
                "message": (
                    "No overnight per-sample data in this window. The fetcher "
                    "records it going forward; nights before that are absent."
                ),
                "nights": [],
            }

        baselines: dict[str, dict] = {}
        for metric in CONCERN_DIRECTIONS:
            stats = _robust_baseline([n.get(metric) for n in nights])
            if stats is None:
                continue
            median, scale = stats
            baselines[metric] = {
                "median": _f(median),
                "scale": _f(scale),
                "n": sum(1 for n in nights if n.get(metric) is not None),
            }

        latest = nights[-1]
        deviations: list[dict] = []
        for metric, concern in CONCERN_DIRECTIONS.items():
            base = baselines.get(metric)
            value = latest.get(metric)
            if base is None or value is None or not base["scale"]:
                continue
            z = (float(value) - float(base["median"])) / float(base["scale"])
            if z * concern < _DEVIATION_Z:
                continue
            deviations.append({
                "metric": metric,
                "night_of": latest["night_of"],
                "value": _f(value),
                "baseline_median": base["median"],
                "z_score": _f(z),
                "direction": "above" if z > 0 else "below",
            })
        deviations.sort(key=lambda d: abs(d["z_score"] or 0), reverse=True)

        return {
            "available": True,
            "nights": nights,
            "baselines": baselines,
            "latest_night": latest["night_of"],
            "deviations": deviations,
            "screening": self._screening(nights),
            "notes": [
                "Device-estimated from Garmin's sparse overnight sampling; not "
                "validated against polysomnography and not diagnostic.",
                "Deviations are against this user's own nights in the window, "
                "not population norms.",
            ],
        }

    def _screening(self, nights: list[dict]) -> list[dict]:
        """Published reference points, applied to the recent run of nights.

        Both are deliberately phrased as "worth raising with a clinician if it
        persists alongside symptoms" — neither Garmin's wrist HR nor its
        pulse-ox can support a stronger claim.
        """
        recent = nights[-14:]
        out: list[dict] = []

        dips = [n["hr_dip_pct"] for n in recent if n.get("hr_dip_pct") is not None]
        if len(dips) >= _MIN_BASELINE_NIGHTS:
            blunted = [d for d in dips if d < _HR_DIP_BLUNTED_PCT]
            if len(blunted) > len(dips) / 2:
                out.append({
                    "signal": "blunted_overnight_hr_fall",
                    "nights_assessed": len(dips),
                    "nights_below_threshold": len(blunted),
                    "median_dip_pct": _f(float(np.median(dips))),
                    "threshold_pct": _HR_DIP_BLUNTED_PCT,
                    "interpretation": (
                        "Heart rate fell less than "
                        f"{_HR_DIP_BLUNTED_PCT:.0f}% from sleep onset to its overnight "
                        "low on most recent nights. In the dipping literature a "
                        "reduced nocturnal fall is a context signal for autonomic "
                        "load — commonly driven by alcohol, late training, late "
                        "meals, heat or illness before anything else."
                    ),
                })

        indices = [n["desat_index_per_hour"] for n in recent
                   if n.get("desat_index_per_hour") is not None]
        if len(indices) >= _MIN_BASELINE_NIGHTS:
            median_index = float(np.median(indices))
            if median_index >= _DESAT_INDEX_SCREEN:
                out.append({
                    "signal": "recurrent_overnight_desaturation",
                    "nights_assessed": len(indices),
                    "median_index_per_hour": _f(median_index),
                    "threshold_per_hour": _DESAT_INDEX_SCREEN,
                    "interpretation": (
                        "Repeated >=3-point drops below the local SpO2 baseline "
                        "on most recent nights. This is a device-estimated "
                        "burden index from sparse wrist pulse-ox, NOT a clinical "
                        "oxygen desaturation index — a screening signal worth "
                        "discussing with a clinician if it persists alongside "
                        "snoring, daytime sleepiness or morning headaches."
                    ),
                })
        return out


# Metrics carried through to the baseline/deviation layer, with the direction
# in which a deviation is a CONCERN. Mirrors `_CONCERN_DIRECTIONS` in the
# proactive scanner so an anomalously GOOD night is never narrated as a worry.
CONCERN_DIRECTIONS: dict[str, int] = {
    "hr_dip_pct": -1,                  # a smaller overnight fall is the concern
    "hr_nadir_pct_of_night": 1,        # a later nadir is the concern
    "hrv_trend_pct": -1,
    "spo2_min": -1,
    "spo2_pct_below_90": 1,
    "desat_index_per_hour": 1,
    "respiration_cv": 1,
    "fragmentation_index": 1,
    "waso_minutes": 1,
    "body_battery_per_hour": -1,
}

# A night is only compared against the user's own history once there are this
# many nights to compare against; below it a "deviation" is mostly noise.
_MIN_BASELINE_NIGHTS = 7
# Robust z-scale factor: 1.4826 * MAD estimates the standard deviation of a
# normal distribution, without one bad night inflating it the way SD does.
_MAD_TO_SD = 1.4826
_DEVIATION_Z = 2.0


def _robust_baseline(values: list[float]) -> tuple[float, float] | None:
    """Median and MAD-derived scale for a metric's history.

    Returns None when there are too few nights, or when the history is so
    flat that any deviation would divide by ~zero.
    """
    finite = [v for v in values if v is not None and np.isfinite(v)]
    if len(finite) < _MIN_BASELINE_NIGHTS:
        return None
    arr = np.asarray(finite, dtype="float64")
    median = float(np.median(arr))
    scale = float(np.median(np.abs(arr - median))) * _MAD_TO_SD
    if scale <= 0:
        return None
    return median, scale
