"""Kalshi trading-fee calculation for H0007
(docs/research/experiments/AMENDMENT-20260721-H0007-pre-execution.md,
Finding 5; verified in
docs/research/experiments/AMENDMENT-20260721-H0007-fee-verified.md).

The formula's mathematical STRUCTURE below (ceil(multiplier * P * (1-P) *
contracts) cents, rounded up) matches Kalshi's own documented `fee_type:
"quadratic"` shape. As of 2026-07-21 the CONSTANT is verified: Kalshi's
official fee schedule PDF ("Last updated and effective: July 7, 2026"; sha256
815e2d5127d02d2fb90773d1a3844dc15a987696171eddc4e58de87b59c6124c, archived at
docs/research/experiments/kalshi-fee-schedule-2026-07-07.pdf) states, verbatim
(General Trading Fees Table): ``fees = round_up(M x 0.07 x C x P x (1-P))``
for general (taker) fills, with default per-series multiplier M=1 unless the
series appears in the document's "Non-Standard Fees" table -- neither
KXHIGHNY nor KXLOWTNYC (nor any weather/climate series) appears there, and
the live `GET /series` `fee_multiplier: 1` field independently confirms the
same default applies. `KALSHI_WEATHER_TAKER_FEE_CONFIG` below is that
verified, frozen configuration -- see the amendment for the full
cross-verification (every row of the PDF's own worked-example table,
21 price points x 2 contract counts = 42 values, matches this formula
exactly).

`contract_fee_cents` refuses to run against an unverified `FeeConfig` --
kept as a structural safeguard even though a verified config now exists, so
a caller can never accidentally compute against a config that was never
checked.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal


@dataclass(frozen=True, slots=True)
class FeeConfig:
    """A fee-schedule constant plus its provenance. `verified=True` may only
    be set once the constant has been confirmed against Kalshi's own
    primary/official fee schedule (not a secondary summary) -- see the
    amendment's Finding 5 for the required provenance fields."""

    multiplier: Decimal
    source: str
    verified: bool
    retrieved_at: str | None = None


#: Frozen, verified configuration for H0007's design: a taker fill (buying
#: the favored side at its ask -- AMENDMENT-20260721-H0007-pre-execution.md
#: Sec 8) on a weather-category market, where the general (non-maker)
#: formula and the default per-series multiplier M=1 both apply -- see the
#: module docstring and AMENDMENT-20260721-H0007-fee-verified.md for the
#: full verification. Maker fees (a separate 0.0175 formula, default
#: multiplier M=0 for weather markets per the same PDF) are not relevant to
#: this design, which never rests as a maker order.
KALSHI_WEATHER_TAKER_FEE_CONFIG = FeeConfig(
    multiplier=Decimal("0.07"),
    source=(
        "Kalshi official fee schedule PDF, 'Last updated and effective: "
        "July 7, 2026', General Trading Fees Table (page 2: formula; "
        "pages 4-5: worked-example table, cross-verified exactly): "
        "fees = round_up(M x 0.07 x C x P x (1-P)), M=1 default "
        "(KXHIGHNY/KXLOWTNYC absent from the Non-Standard Fees table, "
        "pages 6-11) -- corroborated by live GET /series fee_multiplier=1 "
        "for both series. sha256 "
        "815e2d5127d02d2fb90773d1a3844dc15a987696171eddc4e58de87b59c6124c "
        "(docs/research/experiments/kalshi-fee-schedule-2026-07-07.pdf). "
        "See docs/research/experiments/AMENDMENT-20260721-H0007-fee-verified.md."
    ),
    verified=True,
    retrieved_at="2026-07-21",
)


def contract_fee_cents(price_cents: int, *, contracts: int, config: FeeConfig) -> int:
    """Kalshi's documented per-contract fee shape: ceil(multiplier * P *
    (1-P) * contracts) cents, where P = price_cents/100. Rounds UP to the
    next whole cent (every source consulted agrees fees round up, never
    down). `price_cents` must be in Kalshi's tradeable range (1-99)."""
    if not config.verified:
        raise ValueError(
            "fee config is not verified against Kalshi's official schedule -- "
            "use KALSHI_WEATHER_TAKER_FEE_CONFIG (see "
            "AMENDMENT-20260721-H0007-fee-verified.md) or verify a new one "
            "the same way. H0007 must not execute against an unverified "
            "fee config."
        )
    if not (1 <= price_cents <= 99):
        raise ValueError(f"price_cents must be 1-99 (Kalshi's tradeable range), got {price_cents}")
    if contracts < 1:
        raise ValueError(f"contracts must be >= 1, got {contracts}")
    p = Decimal(price_cents) / Decimal(100)
    raw_cents = config.multiplier * p * (1 - p) * Decimal(100) * Decimal(contracts)
    return int(raw_cents.to_integral_value(rounding=ROUND_CEILING))
