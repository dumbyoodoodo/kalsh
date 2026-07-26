"""Market-data fixtures the simulator replays: order-book snapshots and trades.

The book is stored YES-referenced (``yes_asks`` = resting offers to SELL YES,
``yes_bids`` = resting offers to BUY YES). ``matchable_levels`` translates an
order on either side into the price/qty levels it would consume, in the ORDER's
own price units, respecting its limit -- so a BUY NO correctly crosses the YES
bids at the complementary price (100 - yes_bid).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from kalshi_weather.execution.models import Action, Side

CONTRACT_VALUE_CENTS = 100


@dataclass(frozen=True)
class OrderBook:
    ticker: str
    captured_at: datetime
    #: (price_cents, quantity) offers to SELL yes, best (lowest) first.
    yes_asks: tuple[tuple[int, int], ...] = ()
    #: (price_cents, quantity) offers to BUY yes, best (highest) first.
    yes_bids: tuple[tuple[int, int], ...] = ()
    source_ref: str = "synthetic"

    def best_yes_ask(self) -> int | None:
        return min((p for p, _ in self.yes_asks), default=None)

    def best_yes_bid(self) -> int | None:
        return max((p for p, _ in self.yes_bids), default=None)

    def spread_cents(self) -> int | None:
        a, b = self.best_yes_ask(), self.best_yes_bid()
        return (a - b) if (a is not None and b is not None) else None

    def matchable_levels(self, side: Side, action: Action, limit: int) -> list[tuple[int, int]]:
        """Levels this order consumes, as (fill_price_in_order_units, qty),
        best-price-first, filtered by the order's limit."""
        if side is Side.YES and action is Action.BUY:
            lv = [(p, q) for p, q in self.yes_asks if p <= limit]
            return sorted(lv, key=lambda x: x[0])
        if side is Side.YES and action is Action.SELL:
            lv = [(p, q) for p, q in self.yes_bids if p >= limit]
            return sorted(lv, key=lambda x: -x[0])
        if side is Side.NO and action is Action.BUY:
            # buying NO crosses YES bids; NO price = 100 - yes_bid
            lv = [(CONTRACT_VALUE_CENTS - p, q) for p, q in self.yes_bids]
            return sorted([(np, q) for np, q in lv if np <= limit], key=lambda x: x[0])
        # SELL NO crosses YES asks; NO price = 100 - yes_ask
        lv = [(CONTRACT_VALUE_CENTS - p, q) for p, q in self.yes_asks]
        return sorted([(np, q) for np, q in lv if np >= limit], key=lambda x: -x[0])


@dataclass(frozen=True)
class Trade:
    ticker: str
    executed_at: datetime
    yes_price_cents: int
    quantity: int
    taker_side: Side | None = None
    source_ref: str = "synthetic"


@dataclass
class MarketData:
    """The explicit, immutable historical/synthetic inputs for one run. All
    lists are pre-sorted by timestamp at load and never mutated during replay."""

    order_books: list[OrderBook] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
