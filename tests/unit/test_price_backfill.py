"""Tests for the historical price (candlestick) backfill job (Phase 7A)."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.config import Environment
from kalshi_weather.ingestion.price_backfill import (
    MarketOutcome,
    run_price_backfill,
    select_backfill_candidates,
)
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.storage.database import session_scope
from kalshi_weather.storage.models import Base, MarketCandlestick
from kalshi_weather.storage.repositories import save_event, save_market_snapshot

OLD_TICKER = "KXHIGHNY-26MAY15-B65.5"  # expired: 404 from candlesticks
GOOD_TICKER = "KXHIGHNY-26JUL14-B94.5"
QUIET_TICKER = "KXLOWTNYC-26JUL10-T60"  # 200 OK, zero candles
BROKEN_TICKER = "KXHIGHNY-26JUL16-T80"  # 500 -> exhausted retries -> api_failure


def _candle_payload(
    end_ts: int, *, close_dollars: str | None = "0.3000", volume: str = "10.00"
) -> dict[str, Any]:
    price: dict[str, str] = {"open_dollars": "0.2800", "high_dollars": "0.3000",
                              "low_dollars": "0.2600", "previous_dollars": "0.2800"}
    if close_dollars is not None:
        price["close_dollars"] = close_dollars
    return {
        "end_period_ts": end_ts,
        "volume_fp": volume,
        "open_interest_fp": "100.00",
        "price": price,
        "yes_bid": {"open_dollars": "0.2700", "high_dollars": "0.2800",
                    "low_dollars": "0.2500", "close_dollars": "0.2700"},
        "yes_ask": {"open_dollars": "0.3100", "high_dollars": "0.3100",
                    "low_dollars": "0.2600", "close_dollars": "0.3000"},
    }


def make_handler(*, fail_broken_permanently: bool = True):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if OLD_TICKER in path:
            return httpx.Response(404, json={"error": {"code": "not_found"}})
        if BROKEN_TICKER in path:
            return httpx.Response(500, json={"error": {"code": "internal"}})
        if QUIET_TICKER in path:
            return httpx.Response(200, json={"candlesticks": [], "ticker": QUIET_TICKER})
        if GOOD_TICKER in path:
            close_ts = int(datetime(2026, 7, 15, 4, 59, tzinfo=UTC).timestamp())
            all_candles = [
                _candle_payload(close_ts - 120, close_dollars="0.2500"),
                _candle_payload(close_ts - 60, close_dollars=None, volume="0.00"),
                _candle_payload(close_ts, close_dollars="0.9900"),
            ]
            # Realistic chunking behavior: only return candles whose
            # end_period_ts falls within the requested [start_ts, end_ts] --
            # a fixed-output mock here would double-count candles once the
            # lookback window spans >1 chunk (it does, at period_interval=1).
            params = dict(request.url.params)
            start_ts, end_ts = int(params["start_ts"]), int(params["end_ts"])
            in_range = [c for c in all_candles if start_ts <= c["end_period_ts"] <= end_ts]
            return httpx.Response(200, json={"candlesticks": in_range, "ticker": GOOD_TICKER})
        raise AssertionError(f"unexpected path {path}")

    return handler


def _client(handler) -> KalshiClient:  # type: ignore[no-untyped-def]
    return KalshiClient(
        base_url="https://example.invalid/trade-api/v2",
        environment=Environment.DEVELOPMENT,
        transport=httpx.MockTransport(handler),
        min_request_interval_seconds=0,
        max_retries=1,
    )


@pytest.fixture
async def session_factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_market(
    session: AsyncSession, ticker: str, *, close_time: datetime, event_ticker: str | None = None
) -> None:
    event_ticker = event_ticker or ticker.rsplit("-", 1)[0]
    await save_event(
        session,
        event_ticker=event_ticker,
        series_ticker=event_ticker.split("-")[0],
        category="Climate and Weather",
        title="t",
        sub_title=None,
    )
    await save_market_snapshot(
        session,
        market_ticker=ticker,
        event_ticker=event_ticker,
        market_type="binary",
        title="t",
        subtitle="s",
        status="finalized",
        yes_bid_cents=0,
        yes_ask_cents=1,
        last_price_cents=0,
        volume=10,
        open_interest=5,
        close_time=close_time,
        rules_primary="rules",
        rules_secondary=None,
        raw_payload_id=None,
        result="no",
        expiration_value=None,
    )


async def test_select_backfill_candidates_orders_oldest_first(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as session:
        await _seed_market(session, "T-NEW", close_time=datetime(2026, 7, 20, tzinfo=UTC))
        await _seed_market(session, "T-OLD", close_time=datetime(2026, 5, 16, tzinfo=UTC))
        await _seed_market(session, "T-MID", close_time=datetime(2026, 6, 1, tzinfo=UTC))

    async with session_scope(session_factory) as session:
        candidates = await select_backfill_candidates(session)
    assert [c.market_ticker for c in candidates] == ["T-OLD", "T-MID", "T-NEW"]


async def test_select_backfill_candidates_excludes_unsettled(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as session:
        await save_event(
            session, event_ticker="T-OPEN-E", series_ticker="T", category="c", title="t",
            sub_title=None,
        )
        await save_market_snapshot(
            session, market_ticker="T-OPEN", event_ticker="T-OPEN-E", market_type="binary",
            title="t", subtitle="s", status="open", yes_bid_cents=1, yes_ask_cents=2,
            last_price_cents=1, volume=1, open_interest=1, close_time=None,
            rules_primary="r", rules_secondary=None, raw_payload_id=None,
        )
    async with session_scope(session_factory) as session:
        assert await select_backfill_candidates(session) == []


async def test_backfill_classifies_each_outcome_correctly(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as session:
        await _seed_market(
            session, OLD_TICKER, close_time=datetime(2026, 5, 16, 4, 59, tzinfo=UTC)
        )
        await _seed_market(
            session, GOOD_TICKER, close_time=datetime(2026, 7, 15, 4, 59, tzinfo=UTC)
        )
        await _seed_market(
            session, QUIET_TICKER, close_time=datetime(2026, 7, 11, tzinfo=UTC)
        )
        await _seed_market(
            session, BROKEN_TICKER, close_time=datetime(2026, 7, 17, tzinfo=UTC)
        )

    client = _client(make_handler())
    async with client:
        report = await run_price_backfill(
            session_factory=session_factory, client=client, resolution_minutes=1
        )

    by_ticker = {r.market_ticker: r for r in report.results}
    assert by_ticker[OLD_TICKER].outcome is MarketOutcome.EXPIRED
    assert by_ticker[GOOD_TICKER].outcome is MarketOutcome.COMPLETE
    assert by_ticker[GOOD_TICKER].saved == 3
    assert by_ticker[QUIET_TICKER].outcome is MarketOutcome.NO_PRICE_DATA
    assert by_ticker[BROKEN_TICKER].outcome is MarketOutcome.API_FAILURE
    # a genuine API failure makes the run "not ok"; the others don't
    assert report.ok is False
    assert len(report.failed) == 1


async def test_backfill_never_aborts_on_one_markets_failure(session_factory) -> None:  # type: ignore[no-untyped-def]
    """All four markets (including the broken one) must appear in the
    report -- a failure must never silently drop a market from the run."""
    async with session_scope(session_factory) as session:
        for ticker, close in (
            (OLD_TICKER, datetime(2026, 5, 16, 4, 59, tzinfo=UTC)),
            (BROKEN_TICKER, datetime(2026, 7, 17, tzinfo=UTC)),
            (GOOD_TICKER, datetime(2026, 7, 15, 4, 59, tzinfo=UTC)),
        ):
            await _seed_market(session, ticker, close_time=close)

    client = _client(make_handler())
    async with client:
        report = await run_price_backfill(
            session_factory=session_factory, client=client, resolution_minutes=1
        )
    assert len(report.results) == 3


async def test_backfill_stores_carried_forward_flag_for_zero_volume_period(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    async with session_scope(session_factory) as session:
        await _seed_market(

            session, GOOD_TICKER, close_time=datetime(2026, 7, 15, 4, 59, tzinfo=UTC)

        )

    client = _client(make_handler())
    async with client:
        await run_price_backfill(
            session_factory=session_factory, client=client, resolution_minutes=1
        )

    async with session_scope(session_factory) as session:
        rows = (
            await session.scalars(
                select(MarketCandlestick)
                .where(MarketCandlestick.market_ticker == GOOD_TICKER)
                .order_by(MarketCandlestick.period_end)
            )
        ).all()
    assert len(rows) == 3
    assert rows[0].volume == 10 and rows[0].price_close_is_carried_forward is False
    # the zero-volume middle candle: close_cents falls back to previous_cents
    assert rows[1].volume == 0
    assert rows[1].price_close_is_carried_forward is True
    assert rows[1].price_close_cents == 28  # previous_dollars = 0.2800
    # yes_bid/yes_ask remain fully populated even with zero volume
    assert rows[1].yes_bid_close_cents == 27
    assert rows[1].yes_ask_close_cents == 30


async def test_backfill_is_idempotent_on_rerun(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as session:
        await _seed_market(

            session, GOOD_TICKER, close_time=datetime(2026, 7, 15, 4, 59, tzinfo=UTC)

        )

    async with _client(make_handler()) as client:
        first = await run_price_backfill(
            session_factory=session_factory, client=client, resolution_minutes=1
        )
    assert first.totals()["saved"] == 3

    async with _client(make_handler()) as client:
        second = await run_price_backfill(
            session_factory=session_factory,
            client=client,
            resolution_minutes=1,
            skip_covered=False,  # force refetch: must dedupe, not duplicate
        )
    assert second.totals()["saved"] == 0
    assert second.totals()["duplicate"] == 3

    # exactly 3 rows total in storage (no duplicates created)
    async with session_scope(session_factory) as session:
        rows = (
            await session.scalars(
                select(MarketCandlestick).where(MarketCandlestick.market_ticker == GOOD_TICKER)
            )
        ).all()
    assert len(rows) == 3


async def test_backfill_resumes_via_skip_covered(session_factory) -> None:  # type: ignore[no-untyped-def]
    """A completed market is skipped on the next run (default skip_covered);
    this is what makes an interrupted multi-market backfill cheap to resume."""
    async with session_scope(session_factory) as session:
        await _seed_market(

            session, GOOD_TICKER, close_time=datetime(2026, 7, 15, 4, 59, tzinfo=UTC)

        )

    async with _client(make_handler()) as client:
        await run_price_backfill(
            session_factory=session_factory, client=client, resolution_minutes=1
        )

    call_count = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return make_handler()(request)

    async with _client(counting_handler) as client:
        report = await run_price_backfill(
            session_factory=session_factory, client=client, resolution_minutes=1
        )
    assert call_count == 0  # no API call at all -- resolved from stored coverage
    assert report.results[0].skipped_already_covered is True
    assert report.results[0].outcome is MarketOutcome.COMPLETE


async def test_dry_run_makes_no_database_writes(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as session:
        await _seed_market(

            session, GOOD_TICKER, close_time=datetime(2026, 7, 15, 4, 59, tzinfo=UTC)

        )

    async with _client(make_handler()) as client:
        report = await run_price_backfill(
            session_factory=session_factory, client=client, resolution_minutes=1, dry_run=True
        )
    assert report.results[0].would_save == 3
    assert report.results[0].saved == 0

    async with session_scope(session_factory) as session:
        rows = (await session.scalars(select(MarketCandlestick))).all()
    assert rows == []


async def test_backfill_filters_by_ticker_event_and_date(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as session:
        await _seed_market(

            session, GOOD_TICKER, close_time=datetime(2026, 7, 15, 4, 59, tzinfo=UTC)

        )
        await _seed_market(
            session, QUIET_TICKER, close_time=datetime(2026, 7, 11, tzinfo=UTC)
        )

    async with session_scope(session_factory) as session:
        by_ticker = await select_backfill_candidates(session, tickers=[GOOD_TICKER])
        assert [c.market_ticker for c in by_ticker] == [GOOD_TICKER]

        by_date = await select_backfill_candidates(
            session, start_date=datetime(2026, 7, 14).date(), end_date=datetime(2026, 7, 16).date()
        )
        assert [c.market_ticker for c in by_date] == [GOOD_TICKER]

        limited = await select_backfill_candidates(session, limit=1)
        assert len(limited) == 1
