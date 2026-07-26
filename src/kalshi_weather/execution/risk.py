"""Pre-trade risk engine. Runs BEFORE an order is accepted; a rejection carries
a specific machine-readable reason. Never silently resizes an order."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from kalshi_weather.execution.ledger import Portfolio
from kalshi_weather.execution.market import OrderBook
from kalshi_weather.execution.models import Action, OrderIntent
from kalshi_weather.execution.policy import RiskConfig


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str | None = None


def _event_of(ticker: str) -> str:
    # event = ticker minus the trailing "-<threshold>" (best-effort grouping)
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


def check_order(
    intent: OrderIntent,
    portfolio: Portfolio,
    config: RiskConfig,
    *,
    book: OrderBook | None,
    now: datetime,
    open_order_count: int,
    seen_order_ids: set[str],
) -> RiskDecision:
    """Approve or reject with a specific reason. Checks are ordered cheap-first."""
    if intent.order_id in seen_order_ids:
        return RiskDecision(False, "duplicate_order_id")
    if not config.price_min_cents <= intent.limit_price_cents <= config.price_max_cents:
        return RiskDecision(False, "price_out_of_bounds")
    if intent.quantity > config.max_order_quantity:
        return RiskDecision(False, "max_order_quantity_exceeded")
    if open_order_count >= config.max_open_orders:
        return RiskDecision(False, "max_open_orders_exceeded")

    pos = portfolio.positions.get(intent.ticker)
    projected_side_qty = intent.quantity + ((pos.yes_qty + pos.no_qty) if pos is not None else 0)
    if projected_side_qty > config.max_position_per_market:
        return RiskDecision(False, "max_position_per_market_exceeded")

    if intent.action is Action.BUY:
        worst_cost = intent.quantity * intent.limit_price_cents
        if portfolio.cash_cents - worst_cost < config.min_available_cash_cents:
            return RiskDecision(False, "insufficient_cash")
        projected_gross = portfolio.gross_exposure_cents() + worst_cost
        if projected_gross > config.max_gross_exposure_cents:
            return RiskDecision(False, "max_gross_exposure_exceeded")
        projected_loss = portfolio.max_possible_loss_cents() + worst_cost
        if projected_loss > config.max_total_possible_loss_cents:
            return RiskDecision(False, "max_total_possible_loss_exceeded")

    markets_with_exposure = len(portfolio.open_positions())
    if pos is None and markets_with_exposure >= config.max_markets_with_exposure:
        return RiskDecision(False, "max_markets_with_exposure_exceeded")

    if book is not None:
        age = (now - book.captured_at).total_seconds()
        if age > config.max_book_age_seconds:
            return RiskDecision(False, "stale_order_book")
        spread = book.spread_cents()
        if spread is not None and spread > config.max_spread_cents:
            return RiskDecision(False, "spread_too_wide")

    return RiskDecision(True, None)
