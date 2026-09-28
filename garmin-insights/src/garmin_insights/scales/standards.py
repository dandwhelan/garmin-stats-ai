"""Fitdays-style derived fields and Low/Standard/High ratings for a scan.

Ranges are calibrated against the Fitdays app's own labels on real scans
(e.g. male 7.4% body fat -> Standard, subcutaneous 5.3% -> Low; limb muscle
arms 80-115%, trunk/legs 90-110%, limb fat 80-160%). They are display context,
not clinical thresholds.
"""

from __future__ import annotations

from typing import Any

# (low_below, high_above) per sex; value in [low, high] is "Standard".
_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "male": {
        "bmi": (18.5, 25.0),
        "body_fat_pct": (5.0, 20.0),
        "body_water_pct": (55.0, 70.0),
        "muscle_rate_pct": (75.0, 89.0),
        "skeletal_muscle_pct": (49.0, 59.0),
        "protein_pct": (16.0, 20.0),
        "subcutaneous_fat_pct": (8.6, 16.7),
        "visceral_fat": (1.0, 9.0),
    },
    "female": {
        "bmi": (18.5, 25.0),
        "body_fat_pct": (18.0, 28.0),
        "body_water_pct": (45.0, 60.0),
        "muscle_rate_pct": (70.0, 85.0),
        "skeletal_muscle_pct": (40.0, 50.0),
        "protein_pct": (16.0, 20.0),
        "subcutaneous_fat_pct": (18.5, 26.7),
        "visceral_fat": (1.0, 9.0),
    },
}
_SEG_MUSCLE = {"left_arm": (80.0, 115.0), "right_arm": (80.0, 115.0),
               "trunk": (90.0, 110.0), "left_leg": (90.0, 110.0), "right_leg": (90.0, 110.0)}
_SEG_FAT = (80.0, 160.0)


def _is_female(sex: str | None) -> bool:
    return str(sex or "").strip().lower() in ("female", "f", "2")


def _rate(value: Any, rng: tuple[float, float]) -> str | None:
    if value is None:
        return None
    v = float(value)
    return "Low" if v < rng[0] else "High" if v > rng[1] else "Standard"


def ideal_weight_kg(height_cm: float | None, sex: str | None) -> float | None:
    """Fitdays' ideal weight: (h-70)*0.6 for women, BMI 22 for men."""
    if not height_cm:
        return None
    if _is_female(sex):
        return round((height_cm - 70.0) * 0.6, 2)
    return round(22.0 * (height_cm / 100.0) ** 2, 2)


def enrich(metrics: dict, extras: dict, weight_kg: float, height_cm: float | None,
           sex: str | None, age: float | None) -> dict:
    """Return new extras with muscle rate, ideal weight and ratings added."""
    out = dict(extras)
    muscle = metrics.get("muscle_mass_kg")
    if muscle is not None and weight_kg:
        out["muscle_rate_pct"] = round(100.0 * muscle / weight_kg, 1)
    ideal = ideal_weight_kg(height_cm, sex)
    if ideal is not None:
        out["ideal_weight_kg"] = ideal

    ranges = _RANGES["female" if _is_female(sex) else "male"]
    values = {**metrics, **out}
    ratings = {k: r for k, rng in ranges.items() if (r := _rate(values.get(k), rng))}
    if metrics.get("metabolic_age") is not None and age:
        ratings["metabolic_age"] = "Excellent" if metrics["metabolic_age"] <= age else "High"
    if ratings:
        out["ratings"] = ratings

    segs = out.get("segments")
    if isinstance(segs, dict):
        out["segments"] = {
            k: {**s,
                **({"muscle_rating": _rate(s.get("muscle_pct"), _SEG_MUSCLE[k])}
                   if k in _SEG_MUSCLE and s.get("muscle_pct") is not None else {}),
                **({"fat_rating": _rate(s.get("fat_pct"), _SEG_FAT)}
                   if s.get("fat_pct") is not None else {})}
            for k, s in segs.items()
        }
    return out
