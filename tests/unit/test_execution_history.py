"""Unit tests for the historical replay adapter (no database).

Covers pure conversion (book normalization, environment policy, query
validation), market-status gating, and export/import round-trip determinism.
PostgreSQL timestamp/NULL semantics and synthetic<->historical equivalence are
covered in tests/integration/test_history_adapter_postgres.py.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshi_weather.execution.history import (
    ExclusionCounts,
    HistoricalReplayData,
    HistoryEnvironmentPolicy,
    HistoryQuery,
    QualityReport,
    normalize_book,
)
from kalshi_weather.execution.history_replay import (
    build_history_replay_config,
    export_replay_data,
    load_exported_data,
    run_history_replay,
)
from kalshi_weather.execution.market import MarketData, OrderBook, Trade
from kalshi_weather.execution.models import (
    Action,
    OrderIntent,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
)
from kalshi_weather.execution.policy import ExecutionPolicy, FillMode, RiskConfig
from kalshi_weather.execution.replay import MarketStatusEvent, SettlementEvent
from kalshi_weather.storage.models import OrderbookSnapshot


def T(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 7, 26, h, m, s, tzinfo=UTC)


# --- book normalization ----------------------------------------------------


def test_normalize_book_yes_no_transform() -> None:
    row = OrderbookSnapshot(
        id=7,
        market_ticker="M",
        captured_at=T(20),
        yes_levels_json=[[3, 100], [2, 50]],  # YES bids
        no_levels_json=[[1, 2], [2, 3001]],  # NO bids -> YES asks at 100-p
    )
    book = normalize_book(row)
    assert book.yes_bids == ((3, 100), (2, 50))
    assert book.yes_asks == ((99, 2), (98, 3001))  # (100 - no_price)
    assert book.source_ref == "orderbook_snapshots:7"
    # best derived quote is consistent with the simulator's own book math
    assert book.best_yes_bid() == 3
    assert book.best_yes_ask() == 98


def test_normalize_naive_timestamp_becomes_utc() -> None:
    row = OrderbookSnapshot(
        id=1,
        market_ticker="M",
        captured_at=datetime(2026, 7, 26, 20),
        yes_levels_json=[],
        no_levels_json=[],
    )
    assert normalize_book(row).captured_at.tzinfo is UTC


# --- environment policy ----------------------------------------------------


def test_environment_policy_production_only_by_default() -> None:
    p = HistoryEnvironmentPolicy()
    assert p.admits("production", data_type="trades") is True
    assert p.admits("demo", data_type="trades") is False
    assert p.admits(None, data_type="trades") is False
    assert p.admits("unknown", data_type="trades") is False


def test_environment_policy_null_allowed_only_when_explicit() -> None:
    p = HistoryEnvironmentPolicy(allow_null_for=frozenset({"market_status"}))
    assert p.admits(None, data_type="market_status") is True
    assert p.admits(None, data_type="trades") is False
    assert p.admits("demo", data_type="market_status") is False  # demo still rejected


# --- query validation ------------------------------------------------------


def test_query_requires_tickers_and_valid_range() -> None:
    with pytest.raises(ValueError):
        HistoryQuery(start=T(1), end=T(2), tickers=())
    with pytest.raises(ValueError):
        HistoryQuery(start=T(2), end=T(1), tickers=("M",))


# --- market-status gating (via the single replay engine) -------------------


def _oi(oid: str, tk: str, at: datetime, price: int = 50, qty: int = 1) -> OrderIntent:
    return OrderIntent(
        order_id=oid,
        strategy_id="s",
        ticker=tk,
        side=Side.YES,
        action=Action.BUY,
        order_type=OrderType.MARKETABLE_LIMIT,
        limit_price_cents=price,
        quantity=qty,
        submitted_at=at,
        time_in_force=TimeInForce.IOC,
    )


def _loaded(md: MarketData, settlements=None, status=None):  # type: ignore[no-untyped-def]
    from kalshi_weather.execution.history_replay import LoadedExport

    return LoadedExport(
        market_data=md,
        settlements=settlements or [],
        market_status=status or [],
        manifest={"export_id": "test"},
    )


def _cfg(loaded, intents):  # type: ignore[no-untyped-def]
    return build_history_replay_config(
        run_id="t",
        initial_cash_cents=100_00,
        policy=ExecutionPolicy(fill_mode=FillMode.MARKETABLE, max_book_age_seconds=7200),
        risk=RiskConfig(max_book_age_seconds=7200),
        loaded=loaded,
        order_intents=intents,
    )


def test_order_before_open_is_rejected_market_not_open() -> None:
    book = OrderBook(ticker="M", captured_at=T(9), yes_asks=((40, 10),))
    status = [MarketStatusEvent("M", "open", T(10)), MarketStatusEvent("M", "closed", T(12))]
    loaded = _loaded(MarketData(order_books=[book]), status=status)
    sim = run_history_replay(_cfg(loaded, [_oi("early", "M", T(9, 30), price=40)]))
    assert sim.orders[0].state is OrderState.REJECTED
    assert sim.orders[0].reject_reason == "market_not_open"


def test_order_after_close_is_rejected() -> None:
    book = OrderBook(ticker="M", captured_at=T(11), yes_asks=((40, 10),))
    status = [MarketStatusEvent("M", "open", T(10)), MarketStatusEvent("M", "closed", T(12))]
    loaded = _loaded(MarketData(order_books=[book]), status=status)
    sim = run_history_replay(_cfg(loaded, [_oi("late", "M", T(13), price=40)]))
    assert sim.orders[0].reject_reason == "market_not_open"


def test_order_in_open_window_fills() -> None:
    book = OrderBook(ticker="M", captured_at=T(10, 30), yes_asks=((40, 10),))
    status = [MarketStatusEvent("M", "open", T(10)), MarketStatusEvent("M", "closed", T(12))]
    loaded = _loaded(MarketData(order_books=[book]), status=status)
    sim = run_history_replay(_cfg(loaded, [_oi("ok", "M", T(11), price=40, qty=3)]))
    assert sim.orders[0].state is OrderState.FILLED and sim.orders[0].filled_quantity == 3


def test_no_status_data_means_no_gating() -> None:
    # Without status events for a ticker the window is unknown, not closed.
    book = OrderBook(ticker="M", captured_at=T(10), yes_asks=((40, 10),))
    loaded = _loaded(MarketData(order_books=[book]))
    sim = run_history_replay(_cfg(loaded, [_oi("ok", "M", T(11), price=40)]))
    assert sim.orders[0].state is OrderState.FILLED


# --- export/import round-trip + determinism --------------------------------


def _sample_data() -> HistoricalReplayData:
    md = MarketData(
        order_books=[
            OrderBook(
                "M",
                T(10),
                yes_asks=((40, 10),),
                yes_bids=((38, 5),),
                source_ref="orderbook_snapshots:1",
            )
        ],
        trades=[
            Trade(
                "M",
                T(11),
                yes_price_cents=41,
                quantity=7,
                taker_side=Side.YES,
                source_ref="trades:t1",
            )
        ],
    )
    return HistoricalReplayData(
        market_data=md,
        settlements=[SettlementEvent("M", Side.YES, T(18))],
        market_status=[
            MarketStatusEvent("M", "open", T(9)),
            MarketStatusEvent("M", "closed", T(12)),
        ],
        counts=ExclusionCounts(source_rows_read=3, events_emitted=4),
        quality=QualityReport(order_book_events=1, trade_events=1),
    )


def test_export_is_deterministic_and_refuses_overwrite(tmp_path: Path) -> None:
    data = _sample_data()
    q = HistoryQuery(start=T(0), end=T(23), tickers=("M",))
    m1 = export_replay_data(q, data, tmp_path / "a")
    m2 = export_replay_data(q, data, tmp_path / "b")
    assert m1["content_hashes"] == m2["content_hashes"]  # deterministic content hashes
    with pytest.raises(FileExistsError):
        export_replay_data(q, data, tmp_path / "a")  # immutable


def test_export_import_round_trip_preserves_events(tmp_path: Path) -> None:
    data = _sample_data()
    q = HistoryQuery(start=T(0), end=T(23), tickers=("M",))
    export_replay_data(q, data, tmp_path / "e")
    loaded = load_exported_data(tmp_path / "e")
    assert len(loaded.market_data.order_books) == 1
    b = loaded.market_data.order_books[0]
    assert b.yes_asks == ((40, 10),) and b.yes_bids == ((38, 5),)
    assert loaded.market_data.trades[0].taker_side is Side.YES
    assert loaded.settlements[0].result is Side.YES
    assert [(s.ticker, s.state) for s in loaded.market_status] == [("M", "open"), ("M", "closed")]


def test_full_history_replay_reconciles(tmp_path: Path) -> None:
    data = _sample_data()
    q = HistoryQuery(start=T(0), end=T(23), tickers=("M",))
    export_replay_data(q, data, tmp_path / "e")
    loaded = load_exported_data(tmp_path / "e")
    sim = run_history_replay(_cfg(loaded, [_oi("b", "M", T(10, 30), price=40, qty=5)]))
    # filled 5 @ 40, settled YES -> payout 500, realized 300
    assert sim.orders[0].filled_quantity == 5
    from kalshi_weather.execution.ledger import replay

    rp = replay(100_00, sim.ledger.entries)
    assert rp.cash_cents == sim.portfolio.cash_cents
    assert sim.portfolio.realized_pnl_cents() == 300
