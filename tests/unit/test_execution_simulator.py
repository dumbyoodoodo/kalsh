"""Deterministic scenario + invariant tests for the paper-trading simulator.

Money is asserted in exact integer cents. Synthetic fixtures throughout (no
exchange I/O, no H0019). Covers the Phase 15 scenario list and Phase 16
invariants.
"""

from datetime import UTC, datetime

import pytest

from kalshi_weather.execution.engine import Simulator
from kalshi_weather.execution.ledger import replay
from kalshi_weather.execution.market import MarketData, OrderBook, Trade
from kalshi_weather.execution.models import (
    AccountingError,
    Action,
    InvalidTransitionError,
    Order,
    OrderIntent,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
)
from kalshi_weather.execution.policy import (
    ExecutionPolicy,
    FeeModel,
    FillMode,
    RiskConfig,
)


def T(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 1, 1, h, m, s, tzinfo=UTC)


def _sim(mode=FillMode.SYNTHETIC, cash=100_00, fee=None, risk=None):  # type: ignore[no-untyped-def]
    pol = ExecutionPolicy(fill_mode=mode, fee_model=fee) if fee else ExecutionPolicy(fill_mode=mode)
    return Simulator(policy=pol, risk=risk or RiskConfig(), initial_cash_cents=cash)


def _oi(oid, tk, side, act, lim, q, at, tif=TimeInForce.IOC, typ=OrderType.MARKETABLE_LIMIT):  # type: ignore[no-untyped-def]
    return OrderIntent(
        order_id=oid,
        strategy_id="s",
        ticker=tk,
        side=side,
        action=act,
        order_type=typ,
        limit_price_cents=lim,
        quantity=q,
        submitted_at=at,
        time_in_force=tif,
    )


def _book(tk, at, asks=(), bids=()):  # type: ignore[no-untyped-def]
    return OrderBook(ticker=tk, captured_at=at, yes_asks=tuple(asks), yes_bids=tuple(bids))


# --- Phase 15 scenarios ----------------------------------------------------


def test_immediate_full_fill() -> None:
    s = _sim(FillMode.MARKETABLE)
    o = s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 10, T(9)), book=_book("M", T(9), asks=((40, 10),))
    )
    assert o.state is OrderState.FILLED and o.filled_quantity == 10
    assert s.portfolio.cash_cents == 100_00 - 400


def test_partial_fill_remainder_canceled() -> None:
    s = _sim(FillMode.MARKETABLE)
    o = s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 20, T(9)),
        book=_book("M", T(9), asks=((40, 10), (41, 5))),
    )
    assert o.filled_quantity == 10 and o.state is OrderState.CANCELED  # limit 40 excludes the 41s


def test_no_fill() -> None:
    s = _sim(FillMode.MARKETABLE)
    o = s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 5, T(9)), book=_book("M", T(9), asks=((45, 10),))
    )
    assert o.filled_quantity == 0 and o.state is OrderState.CANCELED


def test_passive_order_later_fills() -> None:
    s = _sim(FillMode.PASSIVE)
    trades = [Trade("M", T(10), yes_price_cents=39, quantity=8)]  # sold through our 40 bid
    o = s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 5, T(9), tif=TimeInForce.GTC),
        book=None,
        subsequent_trades=trades,
    )
    assert o.filled_quantity == 5 and s.fills[0].liquidity.value == "maker"


def test_passive_order_never_fills_without_volume() -> None:
    s = _sim(FillMode.PASSIVE)
    o = s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 5, T(9), tif=TimeInForce.GTC),
        book=None,
        subsequent_trades=[],
    )
    assert o.filled_quantity == 0 and o.state is OrderState.RESTING


def test_passive_ignores_pre_submission_trades() -> None:
    s = _sim(FillMode.PASSIVE)
    trades = [Trade("M", T(8), yes_price_cents=39, quantity=100)]  # BEFORE submission
    o = s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 5, T(9), tif=TimeInForce.GTC),
        book=None,
        subsequent_trades=trades,
    )
    assert o.filled_quantity == 0  # pre-submission volume is not fill evidence


def test_order_expires_at_end() -> None:
    from kalshi_weather.execution.replay import ReplayConfig, run_replay

    cfg = ReplayConfig(
        run_id="r",
        initial_cash_cents=100_00,
        policy=ExecutionPolicy(fill_mode=FillMode.PASSIVE),
        risk=RiskConfig(),
        market_data=MarketData(),
        order_intents=[_oi("o", "M", Side.YES, Action.BUY, 40, 5, T(9), tif=TimeInForce.GTC)],
    )
    sim = run_replay(cfg)
    assert sim.orders[0].state is OrderState.EXPIRED


