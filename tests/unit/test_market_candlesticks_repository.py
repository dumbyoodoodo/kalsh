"""Repository tests for market_candlesticks (Phase 7A price ingestion)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import (
    get_candlestick_coverage,
    get_candlestick_covered_tickers,
    get_latest_candlestick_period_end,
    list_candlesticks_for_market,
    save_market_candlestick,
)


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


def _candle(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "market_ticker": "KXHIGHNY-26MAY15-B65.5",
        "series_ticker": "KXHIGHNY",
        "period_interval_seconds": 60,
        "period_start": datetime(2026, 5, 15, 0, 0, tzinfo=UTC),
        "period_end": datetime(2026, 5, 15, 0, 1, tzinfo=UTC),
        "price_open_cents": 28,
        "price_high_cents": 30,
        "price_low_cents": 26,
        "price_close_cents": 30,
        "price_mean_cents": 27,
        "price_close_is_carried_forward": False,
        "yes_bid_open_cents": 27,
        "yes_bid_high_cents": 28,
        "yes_bid_low_cents": 25,
        "yes_bid_close_cents": 27,
        "yes_ask_open_cents": 31,
        "yes_ask_high_cents": 31,
        "yes_ask_low_cents": 26,
        "yes_ask_close_cents": 30,
        "volume": 274,
        "open_interest": 2214,
        "raw_payload_id": None,
    }
    defaults.update(overrides)
    return defaults


async def test_save_market_candlestick_inserts_new(session: AsyncSession) -> None:
    result = await save_market_candlestick(session, **_candle())  # type: ignore[arg-type]
    assert result.was_duplicate is False
    assert result.record.price_close_cents == 30
    assert result.record.volume == 274


async def test_save_market_candlestick_dedups_on_natural_key(session: AsyncSession) -> None:
    """Re-fetching the same (market, resolution, period_end) must never
    overwrite -- candles are immutable historical facts once elapsed."""
    first = await save_market_candlestick(session, **_candle())  # type: ignore[arg-type]
    second = await save_market_candlestick(
        session, **_candle(price_close_cents=999, volume=1)  # type: ignore[arg-type]
    )
    assert second.was_duplicate is True
    assert second.record.id == first.record.id
    assert second.record.price_close_cents == 30  # untouched, not overwritten
    assert second.record.volume == 274


async def test_save_market_candlestick_distinguishes_resolution_and_market(
    session: AsyncSession,
) -> None:
    """Same period_end, different resolution or different market -> distinct
    rows, never conflated by the dedup key."""
    a = await save_market_candlestick(session, **_candle())  # type: ignore[arg-type]
    b = await save_market_candlestick(
        session, **_candle(period_interval_seconds=3600)  # type: ignore[arg-type]
    )
    c = await save_market_candlestick(
        session, **_candle(market_ticker="KXHIGHNY-26MAY15-B67.5")  # type: ignore[arg-type]
    )
    assert a.was_duplicate is b.was_duplicate is c.was_duplicate is False
    assert len({a.record.id, b.record.id, c.record.id}) == 3


async def test_get_latest_candlestick_period_end(session: AsyncSession) -> None:
    ticker = "KXHIGHNY-26MAY15-B65.5"
    assert await get_latest_candlestick_period_end(session, ticker, 60) is None

    await save_market_candlestick(session, **_candle())  # type: ignore[arg-type]
    await save_market_candlestick(
        session,
        **_candle(period_end=datetime(2026, 5, 15, 0, 2, tzinfo=UTC)),  # type: ignore[arg-type]
    )
    latest = await get_latest_candlestick_period_end(session, ticker, 60)
    assert latest is not None
    assert latest.minute == 2

    # a different resolution for the same market must not affect this query
    assert await get_latest_candlestick_period_end(session, ticker, 3600) is None


async def test_get_candlestick_coverage(session: AsyncSession) -> None:
    ticker = "KXHIGHNY-26MAY15-B65.5"
    empty = await get_candlestick_coverage(session, ticker, 60)
    assert empty == {"candle_count": 0, "earliest": None, "latest": None}

    await save_market_candlestick(session, **_candle())  # type: ignore[arg-type]
    await save_market_candlestick(
        session,
        **_candle(  # type: ignore[arg-type]
            period_start=datetime(2026, 5, 15, 0, 5, tzinfo=UTC),
            period_end=datetime(2026, 5, 15, 0, 6, tzinfo=UTC),
        ),
    )
    coverage = await get_candlestick_coverage(session, ticker, 60)
    assert coverage["candle_count"] == 2
    assert coverage["earliest"].minute == 0
    assert coverage["latest"].minute == 6


async def test_list_candlesticks_for_market_ordering_and_range(session: AsyncSession) -> None:
    ticker = "KXHIGHNY-26MAY15-B65.5"
    for minute in (5, 1, 3):
        await save_market_candlestick(
            session,
            **_candle(  # type: ignore[arg-type]
                period_start=datetime(2026, 5, 15, 0, minute, tzinfo=UTC),
                period_end=datetime(2026, 5, 15, 0, minute + 1, tzinfo=UTC),
            ),
        )
    all_rows = await list_candlesticks_for_market(session, ticker, 60)
    assert [c.period_end.minute for c in all_rows] == [2, 4, 6]  # oldest first

    bounded = await list_candlesticks_for_market(
        session,
        ticker,
        60,
        start=datetime(2026, 5, 15, 0, 3, tzinfo=UTC),
        end=datetime(2026, 5, 15, 0, 5, tzinfo=UTC),
    )
    assert [c.period_end.minute for c in bounded] == [4]


async def test_get_candlestick_covered_tickers(session: AsyncSession) -> None:
    assert await get_candlestick_covered_tickers(session, 60) == set()
    await save_market_candlestick(session, **_candle())  # type: ignore[arg-type]
    await save_market_candlestick(
        session, **_candle(market_ticker="KXHIGHNY-26MAY15-B67.5")  # type: ignore[arg-type]
    )
    covered = await get_candlestick_covered_tickers(session, 60)
    assert covered == {"KXHIGHNY-26MAY15-B65.5", "KXHIGHNY-26MAY15-B67.5"}
    # a resolution with no stored candles returns empty, not an error
    assert await get_candlestick_covered_tickers(session, 3600) == set()
