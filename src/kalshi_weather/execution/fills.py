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

from kalshi_weather.execution.market import CONTRACT_VALUE_CENTS, OrderBook, Trade
from kalshi_weather.execution.models import Action, Liquidity, OrderIntent, Side
from kalshi_weather.execution.policy import ExecutionPolicy


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


def simulate_passive(
    intent: OrderIntent, subsequent_trades: list[Trade], policy: ExecutionPolicy
) -> list[SimFill]:
    """Mode B: a resting order fills only up to the realized subsequent volume
    that traded through its price, minus the queue-ahead assumption. Fills at
    the order's own limit (maker). Only trades strictly after submission count."""
    volume_through = sum(
        t.quantity
        for t in subsequent_trades
        if t.executed_at > intent.submitted_at and _would_fill_passive(intent, t)
    )
    fillable = max(0, volume_through - policy.queue_ahead_contracts)
    take = min(intent.quantity, fillable)
    if take <= 0:
        return []
    return [SimFill(take, intent.limit_price_cents, Liquidity.MAKER, "passive:subsequent_volume")]
