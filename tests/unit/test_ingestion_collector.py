import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.config import Environment
from kalshi_weather.domain.time import utc_now
from kalshi_weather.ingestion.collector import run_collection_cycle, run_collector_loop
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.kalshi.pagination import PaginationSafetyError
from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import get_latest_trade_timestamp

EVENT_TICKER = "KXHIGHNY-26JUL21"
GOOD_TICKER = "KXHIGHNY-26JUL21-T85"
BAD_ORDERBOOK_TICKER = "KXHIGHNY-26JUL21-T90"

TRADE_ID = "trade-1"

DEFAULT_LOOKBACK_DAYS = 30

# A real Climate & Weather market observed to have a very large trade history
# (>100,000 trades) -- the exact scenario that deadlocked collection before
# this fix (see docs/adr/0002-ingestion-collector.md decision 9). No
# category/ticker special-casing exists in the fix itself; this ticker is
# only used to model that scenario in tests.
HIGH_HISTORY_TICKER = "KXERUPTSUPER-0-50JAN01"
HIGH_HISTORY_EVENT_TICKER = "KXERUPTSUPER-0-50JAN01EV"


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


def make_high_history_handler():  # type: ignore[no-untyped-def]
    """Models a market whose full trade history is far larger than
    `paginate()`'s `max_pages` guard permits: an unbounded (`min_ts=None`)
    fetch never terminates within the guard, while a bounded, recent-only
    fetch (what bootstrap now requests) returns a single small page."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)

        if path.endswith("/series"):
            return httpx.Response(
                200,
                json={
                    "series": [
                        {
                            "ticker": "KXERUPTSUPER",
                            "category": "Climate and Weather",
                            "title": "t",
                        }
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
                            "event_ticker": HIGH_HISTORY_EVENT_TICKER,
                            "series_ticker": "KXERUPTSUPER",
                            "category": "Climate and Weather",
                            "title": "t",
                        }
                    ],
                    "cursor": "",
                },
            )
        if path.endswith("/markets"):
            return httpx.Response(
                200, json={"markets": [_market(HIGH_HISTORY_TICKER)], "cursor": ""}
            )
        if path.endswith(f"/markets/{HIGH_HISTORY_TICKER}/orderbook"):
            return httpx.Response(
                200, json={"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
            )
        if path.endswith("/markets/trades"):
            if "min_ts" not in params:
                # Simulates a market with >100,000 trades: every page returns
                # a new item and a fresh cursor, so an unbounded fetch never
                # naturally terminates -- this is exactly what previously
                # deadlocked collection (paginate() aborts at max_pages,
                # discarding every trade already fetched, so no checkpoint is
                # ever established).
                cursor = params.get("cursor")
                page_number = int(cursor.removeprefix("page-")) + 1 if cursor else 1
                return httpx.Response(
                    200,
                    json={
                        "trades": [
                            {
                                "trade_id": f"deep-history-{page_number}",
                                "ticker": HIGH_HISTORY_TICKER,
                                "created_time": "2020-01-01T00:00:00Z",
                                "yes_price_dollars": "0.5000",
                                "no_price_dollars": "0.5000",
                                "count_fp": "1.00",
                                "taker_side": "yes",
                            }
                        ],
                        "cursor": f"page-{page_number}",
                    },
                )
            # Bootstrap-bounded fetch: only recent trades exist in this
            # window, so it terminates immediately.
            return httpx.Response(
                200,
                json={
                    "trades": [
                        {
                            "trade_id": "recent-1",
                            "ticker": HIGH_HISTORY_TICKER,
                            "created_time": "2026-07-15T00:00:00Z",
                            "yes_price_dollars": "0.5000",
                            "no_price_dollars": "0.5000",
                            "count_fp": "1.00",
                            "taker_side": "yes",
                        }
                    ],
                    "cursor": "",
                },
            )
        raise AssertionError(f"unexpected path in high-history collector test: {path}")

    return handler


def _client(handler) -> KalshiClient:  # type: ignore[no-untyped-def]
    return KalshiClient(
        base_url="https://example.invalid/trade-api/v2",
        environment=Environment.DEVELOPMENT,
        transport=httpx.MockTransport(handler),
        # Request throttling is exercised elsewhere (test_kalshi_client.py);
        # disabled here so the max-pages regression test (~1000 requests) runs
        # fast instead of taking ~100s against the real default spacing.
        min_request_interval_seconds=0.0,
    )


async def test_run_collection_cycle_saves_markets_orderbooks_and_trades(
    session: AsyncSession,
) -> None:
    seen_min_ts: list[int | None] = []
    async with _client(make_handler(seen_min_ts)) as client:
        stats = await run_collection_cycle(
            client,
            session,
            category="Climate and Weather",
            market_status="open",
            trade_bootstrap_lookback_days=DEFAULT_LOOKBACK_DAYS,
        )

    assert stats.markets_discovered == 2
    assert stats.market_snapshots_saved == 2
    # GOOD_TICKER's orderbook succeeds; BAD_ORDERBOOK_TICKER's fails and is isolated
    assert stats.orderbooks_saved == 1
    assert stats.errors == 1
    # one distinct trade per ticker (2 tickers), both saved as new
    assert stats.trades_saved == 2
    assert stats.trades_duplicate == 0
    # first cycle: no prior trades stored for either ticker, so both fetches
    # go through the bounded bootstrap path, never a full-history (None) fetch
    assert stats.trades_bootstrapped == 2
    assert seen_min_ts == [seen_min_ts[0], seen_min_ts[1]]
    assert all(min_ts is not None for min_ts in seen_min_ts)


async def test_run_collection_cycle_second_run_dedupes_and_uses_incremental_min_ts(
    session: AsyncSession,
) -> None:
    seen_min_ts: list[int | None] = []
    handler = make_handler(seen_min_ts)

    async with _client(handler) as client:
        first_stats = await run_collection_cycle(
            client,
            session,
            category="Climate and Weather",
            market_status="open",
            trade_bootstrap_lookback_days=DEFAULT_LOOKBACK_DAYS,
        )
    async with _client(handler) as client:
        stats = await run_collection_cycle(
            client,
            session,
            category="Climate and Weather",
            market_status="open",
            trade_bootstrap_lookback_days=DEFAULT_LOOKBACK_DAYS,
        )

    assert first_stats.trades_bootstrapped == 2
    assert stats.market_snapshots_duplicate == 2  # nothing changed since cycle 1
    assert stats.orderbooks_duplicate == 1  # GOOD_TICKER's book is unchanged
    # both tickers' trades were already stored in cycle 1 -- same trade_id comes
    # back and is correctly deduped, not double-counted
    assert stats.trades_saved == 0
    assert stats.trades_duplicate == 2
    # second cycle has a checkpoint for both tickers, so it's fully
    # incremental again -- no bootstrap path taken.
    assert stats.trades_bootstrapped == 0
    # second cycle's trade fetches should now pass a min_ts derived from stored trades
    assert seen_min_ts[2] is not None
    assert seen_min_ts[3] is not None


async def test_bootstrap_min_ts_reflects_configured_lookback_window(
    session: AsyncSession,
) -> None:
    """Bootstrap window is respected: the min_ts requested for an
    un-checkpointed ticker is `now - lookback_days`, configurable via
    `trade_bootstrap_lookback_days` -- not the market's entire history."""
    seen_min_ts: list[int | None] = []
    lookback_days = 7

    before = utc_now()
    async with _client(make_handler(seen_min_ts)) as client:
        stats = await run_collection_cycle(
            client,
            session,
            category="Climate and Weather",
            market_status="open",
            trade_bootstrap_lookback_days=lookback_days,
        )
    after = utc_now()

    assert stats.trades_bootstrapped == 2
    expected_floor = int((before - timedelta(days=lookback_days)).timestamp())
    expected_ceiling = int((after - timedelta(days=lookback_days)).timestamp())
    for min_ts in seen_min_ts:
        assert min_ts is not None
        assert expected_floor <= min_ts <= expected_ceiling


