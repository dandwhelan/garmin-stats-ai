"""Continuous glucose (FreeStyle Libre) ingestion + quality-filtered daily metrics.

Readings land in ``glucose_readings`` from two places:

* the fetcher's Nightscout pull (``garmin_grafana.glucose_fetch``) — Juggluco
  or xDrip+ on the phone expose a Nightscout-style ``/api/v1/entries`` feed;
* a LibreView CSV export uploaded through the web UI (``parse_libreview_csv``).

Values are stored in mg/dL (Nightscout's native unit) with UTC timestamps and
converted to mmol/L for everything user-facing.

Two artefacts are flagged before any metric is computed, because both would
otherwise be read as real physiology:

* **early sensor** — the first 24 h of a sensor reads less accurately
  (Abbott's own accuracy data is worst on day 1). Flagged and excluded from
  daily metrics.
* **compression lows** — lying on the sensor arm squeezes interstitial fluid
  and produces a fast, deep, short-lived overnight dip that recovers as soon
  as the user rolls over. Flagged and excluded from daily metrics.

Glucose is a personal-trend signal here, never a diagnosis: the ranges used
(3.9-7.8 mmol/L, "time in tight range") describe typical non-diabetic
physiology and the sensor reads ~±10 % against blood, worse at the low end.
"""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import numpy as np
import pandas as pd

from garmin_insights.insights.overnight import _system_zone, _to_local_naive

logger = logging.getLogger(__name__)

MGDL_PER_MMOL = 18.0182

# "Time in tight range" — the non-diabetic band used in CGM research on
# people without diabetes (Battelino 2023 consensus; Shah 2019 normative CGM).
TIGHT_LOW_MMOL = 3.9
TIGHT_HIGH_MMOL = 7.8

# Sensor-quality heuristics -------------------------------------------------
EARLY_SENSOR_HOURS = 24
# A Libre sensor lasts 14 days and a new one needs a 60-minute warm-up, so a
# gap of an hour or more after ~13 days of continuous data is a sensor swap.
_SWAP_MIN_GAP_MIN = 55
_SWAP_MIN_SESSION_DAYS = 13
# Any gap this long starts a new session whatever the history says.
_NEW_SESSION_GAP_H = 24

# Compression low: overnight, ≥1.5 mmol/L below the preceding hour's median,
# reached at ≥0.04 mmol/L/min, and recovered within 2 h.
_COMPRESSION_HOURS = (22, 8)       # local wall-clock window [22:00, 08:00)
_COMPRESSION_DROP_MMOL = 1.5
_COMPRESSION_MIN_RATE = 0.04       # mmol/L per minute
_COMPRESSION_MAX_MIN = 120
_COMPRESSION_RECOVER_MMOL = 0.6

# A day needs this much sensor coverage before its metrics are reported.
MIN_DAY_COVERAGE_PCT = 50.0

