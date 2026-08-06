"""PostgreSQL-backed tests for the historical replay adapter (Phase 13).

SQLite cannot stand in for the timestamp (tz-naive columns) and NULL/environment
semantics the adapter depends on, so this seeds a throwaway PostgreSQL database
with production/demo/NULL rows and asserts, through the real loaders, that:

- only production liquidity is admitted; demo and NULL are excluded (not zeroed);
- exact-duplicate books are dropped and same-instant conflicts are rejected;
- zero-quantity trades are excluded as malformed and counted;
- settlement availability differs by timestamp mode (exchange determination time
  vs collector ingestion time);
- the same logical interval produces the same replay result whether the events
  come from the database adapter or an equivalent synthetic fixture.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kalshi_weather.execution.history import (
    HistoryQuery,
    TimestampMode,
    load_historical_replay_data,
)
from kalshi_weather.execution.history_replay import (
    LoadedExport,
    build_history_replay_config,
    run_history_replay,
)
from kalshi_weather.execution.market import MarketData, OrderBook, Trade
from kalshi_weather.execution.models import Action, OrderIntent, OrderType, Side, TimeInForce
from kalshi_weather.execution.policy import ExecutionPolicy, FillMode, RiskConfig
from kalshi_weather.execution.replay import MarketStatusEvent, SettlementEvent
from kalshi_weather.storage.models import (
    Base,
    MarketSnapshot,
    OrderbookSnapshot,
    RawApiPayload,
    TradeRecord,
)

pytestmark = pytest.mark.integration

_TEST_DB = "test_kalshi_history_it"
TK = "KXTEST-26JUL26-T50"


def _with_db(url: str, db: str) -> str:
    return make_url(url).set(database=db).render_as_string(hide_password=False)


@pytest.fixture
def throwaway_db():  # type: ignore[no-untyped-def]
    from kalshi_weather.config import get_settings

    real = str(get_settings().database_url)
    assert make_url(real).database != _TEST_DB
    admin = _with_db(real, "postgres")
    try:
        eng = create_engine(admin, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            c.execute(text("select 1"))
    except Exception:
        pytest.skip("no reachable PostgreSQL server for integration test")
    with eng.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DB}"))
        c.execute(text(f"CREATE DATABASE {_TEST_DB}"))
    try:
        yield _with_db(admin, _TEST_DB)
    finally:
        with eng.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DB}"))


def T(h: int, m: int = 0, s: int = 0, us: int = 0) -> datetime:
    return datetime(2026, 7, 26, h, m, s, us, tzinfo=UTC)


async def _seed(url: str) -> None:
    eng = create_async_engine(url)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as s:
        pay = RawApiPayload(
            source="kalshi",
            endpoint_or_channel="/trades",
            request_key="k",
            received_at=T(20, 5),
            http_status=200,
            content_hash="p1",
            payload_json={},
            environment="production",
        )
        s.add(pay)
        await s.flush()
        # --- order books: 2 distinct prod, 1 exact-dup, 1 conflict pair, demo, null
        s.add_all(
            [
                OrderbookSnapshot(
                    market_ticker=TK,
                    captured_at=T(20, 10),
                    yes_levels_json=[[40, 10]],
                    no_levels_json=[[55, 8]],
                    content_hash="a",
                    environment="production",
                ),
                OrderbookSnapshot(
                    market_ticker=TK,
                    captured_at=T(20, 20),
                    yes_levels_json=[[41, 5]],
                    no_levels_json=[[54, 7]],
                    content_hash="b",
                    environment="production",
                ),
                OrderbookSnapshot(
                    market_ticker=TK,
                    captured_at=T(20, 20),
                    yes_levels_json=[[41, 5]],
                    no_levels_json=[[54, 7]],
                    content_hash="b",
                    environment="production",
                ),  # exact dup
                OrderbookSnapshot(
                    market_ticker=TK,
                    captured_at=T(20, 30),
                    yes_levels_json=[[42, 1]],
                    no_levels_json=[[53, 1]],
                    content_hash="c1",
                    environment="production",
                ),
                OrderbookSnapshot(
                    market_ticker=TK,
                    captured_at=T(20, 30),
                    yes_levels_json=[[43, 9]],
                    no_levels_json=[[52, 9]],
                    content_hash="c2",
                    environment="production",
                ),  # conflict
                OrderbookSnapshot(
                    market_ticker=TK,
                    captured_at=T(20, 40),
                    yes_levels_json=[[40, 10]],
                    no_levels_json=[[55, 8]],
                    content_hash="d",
                    environment="demo",
                ),
                OrderbookSnapshot(
                    market_ticker=TK,
                    captured_at=T(20, 41),
                    yes_levels_json=[[40, 10]],
                    no_levels_json=[[55, 8]],
                    content_hash="e",
                    environment=None,
                ),
            ]
        )
        # --- trades: 2 prod (one zero-qty malformed), demo, null
        s.add_all(
            [
                TradeRecord(
                    trade_id="t1",
                    market_ticker=TK,
                    executed_at=T(20, 15),
                    price_cents=41,
                    count=3,
                    taker_side="yes",
                    raw_payload_id=pay.id,
                    environment="production",
                ),
                TradeRecord(
                    trade_id="t2",
                    market_ticker=TK,
                    executed_at=T(20, 16),
                    price_cents=41,
                    count=0,
                    taker_side="no",
                    raw_payload_id=pay.id,
                    environment="production",
                ),  # malformed
                TradeRecord(
                    trade_id="t3",
                    market_ticker=TK,
                    executed_at=T(20, 17),
                    price_cents=41,
                    count=2,
                    taker_side="yes",
                    raw_payload_id=pay.id,
                    environment="demo",
                ),
                TradeRecord(
                    trade_id="t4",
                    market_ticker=TK,
                    executed_at=T(20, 18),
                    price_cents=41,
                    count=2,
                    taker_side="yes",
                    raw_payload_id=pay.id,
                    environment=None,
                ),
            ]
        )
        # --- market snapshots: active then finalized w/ result 'no';
        #     settlement_ts (determination) tz-NAIVE, observed later (ingestion)
        s.add_all(
            [
                MarketSnapshot(
                    market_ticker=TK,
                    status="active",
                    observed_at=T(20, 5),
                    environment="production",
                ),
                MarketSnapshot(
                    market_ticker=TK,
                    status="finalized",
                    result="no",
                    settlement_ts=datetime(2026, 7, 26, 21, 0, 0),  # tz-naive determination time
                    observed_at=T(22, 30),  # ingestion, ~1.5h later
                    environment="production",
                ),
            ]
        )
        await s.commit()
    await eng.dispose()


def _query(mode: TimestampMode) -> HistoryQuery:
    return HistoryQuery(start=T(0), end=T(23), tickers=(TK,), mode=mode)


@pytest.mark.asyncio
async def test_environment_dedup_conflict_and_malformed(throwaway_db: str) -> None:
    await _seed(throwaway_db)
    eng = create_async_engine(throwaway_db)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with Session() as s:
            data = await load_historical_replay_data(s, _query(TimestampMode.EXCHANGE_TIME))
    finally:
        await eng.dispose()
    tickers_books = {b.ticker for b in data.market_data.order_books}
    # prod books at 20:10 and 20:20 survive; 20:20 dup removed; 20:30 conflict pair rejected;
    # demo + null excluded by environment.
    prices = sorted(b.captured_at.minute for b in data.market_data.order_books)
    assert prices == [10, 20]
    assert data.counts.exact_duplicates_removed >= 1
    assert data.counts.conflicts_rejected == 2  # the 20:30 pair
    assert data.counts.excluded_by_environment >= 2  # demo + null books (+ demo/null trades)
    # trades: only prod non-zero (t1); zero-qty malformed; demo/null excluded
    assert [t.source_ref for t in data.market_data.trades] == ["trades:t1"]
    assert data.quality.malformed_records >= 1
    assert tickers_books == {TK}


@pytest.mark.asyncio
async def test_settlement_available_at_differs_by_mode(throwaway_db: str) -> None:
    await _seed(throwaway_db)
    eng = create_async_engine(throwaway_db)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with Session() as s:
            ex = await load_historical_replay_data(s, _query(TimestampMode.EXCHANGE_TIME))
            ca = await load_historical_replay_data(s, _query(TimestampMode.COLLECTOR_AVAILABLE))
    finally:
        await eng.dispose()
    assert ex.settlements[0].result is Side.NO
    # exchange-time uses the determination time (naive -> UTC); collector-available
    # uses the ingestion time of the finalizing snapshot.
    assert ex.settlements[0].available_at == datetime(2026, 7, 26, 21, 0, tzinfo=UTC)
    assert ca.settlements[0].available_at == T(22, 30)


@pytest.mark.asyncio
async def test_synthetic_and_historical_produce_identical_replay(throwaway_db: str) -> None:
    await _seed(throwaway_db)
    eng = create_async_engine(throwaway_db)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with Session() as s:
            data = await load_historical_replay_data(s, _query(TimestampMode.EXCHANGE_TIME))
    finally:
        await eng.dispose()

    # equivalent synthetic fixture of the SAME admitted events
    synthetic = LoadedExport(
        market_data=MarketData(
            order_books=[
                OrderBook(TK, T(20, 10), yes_asks=((45, 8),), yes_bids=((40, 10),), source_ref="x"),
                OrderBook(TK, T(20, 20), yes_asks=((46, 7),), yes_bids=((41, 5),), source_ref="x"),
            ],
            trades=[
                Trade(
                    TK,
                    T(20, 15),
                    yes_price_cents=41,
                    quantity=3,
                    taker_side=Side.YES,
                    source_ref="x",
                )
            ],
        ),
        settlements=[SettlementEvent(TK, Side.NO, datetime(2026, 7, 26, 21, 0, tzinfo=UTC))],
        market_status=[
            MarketStatusEvent(TK, "open", T(20, 5)),
            MarketStatusEvent(TK, "finalized", T(22, 30)),
        ],
        manifest={},
    )
    hist = LoadedExport(
        market_data=data.market_data,
        settlements=data.settlements,
        market_status=data.market_status,
        manifest={},
    )

    def _run(loaded: LoadedExport):  # type: ignore[no-untyped-def]
        intent = OrderIntent(
            order_id="o",
            strategy_id="s",
            ticker=TK,
            side=Side.YES,
            action=Action.BUY,
            order_type=OrderType.MARKETABLE_LIMIT,
            limit_price_cents=50,
            quantity=5,
            submitted_at=T(20, 25),
            time_in_force=TimeInForce.IOC,
        )
        cfg = build_history_replay_config(
            run_id="t",
            initial_cash_cents=100_00,
            policy=ExecutionPolicy(fill_mode=FillMode.MARKETABLE, max_book_age_seconds=7200),
            risk=RiskConfig(max_book_age_seconds=7200),
            loaded=loaded,
            order_intents=[intent],
        )
        return run_history_replay(cfg)

    sh, hh = _run(synthetic), _run(hist)
    # both fill 5 @ 46 (latest book <= 20:25 has best ask 46), settle NO -> full loss
    assert sh.orders[0].filled_quantity == hh.orders[0].filled_quantity == 5
    assert sh.fills[0].price_cents == hh.fills[0].price_cents == 46
    assert sh.portfolio.cash_cents == hh.portfolio.cash_cents
    assert sh.portfolio.realized_pnl_cents() == hh.portfolio.realized_pnl_cents()


@pytest.mark.asyncio
async def test_coverage_over_postgres_flags_book_gap(throwaway_db: str) -> None:
    # The seeded books span 20:10..20:30 with a same-instant conflict pair at
    # 20:30 (rejected). Add a coverage evaluation over the real DB load: the
    # median/max book gaps are computed from tz-naive-normalized timestamps.
    from kalshi_weather.execution.coverage import ReplayCoveragePolicy, evaluate_coverage

    await _seed(throwaway_db)
    eng = create_async_engine(throwaway_db)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with Session() as s:
            data = await load_historical_replay_data(s, _query(TimestampMode.EXCHANGE_TIME))
    finally:
        await eng.dispose()
    # a tight book-age policy makes the ~10 min gap fatal; a loose one only warns.
    # (max_conflicts raised so the seeded conflict pair doesn't mask the gap gate.)
    tight = evaluate_coverage(
        data, (TK,), ReplayCoveragePolicy(max_individual_book_age_seconds=60, max_conflicts=5)
    )
    loose = evaluate_coverage(
        data, (TK,), ReplayCoveragePolicy(max_individual_book_age_seconds=3600, max_conflicts=5)
    )
    assert tight.verdict.value == "INSUFFICIENT_DATA"
    assert loose.verdict.value in ("HIGH_CONFIDENCE", "LIMITED_CONFIDENCE")
    # conflicts surfaced per-market from the real load
    assert loose.markets[0].conflicts == 2


@pytest.mark.asyncio
async def test_adapter_is_read_only_and_deterministic(throwaway_db: str) -> None:
    await _seed(throwaway_db)
    eng = create_async_engine(throwaway_db)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with Session() as s:
            a = await load_historical_replay_data(s, _query(TimestampMode.COLLECTOR_AVAILABLE))
            b = await load_historical_replay_data(s, _query(TimestampMode.COLLECTOR_AVAILABLE))
    finally:
        await eng.dispose()
    # deterministic across two loads
    assert [x.source_ref for x in a.market_data.order_books] == [
        x.source_ref for x in b.market_data.order_books
    ]
    assert a.counts.to_manifest() == b.counts.to_manifest()
    # source rows unchanged (read-only): re-open and count
    eng2 = create_async_engine(throwaway_db)
    try:
        async with eng2.connect() as c:
            n = await c.scalar(text("select count(*) from orderbook_snapshots"))
    finally:
        await eng2.dispose()
    assert n == 7  # all seeded rows still present; nothing mutated
