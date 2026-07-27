"""Conservative fill models. All are deterministic and never invent price or
depth: an order fills only against visible book depth (Mode A) or against
volume that traded *after* submission through its price (Mode B). Mode C is just
Mode A over hand-authored synthetic books.

Historical caveat (documented): true queue position is not reconstructable from
public data, so Mode B is deliberately pessimistic -- it requires realized
subsequent volume beyond a configurable queue-ahead assumption before filling a
passive order, and never uses any pre-submission information as fill evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from kalshi_weather.execution.market import CONTRACT_VALUE_CENTS, OrderBook, Trade
from kalshi_weather.execution.models import Action, Liquidity, OrderIntent, Side
from kalshi_weather.execution.policy import ExecutionPolicy

if TYPE_CHECKING:
    from kalshi_weather.execution.availability import TickerAvailability


@dataclass(frozen=True)
class SimFill:
    quantity: int
    price_cents: int
    liquidity: Liquidity
    source_ref: str


def simulate_marketable(
    intent: OrderIntent, book: OrderBook, policy: ExecutionPolicy
) -> tuple[list[SimFill], int]:
    """Mode A: consume visible depth best-price-first, respecting the limit.
    Returns (fills, remaining_unfilled)."""
    fills: list[SimFill] = []
    remaining = intent.quantity
    for price, qty in book.matchable_levels(intent.side, intent.action, intent.limit_price_cents):
        if remaining <= 0:
            break
        take = min(remaining, qty)  # never more than visible depth
        if take <= 0:
            continue
        fills.append(SimFill(take, price, Liquidity.TAKER, book.source_ref))
        remaining -= take
    return fills, remaining


def _would_fill_passive(intent: OrderIntent, trade: Trade) -> bool:
    """Does a trade after submission cross a resting order at ``intent`` limit?"""
    y = trade.yes_price_cents
    lim = intent.limit_price_cents
    if intent.side is Side.YES:
        return y <= lim if intent.action is Action.BUY else y >= lim
    no_price = CONTRACT_VALUE_CENTS - y
    return no_price <= lim if intent.action is Action.BUY else no_price >= lim


def compatible_taker_side(side: Side, action: Action) -> Side:
    """The taker (aggressor) side whose trades fill a resting order. A resting
    YES BUY (bid) is hit by a taker aggressively selling YES (taker_side NO); a
    resting YES SELL (ask) is lifted by a taker buying YES (taker_side YES); and
    the mirror for NO-side orders."""
    if side is Side.YES:
        return Side.NO if action is Action.BUY else Side.YES
    return Side.YES if action is Action.BUY else Side.NO


def _direction_ok(intent: OrderIntent, trade: Trade, policy: ExecutionPolicy) -> bool:
    """Whether a trade's taker direction is admissible as passive fill evidence."""
    if not policy.passive_require_taker_direction:
        return True
    if trade.taker_side is None:
        return policy.passive_allow_unknown_taker  # unknown: excluded by default
    return trade.taker_side is compatible_taker_side(intent.side, intent.action)


def simulate_passive(
    intent: OrderIntent,
    subsequent_trades: list[Trade],
    policy: ExecutionPolicy,
    *,
    availability: TickerAvailability | None = None,
    allow_likely_observed: bool = False,
) -> list[SimFill]:
    """Mode B: a resting order fills only up to the realized subsequent volume
    that traded through its price on the COMPATIBLE aggressive side, minus the
    queue-ahead assumption. Fills at the order's own limit (maker). Only trades
    strictly after submission count.

    Continuity is bounded by TWO safeguards, whichever truncates earlier:

    * Observed availability (primary, when supplied): queue-ahead accumulation
      continues only while observation continuity holds. The first
      continuity-breaking interval after submission (COLLECTOR_UNAVAILABLE,
      UNKNOWN, MARKET_NOT_ELIGIBLE, or -- unless ``allow_likely_observed`` --
      LIKELY_OBSERVED) truncates the evidence. A later trade never bridges an
      unobserved interval.
    * Trade-data gap (secondary safeguard): a gap between consecutive qualifying
      trades wider than ``max_passive_trade_gap_seconds`` also truncates. It
      never OVERRIDES an availability interruption -- both apply."""
    avail_break: datetime | None = None
    if availability is not None:
        avail_break = availability.first_break_after(
            intent.submitted_at, allow_likely=allow_likely_observed
        )

    qualifying = sorted(
        (
            t
            for t in subsequent_trades
            if t.executed_at > intent.submitted_at
            and (avail_break is None or t.executed_at < avail_break)
            and _would_fill_passive(intent, t)
            and _direction_ok(intent, t, policy)
        ),
        key=lambda t: t.executed_at,
    )
    # Secondary safeguard: truncate at the first gap between consecutive
    # qualifying trades wider than the policy allows (an unobserved interval,
    # across which we do not assume the order held its queue position). The wait
    # from submission to the first fill-trade is normal resting, not a data gap.
    volume_through = 0
    prev: datetime | None = None
    for t in qualifying:
        if (
            prev is not None
            and (t.executed_at - prev).total_seconds() > policy.max_passive_trade_gap_seconds
        ):
            break
        volume_through += t.quantity
        prev = t.executed_at
    fillable = max(0, volume_through - policy.queue_ahead_contracts)
    take = min(intent.quantity, fillable)
    if take <= 0:
        return []
    return [SimFill(take, intent.limit_price_cents, Liquidity.MAKER, "passive:subsequent_volume")]
