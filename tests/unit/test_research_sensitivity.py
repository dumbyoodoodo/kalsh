"""Sensitivity reports: base result untouched, single-parameter perturbations
only, deterministic ordering, and specification hygiene."""

from collections.abc import Mapping

import pytest

from kalshi_weather.research.sensitivity import SensitivityError, sensitivity_report

BASE = {"l2": 1.0, "clip": 0.02, "seed": 7}


def evaluate(params: Mapping[str, object]) -> dict[str, float]:
    # Synthetic deterministic "experiment": loss depends on l2 and clip only.
    l2 = float(params["l2"])  # type: ignore[arg-type]
    clip = float(params["clip"])  # type: ignore[arg-type]
    return {"brier": 0.20 + 0.01 * l2 + 0.1 * clip}


def test_base_metrics_are_primary_and_unchanged() -> None:
    report = sensitivity_report(
        base_params=BASE, perturbations={"l2": [0.5, 2.0]}, evaluate=evaluate
    )
    assert report.base_params == BASE
    assert report.base_metrics["brier"] == pytest.approx(0.212)
    # perturbations report deltas without altering the base
    deltas = {(p.param, p.value): p.deltas_vs_base["brier"] for p in report.perturbations}
    assert deltas[("l2", 0.5)] == pytest.approx(-0.005)
    assert deltas[("l2", 2.0)] == pytest.approx(0.01)


def test_single_parameter_at_a_time_and_deterministic_order() -> None:
    report = sensitivity_report(
        base_params=BASE,
        perturbations={"clip": [0.05], "l2": [2.0]},
        evaluate=evaluate,
    )
    assert [p.param for p in report.perturbations] == ["clip", "l2"]  # sorted
    for p in report.perturbations:
        # each trial changed exactly the named parameter
        assert p.metrics["brier"] != report.base_metrics["brier"]
    again = sensitivity_report(
        base_params=BASE, perturbations={"clip": [0.05], "l2": [2.0]}, evaluate=evaluate
    )
    assert report == again


def test_rejects_unknown_param_empty_values_and_base_repeat() -> None:
    with pytest.raises(SensitivityError, match="not in base_params"):
        sensitivity_report(base_params=BASE, perturbations={"ghost": [1]}, evaluate=evaluate)
    with pytest.raises(SensitivityError, match="no perturbation values"):
        sensitivity_report(base_params=BASE, perturbations={"l2": []}, evaluate=evaluate)
    with pytest.raises(SensitivityError, match="repeats the base value"):
        sensitivity_report(base_params=BASE, perturbations={"l2": [1.0]}, evaluate=evaluate)


def test_rejects_perturbed_run_that_drops_a_metric() -> None:
    calls = {"n": 0}

    def flaky(params: Mapping[str, object]) -> dict[str, float]:
        calls["n"] += 1
        return {"brier": 0.2} if calls["n"] == 1 else {}

    with pytest.raises(SensitivityError, match="omitted base metrics"):
        sensitivity_report(base_params=BASE, perturbations={"l2": [2.0]}, evaluate=flaky)
