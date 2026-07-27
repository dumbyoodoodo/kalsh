"""Benchmark comparison: known-result fixtures, group-level resampling,
determinism, and agreement with the frozen H0018 bootstrap on Brier."""

import numpy as np
import pytest

from kalshi_weather.experiments.baseline import grouped_bootstrap_brier_diff
from kalshi_weather.research.benchmark import (
    BenchmarkError,
    compare_probabilities,
    grouped_bootstrap_diff,
    per_row_losses,
)


def test_per_row_losses_known_values() -> None:
    p = np.array([0.8, 0.3])
    y = np.array([1.0, 0.0])
    brier = per_row_losses(p, y, metric="brier")
    assert brier == pytest.approx([0.04, 0.09])
    ll = per_row_losses(p, y, metric="log_loss")
    assert ll == pytest.approx([-np.log(0.8), -np.log(0.7)])


def test_perfect_model_beats_bad_benchmark_with_ci_below_zero() -> None:
    rng = np.random.default_rng(7)
    y = (rng.random(200) < 0.5).astype(float)
    p_model = np.clip(y * 0.9 + 0.05, 0, 1)  # near-perfect
    p_bench = np.full(200, 0.5)
    groups = np.repeat(np.arange(50), 4)
    cmp_ = compare_probabilities(p_model, p_bench, y, groups, metric="brier", seed=1)
    assert cmp_.mean_diff < 0
    assert cmp_.ci95_hi < 0 and cmp_.excludes_zero
    assert cmp_.n_groups == 50 and cmp_.n_rows == 200


def test_identical_forecasts_give_zero_diff_and_no_exclusion() -> None:
    p = np.array([0.4, 0.6, 0.7, 0.2])
    y = np.array([0.0, 1.0, 1.0, 0.0])
    cmp_ = compare_probabilities(p, p, y, np.array(["a", "a", "b", "b"]), seed=3)
    assert cmp_.mean_diff == 0.0
    assert not cmp_.excludes_zero


def test_deterministic_given_seed_and_sensitive_to_seed() -> None:
    rng = np.random.default_rng(11)
    y = (rng.random(60) < 0.4).astype(float)
    pm, pb = rng.random(60), rng.random(60)
    g = np.repeat(np.arange(15), 4)
    a = compare_probabilities(pm, pb, y, g, seed=42)
    b = compare_probabilities(pm, pb, y, g, seed=42)
    c = compare_probabilities(pm, pb, y, g, seed=43)
    assert (a.ci95_lo, a.ci95_hi) == (b.ci95_lo, b.ci95_hi)
    assert (a.ci95_lo, a.ci95_hi) != (c.ci95_lo, c.ci95_hi)


def test_bootstrap_resamples_groups_not_rows() -> None:
    # One group with huge diff, many with tiny: row-level resampling would
    # frequently exclude the big group's rows individually; group-level
    # resampling either includes ALL its rows or none. With 2 rows per group
    # and within-group-identical diffs, every bootstrap mean must be an
    # average of group means -- i.e. a multiple of 0.5*(count combinations).
    diff = np.array([10.0, 10.0, 0.0, 0.0, 0.0, 0.0])
    groups = np.array(["big", "big", "s1", "s1", "s2", "s2"])
    cmp_ = grouped_bootstrap_diff(diff, groups, metric="brier", n_boot=500, seed=5)
    # attainable bootstrap means: (10k)/3 for k in {0..3} -> {0, 3.33, 6.67, 10}
    assert cmp_.mean_diff == pytest.approx(10.0 / 3)
    attainable = [0.0, 10.0 / 3, 20.0 / 3, 10.0]
    assert any(cmp_.ci95_lo == pytest.approx(v) for v in attainable)
    assert any(cmp_.ci95_hi == pytest.approx(v) for v in attainable)


def test_matches_frozen_h0018_bootstrap_on_brier() -> None:
    """The generic comparison must reproduce the frozen baseline function
    exactly for the Brier case (same resampling scheme, same seed)."""
    rng = np.random.default_rng(2026)
    y = (rng.random(80) < 0.5).astype(float)
    pm, pb = rng.random(80), rng.random(80)
    g = np.repeat(np.arange(20), 4)
    frozen = grouped_bootstrap_brier_diff(pm, pb, y, g, n_boot=300, seed=99)
    generic = compare_probabilities(pm, pb, y, g, metric="brier", n_boot=300, seed=99)
    assert generic.mean_diff == pytest.approx(frozen["mean_brier_diff"])
    assert generic.ci95_lo == pytest.approx(frozen["ci95_lo"])
    assert generic.ci95_hi == pytest.approx(frozen["ci95_hi"])


def test_invalid_inputs_raise() -> None:
    with pytest.raises(BenchmarkError):
        per_row_losses(np.array([0.5]), np.array([1.0, 0.0]), metric="brier")
    with pytest.raises(BenchmarkError):
        grouped_bootstrap_diff(np.array([]), np.array([]), metric="brier", seed=1)
    with pytest.raises(BenchmarkError):
        grouped_bootstrap_diff(np.array([1.0]), np.array(["a"]), metric="brier", n_boot=0, seed=1)
