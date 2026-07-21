import asyncio

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.config import Environment
from kalshi_weather.ingestion.collector import run_collection_cycle, run_collector_loop
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.storage.models import Base

EVENT_TICKER = "KXHIGHNY-26JUL21"
GOOD_TICKER = "KXHIGHNY-26JUL21-T85"
BAD_ORDERBOOK_TICKER = "KXHIGHNY-26JUL21-T90"

TRADE_ID = "trade-1"


@pytest.fixture
async def session():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _market(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "event_ticker": EVENT_TICKER,
        "market_type": "binary",
        "title": "t",
        "subtitle": "s",
        "status": "open",
        "yes_bid": 42,
        "yes_ask": 47,
        "volume": 10,
        "open_interest": 5,
        "rules_primary": "rules",
    }


def make_handler(trade_min_ts_seen: list[int | None]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)

        if path.endswith("/series"):
            return httpx.Response(
                200,
                json={
                    "series": [
                        {"ticker": "KXHIGHNY", "category": "Climate and Weather", "title": "t"}
                    ],
                    "cursor": "",
                },
            )
        if path.endswith("/events"):
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "event_ticker": EVENT_TICKER,
                            "series_ticker": "KXHIGHNY",
                            "category": "Climate and Weather",
                            "title": "t",
                        }
                    ],
                    "cursor": "",
                },
            )
        if path.endswith("/markets"):
            return httpx.Response(
                200,
                json={
                    "markets": [_market(GOOD_TICKER), _market(BAD_ORDERBOOK_TICKER)],
                    "cursor": "",
                },
            )
        if path.endswith(f"/markets/{GOOD_TICKER}/orderbook"):
            return httpx.Response(
                200, json={"orderbook_fp": {"yes_dollars": [["0.4200", "2.00"]], "no_dollars": []}}
            )
        if path.endswith(f"/markets/{BAD_ORDERBOOK_TICKER}/orderbook"):
            # malformed: yes_dollars is not a list of [price, qty] pairs
            return httpx.Response(
                200, json={"orderbook_fp": {"yes_dollars": "broken", "no_dollars": []}}
            )
        if path.endswith("/markets/trades"):
            trade_min_ts_seen.append(int(params["min_ts"]) if "min_ts" in params else None)
            return httpx.Response(
                200,
                json={
                    "trades": [
                        {
                            # trade_id is per-ticker (matching real Kalshi trade_id
                            # uniqueness) so each market's dedup/min_ts behavior can
                            # be verified independently.
                            "trade_id": f"{TRADE_ID}-{params.get('ticker', GOOD_TICKER)}",
                            "ticker": params.get("ticker", GOOD_TICKER),
                            "created_time": "2026-07-20T12:00:00Z",
                            "yes_price_dollars": "0.4200",
                            "no_price_dollars": "0.5800",
                            "count_fp": "2.00",
                            "taker_side": "yes",
                        }
                    ],
                    "cursor": "",
                },
            )
        raise AssertionError(f"unexpected path in collector test: {path}")

    return handler


def _client(handler) -> KalshiClient:  # type: ignore[no-untyped-def]
    return KalshiClient(
        base_url="https://example.invalid/trade-api/v2",
        environment=Environment.DEVELOPMENT,
        transport=httpx.MockTransport(handler),
    )


async def test_run_collection_cycle_saves_markets_orderbooks_and_trades(
    session: AsyncSession,
) -> None:
    seen_min_ts: list[int | None] = []
    async with _client(make_handler(seen_min_ts)) as client:
        stats = await run_collection_cycle(
            client, session, category="Climate and Weather", market_status="open"
        )

    assert stats.markets_discovered == 2
    assert stats.market_snapshots_saved == 2
    # GOOD_TICKER's orderbook succeeds; BAD_ORDERBOOK_TICKER's fails and is isolated
    assert stats.orderbooks_saved == 1
    assert stats.errors == 1
    # one distinct trade per ticker (2 tickers), both saved as new
    assert stats.trades_saved == 2
    assert stats.trades_duplicate == 0
    # first cycle: no prior trades stored, so min_ts must be absent both times
    assert seen_min_ts == [None, None]


async def test_run_collection_cycle_second_run_dedupes_and_uses_incremental_min_ts(
    session: AsyncSession,
) -> None:
    seen_min_ts: list[int | None] = []
    handler = make_handler(seen_min_ts)

    async with _client(handler) as client:
        await run_collection_cycle(
            client, session, category="Climate and Weather", market_status="open"
        )
    async with _client(handler) as client:
        stats = await run_collection_cycle(
            client, session, category="Climate and Weather", market_status="open"
        )

    assert stats.market_snapshots_duplicate == 2  # nothing changed since cycle 1
    assert stats.orderbooks_duplicate == 1  # GOOD_TICKER's book is unchanged
    # both tickers' trades were already stored in cycle 1 -- same trade_id comes
    # back and is correctly deduped, not double-counted
    assert stats.trades_saved == 0
    assert stats.trades_duplicate == 2
    # second cycle's trade fetches should now pass a min_ts derived from stored trades
    assert seen_min_ts[2] is not None
    assert seen_min_ts[3] is not None


async def test_run_collector_loop_respects_max_cycles() -> None:
    call_count = 0

    def client_factory(_session: AsyncSession) -> KalshiClient:
        nonlocal call_count
        call_count += 1
        return _client(make_handler([]))

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    stop_event = asyncio.Event()
    await run_collector_loop(
        session_factory=session_factory,
        client_factory=client_factory,
        category="Climate and Weather",
        market_status="open",
        interval_seconds=0,
        stop_event=stop_event,
        max_cycles=2,
    )
    await engine.dispose()

    assert call_count == 2


async def test_run_collector_loop_honors_already_set_stop_event() -> None:
    call_count = 0

    def client_factory(_session: AsyncSession) -> KalshiClient:
        nonlocal call_count
        call_count += 1
        return _client(make_handler([]))

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    stop_event = asyncio.Event()
    stop_event.set()
    await run_collector_loop(
        session_factory=session_factory,
        client_factory=client_factory,
        category="Climate and Weather",
        market_status="open",
        interval_seconds=60,
        stop_event=stop_event,
        max_cycles=None,
    )
    await engine.dispose()

    assert call_count == 0


async def test_run_collector_loop_records_run_metrics() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    await run_collector_loop(
        session_factory=session_factory,
        client_factory=lambda _s: _client(make_handler([])),
        category="Climate and Weather",
        market_status="open",
        interval_seconds=0,
        stop_event=asyncio.Event(),
        max_cycles=2,
    )

    from sqlalchemy import select

    from kalshi_weather.storage.models import CollectorRun

    async with session_factory() as session:
        runs = (await session.scalars(select(CollectorRun))).all()
    await engine.dispose()

    assert len(runs) == 2
    assert all(r.collector == "kalshi" for r in runs)
    assert all(r.success for r in runs)
    assert all(r.requests_attempted > 0 for r in runs)
    assert runs[0].stats_json["markets_discovered"] == 2
