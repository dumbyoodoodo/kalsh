"""Kalshi trading-fee calculation for H0007
(docs/research/experiments/AMENDMENT-20260721-H0007-pre-execution.md,
Finding 5).

The formula's mathematical STRUCTURE below (ceil(multiplier * P * (1-P) *
contracts) cents, rounded up) matches Kalshi's publicly documented
percentage-of-p(1-p) fee shape, corroborated by multiple independent
secondary sources during the amendment's fee-verification attempt. The
CONSTANT is deliberately **not** hardcoded as a verified production value:
Kalshi's own primary fee-schedule documents (kalshi.com/docs/kalshi-fee-
schedule.pdf, kalshi.com/fee-schedule) were unreachable from this
environment (HTTP 429 / bot checkpoint on every attempt -- the same class of
failure this project already documented for docs.kalshi.com in
docs/API_VERIFICATION.md), and Kalshi's own help center states "some markets
have fees that are different from those of other markets" -- so a single
constant cannot be assumed uniform across the weather category without the
primary schedule confirming it. See the amendment for the full attempt log.

`contract_fee_cents` refuses to run against an unverified `FeeConfig` --
this is the enforcement mechanism that keeps H0007 BLOCKED on this step
rather than silently proceeding on a guessed number.
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


def contract_fee_cents(price_cents: int, *, contracts: int, config: FeeConfig) -> int:
    """Kalshi's documented per-contract fee shape: ceil(multiplier * P *
    (1-P) * contracts) cents, where P = price_cents/100. Rounds UP to the
    next whole cent (every source consulted agrees fees round up, never
    down). `price_cents` must be in Kalshi's tradeable range (1-99)."""
    if not config.verified:
        raise ValueError(
            "fee config is not verified against Kalshi's official schedule -- "
            "see AMENDMENT-20260721-H0007-pre-execution.md Finding 5. "
            "H0007 must not execute against an unverified fee config."
        )
    if not (1 <= price_cents <= 99):
        raise ValueError(f"price_cents must be 1-99 (Kalshi's tradeable range), got {price_cents}")
    if contracts < 1:
        raise ValueError(f"contracts must be >= 1, got {contracts}")
    p = Decimal(price_cents) / Decimal(100)
    raw_cents = config.multiplier * p * (1 - p) * Decimal(100) * Decimal(contracts)
    return int(raw_cents.to_integral_value(rounding=ROUND_CEILING))