async def test_direct_full_history_fetch_hits_max_pages_safety_guard() -> None:
    """Documents the exact deadlock this fix avoids: an unbounded
    (`min_ts=None`) fetch against a market with a very large trade history
    still aborts at `paginate()`'s `max_pages` guard -- that guard is
    untouched by this fix. The collector itself never triggers this anymore
    for a ticker with no checkpoint (see the next test)."""
    async with _client(make_high_history_handler()) as client:
        with pytest.raises(PaginationSafetyError):
            await client.list_trades(ticker=HIGH_HISTORY_TICKER, min_ts=None)


async def test_high_history_market_bootstraps_instead_of_full_history(
    session: AsyncSession,
) -> None:
    """A newly discovered market with a very large trade history (the
    reported deadlock scenario) is bootstrapped, not fetched in full: trades
    are saved, a checkpoint is established, and no error occurs."""
    async with _client(make_high_history_handler()) as client:
        stats = await run_collection_cycle(
            client,
            session,
            category="Climate and Weather",
            market_status="open",
            trade_bootstrap_lookback_days=DEFAULT_LOOKBACK_DAYS,
        )

    assert stats.errors == 0
    assert stats.trades_bootstrapped == 1
    assert stats.trades_saved == 1
    assert stats.trades_duplicate == 0

    # the checkpoint is now established, so every future cycle is a normal
    # incremental fetch rather than repeating the deadlocked full-history fetch.
    latest = await get_latest_trade_timestamp(session, HIGH_HISTORY_TICKER)
    assert latest is not None


async def test_repeated_cycles_after_bootstrap_are_idempotent(session: AsyncSession) -> None:
    """Deterministic reruns: bootstrapping happens exactly once per ticker;
    every subsequent identical rerun is a stable, deduped, incremental fetch."""
    handler = make_handler([])

    async with _client(handler) as client:
        first = await run_collection_cycle(
            client,
            session,
            category="Climate and Weather",
            market_status="open",
            trade_bootstrap_lookback_days=DEFAULT_LOOKBACK_DAYS,
        )
    async with _client(handler) as client:
        second = await run_collection_cycle(
            client,
            session,
            category="Climate and Weather",
            market_status="open",
            trade_bootstrap_lookback_days=DEFAULT_LOOKBACK_DAYS,
        )
    async with _client(handler) as client:
        third = await run_collection_cycle(
            client,
            session,
            category="Climate and Weather",
            market_status="open",
            trade_bootstrap_lookback_days=DEFAULT_LOOKBACK_DAYS,
        )

    assert first.trades_bootstrapped == 2
    assert first.trades_saved == 2
    assert second.trades_bootstrapped == 0
    assert third.trades_bootstrapped == 0
    assert second.trades_saved == 0
    assert third.trades_saved == 0
    assert second.trades_duplicate == third.trades_duplicate == 2


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
        trade_bootstrap_lookback_days=DEFAULT_LOOKBACK_DAYS,
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
        trade_bootstrap_lookback_days=DEFAULT_LOOKBACK_DAYS,
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
        trade_bootstrap_lookback_days=DEFAULT_LOOKBACK_DAYS,
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
