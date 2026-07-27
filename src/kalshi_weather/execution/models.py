"""Execution-domain models. All money is integer cents; a Kalshi binary
contract is worth 0 or 100 cents at settlement, and its live price is 1..99.

Internal representation (documented, ADR 0015): a Position holds LONG YES and
LONG NO contract lots -- the only two things one can hold on Kalshi's
collateralized binary book. "Selling YES" beyond the held YES quantity opens NO
at the complementary price (100 - yes_price), matching Kalshi mechanics, and
vice-versa. Cost basis is tracked as total integer cents per side so proportional
closes stay exact (no per-unit rounding drift). User-facing order semantics stay
YES/NO + buy/sell.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

CONTRACT_VALUE_CENTS = 100


class Side(StrEnum):
    YES = "yes"
    NO = "no"


class Action(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKETABLE_LIMIT = "marketable_limit"
    PASSIVE_LIMIT = "passive_limit"


class TimeInForce(StrEnum):
    IOC = "ioc"  # immediate-or-cancel: unfilled remainder is canceled at once
    GTC = "gtc"  # good-till-cancel (or expiration)
    GTD = "gtd"  # good-till-date (requires expires_at)


class OrderState(StrEnum):
    CREATED = "created"
    ACCEPTED = "accepted"
    RESTING = "resting"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"
    SIMULATED = "simulated"


#: Explicit lifecycle transition table. Anything not listed is invalid.
_VALID_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.ACCEPTED, OrderState.REJECTED}),
    OrderState.ACCEPTED: frozenset(
        {
            OrderState.RESTING,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.RESTING: frozenset(
        {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED}
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED}
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELED: frozenset(),
    OrderState.EXPIRED: frozenset(),
    OrderState.REJECTED: frozenset(),
}


class InvalidTransitionError(RuntimeError):
    """Raised on an order lifecycle transition not in the explicit table."""


class AccountingError(RuntimeError):
    """Raised when an accounting invariant would be violated."""


@dataclass(frozen=True)
class OrderIntent:
    """A hypothetical order a strategy wishes to place. Frozen: an intent is an
    input, never mutated."""

    order_id: str
    strategy_id: str
    ticker: str
    side: Side
    action: Action
    order_type: OrderType
    limit_price_cents: int
    quantity: int
    submitted_at: datetime
    time_in_force: TimeInForce = TimeInForce.IOC
    expires_at: datetime | None = None
    signal_at: datetime | None = None
    model_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Price-protection: every order (including a "market" one) MUST carry a
        # bounded limit in the tradable range -- no unbounded market orders.
        if not 1 <= self.limit_price_cents <= 99:
            raise ValueError(f"limit_price_cents must be 1..99, got {self.limit_price_cents}")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.time_in_force is TimeInForce.GTD and self.expires_at is None:
            raise ValueError("GTD order requires expires_at")


@dataclass
class Order:
    """The mutable lifecycle state of a submitted intent."""

    intent: OrderIntent
    state: OrderState = OrderState.CREATED
    filled_quantity: int = 0
    reject_reason: str | None = None

    @property
    def remaining(self) -> int:
        return self.intent.quantity - self.filled_quantity

    def transition(self, to: OrderState) -> None:
        if to not in _VALID_TRANSITIONS[self.state]:
            raise InvalidTransitionError(
                f"order {self.intent.order_id}: {self.state} -> {to} is not allowed"
            )
        self.state = to

    def is_open(self) -> bool:
        return self.state in (OrderState.ACCEPTED, OrderState.RESTING, OrderState.PARTIALLY_FILLED)


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    ticker: str
    side: Side
    action: Action
    quantity: int
    price_cents: int
    filled_at: datetime
    fee_cents: int  # net fee charged to cash (after any rounding rebate)
    liquidity: Liquidity
    source_ref: str  # snapshot/trade id or synthetic marker
    execution_policy_version: str
    fee_model_version: str | None = None
    #: Optional fee decomposition (populated by fee models that expose it, e.g.
    #: the authoritative Kalshi model). All values in centicents ($0.0001).
    fee_market_schedule: str | None = None
    fee_trade_centicents: int | None = None
    fee_rounding_centicents: int | None = None
    fee_rebate_centicents: int | None = None


@dataclass
class Position:
    """Collateralized YES/NO long lots for one market (integer cents)."""

    ticker: str
    yes_qty: int = 0
    yes_cost_cents: int = 0  # total cents paid for the held YES lot
    no_qty: int = 0
    no_cost_cents: int = 0
    realized_pnl_cents: int = 0
    fees_cents: int = 0
    settled: bool = False

    def avg_yes_cost(self) -> int:
        return self.yes_cost_cents // self.yes_qty if self.yes_qty else 0

    def avg_no_cost(self) -> int:
        return self.no_cost_cents // self.no_qty if self.no_qty else 0

    def is_open(self) -> bool:
        return (self.yes_qty > 0 or self.no_qty > 0) and not self.settled

    def apply_fill(self, side: Side, action: Action, qty: int, price: int, fee: int) -> int:
        """Apply a fill; return the signed cash delta in cents (negative = paid).
        Buying opens a long lot at ``price``; selling closes the same side first,
        then opens the opposite side at ``100 - price`` for any excess (Kalshi
        mechanics). Fees are always a cash outflow."""
        if self.settled:
            raise AccountingError(f"{self.ticker}: cannot trade a settled market")
        self.fees_cents += fee
        cash = -fee
        if action is Action.BUY:
            cash -= qty * price
            if side is Side.YES:
                self.yes_qty += qty
                self.yes_cost_cents += qty * price
            else:
                self.no_qty += qty
                self.no_cost_cents += qty * price
            return cash
        # SELL: close the named side first, open the opposite for any remainder.
        if side is Side.YES:
            cash += self._sell(qty, price, close_yes=True)
        else:
            cash += self._sell(qty, price, close_yes=False)
        return cash

    def _sell(self, qty: int, price: int, *, close_yes: bool) -> int:
        held = self.yes_qty if close_yes else self.no_qty
        cost = self.yes_cost_cents if close_yes else self.no_cost_cents
        close = min(qty, held)
        cash = 0
        if close > 0:
            # proportional, integer-exact cost removal (residual stays with the lot)
            cost_removed = cost * close // held
            proceeds = close * price
            self.realized_pnl_cents += proceeds - cost_removed
            cash += proceeds
            if close_yes:
                self.yes_qty -= close
                self.yes_cost_cents -= cost_removed
            else:
                self.no_qty -= close
                self.no_cost_cents -= cost_removed
        remainder = qty - close
        if remainder > 0:  # open the opposite side at the complementary price
            open_price = CONTRACT_VALUE_CENTS - price
            cash -= remainder * open_price
            if close_yes:
                self.no_qty += remainder
                self.no_cost_cents += remainder * open_price
            else:
                self.yes_qty += remainder
                self.yes_cost_cents += remainder * open_price
        return cash

    def unrealized_pnl_cents(self, yes_mark_cents: int | None) -> int:
        """Mark-to-market vs a YES mark price (NO marks at 100-yes). None if no
        mark is available."""
        if yes_mark_cents is None or self.settled:
            return 0
        yes_val = self.yes_qty * yes_mark_cents - self.yes_cost_cents
        no_val = self.no_qty * (CONTRACT_VALUE_CENTS - yes_mark_cents) - self.no_cost_cents
        return yes_val + no_val

    def max_loss_cents(self) -> int:
        """Worst-case additional loss on the open lots: total cost still at risk
        minus the guaranteed payout (min(yes,no) contracts pay 100 whichever way
        it settles)."""
        if self.settled:
            return 0
        guaranteed = min(self.yes_qty, self.no_qty) * CONTRACT_VALUE_CENTS
        return max(0, self.yes_cost_cents + self.no_cost_cents - guaranteed)

    def settle(self, result: Side) -> int:
        """Settle at YES or NO. Returns the payout cash (cents). Idempotent: a
        second call on an already-settled position returns 0."""
        if self.settled:
            return 0
        payout = (self.yes_qty if result is Side.YES else self.no_qty) * CONTRACT_VALUE_CENTS
        cost = self.yes_cost_cents + self.no_cost_cents
        self.realized_pnl_cents += payout - cost
        self.yes_qty = self.no_qty = 0
        self.yes_cost_cents = self.no_cost_cents = 0
        self.settled = True
        return payout
