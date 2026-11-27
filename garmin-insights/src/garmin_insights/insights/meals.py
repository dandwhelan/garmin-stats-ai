"""Meal log + per-meal glucose response.

A meal is a local wall-clock time plus a description and (optional) macros.
Macros come from the user, a Claude photo estimate (``estimate_from_photo``)
or an Open Food Facts barcode lookup (``lookup_barcode``).

For every meal with enough CGM coverage, ``meal_response`` computes:

* **baseline** — median glucose in the 30 min before eating;
* **rise** — peak above baseline within 2 h, and **time to peak**;
* **iAUC** — incremental area above baseline over 0-2 h (mmol/L·h);
* **dip** — how far glucose fell *below* baseline 2-4 h later. A post-meal
  dip predicts hunger and energy slumps (Wyatt 2021, *Nature Metabolism*,
  PREDICT 2, n≈1,070) — the "sluggish after lunch" signal.

Context flags travel with each response so a reader never mistakes a
confounded curve for the food's effect: ``overlap`` (another meal inside the
window), ``exercise`` (a recorded activity during 0-2 h), ``partial``
(coverage gaps). Identical meals vary a lot between days (Hall 2018; Berry
2020 PREDICT 1), so food-level summaries are only marked ``reliable`` from
three servings.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import requests

from garmin_insights.insights import glucose

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eaten_at TEXT NOT NULL,
    date TEXT NOT NULL,
    description TEXT NOT NULL,
    carbs_g REAL, sugar_g REAL, protein_g REAL, fat_g REAL, fibre_g REAL, kcal REAL,
    tags TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    estimate_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_meals_date ON meals (date);
"""

MACRO_FIELDS = ("carbs_g", "sugar_g", "protein_g", "fat_g", "fibre_g", "kcal")
SOURCES = ("manual", "photo", "barcode", "repeat")

# Response windows (minutes after the logged eating time).
_BASELINE_MIN = 30
_PEAK_WINDOW_MIN = 120
_DIP_WINDOW = (120, 240)
_MIN_WINDOW_COVERAGE = 0.6
# A dip only counts when it is a real fall below baseline, not sensor noise.
_DIP_MIN_MMOL = 0.3
RELIABLE_SERVINGS = 3

_trapezoid = getattr(np, "trapezoid", None) or np.trapz  # numpy<2 fallback

# Unlogged-rise detection (daytime, away from logged meals and activities).
_UNLOGGED_RISE_MMOL = 2.0
_UNLOGGED_WITHIN_MIN = 60
_UNLOGGED_MEAL_GAP_MIN = 60
_UNLOGGED_HOURS = (6, 23)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def normalise_eaten_at(value: str) -> str:
    """Accept ``YYYY-MM-DDTHH:MM[:SS]`` (local) → ``YYYY-MM-DDTHH:MM``."""
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"eaten_at must be a local ISO datetime, got {value!r}") from None
    if dt.tzinfo is not None:  # convert to local wall clock
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M")


def _clean_macro(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 1) if np.isfinite(f) and 0 <= f < 10000 else None