def test_risk_rejection_max_order_quantity() -> None:
    s = _sim(FillMode.MARKETABLE, risk=RiskConfig(max_order_quantity=5))
    o = s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 10, T(9)), book=_book("M", T(9), asks=((40, 10),))
    )
    assert o.state is OrderState.REJECTED and o.reject_reason == "max_order_quantity_exceeded"


def test_insufficient_cash() -> None:
    s = _sim(FillMode.MARKETABLE, cash=100)
    o = s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 10, T(9)), book=_book("M", T(9), asks=((40, 10),))
    )
    assert o.reject_reason == "insufficient_cash"


def test_max_position_rejection() -> None:
    s = _sim(FillMode.MARKETABLE, risk=RiskConfig(max_position_per_market=8))
    o = s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 10, T(9)), book=_book("M", T(9), asks=((40, 10),))
    )
    assert o.reject_reason == "max_position_per_market_exceeded"


def test_yes_settlement() -> None:
    s = _sim(FillMode.MARKETABLE)
    s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 10, T(9)), book=_book("M", T(9), asks=((40, 10),))
    )
    s.settle("M", Side.YES, T(18))
    assert s.portfolio.cash_cents == 100_00 - 400 + 1000  # +10*100 payout
    assert s.portfolio.realized_pnl_cents() == 600


def test_no_settlement_loses_cost() -> None:
    s = _sim(FillMode.MARKETABLE)
    s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 10, T(9)), book=_book("M", T(9), asks=((40, 10),))
    )
    s.settle("M", Side.NO, T(18))
    assert s.portfolio.cash_cents == 100_00 - 400  # no payout
    assert s.portfolio.realized_pnl_cents() == -400


def test_position_reduction() -> None:
    s = _sim(FillMode.MARKETABLE)
    s.submit(
        _oi("a", "M", Side.YES, Action.BUY, 40, 10, T(9)), book=_book("M", T(9), asks=((40, 10),))
    )
    s.submit(
        _oi("b", "M", Side.YES, Action.SELL, 45, 4, T(10)), book=_book("M", T(10), bids=((45, 10),))
    )
    pos = s.portfolio.position("M")
    assert pos.yes_qty == 6 and pos.realized_pnl_cents == 4 * (45 - 40)  # realized on the 4 closed


def test_position_reversal() -> None:
    s = _sim(FillMode.SYNTHETIC)
    s.submit(
        _oi("a", "M", Side.YES, Action.BUY, 40, 10, T(9)), book=_book("M", T(9), asks=((40, 10),))
    )
    s.submit(
        _oi("b", "M", Side.YES, Action.SELL, 45, 15, T(10)),
        book=_book("M", T(10), bids=((45, 20),)),
    )
    pos = s.portfolio.position("M")
    assert pos.yes_qty == 0 and pos.no_qty == 5 and pos.no_cost_cents == 5 * 55


def test_fee_application() -> None:
    fee = FeeModel(version="test-fee", rate_bps=700, charge_on_maker=True)
    # price 40 -> risk 40*60=2400; raw=700*2400*10=16.8M; /1e6 => ceil(16.8)=17 cents
    computed = fee.fee_cents(
        price_cents=40, quantity=10, side=Side.YES, action=Action.BUY, liquidity=None
    )  # type: ignore[arg-type]
    assert computed == 17
    s = _sim(FillMode.MARKETABLE, fee=fee)
    s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 10, T(9)), book=_book("M", T(9), asks=((40, 10),))
    )
    assert s.portfolio.fees_cents == 17
    assert s.portfolio.cash_cents == 100_00 - 400 - 17


def test_multiple_markets_and_same_timestamp() -> None:
    from kalshi_weather.execution.replay import ReplayConfig, run_replay

    md = MarketData(
        order_books=[_book("A", T(9), asks=((30, 5),)), _book("B", T(9), asks=((60, 5),))]
    )
    cfg = ReplayConfig(
        run_id="r",
        initial_cash_cents=100_00,
        policy=ExecutionPolicy(fill_mode=FillMode.MARKETABLE),
        risk=RiskConfig(),
        market_data=md,
        order_intents=[
            _oi("a", "A", Side.YES, Action.BUY, 30, 5, T(9)),
            _oi("b", "B", Side.YES, Action.BUY, 60, 5, T(9)),
        ],
    )
    sim = run_replay(cfg)
    assert all(o.state is OrderState.FILLED for o in sim.orders)  # deterministic order-id tiebreak