# Source preference when two sources report the same minute.
_SOURCE_PRIORITY = {"nightscout": 0, "libreview_csv": 1}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS glucose_readings (
    time TEXT NOT NULL,
    source TEXT NOT NULL,
    mg_dl REAL NOT NULL,
    trend TEXT,
    device TEXT,
    sensor_id TEXT,
    record_type TEXT,
    PRIMARY KEY (time, source)
);
CREATE INDEX IF NOT EXISTS idx_glucose_readings_time ON glucose_readings (time);
"""

# Summary keys merged into daily_summaries (all mmol/L or %).
SUMMARY_KEYS = (
    "glucose_mean_mmol", "glucose_sd_mmol", "glucose_cv_pct",
    "glucose_tir_pct", "glucose_above_pct", "glucose_below_pct",
    "glucose_min_mmol", "glucose_max_mmol",
    "glucose_overnight_mmol", "glucose_fasting_mmol",
    "glucose_coverage_pct",
)


def to_mmol(mg_dl: float) -> float:
    return mg_dl / MGDL_PER_MMOL


def to_mgdl(mmol: float) -> float:
    return mmol * MGDL_PER_MMOL


def _utc_iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def ensure_schema(db_path: str) -> None:
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def upsert_readings(db_path: str, rows: Iterable[dict[str, Any]]) -> int:
    """Insert/replace readings keyed on (time, source). Returns rows written.

    Each row needs ``time`` (UTC ISO ``...Z``), ``source`` and ``mg_dl``;
    ``trend``, ``device``, ``sensor_id`` and ``record_type`` are optional.
    """
    rows = [r for r in rows if r.get("time") and r.get("mg_dl") is not None]
    if not rows:
        return 0
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executemany(
            """
            INSERT INTO glucose_readings
                (time, source, mg_dl, trend, device, sensor_id, record_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(time, source) DO UPDATE SET
                mg_dl = excluded.mg_dl,
                trend = excluded.trend,
                device = excluded.device,
                sensor_id = excluded.sensor_id,
                record_type = excluded.record_type
            """,
            [
                (r["time"], r["source"], float(r["mg_dl"]), r.get("trend"),
                 r.get("device"), r.get("sensor_id"), r.get("record_type"))
                for r in rows
            ],
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def load_readings(db_path: str, start_utc: datetime, end_utc: datetime) -> pd.DataFrame:
    """Readings in [start_utc, end_utc), deduplicated across sources.

    Returns columns ``time`` (tz-aware UTC), ``mg_dl``, ``mmol``, ``source``,
    ``sensor_id``, ``record_type``; empty frame when there is nothing (or no
    table yet).
    """
    cols = ["time", "mg_dl", "mmol", "source", "sensor_id", "record_type"]
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            df = pd.read_sql_query(
                "SELECT time, mg_dl, source, sensor_id, record_type FROM glucose_readings "
                "WHERE time >= ? AND time < ? ORDER BY time",
                conn,
                params=(_utc_iso(start_utc), _utc_iso(end_utc)),
            )
        finally:
            conn.close()
    except Exception as e:  # table missing on a DB that never had glucose
        logger.debug("glucose_readings query failed: %s", e)
        return pd.DataFrame(columns=cols)
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["sensor_id"] = df["sensor_id"].astype(object).where(df["sensor_id"].notna(), None)
    df = _dedupe_sources(df)
    df["mmol"] = df["mg_dl"] / MGDL_PER_MMOL
    return df[cols].reset_index(drop=True)


def _dedupe_sources(df: pd.DataFrame) -> pd.DataFrame:
    """One reading per minute, preferring the densest source.

    Also drops a CSV reading when a Nightscout reading exists within 5
    minutes of it — the same sensor seen twice at different resolutions would
    otherwise give 15-min historic points double weight in a 1-min series.
    """
    df = df.copy()
    df["_prio"] = df["source"].map(_SOURCE_PRIORITY).fillna(9)
    df["_minute"] = df["time"].dt.floor("min")
    df = df.sort_values(["_minute", "_prio"]).drop_duplicates("_minute", keep="first")
    ns = df[df["source"] == "nightscout"]
    if not ns.empty and (df["source"] != "nightscout").any():
        other = df[df["source"] != "nightscout"].sort_values("time")
        near = pd.merge_asof(
            other[["time"]].reset_index(),
            ns[["time"]].rename(columns={"time": "ns_time"}).sort_values("ns_time"),
            left_on="time", right_on="ns_time",
            direction="nearest", tolerance=pd.Timedelta(minutes=5),
        )
        drop_idx = near.loc[near["ns_time"].notna(), "index"]
        df = df.drop(index=drop_idx)
    return df.drop(columns=["_prio", "_minute"]).sort_values("time")


# ---------------------------------------------------------------------------
# LibreView CSV import
# ---------------------------------------------------------------------------
_CSV_TS_FORMATS_DAY_FIRST = ("%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y %I:%M %p", "%d/%m/%Y %I:%M %p")
_CSV_TS_FORMATS_MONTH_FIRST = ("%m-%d-%Y %I:%M %p", "%m/%d/%Y %I:%M %p", "%m-%d-%Y %H:%M", "%m/%d/%Y %H:%M")
_CSV_TS_FORMATS_ISO = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


class GlucoseImportError(ValueError):
    pass


def _detect_day_first(stamps: list[str]) -> bool:
    """Decide DD-MM vs MM-DD from the whole file, defaulting to day-first (UK)."""
    for s in stamps:
        parts = s.replace("/", "-").split(" ")[0].split("-")
        if len(parts) < 3 or len(parts[0]) == 4:
            continue
        try:
            a, b = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if a > 12:
            return True
        if b > 12:
            return False
    return True


def _parse_local(stamp: str, formats: tuple[str, ...]) -> datetime | None:
    stamp = stamp.strip()
    for fmt in formats:
        try:
            return datetime.strptime(stamp, fmt)
        except ValueError:
            continue
    return None


def _local_naive_to_utc(naive: list[datetime]) -> list[datetime | None]:
    """Local wall-clock times → UTC. The ambiguous DST hour is dropped."""
    zone = _system_zone()
    if zone is None:
        return [n.astimezone().astimezone(timezone.utc) for n in naive]
    idx = pd.DatetimeIndex(naive).tz_localize(zone, ambiguous="NaT", nonexistent="shift_forward")
    return [None if pd.isna(t) else t.tz_convert("UTC").to_pydatetime() for t in idx]


def _num(value: str) -> float | None:
    value = (value or "").strip().replace(",", ".")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:  # "LO" / "HI" and other non-numeric markers
        return None


def parse_libreview_csv(text: str) -> list[dict[str, Any]]:
    """Parse a LibreView "Download glucose data" CSV into reading rows.

    Keeps Record Type 0 (15-minute historic) and 1 (scan) glucose values;
    notes, insulin, food and ketone rows are ignored. Units come from the
    header (``mmol/L`` or ``mg/dL``). Timestamps in the export are local
    wall-clock times and are converted to UTC.
    """
    if not text or not text.strip():
        raise GlucoseImportError("The file is empty.")
    lines = text.lstrip("﻿").splitlines()
    header_i = next(
        (i for i, ln in enumerate(lines[:10]) if "Record Type" in ln and "Timestamp" in ln),
        None,
    )
    if header_i is None:
        raise GlucoseImportError(
            "This doesn't look like a LibreView glucose export (no 'Record Type' header)."
        )
    delimiter = ";" if lines[header_i].count(";") > lines[header_i].count(",") else ","
    reader = csv.reader(io.StringIO("\n".join(lines[header_i:])), delimiter=delimiter)
    header = [h.strip() for h in next(reader)]

    def col(*needles: str) -> int | None:
        for i, h in enumerate(header):
            if all(n.lower() in h.lower() for n in needles):
                return i
        return None

    i_ts = col("Timestamp")
    i_type = col("Record Type")
    i_hist = col("Historic Glucose")
    i_scan = col("Scan Glucose")
    i_serial = col("Serial")
    i_device = col("Device")
    if i_ts is None or i_type is None or (i_hist is None and i_scan is None):
        raise GlucoseImportError("Missing timestamp / record type / glucose columns.")
    unit_header = header[i_hist if i_hist is not None else i_scan].lower()
    factor = 1.0 if "mg/dl" in unit_header else MGDL_PER_MMOL

    raw: list[tuple[str, str, float, str | None, str | None]] = []
    for row in reader:
        if len(row) <= max(i_ts, i_type):
            continue
        row = row + [""] * (len(header) - len(row))  # exports drop trailing empties
        rtype = row[i_type].strip()
        if rtype == "0" and i_hist is not None:
            value, kind = _num(row[i_hist]), "historic"
        elif rtype == "1" and i_scan is not None:
            value, kind = _num(row[i_scan]), "scan"
        else:
            continue
        if value is None or value <= 0:
            continue
        raw.append((
            row[i_ts].strip(), kind, value * factor,
            row[i_serial].strip() if i_serial is not None else None,
            row[i_device].strip() if i_device is not None else None,
        ))
    if not raw:
        raise GlucoseImportError("No glucose readings found in the file.")

    stamps = [r[0] for r in raw]
    if stamps[0][:4].isdigit() and stamps[0][4] == "-":
        formats = _CSV_TS_FORMATS_ISO
    elif _detect_day_first(stamps):
        formats = _CSV_TS_FORMATS_DAY_FIRST + _CSV_TS_FORMATS_ISO
    else:
        formats = _CSV_TS_FORMATS_MONTH_FIRST + _CSV_TS_FORMATS_ISO
    parsed = [(_parse_local(s, formats), r) for s, r in zip(stamps, raw)]
    parsed = [(dt, r) for dt, r in parsed if dt is not None]
    if not parsed:
        raise GlucoseImportError(f"Unrecognised timestamp format, e.g. {stamps[0]!r}.")
    utc = _local_naive_to_utc([dt for dt, _ in parsed])

    out: list[dict[str, Any]] = []
    for ts, (_, (_stamp, kind, mg, serial, device)) in zip(utc, parsed):
        if ts is None or not (40 <= mg <= 500):
            continue
        out.append({
            "time": _utc_iso(ts), "source": "libreview_csv", "mg_dl": round(mg, 1),
            "trend": None, "device": device or None, "sensor_id": serial or None,
            "record_type": kind,
        })
    return out


# ---------------------------------------------------------------------------
# Quality flags
# ---------------------------------------------------------------------------
def flag_quality(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``session``, ``early_sensor`` and ``compression_low`` columns.

    Expects the output of ``load_readings`` (UTC ``time``, ``mmol``). The
    frame also gains ``local`` (tz-naive local wall-clock time).
    """
    df = df.sort_values("time").reset_index(drop=True).copy()
    if df.empty:
        for c in ("local", "session", "early_sensor", "compression_low"):
            df[c] = pd.Series(dtype="object")
        return df
    df["local"] = _to_local_naive(pd.DatetimeIndex(df["time"]))

    # -- sensor sessions --
    sessions = np.zeros(len(df), dtype=int)
    session_start = df["time"].iloc[0]
    sid_prev = df["sensor_id"].iloc[0] if "sensor_id" in df else None
    current = 0
    times = df["time"].tolist()
    sids = df["sensor_id"].tolist() if "sensor_id" in df else [None] * len(df)
    for i in range(1, len(df)):
        gap = times[i] - times[i - 1]
        sid = sids[i]
        new = False
        if sid and sid_prev and sid != sid_prev:
            new = True
        elif gap >= pd.Timedelta(hours=_NEW_SESSION_GAP_H):
            new = True
        elif (gap >= pd.Timedelta(minutes=_SWAP_MIN_GAP_MIN)
              and times[i - 1] - session_start >= pd.Timedelta(days=_SWAP_MIN_SESSION_DAYS)):
            new = True
        if new:
            current += 1
            session_start = times[i]
        sessions[i] = current
        if sid:
            sid_prev = sid
    df["session"] = sessions
    starts = df.groupby("session")["time"].transform("min")
    df["early_sensor"] = (df["time"] - starts) < pd.Timedelta(hours=EARLY_SENSOR_HOURS)

    df["compression_low"] = _compression_lows(df)
    return df


def _in_overnight_window(local: pd.Series) -> pd.Series:
    h = local.dt.hour
    start, end = _COMPRESSION_HOURS
    return (h >= start) | (h < end)


def _compression_lows(df: pd.DataFrame) -> pd.Series:
    flags = pd.Series(False, index=df.index)
    if len(df) < 4:
        return flags
    s = df.set_index("time")["mmol"]
    # Median of the preceding hour (excluding the current point).
    baseline = s.shift(1).rolling("60min", min_periods=2).median().to_numpy()
    mmol = df["mmol"].to_numpy()
    times = df["time"].reset_index(drop=True)
    overnight = _in_overnight_window(df["local"]).to_numpy()
    low = (mmol <= baseline - _COMPRESSION_DROP_MMOL) & overnight

    i, n = 0, len(df)
    while i < n:
        if not low[i]:
            i += 1
            continue
        base = baseline[i]
        j = i
        while j + 1 < n and mmol[j + 1] < base - _COMPRESSION_RECOVER_MMOL:
            j += 1
        # Entry: last point before the dip that was still near baseline.
        k = i - 1
        while k >= 0 and mmol[k] < base - 0.5:
            k -= 1
        dip_min = float(np.min(mmol[i:j + 1]))
        entry_ref = mmol[k] if k >= 0 else base
        t_entry = times[k] if k >= 0 else times[i]
        fall_min = max((times[int(np.argmin(mmol[i:j + 1])) + i] - t_entry).total_seconds() / 60, 1.0)
        rate = (entry_ref - dip_min) / fall_min
        duration = (times[j] - times[i]).total_seconds() / 60
        recovered = j + 1 < n and mmol[j + 1] >= base - _COMPRESSION_RECOVER_MMOL
        if rate >= _COMPRESSION_MIN_RATE and duration <= _COMPRESSION_MAX_MIN and recovered:
            flags.iloc[i:j + 1] = True
        i = j + 1
    return flags


# ---------------------------------------------------------------------------
# Daily metrics
# ---------------------------------------------------------------------------
def _sampling_minutes(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 15.0
    diffs = df["time"].diff().dt.total_seconds().dropna() / 60
    diffs = diffs[diffs > 0]
    return float(np.clip(diffs.median(), 1.0, 15.0)) if not diffs.empty else 15.0


def _round(v: float | None, nd: int = 1) -> float | None:
    if v is None or not np.isfinite(v):
        return None
    return round(float(v), nd)


def day_metrics(day_df: pd.DataFrame, interval_min: float) -> dict[str, Any]:
    """Metrics for one local day's flagged readings (already filtered to the day).

    Early-sensor and compression-low readings are excluded. Returns only the
    coverage keys when coverage is below ``MIN_DAY_COVERAGE_PCT``.
    """
    n_total = len(day_df)
    clean = day_df[~day_df["early_sensor"] & ~day_df["compression_low"]]
    coverage = min(100.0, len(clean) * interval_min / 1440 * 100)
    out: dict[str, Any] = {
        "glucose_coverage_pct": _round(coverage, 0),
        "glucose_readings": n_total,
        "glucose_compression_lows": int(day_df["compression_low"].sum()),
        "glucose_early_sensor": bool(day_df["early_sensor"].any()),
    }
    if coverage < MIN_DAY_COVERAGE_PCT or clean.empty:
        return out
    v = clean["mmol"].to_numpy()
    mean = float(v.mean())
    sd = float(v.std(ddof=1)) if len(v) > 1 else 0.0
    hours = clean["local"].dt.hour
    overnight = clean.loc[hours < 6, "mmol"]
    fasting = clean.loc[(hours >= 4) & (hours < 6), "mmol"]
    out.update({
        "glucose_mean_mmol": _round(mean),
        "glucose_sd_mmol": _round(sd, 2),
        "glucose_cv_pct": _round(sd / mean * 100 if mean else None),
        "glucose_tir_pct": _round(((v >= TIGHT_LOW_MMOL) & (v <= TIGHT_HIGH_MMOL)).mean() * 100),
        "glucose_above_pct": _round((v > TIGHT_HIGH_MMOL).mean() * 100),
        "glucose_below_pct": _round((v < TIGHT_LOW_MMOL).mean() * 100),
        "glucose_min_mmol": _round(v.min()),
        "glucose_max_mmol": _round(v.max()),
        "glucose_overnight_mmol": _round(overnight.mean()) if len(overnight) >= 3 else None,
        "glucose_fasting_mmol": _round(fasting.mean()) if len(fasting) >= 2 else None,
    })
    return {k: val for k, val in out.items() if val is not None}


def _utc_window(start: str, end: str, pad_days: int = 0) -> tuple[datetime, datetime]:
    """UTC bounds that safely cover local days [start, end] (+/- 1 day for tz)."""
    s = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc) - timedelta(days=1 + pad_days)
    e = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=2)
    return s, e