def save_meal(db_path: str, meal: dict[str, Any], meal_id: int | None = None) -> int:
    eaten_at = normalise_eaten_at(meal.get("eaten_at", ""))
    description = str(meal.get("description") or "").strip()[:300]
    if not description:
        raise ValueError("description is required")
    source = meal.get("source") if meal.get("source") in SOURCES else "manual"
    tags = meal.get("tags")
    if isinstance(tags, (list, tuple)):
        tags = ",".join(str(t).strip().lower() for t in tags if str(t).strip())
    estimate = meal.get("estimate")
    values = (
        eaten_at, eaten_at[:10], description,
        *(_clean_macro(meal.get(f)) for f in MACRO_FIELDS),
        (tags or None), source, json.dumps(estimate) if estimate else None,
    )
    conn = _conn(db_path)
    try:
        if meal_id is None:
            cur = conn.execute(
                "INSERT INTO meals (eaten_at, date, description, carbs_g, sugar_g, protein_g, "
                "fat_g, fibre_g, kcal, tags, source, estimate_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            conn.commit()
            return int(cur.lastrowid)
        cur = conn.execute(
            "UPDATE meals SET eaten_at=?, date=?, description=?, carbs_g=?, sugar_g=?, protein_g=?, "
            "fat_g=?, fibre_g=?, kcal=?, tags=?, source=?, estimate_json=? WHERE id=?",
            (*values, meal_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise KeyError(meal_id)
        return meal_id
    finally:
        conn.close()


def delete_meal(db_path: str, meal_id: int) -> bool:
    conn = _conn(db_path)
    try:
        cur = conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_meals(db_path: str, start: str, end: str) -> list[dict[str, Any]]:
    """Meals on local dates [start, end], oldest first."""
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM meals WHERE date >= ? AND date <= ? ORDER BY eaten_at", (start, end)
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        d.pop("estimate_json", None)
        d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
        out.append({k: v for k, v in d.items() if v is not None})
    return out


def get_meal(db_path: str, meal_id: int) -> dict[str, Any] | None:
    conn = _conn(db_path)
    try:
        r = conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    d = {k: r[k] for k in r.keys()}
    d.pop("estimate_json", None)
    d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
    return {k: v for k, v in d.items() if v is not None}


def daily_meal_metrics(db_path: str, start: str, end: str) -> dict[str, dict[str, Any]]:
    """``{date: {meals_logged, meal_carbs_g, last_meal_hour, first_meal_hour}}``.

    ``last_meal_hour`` is a decimal local hour (20.5 = 20:30); the night that
    follows is keyed to the NEXT date in sleep_summary (wake-date keying).
    """
    out: dict[str, dict[str, Any]] = {}
    for m in get_meals(db_path, start, end):
        d = out.setdefault(m["date"], {"meals_logged": 0})
        d["meals_logged"] += 1
        if m.get("carbs_g") is not None:
            d["meal_carbs_g"] = round(d.get("meal_carbs_g", 0) + m["carbs_g"], 1)
        hour = int(m["eaten_at"][11:13]) + int(m["eaten_at"][14:16]) / 60
        d["first_meal_hour"] = round(min(d.get("first_meal_hour", 99), hour), 2)
        d["last_meal_hour"] = round(max(d.get("last_meal_hour", -1), hour), 2)
    return out


def recent_staples(db_path: str, limit: int = 8, days: int = 60) -> list[dict[str, Any]]:
    """Most-logged meals of the last ``days`` — one-tap re-log chips.

    Uses the latest macros for each description (case-insensitive).
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT m.* , c.n FROM meals m
            JOIN (SELECT lower(description) AS d, COUNT(*) AS n, MAX(eaten_at) AS last
                  FROM meals WHERE date >= ? GROUP BY lower(description)) c
              ON lower(m.description) = c.d AND m.eaten_at = c.last
            ORDER BY c.n DESC, c.last DESC LIMIT ?
            """,
            (since, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {k: r[k] for k in ("description", *MACRO_FIELDS, "tags", "n") if r[k] is not None}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Open Food Facts
# ---------------------------------------------------------------------------
OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
_OFF_FIELDS = "product_name,brands,serving_size,serving_quantity,nutriments"


def lookup_barcode(code: str, timeout: float = 10) -> dict[str, Any] | None:
    """Per-100 g macros (and serving size when known) for a barcode, or None."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if not 6 <= len(code) <= 14:
        raise ValueError("barcode must be 6-14 digits")
    r = requests.get(
        OFF_URL.format(code=code), params={"fields": _OFF_FIELDS}, timeout=timeout,
        headers={"User-Agent": "garmin-insights/1.0 (personal health dashboard)"},
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    body = r.json()
    if body.get("status") != 1 or not body.get("product"):
        return None
    p = body["product"]
    n = p.get("nutriments") or {}

    def g(key: str) -> float | None:
        return _clean_macro(n.get(f"{key}_100g"))

    name = " ".join(x for x in (p.get("brands", "").split(",")[0].strip(), p.get("product_name", "")) if x)
    return {
        "barcode": code,
        "name": name or code,
        "per_100g": {
            "carbs_g": g("carbohydrates"), "sugar_g": g("sugars"), "protein_g": g("proteins"),
            "fat_g": g("fat"), "fibre_g": g("fiber"), "kcal": g("energy-kcal"),
        },
        "serving_g": _clean_macro(p.get("serving_quantity")),
        "serving_size": p.get("serving_size"),
    }


# ---------------------------------------------------------------------------
# Photo estimate (Claude vision)
# ---------------------------------------------------------------------------
_ESTIMATE_TOOL = {
    "name": "record_meal_estimate",
    "description": "Record the estimated contents and macronutrients of the meal in the photo.",
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Short meal name, e.g. 'Porridge with banana and honey'."},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "grams": {"type": "number"},
                    },
                    "required": ["name"],
                },
            },
            "carbs_g": {"type": "number"},
            "sugar_g": {"type": "number"},
            "protein_g": {"type": "number"},
            "fat_g": {"type": "number"},
            "fibre_g": {"type": "number"},
            "kcal": {"type": "number"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lower-case food-type tags, e.g. 'refined carbs', 'wholegrain', 'fruit', 'dairy', 'alcohol', 'fried', 'high protein', 'sweet'.",
            },
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "notes": {"type": "string", "description": "Anything uncertain (hidden sauces, portion size)."},
        },
        "required": ["description", "carbs_g", "protein_g", "fat_g", "kcal", "confidence"],
    },
}

_ESTIMATE_PROMPT = (
    "Estimate this meal for a personal food log that is compared against a "
    "continuous glucose monitor. Identify each item and its likely portion in "
    "grams, then total the carbohydrate, sugar, protein, fat, fibre and kcal. "
    "Prefer typical UK portions when unsure and say what you were unsure of."
)

_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def estimate_from_photo(client: Any, model: str, image_b64: str, media_type: str,
                        note: str = "") -> dict[str, Any]:
    """Ask Claude for a structured macro estimate of a meal photo."""
    if media_type not in _MEDIA_TYPES:
        raise ValueError(f"unsupported image type {media_type!r}")
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except Exception:
        raise ValueError("image is not valid base64") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("image is larger than 5 MB")
    content: list[dict[str, Any]] = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
        {"type": "text", "text": _ESTIMATE_PROMPT + (f"\n\nThe user adds: {note.strip()[:500]}" if note.strip() else "")},
    ]
    response = client.messages.create(
        model=model,
        max_tokens=1500,
        tools=[_ESTIMATE_TOOL],
        tool_choice={"type": "tool", "name": _ESTIMATE_TOOL["name"]},
        messages=[{"role": "user", "content": content}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            est = dict(block.input)
            for f in MACRO_FIELDS:
                if f in est:
                    est[f] = _clean_macro(est[f])
            est["tags"] = [str(t).lower() for t in est.get("tags") or []][:8]
            return est
    raise RuntimeError("model returned no estimate")


# ---------------------------------------------------------------------------
# Glucose response
# ---------------------------------------------------------------------------
def _activity_windows(db_path: str, start: str, end: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Recorded activities as local-naive (start, end) windows."""
    s = (datetime.fromisoformat(start) - timedelta(days=1)).strftime("%Y-%m-%d")
    e = (datetime.fromisoformat(end) + timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            df = pd.read_sql_query(
                "SELECT time, elapsed_duration FROM activity_summary WHERE time >= ? AND time < ?",
                conn, params=(s, e),
            )
        finally:
            conn.close()
    except Exception:
        return []
    if df.empty:
        return []
    # activity_summary.time is the GMT start time.
    starts = pd.to_datetime(df["time"], format="ISO8601", utc=True)
    local = glucose._to_local_naive(pd.DatetimeIndex(starts))
    durs = pd.to_timedelta(df["elapsed_duration"].fillna(0), unit="s")
    return [(st, st + d) for st, d in zip(local, durs)]


def _window(curve: pd.Series, t0: pd.Timestamp, a_min: float, b_min: float) -> pd.Series:
    return curve[(curve.index >= t0 + pd.Timedelta(minutes=a_min)) &
                 (curve.index <= t0 + pd.Timedelta(minutes=b_min))]


def meal_response(curve: pd.Series, t0: pd.Timestamp, interval_min: float) -> dict[str, Any] | None:
    """Response metrics for one meal from a clean local-time glucose curve (mmol/L)."""
    pre = _window(curve, t0, -_BASELINE_MIN, 0)
    post = _window(curve, t0, 0, _PEAK_WINDOW_MIN)
    expected = _PEAK_WINDOW_MIN / interval_min
    if pre.empty or len(post) < max(3, _MIN_WINDOW_COVERAGE * expected):
        return None
    baseline = float(pre.median())
    peak_t = post.idxmax()
    peak = float(post.max())
    mins = (post.index - t0).total_seconds().to_numpy() / 60
    excess = np.clip(post.to_numpy() - baseline, 0, None)
    iauc = float(_trapezoid(excess, mins)) / 60 if len(post) > 1 else 0.0
    out: dict[str, Any] = {
        "baseline_mmol": round(baseline, 1),
        "peak_mmol": round(peak, 1),
        "rise_mmol": round(max(0.0, peak - baseline), 1),
        "time_to_peak_min": int(round((peak_t - t0).total_seconds() / 60)),
        "iauc_mmol_h": round(iauc, 2),
    }
    late = _window(curve, t0, *_DIP_WINDOW)
    if len(late) >= max(3, _MIN_WINDOW_COVERAGE * (_DIP_WINDOW[1] - _DIP_WINDOW[0]) / interval_min):
        low = float(late.min())
        dip = baseline - low
        out["dip_mmol"] = round(dip, 1) if dip >= _DIP_MIN_MMOL else 0.0
        out["dip_min_after"] = int(round((late.idxmin() - t0).total_seconds() / 60))
    if len(post) < 0.85 * expected:
        out["partial"] = True
    return out


def _clean_curve(df: pd.DataFrame) -> pd.Series:
    clean = df[~df["early_sensor"] & ~df["compression_low"]]
    return pd.Series(clean["mmol"].to_numpy(), index=pd.DatetimeIndex(clean["local"])).sort_index()


def analyse(db_path: str, start: str, end: str) -> dict[str, Any]:
    """Meals in [start, end] with their glucose responses, food summaries and unlogged rises."""
    meals = get_meals(db_path, start, end)
    # Responses reach 4 h past the last meal of ``end``; load the next day too.
    g_end = (datetime.fromisoformat(end) + timedelta(days=1)).strftime("%Y-%m-%d")
    df = glucose.load_flagged(db_path, start, g_end)
    has_glucose = not df.empty
    curve = _clean_curve(df) if has_glucose else pd.Series(dtype=float)
    interval = glucose._sampling_minutes(df) if has_glucose else 15.0
    activities = _activity_windows(db_path, start, end) if meals or has_glucose else []
    times = [pd.Timestamp(m["eaten_at"]) for m in meals]

    for i, m in enumerate(meals):
        if not has_glucose:
            break
        t0 = times[i]
        resp = meal_response(curve, t0, interval)
        if resp is None:
            continue
        flags = []
        if any(abs((t - t0).total_seconds()) / 60 < _PEAK_WINDOW_MIN and j != i
               for j, t in enumerate(times)):
            flags.append("overlap")
        w_end = t0 + pd.Timedelta(minutes=_PEAK_WINDOW_MIN)
        if any(a < w_end and b > t0 for a, b in activities):
            flags.append("exercise")
        if resp.pop("partial", False):
            flags.append("partial")
        resp["flags"] = flags
        m["response"] = resp

    return {
        "start": start,
        "end": end,
        "glucose_available": has_glucose,
        "meals": meals,
        "foods": food_summary(meals),
        "unlogged_rises": unlogged_rises(curve, times, activities, start, end) if has_glucose else [],
    }


def food_summary(meals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Median response per meal description, ranked by rise.

    Confounded responses (overlap / exercise) are left out. ``reliable`` only
    from RELIABLE_SERVINGS clean servings.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    names: dict[str, str] = {}
    for m in meals:
        r = m.get("response")
        if not r or set(r.get("flags", [])) & {"overlap", "exercise"}:
            continue
        key = m["description"].strip().lower()
        groups.setdefault(key, []).append(r)
        names.setdefault(key, m["description"].strip())
    out = []
    for key, rs in groups.items():
        dips = [r["dip_mmol"] for r in rs if "dip_mmol" in r]
        out.append({
            "description": names[key],
            "servings": len(rs),
            "median_rise_mmol": round(float(np.median([r["rise_mmol"] for r in rs])), 1),
            "median_iauc_mmol_h": round(float(np.median([r["iauc_mmol_h"] for r in rs])), 2),
            "median_dip_mmol": round(float(np.median(dips)), 1) if dips else None,
            "rise_range_mmol": [min(r["rise_mmol"] for r in rs), max(r["rise_mmol"] for r in rs)],
            "reliable": len(rs) >= RELIABLE_SERVINGS,
        })
    out.sort(key=lambda f: (-f["median_rise_mmol"], f["description"]))
    return out


def unlogged_rises(curve: pd.Series, meal_times: list[pd.Timestamp],
                   activities: list[tuple[pd.Timestamp, pd.Timestamp]],
                   start: str, end: str) -> list[dict[str, Any]]:
    """Daytime rises ≥2 mmol/L within an hour with no meal or activity nearby."""
    if curve.empty:
        return []
    c = curve[(curve.index >= pd.Timestamp(start)) &
              (curve.index < pd.Timestamp(end) + pd.Timedelta(days=1))]
    # 5-minute means keep the forward scan cheap on 1-minute Juggluco data.
    c = c.resample("5min").mean().dropna()
    out: list[dict[str, Any]] = []
    last_flag: pd.Timestamp | None = None
    idx = c.index
    vals = c.to_numpy()
    ends = idx.searchsorted(idx + pd.Timedelta(minutes=_UNLOGGED_WITHIN_MIN), side="right")
    for i in range(len(c)):
        t = idx[i]
        if not (_UNLOGGED_HOURS[0] <= t.hour < _UNLOGGED_HOURS[1]):
            continue
        if last_flag is not None and t - last_flag < pd.Timedelta(hours=3):
            continue
        ahead = vals[i + 1:ends[i]]
        if ahead.size == 0 or ahead.max() - vals[i] < _UNLOGGED_RISE_MMOL:
            continue
        if any(abs((t - mt).total_seconds()) / 60 <= _UNLOGGED_MEAL_GAP_MIN for mt in meal_times):
            continue
        k = int(np.argmax(ahead))
        peak_t = idx[i + 1 + k]
        ahead = pd.Series(ahead)
        if any(a <= peak_t and b >= t - pd.Timedelta(minutes=30) for a, b in activities):
            continue  # hard training raises glucose too
        out.append({
            "start": t.strftime("%Y-%m-%dT%H:%M"),
            "peak_at": peak_t.strftime("%Y-%m-%dT%H:%M"),
            "rise_mmol": round(float(ahead.max() - vals[i]), 1),
        })
        last_flag = t
    return out
