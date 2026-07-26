"""Provenance-aware inclusion policy for the canonical research dataset.

The environment cutover (ADR 0014) means historical Kalshi rows are a mixture of
``production``, ``demo``, ``unknown``, and pre-provenance ``NULL``. Demo and
unknown/NULL *liquidity* (volume, open interest, order-book depth, spread, trade
activity) is not real production liquidity and must never silently enter a
modeling dataset (ADR 0013). This module is the single place that decides, per
row, whether a liquidity feature is admissible, and classifies each row's
provenance for transparent reporting.

Two rules matter:

1. **Liquidity is production-only.** Volume/OI/order-book/trade features are kept
   only for ``environment='production'`` rows. Everything else is treated as
   *missing* -- never zero, never reinterpreted.

2. **Candlestick prices are deterministically production even when NULL.** The
   sole writer of ``market_candlesticks`` is the price-sync backfill, whose
   client has only ever read production (hard-coded before the cutover, resolved
   to production after -- ADR 0002/0014). No code path has ever written a demo
   candlestick. A pre-provenance (NULL) candlestick is therefore *deterministic*
   production, not a heuristic guess, and its price is admissible.

No source row is mutated. Classification lives only in the dataset-building
layer.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

#: Canonical provenance classes surfaced in the dataset (distinct from the raw
#: ``environment`` string, which does not encode the deterministic candlestick
#: rule).
VERIFIED_PRODUCTION = "verified_production"
VERIFIED_DEMO = "verified_demo"
HISTORICAL_UNKNOWN = "historical_unknown"

#: Tables whose sole writer is the always-production price-sync path, so a NULL
#: (pre-provenance) row is deterministically production. Keep this list tight:
#: adding a table here is a claim that NO code path ever wrote it from demo.
_DETERMINISTIC_PRODUCTION_TABLES = frozenset({"market_candlesticks"})


@dataclass(frozen=True, slots=True)
class EnvironmentPolicy:
    """How the builder treats each environment for each data category.

    Defaults implement the ADR 0014 canonical policy: liquidity from verified
    production only; prices from production including deterministically-production
    NULL candlesticks; results/identity environment-invariant (ADR 0013)."""

    #: Environments admissible for liquidity-derived features. Production only.
    liquidity_environments: frozenset[str] = frozenset({"production"})
    #: Whether NULL candlesticks are admitted as production prices (see module
    #: docstring -- deterministic, not heuristic).
    price_includes_deterministic_null_candles: bool = True
    #: Settlement results/identity are environment-invariant (ADR 0013), so no
    #: environment restriction is applied to labels. Recorded for the manifest.
    labels_are_environment_invariant: bool = True

    def to_manifest(self) -> dict[str, object]:
        return {
            "liquidity_environments": sorted(self.liquidity_environments),
            "price_includes_deterministic_null_candles": (
                self.price_includes_deterministic_null_candles
            ),
            "labels_are_environment_invariant": self.labels_are_environment_invariant,
            "policy_reference": "docs/adr/0014-production-data-collection-cutover.md",
        }


def classify_provenance(environment: str | None, *, table: str) -> str:
    """Map a row's raw ``environment`` to a canonical provenance class.

    ``production`` -> verified_production; ``demo`` -> verified_demo; ``NULL`` on
    a deterministically-production table -> verified_production; everything else
    (``unknown``, and ``NULL`` on a mixed-writer table) -> historical_unknown.
    Never guesses from liquidity signatures or timestamp proximity.
    """
    if environment == "production":
        return VERIFIED_PRODUCTION
    if environment == "demo":
        return VERIFIED_DEMO
    if environment is None and table in _DETERMINISTIC_PRODUCTION_TABLES:
        return VERIFIED_PRODUCTION
    return HISTORICAL_UNKNOWN


def liquidity_admissible(environment: str | None, policy: EnvironmentPolicy) -> bool:
    """True iff a row's liquidity fields may be used. Only true environments in
    the policy qualify -- NULL/unknown never carry admissible liquidity, even on
    a deterministic-production table (a NULL candle's *price* is admissible, its
    *volume* is not: the price is immutable per elapsed period, but demo vs
    production volume genuinely differ and a NULL row's volume is unattributable)."""
    return environment in policy.liquidity_environments


def price_admissible(environment: str | None, policy: EnvironmentPolicy, *, table: str) -> bool:
    """True iff a row's price fields may be used. Production always; NULL on a
    deterministic-production table when the policy allows it."""
    if environment in policy.liquidity_environments:
        return True
    return (
        policy.price_includes_deterministic_null_candles
        and environment is None
        and table in _DETERMINISTIC_PRODUCTION_TABLES
    )


@dataclass(slots=True)
class ProvenanceCoverage:
    """Accumulates per-table environment counts and exclusion reasons for the
    coverage report. Purely descriptive -- it never drops or alters rows."""

    by_environment: dict[str, Counter[str]] = field(default_factory=dict)
    by_provenance: dict[str, Counter[str]] = field(default_factory=dict)
    liquidity_excluded: Counter[str] = field(default_factory=Counter)

    def record(self, table: str, environment: str | None, policy: EnvironmentPolicy) -> None:
        env_key = environment if environment is not None else "NULL"
        self.by_environment.setdefault(table, Counter())[env_key] += 1
        self.by_provenance.setdefault(table, Counter())[
            classify_provenance(environment, table=table)
        ] += 1
        if not liquidity_admissible(environment, policy):
            self.liquidity_excluded[table] += 1

    def to_report(self) -> dict[str, object]:
        return {
            "by_environment": {t: dict(c) for t, c in sorted(self.by_environment.items())},
            "by_provenance": {t: dict(c) for t, c in sorted(self.by_provenance.items())},
            "liquidity_rows_excluded_non_production": dict(self.liquidity_excluded),
        }