def test_stale_order_book_rejection() -> None:
    s = _sim(FillMode.MARKETABLE, risk=RiskConfig(max_book_age_seconds=60))
    o = s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 5, T(9, 30)),
        book=_book("M", T(9), asks=((40, 5),)),
        now=T(9, 30),
    )
    assert o.reject_reason == "stale_order_book"


def test_duplicate_order_id_rejected() -> None:
    s = _sim(FillMode.MARKETABLE)
    s.submit(
        _oi("dup", "M", Side.YES, Action.BUY, 40, 1, T(9)), book=_book("M", T(9), asks=((40, 5),))
    )
    o2 = s.submit(
        _oi("dup", "M", Side.YES, Action.BUY, 40, 1, T(9)), book=_book("M", T(9), asks=((40, 5),))
    )
    assert o2.reject_reason == "duplicate_order_id"


# --- Phase 16 invariants ---------------------------------------------------


def test_invalid_lifecycle_transition_fails() -> None:
    o = Order(intent=_oi("o", "M", Side.YES, Action.BUY, 40, 1, T(9)))
    o.transition(OrderState.ACCEPTED)
    o.transition(OrderState.FILLED)
    with pytest.raises(InvalidTransitionError):
        o.transition(OrderState.RESTING)  # FILLED is terminal


def test_fill_never_exceeds_depth_or_violates_limit() -> None:
    from kalshi_weather.execution.fills import simulate_marketable

    book = _book("M", T(9), asks=((40, 3), (41, 100)))
    fills, rem = simulate_marketable(
        _oi("o", "M", Side.YES, Action.BUY, 40, 50, T(9)), book, ExecutionPolicy()
    )
    assert sum(f.quantity for f in fills) == 3  # only depth <= limit
    assert all(f.price_cents <= 40 for f in fills) and rem == 47


def test_settled_position_cannot_trade_or_remain_open() -> None:
    s = _sim(FillMode.MARKETABLE)
    s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 5, T(9)), book=_book("M", T(9), asks=((40, 5),))
    )
    s.settle("M", Side.YES, T(18))
    assert not s.portfolio.position("M").is_open()
    with pytest.raises(AccountingError):
        s.portfolio.position("M").apply_fill(Side.YES, Action.BUY, 1, 40, 0)


def test_duplicate_settlement_is_idempotent() -> None:
    s = _sim(FillMode.MARKETABLE)
    s.submit(
        _oi("o", "M", Side.YES, Action.BUY, 40, 5, T(9)), book=_book("M", T(9), asks=((40, 5),))
    )
    c1 = s.portfolio.cash_cents
    s.settle("M", Side.YES, T(18))
    c2 = s.portfolio.cash_cents
    s.settle("M", Side.YES, T(18, 1))  # again
    assert s.portfolio.cash_cents == c2 and c2 != c1


def test_ledger_replay_is_deterministic() -> None:
    s = _sim(FillMode.MARKETABLE)
    s.submit(
        _oi("a", "M", Side.YES, Action.BUY, 40, 10, T(9)), book=_book("M", T(9), asks=((40, 10),))
    )
    s.submit(
        _oi("b", "M", Side.YES, Action.SELL, 45, 4, T(10)), book=_book("M", T(10), bids=((45, 10),))
    )
    s.settle("M", Side.YES, T(18))
    rp = replay(100_00, s.ledger.entries)
    assert rp.cash_cents == s.portfolio.cash_cents
    assert rp.realized_pnl_cents() == s.portfolio.realized_pnl_cents()
    assert rp.cash_cents + rp.reserved_cents >= 0


def test_fees_never_negative() -> None:
    fee = FeeModel(rate_bps=500)
    for p in range(1, 100):
        assert (
            fee.fee_cents(
                price_cents=p, quantity=7, side=Side.YES, action=Action.BUY, liquidity=None
            )
            >= 0
        )  # type: ignore[arg-type]


def test_deterministic_same_inputs_same_output() -> None:
    from kalshi_weather.execution.replay import ReplayConfig, run_replay

    def build() -> Simulator:
        md = MarketData(order_books=[_book("M", T(9), asks=((40, 10),), bids=((45, 10),))])
        cfg = ReplayConfig(
            run_id="r",
            initial_cash_cents=100_00,
            policy=ExecutionPolicy(fill_mode=FillMode.MARKETABLE),
            risk=RiskConfig(),
            market_data=md,
            order_intents=[_oi("a", "M", Side.YES, Action.BUY, 40, 10, T(9))],
            settlements=[],
        )
        return run_replay(cfg)

    a, b = build(), build()
    assert a.portfolio.cash_cents == b.portfolio.cash_cents
    assert [e.cash_delta_cents for e in a.ledger.entries] == [
        e.cash_delta_cents for e in b.ledger.entries
    ]
