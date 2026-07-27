"""Feature-ablation reports: how much does each *declared* feature family
contribute, measured by refitting without it?

The workbench does not fit models itself -- the caller supplies a
``fit_and_eval`` callable that trains on train data and returns a scalar
validation metric for a given feature list. That keeps this module free of
any model class, and keeps ablation honest: the exact same fitting procedure
runs with and without a family, differing only in the feature list.

Ablation is a **train/validation diagnostic only**. Running it on test data
would be adaptive test reuse; the callable the caller passes must not touch
a held-out test set, and the report records that contract explicitly.

Feature families are declared up front (no hidden feature selection): every
feature must belong to exactly one family, and the union of families must be
exactly the feature list -- anything else raises.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass


class AblationError(ValueError):
    """Invalid ablation specification."""


@dataclass(frozen=True)
class FamilyAblation:
    """One family's ablation: metric with all features vs without the family."""

    family: str
    features_removed: tuple[str, ...]
    metric_full: float
    metric_ablated: float

    @property
    def delta(self) -> float:
        """ablated - full; for a loss metric, positive means the family helps."""
        return self.metric_ablated - self.metric_full


@dataclass(frozen=True)
class AblationReport:
    """Full drop-one-family report, deterministic family order."""

    metric_name: str
    metric_full: float
    families: tuple[FamilyAblation, ...]
    evaluated_on: str  # recorded caller contract, e.g. "validation"


def ablation_report(
    *,
    all_features: Sequence[str],
    feature_families: Mapping[str, Sequence[str]],
    fit_and_eval: Callable[[Sequence[str]], float],
    metric_name: str,
    evaluated_on: str = "validation",
) -> AblationReport:
    """Drop each declared family in turn and re-evaluate.

    ``fit_and_eval(features)`` must be deterministic (fixed seed inside the
    caller) -- the full-feature run is evaluated once and reused as the
    baseline for every family's delta.
    """
    if "test" in evaluated_on.lower():
        raise AblationError("ablation must not be evaluated on a held-out test set")
    features = list(all_features)
    if len(set(features)) != len(features):
        raise AblationError("duplicate feature names")

    covered: set[str] = set()
    for family, members in feature_families.items():
        member_set = set(members)
        if not member_set:
            raise AblationError(f"family {family!r} is empty")
        unknown = member_set - set(features)
        if unknown:
            raise AblationError(f"family {family!r} references unknown features {sorted(unknown)}")
        overlap = member_set & covered
        if overlap:
            raise AblationError(f"features {sorted(overlap)} belong to more than one family")
        covered |= member_set
    uncovered = set(features) - covered
    if uncovered:
        raise AblationError(f"features {sorted(uncovered)} belong to no declared family")

    metric_full = float(fit_and_eval(tuple(features)))
    results: list[FamilyAblation] = []
    for family in sorted(feature_families):  # deterministic order
        removed = tuple(f for f in features if f in set(feature_families[family]))
        kept = tuple(f for f in features if f not in set(removed))
        if not kept:
            raise AblationError(f"removing family {family!r} would leave zero features")
        results.append(
            FamilyAblation(
                family=family,
                features_removed=removed,
                metric_full=metric_full,
                metric_ablated=float(fit_and_eval(kept)),
            )
        )
    return AblationReport(
        metric_name=metric_name,
        metric_full=metric_full,
        families=tuple(results),
        evaluated_on=evaluated_on,
    )
