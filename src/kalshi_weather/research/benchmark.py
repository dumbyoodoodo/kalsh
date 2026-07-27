"""Benchmark comparison with grouped-bootstrap confidence intervals.

Generalizes the pattern proven in ``experiments/baseline.py``'s
``grouped_bootstrap_brier_diff`` (which stays untouched -- it belongs to
already-registered frozen experiment specs) to any per-row paired loss:
resample *groups*, not
rows, so correlated rows (e.g. threshold markets on the same event-day) are
never treated as independent evidence.

Sign convention throughout: ``diff = loss_model - loss_benchmark`` per row,
so negative means the model improves on the benchmark. A comparison "excludes
zero" only when the whole 95% CI is on one side -- the caller's preregistered
success rule decides what that means; nothing here declares an edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from kalshi_weather.experiments.baseline import clip_prob

Metric = Literal["brier", "log_loss"]


class BenchmarkError(ValueError):
    """Invalid inputs to a benchmark comparison."""


@dataclass(frozen=True)
class BenchmarkComparison:
    """Point estimate and grouped-bootstrap 95% CI for mean per-row loss diff."""

    metric: str
    mean_diff: float
    ci95_lo: float
    ci95_hi: float
    n_rows: int
    n_groups: int
    n_boot: int
    seed: int

    @property
    def excludes_zero(self) -> bool:
        return self.ci95_hi < 0.0 or self.ci95_lo > 0.0


def per_row_losses(p: np.ndarray, y: np.ndarray, *, metric: Metric) -> np.ndarray:
    """Per-row loss vector for a probability forecast against binary labels."""
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    if p.shape != y.shape:
        raise BenchmarkError(f"shape mismatch: p{p.shape} vs y{y.shape}")
    if metric == "brier":
        return np.asarray((p - y) ** 2, dtype=float)
    pc = clip_prob(p)  # log loss needs clipping for finiteness; same clip as baseline
    return np.asarray(-(y * np.log(pc) + (1.0 - y) * np.log(1.0 - pc)), dtype=float)


def grouped_bootstrap_diff(
    diff: np.ndarray,
    groups: np.ndarray,
    *,
    metric: str,
    n_boot: int = 2000,
    seed: int,
) -> BenchmarkComparison:
    """Bootstrap the mean of ``diff`` by resampling groups with replacement.

    Deterministic given ``seed`` (explicit by design -- no default, so a seed
    is always a recorded, reproducible choice).
    """
    diff = np.asarray(diff, dtype=float)
    groups = np.asarray(groups)
    if diff.shape != groups.shape:
        raise BenchmarkError(f"shape mismatch: diff{diff.shape} vs groups{groups.shape}")
    if diff.size == 0:
        raise BenchmarkError("cannot compare on zero rows")
    if n_boot < 1:
        raise BenchmarkError("n_boot must be >= 1")

    uniq = np.unique(groups)
    group_rows = {g: np.where(groups == g)[0] for g in uniq}
    g_arr = np.array(list(group_rows.keys()), dtype=object)
    rng = np.random.default_rng(seed)
    boots = np.empty(n_boot)
    for i in range(n_boot):
        pick = rng.integers(0, len(g_arr), size=len(g_arr))
        rows = np.concatenate([group_rows[g_arr[j]] for j in pick])
        boots[i] = diff[rows].mean()
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return BenchmarkComparison(
        metric=metric,
        mean_diff=float(diff.mean()),
        ci95_lo=float(lo),
        ci95_hi=float(hi),
        n_rows=diff.size,
        n_groups=len(uniq),
        n_boot=n_boot,
        seed=seed,
    )


def compare_probabilities(
    p_model: np.ndarray,
    p_benchmark: np.ndarray,
    y: np.ndarray,
    groups: np.ndarray,
    *,
    metric: Metric = "brier",
    n_boot: int = 2000,
    seed: int,
) -> BenchmarkComparison:
    """Paired model-vs-benchmark comparison on identical rows.

    Both forecasts are scored on exactly the same rows and labels; the
    per-row loss difference is then bootstrapped at the group level.
    """
    diff = per_row_losses(p_model, y, metric=metric) - per_row_losses(p_benchmark, y, metric=metric)
    return grouped_bootstrap_diff(diff, groups, metric=metric, n_boot=n_boot, seed=seed)
