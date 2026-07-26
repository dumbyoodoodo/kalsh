"""Numpy-only baseline models, probability metrics, and grouped bootstrap
for the H0018 weather-vs-market experiment.

Deliberately dependency-light (no scikit-learn): an L2-regularized logistic
regression fit by IRLS, Platt (logistic) calibration, and the standard
probability-forecast metrics (Brier, log loss, calibration slope/intercept,
ECE). All functions are pure and deterministic given their inputs, so the
experiment reproduces exactly from a fixed seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

#: Fixed probability clip for numerical stability (pre-registered, H0018).
CLIP_LO = 0.02
CLIP_HI = 0.98


def clip_prob(p: np.ndarray) -> np.ndarray:
    return np.clip(p, CLIP_LO, CLIP_HI)  # type: ignore[no-any-return]


def brier(p: np.ndarray, y: np.ndarray) -> float:
    """Mean squared error of probabilistic forecasts -- the primary metric."""
    return float(np.mean((p - y) ** 2))


def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    p = clip_prob(p)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))  # type: ignore[no-any-return]


@dataclass(frozen=True)
class LogisticModel:
    """A fitted logistic regression. ``mu``/``sigma`` standardize inputs the
    same way at predict time as at fit time (recorded so prediction is a pure
    function of the stored parameters)."""

    coef: np.ndarray  # includes intercept as coef[0]
    mu: np.ndarray
    sigma: np.ndarray

    def predict(self, x: np.ndarray) -> np.ndarray:
        xs = (x - self.mu) / self.sigma
        xb = np.hstack([np.ones((xs.shape[0], 1)), xs])
        return _sigmoid(xb @ self.coef)


def fit_logistic(
    x: np.ndarray, y: np.ndarray, *, l2: float = 1.0, iters: int = 100
) -> LogisticModel:
    """L2-regularized logistic regression by IRLS (Newton). Deterministic.

    Features are standardized (intercept unpenalized). ``l2`` is the ridge
    strength on the standardized coefficients. Converges in a few iterations for
    these small, well-conditioned designs."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mu = x.mean(axis=0)
    sigma = x.std(axis=0)
    sigma[sigma == 0] = 1.0
    xs = (x - mu) / sigma
    n, k = xs.shape
    xb = np.hstack([np.ones((n, 1)), xs])
    beta = np.zeros(k + 1)
    # ridge penalty matrix: do not penalize the intercept
    pen = l2 * np.eye(k + 1)
    pen[0, 0] = 0.0
    for _ in range(iters):
        eta = xb @ beta
        p = _sigmoid(eta)
        w = np.clip(p * (1 - p), 1e-6, None)
        # Newton step: beta += (XᵀWX + pen)⁻¹ (Xᵀ(y-p) - pen·beta)
        grad = xb.T @ (y - p) - pen @ beta
        hess = xb.T @ (xb * w[:, None]) + pen
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        beta = beta + step
        if np.max(np.abs(step)) < 1e-8:
            break
    return LogisticModel(coef=beta, mu=mu, sigma=sigma)


def fit_platt(scores: np.ndarray, y: np.ndarray, *, l2: float = 1e-6) -> LogisticModel:
    """Platt scaling: a 1-D logistic on the (log-odds of the) raw probability.
    Fit on train+validation only (never test). Returns a LogisticModel over the
    single logit feature."""
    s = clip_prob(np.asarray(scores, dtype=float))
    logit = np.log(s / (1 - s)).reshape(-1, 1)
    return fit_logistic(logit, y, l2=l2)


def apply_platt(model: LogisticModel, scores: np.ndarray) -> np.ndarray:
    s = clip_prob(np.asarray(scores, dtype=float))
    logit = np.log(s / (1 - s)).reshape(-1, 1)
    return model.predict(logit)


def calibration_slope_intercept(p: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Regress y on the logit of p (a 1-D logistic). Slope 1 / intercept 0 =
    perfectly calibrated; slope < 1 = over-confident."""
    m = fit_platt(p, y, l2=0.0)
    # coef[0] = intercept, coef[1] = slope on the standardized logit; de-standardize
    slope = float(m.coef[1] / m.sigma[0])
    intercept = float(m.coef[0] - m.coef[1] * m.mu[0] / m.sigma[0])
    return slope, intercept


def expected_calibration_error(p: np.ndarray, y: np.ndarray, *, bins: int = 10) -> float:
    """ECE: |mean predicted - observed rate| per equal-width bin, weighted by
    bin population."""
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    ece = 0.0
    n = len(p)
    for b in range(bins):
        mask = idx == b
        if not mask.any():
            continue
        ece += (mask.sum() / n) * abs(p[mask].mean() - y[mask].mean())
    return float(ece)


def reliability_table(p: np.ndarray, y: np.ndarray, *, bins: int = 10) -> list[dict[str, Any]]:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    out = []
    for b in range(bins):
        mask = idx == b
        if not mask.any():
            continue
        out.append(
            {
                "bin_lo": float(edges[b]),
                "bin_hi": float(edges[b + 1]),
                "n": int(mask.sum()),
                "mean_pred": float(p[mask].mean()),
                "obs_rate": float(y[mask].mean()),
            }
        )
    return out


def grouped_bootstrap_brier_diff(
    p_model: np.ndarray,
    p_bench: np.ndarray,
    y: np.ndarray,
    groups: np.ndarray,
    *,
    n_boot: int = 2000,
    seed: int = 20260726,
) -> dict[str, Any]:
    """Bootstrap the mean per-row Brier difference (model - benchmark) by
    RESAMPLING GROUPS (event = station x target_date), so correlated threshold
    markets are not treated as independent. Returns point estimate and 95%
    percentile CI. Deterministic given the seed."""
    p_model, p_bench, y = map(lambda a: np.asarray(a, float), (p_model, p_bench, y))
    diff = (p_model - y) ** 2 - (p_bench - y) ** 2  # per-row Brier diff
    uniq = np.unique(groups)
    # index rows per group once
    group_rows = {g: np.where(groups == g)[0] for g in uniq}
    rng = np.random.default_rng(seed)
    point = float(diff.mean())
    boots = np.empty(n_boot)
    g_arr = np.array(list(group_rows.keys()), dtype=object)
    for i in range(n_boot):
        pick = rng.integers(0, len(g_arr), size=len(g_arr))
        rows = np.concatenate([group_rows[g_arr[j]] for j in pick])
        boots[i] = diff[rows].mean()
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {
        "mean_brier_diff": point,
        "ci95_lo": float(lo),
        "ci95_hi": float(hi),
        "n_rows": len(diff),
        "n_groups": len(uniq),
        "n_boot": n_boot,
        "seed": seed,
        "fraction_boot_negative": float(np.mean(boots < 0)),
    }
