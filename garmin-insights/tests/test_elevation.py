"""Hysteresis elevation gain — sensor jitter must not read as climbing."""

import math

import pytest

from garmin_insights.db.sqlite_repo import elevation_gain_loss


def test_flat_noisy_track_has_no_gain():
    alts = [100 + (1.4 if i % 2 else -1.4) for i in range(500)]
    assert elevation_gain_loss(alts) == (0.0, 0.0)


def test_real_climb_and_descent_are_counted():
    up = [100 + i for i in range(51)]           # +50 m
    down = [150 - i for i in range(1, 31)]      # -30 m
    gain, loss = elevation_gain_loss(up + down)
    assert gain == pytest.approx(48, abs=3)
    assert loss == pytest.approx(-30, abs=3)


def test_noise_on_a_climb_does_not_inflate_it():
    alts = [100 + i * 0.5 + (1.0 if i % 2 else -1.0) for i in range(201)]  # +100 m
    gain, _ = elevation_gain_loss(alts)
    assert gain == pytest.approx(100, abs=5)


def test_skips_missing_samples():
    assert elevation_gain_loss([None, math.nan, 10, None, 20]) == (10.0, 0.0)
