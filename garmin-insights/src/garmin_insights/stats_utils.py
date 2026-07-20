"""Shared statistical helpers — Pearson correlation with p-values and
Benjamini-Hochberg false-discovery-rate (FDR) control.

The correlation/comparison features fan a single window out across many
(driver, marker) pairs. Reporting a bare Pearson ``r`` for each invites the
multiple-comparisons problem: with dozens of pairs over 30-60 days, several
will clear |r|>0.4 by chance alone. These helpers add two things the rest of
the app was missing:

* a two-sided p-value and sample size ``n`` alongside every ``r``;
* a ``significant`` flag computed *after* Benjamini-Hochberg FDR correction
  across all pairs tested together, so the UI and the agent can grey out noise.

SciPy is used when present (it already ships with the analysis stack). When it
is unavailable the p-value falls back to ``None`` and such pairs are treated as
not-significant, rather than crashing a chart render.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

try:  # SciPy is a transitive dependency of the analysis stack.
    from scipy import stats as _scipy_stats
except Exception:  # pragma: no cover - defensive: never break a render
    _scipy_stats = None

# Pairs with fewer complete observations than this are reported with r=None;
# a Pearson r on a handful of points is meaningless.
MIN_PAIRS = 7

# Single behavior→outcome lag policy, shared by every feature that joins a
# logged behavior on day X to a night-derived outcome. Daily-summary rows key
# sleep (and the metrics measured during/at the end of that sleep) to the
# WAKE-UP date, so the night affected by day X's behavior is recorded on
# day X+1 — joining on X pairs the behavior with the night BEFORE it, which
# can reverse or erase real associations. RHR and wake body battery are in
# the set because Garmin derives both from the same overnight window.
NEXT_DAY_LAG_METRICS = frozenset({
    "sleepScore", "deepSleepSeconds", "remSleepSeconds", "lightSleepSeconds",
    "awakeSleepSeconds", "sleepTimeSeconds", "avgOvernightHrv",
    "lowestHeartRate", "restingHeartRate", "bodyBatteryAtWakeTime",
    "restStressDuration", "avgStressLevel",
})


def to_kg(value: float | None) -> float | None:
    """Normalise a Garmin mass reading to kilograms.

    ``body_composition`` masses arrive in grams from the fetcher, but rows
    written by other paths may already be in kg — the >1000 guard (no human
    weighs a tonne) makes the conversion idempotent either way. This is the
    single shared implementation; use it instead of dividing by 1000 inline.
    """
    if value is None:
        return None
    return value / 1000.0 if value > 1000 else value


# Gregorian year (365.2425 days) in milliseconds — closest match to the ms
# durations Garmin returns (observed: 725,809,298,000 ms for a 23-year value).
_MS_PER_YEAR = 31_556_952_000


def metabolic_age_years(value: float | None) -> float | None:
    """Normalise a Garmin metabolic-age reading to years.

    Garmin Connect returns ``metabolicAge`` as a millisecond *duration*
    (e.g. ~7.26e11 ms ≈ 23 years), not plain years — the fetcher's original
    timestamp parse mis-read it for the same reason. Rows written before the
    fetcher-side conversion landed carry raw milliseconds, so this guard (no
    human metabolic age exceeds a millennium) makes reads idempotent either
    way, mirroring ``to_kg``. Rounded to 1 d.p. — ms precision is spurious.
    """
    if value is None:
        return None
    return round(value / _MS_PER_YEAR if value > 1000 else value, 1)


def pearson_r_p(
    x: Sequence[float], y: Sequence[float]
) -> tuple[float | None, float | None, int]:
    """Return ``(r, two_sided_p, n)`` for paired samples.

    ``n`` is the number of complete (non-NaN) pairs. ``r`` and ``p`` are
    ``None`` when ``n < 3`` or either series has zero variance (correlation
    undefined). ``p`` is ``None`` when SciPy is unavailable.
    """
    ax = np.asarray(x, dtype=float)
    ay = np.asarray(y, dtype=float)
    if ax.shape != ay.shape:
        n = int(min(ax.size, ay.size))
        ax, ay = ax[:n], ay[:n]
    mask = ~(np.isnan(ax) | np.isnan(ay))
    ax, ay = ax[mask], ay[mask]
    n = int(ax.size)
    if n < 3 or np.std(ax) == 0 or np.std(ay) == 0:
        return None, None, n
    if _scipy_stats is not None:
        r, p = _scipy_stats.pearsonr(ax, ay)
        return float(r), float(p), n
    # Fallback: r without a p-value (treated as not-significant downstream).
    r = float(np.corrcoef(ax, ay)[0, 1])
    return r, None, n


def benjamini_hochberg(
    pvalues: Sequence[float | None], q: float = 0.05
) -> list[bool]:
    """Per-item significance flags controlling the FDR at level ``q``.

    ``None`` entries (insufficient n / no SciPy) are treated as not significant
    and excluded from the procedure. The BH step-up is applied only across the
    items that carry a real p-value.
    """
    flags = [False] * len(pvalues)
    indexed = [(i, p) for i, p in enumerate(pvalues) if p is not None]
    m = len(indexed)
    if m == 0:
        return flags
    indexed.sort(key=lambda t: t[1])
    # Largest rank k (1-based) with p_(k) <= (k/m) * q; everything up to it passes.
    threshold_rank = 0
    for rank, (_, p) in enumerate(indexed, start=1):
        if p <= (rank / m) * q:
            threshold_rank = rank
    for rank, (orig_i, _) in enumerate(indexed, start=1):
        if rank <= threshold_rank:
            flags[orig_i] = True
    return flags


def finalize_correlations(
    items: list[dict], q: float = 0.05, p_key: str = "p"
) -> list[dict]:
    """Annotate a list of correlation dicts in place with FDR significance.

    Each dict is expected to already carry a raw p-value under ``p_key`` (which
    may be ``None``). Adds/overwrites:

      * ``significant`` — bool, BH-corrected across the whole list;
      * the p-value rounded to 4 dp (or left ``None``).

    Returns the same list for convenience.
    """
    flags = benjamini_hochberg([it.get(p_key) for it in items], q=q)
    for it, flag in zip(items, flags):
        it["significant"] = flag
        p = it.get(p_key)
        it[p_key] = round(p, 4) if isinstance(p, (int, float)) else None
    return items


def correlate_pair(driver_series, marker_series, **extra) -> dict:
    """Build a correlation dict ``{**extra, n, r, p}`` from two pandas Series.

    ``r``/``p`` are ``None`` when there are fewer than ``MIN_PAIRS`` complete
    observations. Call :func:`finalize_correlations` over the collected list
    afterwards to add the BH-corrected ``significant`` flag.
    """
    r, p, n = pearson_r_p(driver_series.to_numpy(), marker_series.to_numpy())
    if n < MIN_PAIRS:
        return {**extra, "n": n, "r": None, "p": None}
    return {
        **extra,
        "n": n,
        "r": None if r is None or np.isnan(r) else round(r, 2),
        "p": p,
    }
