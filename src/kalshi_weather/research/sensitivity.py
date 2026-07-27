"""Sensitivity analysis: how fragile is a result to its incidental choices?

A preregistered experiment freezes parameters that are *choices* rather than
hypotheses (probability clip bounds, regularization strength, horizon,
bootstrap size). A result that flips sign when such a choice wiggles is
fragile, and that fragility must be reported alongside the primary result --
never used to replace it.

This module runs the caller's evaluation once at the frozen base parameters
and once per single-parameter perturbation. It never searches, never
optimizes, and never mutates the base: the primary result is whatever the
base run produced, and the report says so explicitly. Perturbing more than
one parameter at a time is deliberately unsupported -- a grid search in
disguise is still a search.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass


class SensitivityError(ValueError):
    """Invalid sensitivity specification."""


@dataclass(frozen=True)
class PerturbationResult:
    """One single-parameter perturbation and its metric deltas vs base."""

    param: str
    value: object
    metrics: Mapping[str, float]
    deltas_vs_base: Mapping[str, float]


@dataclass(frozen=True)
class SensitivityReport:
    """Base metrics (the primary result -- unchanged) plus all perturbations."""

    base_params: Mapping[str, object]
    base_metrics: Mapping[str, float]
    perturbations: tuple[PerturbationResult, ...]


def sensitivity_report(
    *,
    base_params: Mapping[str, object],
    perturbations: Mapping[str, Sequence[object]],
    evaluate: Callable[[Mapping[str, object]], Mapping[str, float]],
) -> SensitivityReport:
    """Evaluate base once, then each single-parameter perturbation.

    ``evaluate(params)`` must be deterministic (any seed lives inside
    ``params``). Perturbed values equal to the base value are rejected --
    they would double-count the base run as evidence of stability.
    """
    for param, values in perturbations.items():
        if param not in base_params:
            raise SensitivityError(f"perturbed param {param!r} not in base_params")
        if not values:
            raise SensitivityError(f"param {param!r} has no perturbation values")
        for v in values:
            if v == base_params[param]:
                raise SensitivityError(f"perturbation for {param!r} repeats the base value {v!r}")

    base_metrics = {k: float(v) for k, v in evaluate(dict(base_params)).items()}
    results: list[PerturbationResult] = []
    for param in sorted(perturbations):  # deterministic order
        for value in perturbations[param]:
            trial = dict(base_params)
            trial[param] = value
            metrics = {k: float(v) for k, v in evaluate(trial).items()}
            missing = set(base_metrics) - set(metrics)
            if missing:
                raise SensitivityError(f"perturbed run omitted base metrics {sorted(missing)}")
            deltas = {k: metrics[k] - base_metrics[k] for k in base_metrics}
            results.append(
                PerturbationResult(param=param, value=value, metrics=metrics, deltas_vs_base=deltas)
            )
    return SensitivityReport(
        base_params=dict(base_params),
        base_metrics=base_metrics,
        perturbations=tuple(results),
    )