def load_flagged(db_path: str, start: str, end: str) -> pd.DataFrame:
    """Flagged readings covering local days [start, end].

    Loads 15 extra days before ``start`` so a sensor that began before the
    window is not mistaken for one starting at the window edge (which would
    falsely mark its readings as early-sensor). Trimmed back to the window.
    """
    s_utc, e_utc = _utc_window(start, end, pad_days=15)
    df = load_readings(db_path, s_utc, e_utc)
    if df.empty:
        return flag_quality(df)
    df = flag_quality(df)
    day = df["local"].dt.strftime("%Y-%m-%d")
    return df[(day >= start) & (day <= end)].reset_index(drop=True)


def daily_metrics(db_path: str, start: str, end: str) -> dict[str, dict[str, Any]]:
    """``{local_date: metrics}`` for every day in [start, end] with readings."""
    df = load_flagged(db_path, start, end)
    if df.empty:
        return {}
    interval = _sampling_minutes(df)
    df = df.assign(_day=df["local"].dt.strftime("%Y-%m-%d"))
    return {day: day_metrics(g, interval) for day, g in df.groupby("_day")}


def intraday_series(df: pd.DataFrame, max_points: int = 3000) -> list[dict[str, Any]]:
    """Chart-ready points ``{t: local ISO, v: mmol, f: flag}``.

    1-minute data is resampled to 5 minutes (or 15 for long windows) so a
    multi-week window stays light; flagged points keep their flag.
    """
    if df.empty:
        return []
    interval = _sampling_minutes(df)
    span_days = (df["time"].iloc[-1] - df["time"].iloc[0]).total_seconds() / 86400
    bucket = None
    if span_days > 30:
        bucket = "60min"
    elif interval < 5:
        bucket = "15min" if span_days > 7 else "5min"
    if len(df) > max_points and bucket is None:
        bucket = "15min"
    d = df.copy()
    d["flag"] = np.where(d["compression_low"], "compression",
                         np.where(d["early_sensor"], "early", ""))
    if bucket:
        d["_b"] = d["local"].dt.floor(bucket)
        d = d.groupby("_b").agg(
            mmol=("mmol", "mean"),
            flag=("flag", lambda s: "compression" if (s == "compression").any()
                  else ("early" if (s == "early").any() else "")),
        ).reset_index().rename(columns={"_b": "local"})
    return [
        {"t": t.strftime("%Y-%m-%dT%H:%M"), "v": round(float(v), 2), **({"f": f} if f else {})}
        for t, v, f in zip(d["local"], d["mmol"], d["flag"])
    ]


