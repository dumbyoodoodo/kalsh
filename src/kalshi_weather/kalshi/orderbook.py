"""Pure order-book reconstruction logic.

Kalshi's order-book endpoint returns only resting bid levels for each side of
the binary market (yes bids, no bids) -- it does not return asks directly.
Because YES and NO are complements of a single binary outcome, the ask on
each side can be derived from the best bid on the other side:

    best_yes_ask = 100 - best_no_bid
    best_no_ask  = 100 - best_yes_bid

This module is deliberately independent of the live wire schema (plain
tuples in, plain values out) so it can be tested exhaustively without any
network access or API assumptions.
"""

from dataclasses import dataclass

MIN_PRICE_CENTS = 1
MAX_PRICE_CENTS = 99
FULL_RANGE_CENTS = 100

#: A single resting price level: (price_cents, quantity).
Level = tuple[int, int]


class InvalidPriceLevelError(ValueError):
    """Raised when a price level falls outside the valid 1-99 cent range."""


@dataclass(frozen=True, slots=True)
class BestQuote:
    """Best bid/ask on both sides of a binary market, derived from raw bid levels."""

    best_yes_bid_cents: int | None
    best_yes_ask_cents: int | None
    best_no_bid_cents: int | None
    best_no_ask_cents: int | None

    @property
    def yes_spread_cents(self) -> int | None:
        if self.best_yes_bid_cents is None or self.best_yes_ask_cents is None:
            return None
        return self.best_yes_ask_cents - self.best_yes_bid_cents

    @property
    def no_spread_cents(self) -> int | None:
        if self.best_no_bid_cents is None or self.best_no_ask_cents is None:
            return None
        return self.best_no_ask_cents - self.best_no_bid_cents


def _validate_levels(levels: list[Level], *, side: str) -> None:
    for price_cents, quantity in levels:
        if not (MIN_PRICE_CENTS <= price_cents <= MAX_PRICE_CENTS):
            raise InvalidPriceLevelError(
                f"{side} price {price_cents} out of [{MIN_PRICE_CENTS}, {MAX_PRICE_CENTS}]"
            )
        if quantity < 0:
            raise InvalidPriceLevelError(f"{side} quantity {quantity} is negative")


def _best_bid(levels: list[Level]) -> int | None:
    """Highest bid price is the best bid; an empty book has no bid."""
    if not levels:
        return None
    return max(price for price, _quantity in levels)


def reconstruct_best_quote(yes_bids: list[Level], no_bids: list[Level]) -> BestQuote:
    """Derive best YES/NO bid and ask from raw resting bid levels on each side.

    An empty side means no resting bids on that side; the corresponding bid
    is ``None`` and the *other* side's ask (which depends on this bid) is
    also ``None``, since there is nothing to derive it from.
    """
    _validate_levels(yes_bids, side="yes")
    _validate_levels(no_bids, side="no")

    best_yes_bid = _best_bid(yes_bids)
    best_no_bid = _best_bid(no_bids)

    best_yes_ask = FULL_RANGE_CENTS - best_no_bid if best_no_bid is not None else None
    best_no_ask = FULL_RANGE_CENTS - best_yes_bid if best_yes_bid is not None else None

    return BestQuote(
        best_yes_bid_cents=best_yes_bid,
        best_yes_ask_cents=best_yes_ask,
        best_no_bid_cents=best_no_bid,
        best_no_ask_cents=best_no_ask,
    )
