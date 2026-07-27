"""The deterministic simulation engine: order -> risk -> fill -> ledger, plus
cancel / expire / settlement. No exchange I/O of any kind."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kalshi_weather.execution import fills as fillmod
from kalshi_weather.execution.ledger import EntryKind, Ledger, Portfolio
from kalshi_weather.execution.market import OrderBook, Trade
from kalshi_weather.execution.models import (
    Action,
    Fill,
    Order,
    OrderIntent,
    OrderState,
    Position,
    Side,
    TimeInForce,
)
from kalshi_weather.execution.policy import ExecutionPolicy, RiskConfig
from kalshi_weather.execution.risk import check_order


@dataclass
class Simulator:
    policy: ExecutionPolicy
    risk: RiskConfig
    initial_cash_cents: int
    #: Optional per-ticker observed-availability timelines (historical replay).
    #: When present, passive fills consult them for queue continuity. Duck-typed
    #: (TickerAvailability) to keep the engine free of a hard availability import.
    availability: dict[str, Any] = field(default_factory=dict)
    #: Whether LIKELY_OBSERVED intervals keep passive continuity (policy-driven).
    allow_likely_observed: bool = False
    ledger: Ledger = field(init=False)
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    rejections: list[tuple[str, str]] = field(default_factory=list)  # (order_id, reason)
    settled_markets: set[str] = field(default_factory=set)
    _seen_ids: set[str] = field(default_factory=set)
    _fill_seq: int = 0
    _resting_reserved: dict[str, int] = field(default_factory=dict)  # order_id -> reserved cents
    #: Per-order fee-rounding accumulator (centicents), threaded across an order's
    #: fills so rounding rebates converge exactly (Kalshi Predictions API docs).
    _fee_accumulators: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ledger = Ledger(Portfolio(initial_cash_cents=self.initial_cash_cents))
        if self.initial_cash_cents:
            self.ledger.record(
                _EPOCH,
                EntryKind.DEPOSIT,
                self.initial_cash_cents,
                0,
                {"amount_cents": self.initial_cash_cents, "reason": "initial_cash"},
            )

    @property
    def portfolio(self) -> Portfolio:
        return self.ledger.portfolio

    def open_order_count(self) -> int:
        return sum(1 for o in self.orders if o.is_open())

    def reject(self, intent: OrderIntent, reason: str) -> Order:
        """Record a pre-trade rejection (e.g. market not open) without evaluating
        fills. Mirrors the settled-market rejection path in ``submit``."""
        order = Order(intent=intent)
        self.orders.append(order)
        order.transition(OrderState.REJECTED)
        order.reject_reason = reason
        self.rejections.append((intent.order_id, reason))
        self._seen_ids.add(intent.order_id)
        return order

    # --- order submission --------------------------------------------------

    def submit(
        self,
        intent: OrderIntent,
        *,
        book: OrderBook | None,
        subsequent_trades: list[Trade] | None = None,
        now: datetime | None = None,
    ) -> Order:
        now = now or intent.submitted_at
        order = Order(intent=intent)
        self.orders.append(order)

        if intent.ticker in self.settled_markets:
            order.transition(OrderState.REJECTED)
            order.reject_reason = "market_settled"
            self.rejections.append((intent.order_id, "market_settled"))
            self._seen_ids.add(intent.order_id)
            return order

        decision = check_order(
            intent,
            self.portfolio,
            self.risk,
            book=book,
            now=now,
            open_order_count=self.open_order_count(),
            seen_order_ids=self._seen_ids,
        )
        self._seen_ids.add(intent.order_id)
        if not decision.approved:
            order.transition(OrderState.REJECTED)
            order.reject_reason = decision.reason
            self.rejections.append((intent.order_id, decision.reason or "rejected"))
            return order

        order.transition(OrderState.ACCEPTED)

        sim_fills = self._evaluate_fills(intent, book, subsequent_trades or [], now)
        for sf in sim_fills:
            self._book_fill(order, sf, now)

        if order.remaining <= 0:
            if order.state is not OrderState.FILLED:
                order.transition(OrderState.FILLED)
            return order

        # remainder handling
        if intent.time_in_force is TimeInForce.IOC:
            if self.policy.cancel_remainder_on_ioc:
                order.transition(OrderState.CANCELED)
        else:  # GTC / GTD: rest the remainder and reserve its worst-case cost
            order.transition(OrderState.RESTING)
            if intent.action is Action.BUY:
                reserve = order.remaining * intent.limit_price_cents
                if reserve > 0:
                    self.ledger.record(
                        now,
                        EntryKind.RESERVE,
                        -reserve,
                        reserve,
                        {"amount_cents": reserve, "order_id": intent.order_id},
                    )
                    self._resting_reserved[intent.order_id] = reserve
        return order

    def _evaluate_fills(
        self, intent: OrderIntent, book: OrderBook | None, subsequent: list[Trade], now: datetime
    ) -> list[fillmod.SimFill]:
        from kalshi_weather.execution.models import OrderType
        from kalshi_weather.execution.policy import FillMode

        # A passive_limit order always rests (Mode B); a marketable_limit order
        # crosses the book (Mode A/C). PASSIVE policy forces passive for all.
        if (
            intent.order_type is OrderType.PASSIVE_LIMIT
            or self.policy.fill_mode is FillMode.PASSIVE
        ):
            return fillmod.simulate_passive(
                intent,
                subsequent,
                self.policy,
                availability=self.availability.get(intent.ticker),
                allow_likely_observed=self.allow_likely_observed,
            )
        # MARKETABLE / SYNTHETIC both cross the visible book (Mode A / C)
        if book is None:
            return []  # never invent a price on missing data
        # policy-level book guards
        age = (now - book.captured_at).total_seconds()
        if age > self.policy.max_book_age_seconds:
            return []
        spread = book.spread_cents()
        if spread is not None and spread > self.policy.max_spread_cents:
            return []
        result, _ = fillmod.simulate_marketable(intent, book, self.policy)
        return result

    def _book_fill(self, order: Order, sf: fillmod.SimFill, now: datetime) -> None:
        intent = order.intent
        assessment = self.policy.fee_model.assess(
            price_cents=sf.price_cents,
            quantity=sf.quantity,
            side=intent.side,
            action=intent.action,
            liquidity=sf.liquidity,
            ticker=intent.ticker,
            accumulator_centicents=self._fee_accumulators.get(intent.order_id, 0),
        )
        self._fee_accumulators[intent.order_id] = assessment.accumulator_centicents
        fee = assessment.net_fee_cents
        bd = assessment.breakdown
        # release any reservation covering this filled quantity (BUY resting)
        if intent.action is Action.BUY and intent.order_id in self._resting_reserved:
            rel = min(
                self._resting_reserved[intent.order_id], sf.quantity * intent.limit_price_cents
            )
            if rel > 0:
                self.ledger.record(
                    now,
                    EntryKind.RELEASE,
                    rel,
                    -rel,
                    {"amount_cents": rel, "order_id": intent.order_id},
                )
                self._resting_reserved[intent.order_id] -= rel
        # dry-run the fill on a copy to learn the cash delta the ledger records;
        # the ledger then re-applies it and reconciliation asserts they match.
        pos = self.portfolio.position(intent.ticker)
        cash = _dry_run_fill_cash(pos, intent.side, intent.action, sf.quantity, sf.price_cents, fee)
        self.ledger.record(
            now,
            EntryKind.FILL,
            cash,
            0,
            {
                "ticker": intent.ticker,
                "side": intent.side.value,
                "action": intent.action.value,
                "quantity": sf.quantity,
                "price_cents": sf.price_cents,
                "fee_cents": fee,
            },
        )
        self.fills.append(
            Fill(
                fill_id=f"f{self._fill_seq}",
                order_id=intent.order_id,
                ticker=intent.ticker,
                side=intent.side,
                action=intent.action,
                quantity=sf.quantity,
                price_cents=sf.price_cents,
                filled_at=now,
                fee_cents=fee,
                liquidity=sf.liquidity,
                source_ref=sf.source_ref,
                execution_policy_version=self.policy.version,
                fee_model_version=self.policy.fee_model.version,
                fee_market_schedule=bd.market_fee_schedule,
                fee_trade_centicents=bd.trade_fee_centicents,
                fee_rounding_centicents=bd.rounding_fee_centicents,
                fee_rebate_centicents=bd.rebate_centicents,
            )
        )
        self._fill_seq += 1
        order.filled_quantity += sf.quantity
        if order.remaining <= 0:
            if order.state is OrderState.ACCEPTED or order.state is OrderState.PARTIALLY_FILLED:
                order.transition(OrderState.FILLED)
        elif order.state is OrderState.ACCEPTED:
            order.transition(OrderState.PARTIALLY_FILLED)

    # --- cancel / expire / settle -----------------------------------------

    def cancel(self, order: Order, now: datetime, *, expired: bool = False) -> None:
        if not order.is_open():
            return
        self._release_all(order, now)
        order.transition(OrderState.EXPIRED if expired else OrderState.CANCELED)

    def _release_all(self, order: Order, now: datetime) -> None:
        rem = self._resting_reserved.pop(order.intent.order_id, 0)
        if rem > 0:
            self.ledger.record(
                now,
                EntryKind.RELEASE,
                rem,
                -rem,
                {"amount_cents": rem, "order_id": order.intent.order_id},
            )

    def settle(self, ticker: str, result: Side, now: datetime) -> int:
        """Settle a market (idempotent). Cancels any open orders on it first
        (releasing reservations), pays out, marks settled."""
        if ticker in self.settled_markets:
            return 0
        for o in self.orders:
            if o.intent.ticker == ticker and o.is_open():
                self.cancel(o, now)
        pos = self.portfolio.position(ticker)
        payout = _dry_run_settle(pos, result)
        self.ledger.record(
            now, EntryKind.SETTLEMENT, payout, 0, {"ticker": ticker, "result": result.value}
        )
        self.settled_markets.add(ticker)
        return payout


def _dry_run_fill_cash(
    pos: Position, side: Side, action: Action, qty: int, price: int, fee: int
) -> int:
    """Compute the cash delta a fill will cause WITHOUT mutating the real
    position (the ledger entry then re-applies it, and reconciliation asserts
    they match)."""
    return copy.deepcopy(pos).apply_fill(side, action, qty, price, fee)


def _dry_run_settle(pos: Position, result: Side) -> int:
    return copy.deepcopy(pos).settle(result)


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