def summary(db_path: str, start: str, end: str, include_series: bool = True) -> dict[str, Any]:
    """Everything the dashboard / agent need for a window."""
    df = load_flagged(db_path, start, end)
    if df.empty:
        return {"available": False, "start": start, "end": end, "units": "mmol/L"}
    interval = _sampling_minutes(df)
    days = {
        day: day_metrics(g, interval)
        for day, g in df.assign(_day=df["local"].dt.strftime("%Y-%m-%d")).groupby("_day")
    }
    clean = df[~df["early_sensor"] & ~df["compression_low"]]
    out: dict[str, Any] = {
        "available": True,
        "start": start,
        "end": end,
        "units": "mmol/L",
        "tight_range_mmol": [TIGHT_LOW_MMOL, TIGHT_HIGH_MMOL],
        "sampling_minutes": round(interval, 1),
        "sensors": int(df["session"].nunique()),
        "readings": len(df),
        "excluded": {
            "early_sensor": int(df["early_sensor"].sum()),
            "compression_low": int(df["compression_low"].sum()),
        },
        "days": days,
        "first_reading": df["local"].iloc[0].strftime("%Y-%m-%dT%H:%M"),
        "last_reading": df["local"].iloc[-1].strftime("%Y-%m-%dT%H:%M"),
    }
    if not clean.empty:
        v = clean["mmol"].to_numpy()
        out["window"] = {
            "mean_mmol": _round(v.mean()),
            "cv_pct": _round(v.std(ddof=1) / v.mean() * 100) if len(v) > 1 else None,
            "tir_pct": _round(((v >= TIGHT_LOW_MMOL) & (v <= TIGHT_HIGH_MMOL)).mean() * 100),
        }
    if include_series:
        out["series"] = intraday_series(df)
    return out
