"""Activity-dose and training-guidance findings grounded in recent evidence.

Pure functions over the cached daily summaries (plus recorded activities) so
each one is unit-testable without a database. ``InsightScanner`` wires them
into ``run_full_scan`` under the ``fitness`` category; every finding names the
KB rule (``knowledge/medical.py``) whose tier and confounders govern how the
agent may phrase it.

Evidence behind the thresholds:

* steps — ~7,000/day captures most of the mortality benefit
  (Ding 2025 Lancet Public Health dose-response meta-analysis).
* aerobic volume — WHO 2020: 150 min/week moderate, vigorous counts double.
  Hitting it in 1-2 days ("weekend warrior") tracks with similar CVD benefit
  (Khurshid 2023 JAMA), so concentration is reported as a pattern, not a fault.
* vigorous bursts — short daily vigorous bouts in non-exercisers
  (Stamatakis 2022 Nat Med, VILPA) — offered only when weekly volume is short.
* strength — 30-60 min/week, ≥2 days/week (Momma 2022 BJSM; WHO 2020).
* HRV-guided intensity — 7-day rolling ln-HRV vs the person's own baseline
  ± 0.5 SD (Plews 2013; Manresa-Rocamora 2021 meta-analysis).
"""

from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from typing import Any

STEPS_TARGET = 7000
WHO_MVPA_WEEKLY = 150            # moderate-equivalent minutes
WEEKEND_WARRIOR_SHARE = 0.5      # ≥50% of the week's MVPA in ≤2 days
STRENGTH_DAYS_TARGET = 2         # WHO: ≥2 days/week
STRENGTH_MINUTES_RANGE = (30, 60)
HRV_SWC_SD = 0.5                 # smallest worthwhile change, in baseline SDs
HRV_BASELINE_MIN_DAYS = 14
HRV_BASELINE_RELIABLE_DAYS = 21  # mirrors baseline_reliability_guard

# Garmin activity typeKeys that count as muscle-strengthening.
STRENGTH_ACTIVITY_TYPES = frozenset({"strength_training"})


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def steps_dose(summaries: list[dict], min_days: int = 7) -> dict | None:
    """Median complete-day steps vs the ~7,000/day dose-response knee."""
    vals = [
        v for s in summaries
        if s.get("is_complete", True) and (v := _num(s.get("totalSteps"))) and v > 0
    ]
    if len(vals) < min_days:
        return None
    median = statistics.median(vals)
    return {
        "days": len(vals),
        "median_steps": round(median),
        "pct_days_ge_7000": round(100 * sum(v >= STEPS_TARGET for v in vals) / len(vals)),
        "target_steps": STEPS_TARGET,
        "status": "meets" if median >= STEPS_TARGET else "below",
        "steps_to_target": max(0, round(STEPS_TARGET - median)),
    }


def weekly_mvpa(summaries: list[dict], min_days: int = 5) -> dict | None:
    """Last-7-complete-days MVPA volume and how it was distributed.

    Pass the 7 most recent complete days. Vigorous minutes count double
    (WHO). Pattern is ``below_guideline`` / ``weekend_warrior`` /
    ``regular`` (Khurshid 2023 definition: ≥50% of the total in 1-2 days).
    """
    days = [s for s in summaries if s.get("is_complete", True)]
    if len(days) < min_days:
        return None
    per_day, vig_days, vig_total = [], 0, 0.0
    for s in days:
        mod = _num(s.get("moderateIntensityMinutes")) or 0.0
        vig = _num(s.get("vigorousIntensityMinutes")) or 0.0
        per_day.append(mod + 2 * vig)
        vig_total += vig
        vig_days += vig > 0
    total = sum(per_day)
    top_two = sum(sorted(per_day, reverse=True)[:2])
    if total < WHO_MVPA_WEEKLY:
        pattern = "below_guideline"
    elif top_two >= WEEKEND_WARRIOR_SHARE * total:
        pattern = "weekend_warrior"
    else:
        pattern = "regular"
    return {
        "days": len(days),
        "mvpa_equiv": round(total),
        "who_target": WHO_MVPA_WEEKLY,
        "active_days": sum(v > 0 for v in per_day),
        "top_two_days_share": round(top_two / total, 2) if total else None,
        "pattern": pattern,
        "vigorous_days": vig_days,
        "vigorous_per_day": round(vig_total / len(days), 1),
    }


def strength_volume(
    activities: list[dict], today: date, weeks: int = 4
) -> dict | None:
    """Recorded strength sessions per week over the last ``weeks`` weeks.

    ``activities`` are rows with ``activity_type``, ``time`` (ISO string) and
    ``elapsed_duration`` (seconds). Returns None when nothing at all was
    recorded in the window — no recordings means we can't tell, not zero.
    """
    start = (today - timedelta(days=7 * weeks)).isoformat()
    in_window = [a for a in activities if str(a.get("time", ""))[:10] >= start]
    if not in_window:
        return None
    strength = [
        a for a in in_window
        if str(a.get("activity_type") or "").lower() in STRENGTH_ACTIVITY_TYPES
    ]
    minutes = sum((_num(a.get("elapsed_duration")) or 0) for a in strength) / 60
    distinct_days = len({str(a.get("time"))[:10] for a in strength})
    days_per_week = distinct_days / weeks
    minutes_per_week = minutes / weeks
    lo, hi = STRENGTH_MINUTES_RANGE
    return {
        "weeks": weeks,
        "sessions_per_week": round(len(strength) / weeks, 1),
        "days_per_week": round(days_per_week, 1),
        "minutes_per_week": round(minutes_per_week),
        "target_days_per_week": STRENGTH_DAYS_TARGET,
        "status": "meets" if days_per_week >= STRENGTH_DAYS_TARGET else "below",
        "in_30_60_min_range": lo <= minutes_per_week <= hi,
        "note": "Only sessions recorded on the watch are counted.",
    }


def hrv_readiness(summaries: list[dict], today: date) -> dict | None:
    """7-day rolling ln(overnight HRV) vs the user's own normal range.

    ``summaries`` should span ~60 days up to and including today — today's row
    carries LAST NIGHT's HRV (sleep is keyed to the wake date), which is valid
    even though the day itself is incomplete.
    """
    week_start = (today - timedelta(days=6)).isoformat()
    recent, baseline = [], []
    for s in summaries:
        v = _num(s.get("avgOvernightHrv"))
        if not v or v <= 0:
            continue
        (recent if s.get("date", "") >= week_start else baseline).append(math.log(v))
    if len(recent) < 4 or len(baseline) < HRV_BASELINE_MIN_DAYS:
        return None
    mean = statistics.fmean(baseline)
    sd = statistics.stdev(baseline)
    rolling = statistics.fmean(recent)
    low, high = mean - HRV_SWC_SD * sd, mean + HRV_SWC_SD * sd
    if rolling < low:
        status, guidance = "below_normal", "favour low intensity / recovery until it returns to range"
    elif rolling > high:
        status, guidance = "above_normal", "above range — train as planned; not by itself a reason to push harder"
    else:
        status, guidance = "within_normal", "train as planned"
    out = {
        "ln_rolling_7d": round(rolling, 3),
        "ln_band_low": round(low, 3),
        "ln_band_high": round(high, 3),
        "rolling_7d_ms": round(math.exp(rolling), 1),
        "nights_in_window": len(recent),
        "baseline_nights": len(baseline),
        "status": status,
        "guidance": guidance,
    }
    if len(baseline) < HRV_BASELINE_RELIABLE_DAYS:
        out["baseline_low_confidence"] = True
    return out
