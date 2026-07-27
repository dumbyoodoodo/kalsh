"""Fee-aware translation from a probability forecast to a hypothetical
trade decision.

This is the bridge's *mechanical* half only: given a model probability and a
market YES price, compute the after-fee edge on each side using the
authoritative fee model (``execution/fees.py``) and emit a single
hypothetical decision. It makes **no performance claim** -- decisions exist
to be evaluated later under a separately preregistered rule, never summed
into a P&L by this module.

All money is integer cents or ``Decimal`` (never binary float), per the
project's money rules. Edges are expressed in centicents (1/100 cent) so a
probability with more precision than a cent never silently rounds.

Every threshold here is **DRAFT**: ``DraftDecisionThresholds`` must be
constructed explicitly (no tradable defaults), carries a draft version tag,
and is recorded on every decision. Real trading thresholds require their own
preregistration; nothing downstream may treat these as validated.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from kalshi_weather.execution.fees import FeeModelLike
from kalshi_weather.execution.models import Action, Liquidity, Side

#: Centicents (1/100 cent) per whole contract payout dollar-side unit.
_CENTICENTS_PER_CENT = 100


class TranslationError(ValueError):
    """Invalid inputs to a probability->decision translation."""


class DecisionAction(StrEnum):
    BUY_YES = "buy_yes"
    BUY_NO = "buy_no"
    NO_TRADE = "no_trade"


@dataclass(frozen=True)
class DraftDecisionThresholds:
    """Draft-only decision gates. Explicit construction required; no tradable
    defaults exist on purpose."""

    min_net_edge_centicents: int
    version: str = "draft-unvalidated"

    def __post_init__(self) -> None:
        if self.min_net_edge_centicents < 0:
            raise TranslationError("min_net_edge_centicents must be >= 0")
        if "draft" not in self.version:
            raise TranslationError(
                "thresholds are draft-only until separately preregistered; "
                "version must contain 'draft'"
            )


@dataclass(frozen=True)
class HypotheticalDecision:
    """One fee-aware hypothetical decision. Not a performance statement."""

    action: DecisionAction
    ticker: str
    yes_price_cents: int
    model_probability: Decimal
    gross_edge_centicents: int  # best side's edge before fees
    fee_centicents: int  # taker fee on that side (0 when no side is viable)
    net_edge_centicents: int
    thresholds_version: str
    fee_model_version: str
    reason: str


def _require_price(price_cents: int) -> None:
    if not 1 <= price_cents <= 99:
        raise TranslationError(f"yes price {price_cents} outside [1, 99] cents")


def translate_probability(
    *,
    model_probability: Decimal,
    yes_price_cents: int,
    ticker: str,
    fee_model: FeeModelLike,
    thresholds: DraftDecisionThresholds,
    quantity: int = 1,
) -> HypotheticalDecision:
    """Compute the after-fee edge of buying YES vs buying NO and decide.

    Edge convention (per contract, in centicents): fair value of YES is
    ``p * 100`` cents; buying YES at price ``q`` has gross edge
    ``p*100 - q``; buying NO at ``100 - q`` has gross edge ``q - p*100``.
    Fees are assessed as a taker on the side actually bought, via the
    supplied fee model (amortized per contract when quantity > 1).
    """
    _require_price(yes_price_cents)
    if not Decimal(0) <= model_probability <= Decimal(1):
        raise TranslationError(f"model probability {model_probability} outside [0, 1]")
    if quantity < 1:
        raise TranslationError("quantity must be >= 1")

    fair_centicents = int(model_probability * 100 * _CENTICENTS_PER_CENT)
    yes_cost_centicents = yes_price_cents * _CENTICENTS_PER_CENT
    no_price_cents = 100 - yes_price_cents
    no_cost_centicents = no_price_cents * _CENTICENTS_PER_CENT

    gross_yes = fair_centicents - yes_cost_centicents
    gross_no = (100 * _CENTICENTS_PER_CENT - fair_centicents) - no_cost_centicents

    def taker_fee_centicents(side: Side, price_cents: int) -> int:
        assessment = fee_model.assess(
            price_cents=price_cents,
            quantity=quantity,
            side=side,
            action=Action.BUY,
            liquidity=Liquidity.TAKER,
            ticker=ticker,
            accumulator_centicents=0,
        )
        # Amortize the fill fee across contracts, rounding UP (conservative:
        # never understate cost per contract).
        total = assessment.breakdown.net_fee_centicents
        return -(-total // quantity)

    candidates: list[tuple[int, DecisionAction, Side, int, int]] = []
    for gross, action, side, price in (
        (gross_yes, DecisionAction.BUY_YES, Side.YES, yes_price_cents),
        (gross_no, DecisionAction.BUY_NO, Side.NO, no_price_cents),
    ):
        if gross <= 0:
            continue
        fee = taker_fee_centicents(side, price)
        candidates.append((gross - fee, action, side, gross, fee))

    if candidates:
        candidates.sort(key=lambda c: (-c[0], c[1].value))  # best net edge; stable tiebreak
        net, action, _side, gross, fee = candidates[0]
        if net >= thresholds.min_net_edge_centicents:
            return HypotheticalDecision(
                action=action,
                ticker=ticker,
                yes_price_cents=yes_price_cents,
                model_probability=model_probability,
                gross_edge_centicents=gross,
                fee_centicents=fee,
                net_edge_centicents=net,
                thresholds_version=thresholds.version,
                fee_model_version=fee_model.version,
                reason=(
                    f"net edge {net}cc >= draft threshold "
                    f"{thresholds.min_net_edge_centicents}cc"
                ),
            )
        return HypotheticalDecision(
            action=DecisionAction.NO_TRADE,
            ticker=ticker,
            yes_price_cents=yes_price_cents,
            model_probability=model_probability,
            gross_edge_centicents=gross,
            fee_centicents=fee,
            net_edge_centicents=net,
            thresholds_version=thresholds.version,
            fee_model_version=fee_model.version,
            reason=(
                f"net edge {net}cc below draft threshold {thresholds.min_net_edge_centicents}cc"
            ),
        )

    best_gross = max(gross_yes, gross_no)
    return HypotheticalDecision(
        action=DecisionAction.NO_TRADE,
        ticker=ticker,
        yes_price_cents=yes_price_cents,
        model_probability=model_probability,
        gross_edge_centicents=best_gross,
        fee_centicents=0,
        net_edge_centicents=best_gross,
        thresholds_version=thresholds.version,
        fee_model_version=fee_model.version,
        reason="no side has positive gross edge",
    )
