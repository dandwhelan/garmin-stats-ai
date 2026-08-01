"""stats_utils with SciPy unavailable.

SciPy is optional — the module imports it defensively and falls back to a
p-value of ``None``. On any deployment without it (a slimmed Pi install, a
wheel that failed to build) *every* correlation in the app takes this branch,
so it must degrade to "not significant" rather than crash a chart render or,
worse, silently report noise as significant.

test_stats_utils.py covers the SciPy-present path; this file forces the other.
"""

from __future__ import annotations

import importlib
import sys

import numpy as np
import pytest

import garmin_insights.stats_utils as stats_utils


@pytest.fixture
def no_scipy(monkeypatch):
    """Reload stats_utils with the scipy import forced to fail.

    Patching the module-level ``_scipy_stats`` to None would be simpler, but
    reloading also exercises the try/except import itself — the thing that
    actually runs on a SciPy-less box.
    """
    real_import = importlib.__import__

    def blocked(name, *args, **kwargs):
        if name == "scipy" or name.startswith("scipy."):
            raise ImportError("scipy is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked)
    monkeypatch.delitem(sys.modules, "scipy", raising=False)
    monkeypatch.delitem(sys.modules, "scipy.stats", raising=False)

    module = importlib.reload(stats_utils)
    assert module._scipy_stats is None, "fixture failed to block the scipy import"
    yield module

    # Restore the real module for every other test in the session.
    monkeypatch.undo()
    importlib.reload(stats_utils)


# ==================================================================
# pearson_r_p without SciPy
# ==================================================================
def test_r_is_still_computed_without_scipy(no_scipy):
    x = np.array([1.0, 2, 3, 4, 5, 6, 7, 8])
    y = np.array([2.0, 4, 6, 8, 10, 12, 14, 16])
    r, p, n = no_scipy.pearson_r_p(x, y)

    assert r == pytest.approx(1.0)
    assert n == 8
    assert p is None, "no SciPy means no p-value, not a fabricated one"


def test_negative_correlation_without_scipy(no_scipy):
    x = np.array([1.0, 2, 3, 4, 5, 6, 7])
    r, p, n = no_scipy.pearson_r_p(x, -x)
    assert r == pytest.approx(-1.0)
    assert p is None
    assert n == 7


def test_nan_pairs_are_dropped_without_scipy(no_scipy):
    x = np.array([1.0, 2, np.nan, 4, 5, 6, 7, 8])
    y = np.array([2.0, 4, 6, np.nan, 10, 12, 14, 16])
    r, p, n = no_scipy.pearson_r_p(x, y)
    assert n == 6, "both incomplete pairs should be dropped"
    assert r == pytest.approx(1.0)
    assert p is None


def test_zero_variance_still_returns_none_without_scipy(no_scipy):
    x = np.array([5.0] * 8)
    y = np.array([1.0, 2, 3, 4, 5, 6, 7, 8])
    r, p, n = no_scipy.pearson_r_p(x, y)
    assert r is None and p is None
    assert n == 8


def test_too_few_points_without_scipy(no_scipy):
    r, p, n = no_scipy.pearson_r_p(np.array([1.0, 2]), np.array([2.0, 4]))
    assert r is None and p is None and n == 2


# ==================================================================
# The consequence: nothing is ever flagged significant
# ==================================================================
def test_nothing_is_significant_without_scipy(no_scipy):
    """The safe direction. Without p-values BH has nothing to work with, so
    the UI greys everything out instead of promoting noise."""
    x = np.arange(30.0)
    items = [
        no_scipy.correlate_pair(_series(x), _series(x * 2 + 1), pair=f"perfect_{i}")
        for i in range(5)
    ]
    no_scipy.finalize_correlations(items)

    assert all(it["r"] == pytest.approx(1.0) for it in items)
    assert all(it["p"] is None for it in items)
    assert not any(it["significant"] for it in items), (
        "a perfect correlation must still not be flagged significant when the "
        "p-value is unavailable")


def test_benjamini_hochberg_handles_an_all_none_family(no_scipy):
    assert no_scipy.benjamini_hochberg([None] * 6) == [False] * 6


def test_correlate_pair_below_min_pairs_without_scipy(no_scipy):
    x = np.arange(float(no_scipy.MIN_PAIRS - 1))
    out = no_scipy.correlate_pair(_series(x), _series(x), label="short")
    assert out["r"] is None and out["p"] is None
    assert out["n"] == no_scipy.MIN_PAIRS - 1
    assert out["label"] == "short"


def test_finalize_leaves_p_none_and_does_not_round_it(no_scipy):
    items = [{"p": None}, {"p": None}]
    no_scipy.finalize_correlations(items)
    assert all(it["p"] is None and it["significant"] is False for it in items)


# ==================================================================
# Restoration
# ==================================================================
def test_scipy_path_is_restored_after_the_fixture():
    """Guards the fixture itself — a leaked monkeypatch would silently turn
    every other correlation test in the session into a fallback test."""
    assert stats_utils._scipy_stats is not None
    x = np.arange(20.0)
    _r, p, _n = stats_utils.pearson_r_p(x, x * 3)
    assert p is not None


def _series(values):
    import pandas as pd

    return pd.Series(values)
