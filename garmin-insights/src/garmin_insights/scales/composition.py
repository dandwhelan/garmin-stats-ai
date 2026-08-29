"""Bio-impedance (BIA) body-composition estimates.

Ported **verbatim** from ``web/static/app.js`` ``computeBodyComposition()``
(the coefficients, branch order, clamps and sex terms are all reproduced
exactly, including the quirk where the >=65 water branch reassigns ``water``
to 75 *before* the final ``clamp(water * water_coef, ...)`` re-applies the
coefficient). Formulas trace to lswiderski/WebBodyComposition, itself from
wiecosystem/Bluetooth; they are consumer-grade estimates, not clinical
measurements.

The JS returned camelCase keys; this returns the snake_case names the DB
(``body_composition``) and ``POST /api/weigh-in`` already use:

    JS ``bmi``          -> ``bmi``
    JS ``fat``          -> ``body_fat_pct``
    JS ``water``        -> ``body_water_pct``
    JS ``muscle``       -> ``muscle_mass_kg``
    JS ``bone``         -> ``bone_mass_kg``
    JS ``visceral``     -> ``visceral_fat``
    JS ``metabolicAge`` -> ``metabolic_age``

Nothing here raises: unusable inputs degrade to BMI-only, or to ``{}`` when
even BMI is impossible. No segmental (arm/leg/trunk) figures are
synthesised — those only exist if the raw frames carry per-channel
impedance, and inventing them would be fabrication.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["compute_body_composition"]

# Sanity bounds — outside these the input is a typo or a unit mix-up.
_HEIGHT_MIN_CM = 50.0
_HEIGHT_MAX_CM = 250.0
_WEIGHT_MIN_KG = 2.0
_WEIGHT_MAX_KG = 400.0
_AGE_MIN = 1.0
_AGE_MAX = 130.0
_IMPEDANCE_MIN = 1.0
_IMPEDANCE_MAX = 5000.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


def _number(value: Any) -> float | None:
    """Coerce to a finite float, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def compute_body_composition(
    weight_kg: float,
    impedance_ohm: float | None,
    height_cm: float,
    age: float,
    sex: str,
) -> dict[str, Any]:
    """Estimate body composition from weight + whole-body impedance.

    Returns the full metric set when weight, impedance, height, age and a
    known sex are all usable; ``{"bmi": ...}`` when only weight + height
    are; ``{}`` when height or weight are unusable. Never raises.
    """
    weight = _number(weight_kg)
    height = _number(height_cm)
    if (
        weight is None
        or height is None
        or not (_WEIGHT_MIN_KG <= weight <= _WEIGHT_MAX_KG)
        or not (_HEIGHT_MIN_CM <= height <= _HEIGHT_MAX_CM)
    ):
        return {}

    bmi = _clamp(weight / ((height / 100.0) * (height / 100.0)), 10, 90)

    impedance = _number(impedance_ohm)
    age_v = _number(age)
    sex_v = sex.strip().lower() if isinstance(sex, str) else ""
    if (
        impedance is None
        or not (_IMPEDANCE_MIN <= impedance <= _IMPEDANCE_MAX)
        or age_v is None
        or not (_AGE_MIN <= age_v <= _AGE_MAX)
        or sex_v not in ("male", "female")
    ):
        return {"bmi": bmi}

    # --- verbatim port of app.js computeBodyComposition() -----------------
    # Lean body mass coefficient — every other metric hangs off this.
    lbm = (height * 9.058 / 100) * (height / 100)
    lbm += weight * 0.32 + 12.226
    lbm -= impedance * 0.0068
    lbm -= age_v * 0.0542

    fat_base = 0.8
    if sex_v == "female":
        fat_base = 9.25 if age_v <= 49 else 7.25
    coefficient = 1.0
    if sex_v == "male" and weight < 61:
        coefficient = 0.98
    elif sex_v == "female" and weight > 60:
        coefficient = 1.03 if height > 160 else 0.96
    elif sex_v == "female" and weight < 50:
        coefficient = 1.03 if height > 160 else 1.02
    fat = (1.0 - (((lbm - fat_base) * coefficient) / weight)) * 100
    if fat > 63:
        fat = 75
    fat = _clamp(fat, 5, 75)

    water = (100 - fat) * 0.7
    water_coef = 1.02 if water <= 50 else 0.98
    if water * water_coef >= 65:
        water = 75
    water = _clamp(water * water_coef, 35, 75)

    bone = ((0.245691014 if sex_v == "female" else 0.18016894) - (lbm * 0.05158)) * -1
    bone += 0.1 if bone > 2.2 else -0.1
    if sex_v == "female" and bone > 5.1:
        bone = 8
    elif sex_v == "male" and bone > 5.2:
        bone = 8
    bone = _clamp(bone, 0.5, 8)

    muscle = weight - (fat * 0.01 * weight) - bone
    if sex_v == "female" and muscle >= 84:
        muscle = 120
    elif sex_v == "male" and muscle >= 93.5:
        muscle = 120
    muscle = _clamp(muscle, 10, 120)

    if sex_v == "female":
        if weight > (13 - (height * 0.5)) * -1:
            sub = ((height * 1.45) + (height * 0.1158) * height) - 120
            visceral = ((weight * 500 / sub) - 6) + (age_v * 0.07)
        else:
            sub = 0.691 + (height * -0.0024) + (height * -0.0024)
            visceral = (((height * 0.027) - (sub * weight)) * -1) + (age_v * 0.07) - age_v
    elif height < weight * 1.6:
        sub = ((height * 0.4) - (height * (height * 0.0826))) * -1
        visceral = ((weight * 305) / (sub + 48)) - 2.9 + (age_v * 0.15)
    else:
        sub = 0.765 + height * -0.0015
        visceral = (((height * 0.143) - (weight * sub)) * -1) + (age_v * 0.15) - 5.0
    visceral = _clamp(visceral, 1, 50)

    if sex_v == "female":
        metabolic_age = (
            (height * -1.1165)
            + (weight * 1.5784)
            + (age_v * 0.4615)
            + (impedance * 0.0415)
            + 83.2548
        )
    else:
        metabolic_age = (
            (height * -0.7471)
            + (weight * 0.9161)
            + (age_v * 0.4184)
            + (impedance * 0.0517)
            + 54.2267
        )
    metabolic_age = _clamp(metabolic_age, 15, 80)
    # --- end verbatim port ------------------------------------------------

    fat_mass_kg = weight * fat / 100.0
    fat_free_mass_kg = weight - fat_mass_kg
    # Katch-McArdle (1996) resting metabolic rate from fat-free mass:
    # BMR = 370 + 21.6 * FFM(kg). Chosen over Harris-Benedict because BIA
    # already gives us fat-free mass directly.
    bmr_kcal = 370 + 21.6 * fat_free_mass_kg

    return {
        "bmi": bmi,
        "body_fat_pct": fat,
        "body_water_pct": water,
        "muscle_mass_kg": muscle,
        "bone_mass_kg": bone,
        "visceral_fat": visceral,
        "metabolic_age": metabolic_age,
        "extras": {
            "fat_mass_kg": fat_mass_kg,
            "fat_free_mass_kg": fat_free_mass_kg,
            "bmr_kcal": bmr_kcal,
        },
    }
