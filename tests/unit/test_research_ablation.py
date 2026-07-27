"""Feature ablation: known contribution recovery, declared-family hygiene,
determinism, and the no-test-set contract."""

from collections.abc import Sequence

import pytest

from kalshi_weather.research.ablation import AblationError, ablation_report

FEATURES = ["f_signal", "f_noise1", "f_noise2"]
FAMILIES = {"signal": ["f_signal"], "noise": ["f_noise1", "f_noise2"]}

#: Synthetic "validation loss": removing the signal feature costs 0.10;
#: removing noise features costs nothing. Deterministic by construction.
def fake_fit_and_eval(features: Sequence[str]) -> float:
    return 0.20 if "f_signal" in features else 0.30


def test_recovers_known_family_contributions() -> None:
    report = ablation_report(
        all_features=FEATURES,
        feature_families=FAMILIES,
        fit_and_eval=fake_fit_and_eval,
        metric_name="brier",
    )
    by_family = {f.family: f for f in report.families}
    assert by_family["signal"].delta == pytest.approx(0.10)  # removing signal hurts
    assert by_family["noise"].delta == pytest.approx(0.0)  # removing noise is free
    assert report.metric_full == pytest.approx(0.20)
    assert by_family["signal"].features_removed == ("f_signal",)


def test_family_order_and_output_deterministic() -> None:
    a = ablation_report(
        all_features=FEATURES,
        feature_families=FAMILIES,
        fit_and_eval=fake_fit_and_eval,
        metric_name="brier",
    )
    b = ablation_report(
        all_features=FEATURES,
        feature_families=FAMILIES,
        fit_and_eval=fake_fit_and_eval,
        metric_name="brier",
    )
    assert [f.family for f in a.families] == sorted(FAMILIES)
    assert a == b


def test_rejects_evaluation_on_test_set() -> None:
    with pytest.raises(AblationError, match="test"):
        ablation_report(
            all_features=FEATURES,
            feature_families=FAMILIES,
            fit_and_eval=fake_fit_and_eval,
            metric_name="brier",
            evaluated_on="TEST split",
        )


def test_rejects_hidden_or_overlapping_feature_selection() -> None:
    # feature in no family (hidden selection)
    with pytest.raises(AblationError, match="no declared family"):
        ablation_report(
            all_features=FEATURES,
            feature_families={"signal": ["f_signal"]},
            fit_and_eval=fake_fit_and_eval,
            metric_name="brier",
        )
    # feature in two families
    with pytest.raises(AblationError, match="more than one family"):
        ablation_report(
            all_features=FEATURES,
            feature_families={
                "signal": ["f_signal"],
                "noise": ["f_noise1", "f_noise2", "f_signal"],
            },
            fit_and_eval=fake_fit_and_eval,
            metric_name="brier",
        )
    # unknown feature named
    with pytest.raises(AblationError, match="unknown features"):
        ablation_report(
            all_features=FEATURES,
            feature_families={"signal": ["f_signal", "ghost"], "noise": ["f_noise1", "f_noise2"]},
            fit_and_eval=fake_fit_and_eval,
            metric_name="brier",
        )
    # ablating the only family would leave nothing to fit
    with pytest.raises(AblationError, match="zero features"):
        ablation_report(
            all_features=["f_signal"],
            feature_families={"signal": ["f_signal"]},
            fit_and_eval=fake_fit_and_eval,
            metric_name="brier",
        )
