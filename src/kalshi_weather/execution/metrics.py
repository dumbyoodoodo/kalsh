"""Run metrics. Deliberately separates ACCOUNTING/EXECUTION performance from any
predictive claim -- a profitable synthetic scenario is NOT evidence of a real
edge (there is no strategy or market-efficiency claim here)."""

from __future__ import annotations

from typing import Any

from kalshi_weather.execution.engine import Simulator
from kalshi_weather.execution.models import Liquidity, OrderState


def compute_metrics(
    sim: Simulator, *, marks: dict[str, int], equity_curve: list[int]
) -> dict[str, Any]:
    p = sim.portfolio
    orders = sim.orders
    fills = sim.fills
    submitted = len(orders)
    accepted = sum(1 for o in orders if o.state is not OrderState.REJECTED)
    rejected = sum(1 for o in orders if o.state is OrderState.REJECTED)
    filled = sum(1 for o in orders if o.state is OrderState.FILLED)
    partial = sum(1 for o in orders if o.filled_quantity and o.remaining > 0)
    canceled = sum(1 for o in orders if o.state in (OrderState.CANCELED, OrderState.EXPIRED))
    fill_qty = sum(f.quantity for f in fills)
    total_fees = sum(f.fee_cents for f in fills)
    maker = sum(f.quantity for f in fills if f.liquidity is Liquidity.MAKER)
    taker = sum(f.quantity for f in fills if f.liquidity is Liquidity.TAKER)
    notional = sum(f.quantity * f.price_cents for f in fills)
    avg_price = (notional / fill_qty) if fill_qty else 0.0

    equity_now = p.total_equity_cents(marks)
    peak = sim.initial_cash_cents
    max_dd = 0
    for eq in equity_curve or [equity_now]:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    return {
        "accounting": {
            "starting_cash_cents": sim.initial_cash_cents,
            "ending_cash_cents": p.cash_cents,
            "reserved_cents": p.reserved_cents,
            "total_equity_cents": equity_now,
            "realized_pnl_cents": p.realized_pnl_cents(),
            "unrealized_pnl_cents": p.unrealized_pnl_cents(marks),
            "total_fees_cents": p.fees_cents,
            "gross_return": round((equity_now - sim.initial_cash_cents) / sim.initial_cash_cents, 6)
            if sim.initial_cash_cents
            else 0.0,
            "max_drawdown_cents": max_dd,
            "gross_exposure_cents": p.gross_exposure_cents(),
            "net_exposure_cents": p.net_exposure_cents(),
            "max_possible_loss_cents": p.max_possible_loss_cents(),
            "settlement_pnl_cents": sum(
                pos.realized_pnl_cents for pos in p.positions.values() if pos.settled
            ),
        },
        "execution": {
            "orders_submitted": submitted,
            "orders_accepted": accepted,
            "orders_rejected": rejected,
            "orders_filled": filled,
            "orders_partially_filled": partial,
            "orders_canceled_or_expired": canceled,
            "fill_rate": round(filled / submitted, 4) if submitted else 0.0,
            "contracts_filled": fill_qty,
            "avg_execution_price_cents": round(avg_price, 2),
            "maker_contracts": maker,
            "taker_contracts": taker,
            "maker_share": round(maker / fill_qty, 4) if fill_qty else 0.0,
            "turnover_cents": notional,
            "fees_cents": total_fees,
        },
        "predictive": {
            "note": "No predictive/strategy metric is computed here; this is an "
            "execution-and-accounting simulation only. Profit in a synthetic "
            "scenario is NOT evidence of a real edge.",
        },
        "exposure_by_market": {
            pos.ticker: pos.yes_cost_cents + pos.no_cost_cents
            for pos in p.positions.values()
            if pos.is_open()
        },
    }
