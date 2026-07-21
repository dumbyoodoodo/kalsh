"""Tests for the price coverage report and retention-risk summary
(Phase 7A, ops/price_coverage.py)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.domain.time import utc_now
from kalshi_weather.ops.price_coverage import (
    build_price_coverage_report,
    retention_risk_summary,
)
from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import (
    save_event,
    save_market_candlestick,
    save_market_snapshot,
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


async def _seed_market(
    session: AsyncSession,
    ticker: str,
    *,
    close_time: datetime,
    event_ticker: str = "KXHIGHNY-26JUL20",
) -> None:
    await save_event(
        session, event_ticker=event_ticker, series_ticker=event_ticker.split("-")[0],
        category="Climate and Weather", title="t", sub_title=None,
    )
    await save_market_snapshot(
        session, market_ticker=ticker, event_ticker=event_ticker, market_type="binary",
        title="t", subtitle="s", status="finalized", yes_bid_cents=0, yes_ask_cents=1,
        last_price_cents=0, volume=1, open_interest=1, close_time=close_time,
        rules_primary="r", rules_secondary=None, raw_payload_id=None, result="no",
        expiration_value=Decimal(78),
    )


async def _seed_candle(
    session: AsyncSession, ticker: str, *, period_end: datetime, series_ticker: str = "KXHIGHNY"
) -> None:
    await save_market_candlestick(
        session,
        market_ticker=ticker,
        series_ticker=series_ticker,
        period_interval_seconds=60,
        period_start=period_end - timedelta(minutes=1),
        period_end=period_end,
        price_open_cents=80, price_high_cents=82, price_low_cents=78, price_close_cents=81,
        price_mean_cents=80, price_close_is_carried_forward=False,
        yes_bid_open_cents=79, yes_bid_high_cents=80, yes_bid_low_cents=77, yes_bid_close_cents=79,
        yes_ask_open_cents=82, yes_ask_high_cents=83, yes_ask_low_cents=79, yes_ask_close_cents=82,
        volume=10, open_interest=50, raw_payload_id=None,
    )


async def test_empty_store_reports_zero_without_error(session: AsyncSession) -> None:
    report = await build_price_coverage_report(
        session, resolution_minutes=1, observed_retention_days=67
    )
    assert report.markets_attempted == 0
    assert report.total_candles == 0
    assert report.duplicate_row_count == 0


async def test_coverage_distinguishes_complete_incomplete_and_never_captured(
    session: AsyncSession,
) -> None:
    close = datetime(2026, 7, 21, 4, 59, tzinfo=UTC)
    await _seed_market(session, "COMPLETE-T", close_time=close)
    await _seed_candle(session, "COMPLETE-T", period_end=close)  # reaches close -> complete

    await _seed_market(session, "PARTIAL-T", close_time=close)
    await _seed_candle(
        session, "PARTIAL-T", period_end=close - timedelta(hours=5)
    )  # far short of close -> incomplete

    await _seed_market(session, "NEVER-T", close_time=close)  # no candle at all

    report = await build_price_coverage_report(
        session, resolution_minutes=1, observed_retention_days=67
    )
    assert report.markets_attempted == 3
    assert report.markets_captured == 2
    assert report.markets_complete == 1
    assert report.markets_incomplete == 1
    assert report.markets_never_captured == 1
    assert report.total_candles == 2


async def test_coverage_by_event_date_and_variable(session: AsyncSession) -> None:
    close = datetime(2026, 7, 21, 4, 59, tzinfo=UTC)
    await _seed_market(
        session, "KXHIGHNY-26JUL20-T80", close_time=close, event_ticker="KXHIGHNY-26JUL20"
    )
    await _seed_candle(session, "KXHIGHNY-26JUL20-T80", period_end=close)

    report = await build_price_coverage_report(
        session, resolution_minutes=1, observed_retention_days=67
    )
    assert report.coverage_by_event_date == [
        {"date": "2026-07-21", "complete": 1, "incomplete": 0, "never_captured": 0}
    ]
    # variable is "unresolved" since no settlement spec resolves this synthetic ticker
    assert "unresolved" in report.coverage_by_variable
    assert report.coverage_by_variable["unresolved"]["captured"] == 1


async def test_missing_intervals_detected_within_a_markets_own_span(
    session: AsyncSession,
) -> None:
    close = datetime(2026, 7, 21, 4, 59, tzinfo=UTC)
    await _seed_market(session, "GAPPY-T", close_time=close)
    base = close - timedelta(minutes=10)
    # candles at minute 0 and minute 5 of a 0..10 span -> 9 expected, 2 actual, 7 missing
    await _seed_candle(session, "GAPPY-T", period_end=base + timedelta(minutes=1))
    await _seed_candle(session, "GAPPY-T", period_end=base + timedelta(minutes=6))

    report = await build_price_coverage_report(
        session, resolution_minutes=1, observed_retention_days=67
    )
    gaps = {g["market_ticker"]: g for g in report.markets_with_missing_intervals}
    assert "GAPPY-T" in gaps
    assert gaps["GAPPY-T"]["actual_candles"] == 2
    assert gaps["GAPPY-T"]["missing"] == gaps["GAPPY-T"]["expected_candles"] - 2


async def test_likely_lost_to_retention_is_inferred_from_age_not_assumed(
    session: AsyncSession,
) -> None:
    now = utc_now()
    old_close = now - timedelta(days=100)  # well past a 67-day retention window
    recent_close = now - timedelta(days=5)  # still comfortably in-window
    await _seed_market(session, "OLD-UNCAPTURED", close_time=old_close)
    await _seed_market(session, "RECENT-UNCAPTURED", close_time=recent_close)

    report = await build_price_coverage_report(
        session, resolution_minutes=1, observed_retention_days=67, now=now
    )
    lost_tickers = {m["market_ticker"] for m in report.likely_lost_to_retention}
    assert "OLD-UNCAPTURED" in lost_tickers
    assert "RECENT-UNCAPTURED" not in lost_tickers
    assert "inferred" in report.likely_lost_to_retention[0]["note"]


async def test_duplicate_row_count_is_zero_when_dedup_constraint_holds(
    session: AsyncSession,
) -> None:
    close = datetime(2026, 7, 21, 4, 59, tzinfo=UTC)
    await _seed_market(session, "T", close_time=close)
    await _seed_candle(session, "T", period_end=close)
    report = await build_price_coverage_report(
        session, resolution_minutes=1, observed_retention_days=67
    )
    assert report.duplicate_row_count == 0


async def test_retention_summary_oldest_and_nearing_expiry(session: AsyncSession) -> None:
    now = utc_now()
    await _seed_market(session, "VERY-OLD", close_time=now - timedelta(days=65))
    await _seed_market(session, "NEW", close_time=now - timedelta(days=1))

    summary = await retention_risk_summary(
        session,
        resolution_minutes=1,
        observed_retention_days=67,
        warning_buffer_days=10,  # warning kicks in at age >= 57 days
        now=now,
    )
    assert summary.oldest_uncaptured_market is not None
    assert summary.oldest_uncaptured_market["market_ticker"] == "VERY-OLD"
    assert summary.markets_nearing_expiry == 1  # only VERY-OLD is >= 57 days old
    assert summary.ingestion_lag_days == 65


async def test_retention_summary_empty_store(session: AsyncSession) -> None:
    summary = await retention_risk_summary(
        session, resolution_minutes=1, observed_retention_days=67, warning_buffer_days=10
    )
    assert summary.oldest_uncaptured_market is None
    assert summary.markets_nearing_expiry == 0
    assert summary.markets_incomplete_coverage == 0
    assert summary.ingestion_lag_days is None
