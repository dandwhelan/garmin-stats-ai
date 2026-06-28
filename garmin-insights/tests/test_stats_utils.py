"""Tests for the shared correlation + FDR helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd

from garmin_insights.stats_utils import (
    MIN_PAIRS,
    benjamini_hochberg,
    correlate_pair,
    finalize_correlations,
    pearson_r_p,
)


def test_pearson_strong_positive():
    x = np.arange(30.0)
    y = 2 * x + 1
    r, p, n = pearson_r_p(x, y)
    assert n == 30
    assert r > 0.99
    assert p is not None and p < 1e-6


def test_pearson_noise_is_not_significant():
    rs = np.random.RandomState(42)
    r, p, n = pearson_r_p(rs.normal(size=40), rs.normal(size=40))
    assert n == 40
    assert p is not None and p > 0.05


def test_pearson_handles_nan_pairs():
    x = [1.0, 2.0, np.nan, 4.0, 5.0, 6.0, 7.0]
    y = [2.0, 4.0, 6.0, np.nan, 10.0, 12.0, 14.0]
    r, p, n = pearson_r_p(x, y)
    assert n == 5  # two pairs dropped
    assert r > 0.99


def test_pearson_zero_variance_returns_none():
    r, p, n = pearson_r_p([1, 1, 1, 1], [1, 2, 3, 4])
    assert r is None and p is None and n == 4


def test_pearson_too_few_points():
    r, p, n = pearson_r_p([1.0, 2.0], [2.0, 4.0])
    assert r is None and p is None and n == 2


def test_benjamini_hochberg_controls_discoveries():
    # One genuinely tiny p among many large ones — only it should pass.
    flags = benjamini_hochberg([0.0001, 0.6, 0.7, 0.8, 0.9])
    assert flags == [True, False, False, False, False]


def test_benjamini_hochberg_all_significant():
    flags = benjamini_hochberg([0.001, 0.002, 0.003])
    assert all(flags)


def test_benjamini_hochberg_ignores_none():
    flags = benjamini_hochberg([None, 0.0001, None])
    assert flags == [False, True, False]


def test_benjamini_hochberg_empty():
    assert benjamini_hochberg([]) == []
    assert benjamini_hochberg([None, None]) == [False, False]


def test_correlate_pair_below_min_pairs():
    n_pts = MIN_PAIRS - 1
    item = correlate_pair(
        pd.Series(range(n_pts), dtype=float),
        pd.Series(range(n_pts), dtype=float),
        driver="d", marker="m",
    )
    assert item["r"] is None and item["p"] is None
    assert item["n"] == n_pts
    assert item["driver"] == "d" and item["marker"] == "m"


def test_correlate_pair_strong():
    x = pd.Series(np.arange(20.0))
    item = correlate_pair(x, x * 3, driver="d", marker="m")
    assert item["r"] == 1.0
    assert item["n"] == 20


def test_finalize_correlations_adds_significance_and_rounds_p():
    items = [
        {"r": 0.95, "p": 0.00001},
        {"r": 0.05, "p": 0.8},
        {"r": None, "p": None},
    ]
    out = finalize_correlations(items)
    assert out[0]["significant"] is True
    assert out[1]["significant"] is False
    assert out[2]["significant"] is False
    assert out[0]["p"] == 0.0  # rounded to 4dp
