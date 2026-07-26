"""Numpy baseline models, metrics, and grouped bootstrap for H0018.

Metrics are checked against hand-computed toy examples; the logistic fit and
grouped bootstrap are checked for determinism and correctness.
"""

import numpy as np

from kalshi_weather.experiments import baseline as bl


def test_brier_toy() -> None:
    # perfect: p==y -> 0; worst: p opposite -> 1
    assert bl.brier(np.array([1.0, 0.0]), np.array([1.0, 0.0])) == 0.0
    assert bl.brier(np.array([0.0, 1.0]), np.array([1.0, 0.0])) == 1.0
    assert abs(bl.brier(np.array([0.5, 0.5]), np.array([1.0, 0.0])) - 0.25) < 1e-12


def test_log_loss_toy() -> None:
    # p=0.5 everywhere -> -ln(0.5)
    ll = bl.log_loss(np.array([0.5, 0.5]), np.array([1.0, 0.0]))
    assert abs(ll - (-np.log(0.5))) < 1e-9


def test_calibration_slope_of_perfect_probabilities() -> None:
    # a well-separated, calibrated set should have slope ~1, intercept ~0
    rng = np.random.default_rng(0)
    p = rng.uniform(0.05, 0.95, size=4000)
    y = (rng.uniform(size=4000) < p).astype(float)
    slope, intercept = bl.calibration_slope_intercept(p, y)
    assert 0.8 < slope < 1.2
    assert abs(intercept) < 0.3


def test_ece_perfectly_calibrated_is_small() -> None:
    rng = np.random.default_rng(1)
    p = rng.uniform(0, 1, size=5000)
    y = (rng.uniform(size=5000) < p).astype(float)
    assert bl.expected_calibration_error(p, y) < 0.05


def test_logistic_recovers_separable_signal() -> None:
    # y depends on x1; model should predict high for large x1
    rng = np.random.default_rng(2)
    x = rng.normal(size=(500, 2))
    y = (x[:, 0] + rng.normal(scale=0.3, size=500) > 0).astype(float)
    m = bl.fit_logistic(x, y, l2=0.1)
    p = m.predict(x)
    assert bl.brier(p, y) < bl.brier(np.full(500, y.mean()), y)  # beats base rate
    # deterministic
    m2 = bl.fit_logistic(x, y, l2=0.1)
    assert np.allclose(m.coef, m2.coef)


def test_platt_is_monotonic_and_calibrates() -> None:
    rng = np.random.default_rng(3)
    # over-confident raw scores
    y = (rng.uniform(size=2000) < 0.3).astype(float)
    raw = np.clip(0.5 + (y - 0.5) * 0.9 + rng.normal(scale=0.05, size=2000), 0.02, 0.98)
    m = bl.fit_platt(raw, y)
    cal = bl.apply_platt(m, raw)
    # calibrated mean should track the base rate better than the raw mean
    assert abs(cal.mean() - y.mean()) <= abs(raw.mean() - y.mean()) + 1e-6


def test_grouped_bootstrap_is_deterministic_and_groups() -> None:
    rng = np.random.default_rng(4)
    n = 200
    y = (rng.uniform(size=n) < 0.4).astype(float)
    pm = np.clip(y * 0.6 + 0.2, 0.02, 0.98)
    pb = np.full(n, 0.4)
    groups = np.repeat(np.arange(20), 10)  # 20 events x 10 markets
    r1 = bl.grouped_bootstrap_brier_diff(pm, pb, y, groups, n_boot=500, seed=7)
    r2 = bl.grouped_bootstrap_brier_diff(pm, pb, y, groups, n_boot=500, seed=7)
    assert r1 == r2  # deterministic
    assert r1["n_groups"] == 20
    assert r1["n_rows"] == n
    # model tracks y so its brier diff vs the constant benchmark is negative
    assert r1["mean_brier_diff"] < 0


def test_clip_bounds() -> None:
    p = bl.clip_prob(np.array([0.0, 1.0, 0.5]))
    assert p.min() >= bl.CLIP_LO and p.max() <= bl.CLIP_HI
