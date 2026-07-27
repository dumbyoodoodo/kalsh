"""Generic, hypothesis-agnostic research workbench.

Reusable evaluation machinery shared by *future* experiments: grouped
chronological splits and walk-forward folds, benchmark comparison with
grouped-bootstrap confidence intervals, feature-ablation and sensitivity
reports, an append-only research ledger, and a fee-aware translation from
probability forecasts to hypothetical trade decisions.

Design rules (per the workbench preregistration discipline):

- **No hypothesis coupling.** Nothing in this package imports from, reads,
  or references any specific experiment's frozen artifacts. Registered
  experiments' runners and frozen specs stay exactly as registered; this
  package generalizes their *patterns*, not their state (enforced by
  ``tests/unit/test_research_isolation.py``).
- **No current-time dependence.** Every function is pure and deterministic
  given its inputs and an explicit seed -- nothing reads the wall clock.
- **Group-aware by construction.** Splits, folds, and bootstraps operate on
  caller-declared group units (e.g. an event-day), never raw rows, so
  correlated rows can never straddle a train/test boundary.
- **No performance claims.** The translation layer emits hypothetical
  decisions for later, separately preregistered evaluation -- it never
  reports profitability.

Calibration diagnostics (Brier, log loss, ECE, reliability tables,
calibration slope/intercept) already exist as generic pure functions in
``experiments/baseline.py``; they are re-exported here unchanged rather
than duplicated.
"""

from kalshi_weather.experiments.baseline import (
    brier,
    calibration_slope_intercept,
    clip_prob,
    expected_calibration_error,
    log_loss,
    reliability_table,
)

__all__ = [
    "brier",
    "calibration_slope_intercept",
    "clip_prob",
    "expected_calibration_error",
    "log_loss",
    "reliability_table",
]
