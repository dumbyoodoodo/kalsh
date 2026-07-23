"""Tests for the historical price (candlestick) backfill job (Phase 7A)."""

import asyncio
from datetime import UTC, datetime, timedelta
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
    run_price_sync_loop,
    select_backfill_candidates,
)
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.storage.database import session_scope
from kalshi_weather.storage.models import Base, CollectorRun, MarketCandlestick
from kalshi_weather.storage.repositories import (
    save_event,
    save_market_candlestick,
    save_market_snapshot,
)

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
    """A completed market costs nothing on the next sweep run; this is what
    makes an interrupted multi-market backfill cheap to resume.

    Since the 2026-07-23 fix a covered market is dropped during *candidate
    selection* on a sweep-shaped run, so it no longer appears as a result at
    all -- and, critically, no longer consumes `limit`. The defensive
    `skip_covered` check inside `backfill_one_market` is asserted separately,
    in test_explicit_ticker_still_reaches_covered_market_and_skip_covered_holds.
    """
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
    assert report.results == []  # excluded before LIMIT, not skipped after it


async def test_explicit_ticker_still_reaches_covered_market_and_skip_covered_holds(  # type: ignore[no-untyped-def]
    session_factory,
) -> None:
    """Naming a market explicitly opts out of the coverage exclusion, so an
    operator can still inspect or deliberately re-fetch it -- and the
    defensive `skip_covered` check inside `backfill_one_market` still
    classifies it COMPLETE without spending an API call."""
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
            session_factory=session_factory,
            client=client,
            resolution_minutes=1,
            tickers=[GOOD_TICKER],
        )

    assert [r.market_ticker for r in report.results] == [GOOD_TICKER]
    assert report.results[0].skipped_already_covered is True
    assert report.results[0].outcome is MarketOutcome.COMPLETE
    assert call_count == 0  # defensive net resolved it from stored coverage

    # ...and --no-skip-covered still forces a real re-fetch of that market.
    async with _client(counting_handler) as client:
        refetch = await run_price_backfill(
            session_factory=session_factory,
            client=client,
            resolution_minutes=1,
            tickers=[GOOD_TICKER],
            skip_covered=False,
        )
    assert call_count > 0
    assert refetch.results[0].skipped_already_covered is False
    assert refetch.results[0].saved == 0  # re-fetch stored nothing new
    assert refetch.results[0].duplicate > 0


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


# --- Coverage-aware candidate selection (2026-07-23 sync-progress fix) -------
#
# Regression tests for ADR 0010: `select_backfill_candidates` applied LIMIT
# before excluding already-covered markets, so the continuous sync loop spent
# every cycle re-selecting (and skipping) the same oldest fully covered
# markets and never reached newly settled ones. Each test below fails against
# that behaviour.


async def _seed_covered_market(
    session: AsyncSession, ticker: str, *, close_time: datetime, reaches_close: bool = True
) -> None:
    """Seed a settled market plus one stored candle. With ``reaches_close``
    the candle's period_end lands on close_time (covered); otherwise it stops
    a day short, which is well outside COMPLETENESS_TOLERANCE_SECONDS
    (partial)."""
    await _seed_market(session, ticker, close_time=close_time)
    period_end = close_time if reaches_close else close_time - timedelta(days=1)
    await save_market_candlestick(
        session,
        market_ticker=ticker,
        series_ticker=ticker.split("-")[0],
        period_interval_seconds=60,
        period_start=period_end - timedelta(seconds=60),
        period_end=period_end.replace(tzinfo=None),
        price_open_cents=1,
        price_high_cents=2,
        price_low_cents=1,
        price_close_cents=2,
        price_mean_cents=1,
        price_close_is_carried_forward=False,
        yes_bid_open_cents=1,
        yes_bid_high_cents=2,
        yes_bid_low_cents=1,
        yes_bid_close_cents=2,
        yes_ask_open_cents=1,
        yes_ask_high_cents=2,
        yes_ask_low_cents=1,
        yes_ask_close_cents=2,
        volume=1,
        open_interest=1,
        raw_payload_id=None,
    )


async def test_covered_head_does_not_consume_limit(session_factory) -> None:  # type: ignore[no-untyped-def]
    """THE regression test. N covered markets older than M uncovered ones,
    with limit < N: the old code returned only covered markets and made zero
    progress forever."""
    async with session_scope(session_factory) as session:
        for i in range(5):  # covered, and the oldest by close_time
            await _seed_covered_market(
                session, f"KXHIGHNY-26MAY1{i}-T10", close_time=datetime(2026, 5, 10 + i, tzinfo=UTC)
            )
        for i in range(2):  # uncovered, newer
            await _seed_market(
                session, f"KXHIGHNY-26JUL1{i}-T10", close_time=datetime(2026, 7, 10 + i, tzinfo=UTC)
            )

    async with session_scope(session_factory) as session:
        selected = await select_backfill_candidates(session, limit=3, exclude_covered=True)

    assert [c.market_ticker for c in selected] == ["KXHIGHNY-26JUL10-T10", "KXHIGHNY-26JUL11-T10"]


async def test_oldest_uncovered_first_deterministic_order(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Covered and uncovered interleaved by close_time: the oldest *uncovered*
    markets come first, and ties on close_time break deterministically by
    ticker."""
    async with session_scope(session_factory) as session:
        await _seed_covered_market(
            session, "KXHIGHNY-26MAY01-T10", close_time=datetime(2026, 5, 1, tzinfo=UTC)
        )
        await _seed_market(
            session, "KXHIGHNY-26MAY02-T10", close_time=datetime(2026, 5, 2, tzinfo=UTC)
        )
        await _seed_covered_market(
            session, "KXHIGHNY-26MAY03-T10", close_time=datetime(2026, 5, 3, tzinfo=UTC)
        )
        # Tie on close_time, seeded out of ticker order.
        await _seed_market(
            session, "KXHIGHNY-26MAY04-T99", close_time=datetime(2026, 5, 4, tzinfo=UTC)
        )
        await _seed_market(
            session, "KXHIGHNY-26MAY04-T10", close_time=datetime(2026, 5, 4, tzinfo=UTC)
        )

    async with session_scope(session_factory) as session:
        selected = await select_backfill_candidates(session, exclude_covered=True)

    assert [c.market_ticker for c in selected] == [
        "KXHIGHNY-26MAY02-T10",
        "KXHIGHNY-26MAY04-T10",
        "KXHIGHNY-26MAY04-T99",
    ]


async def test_partially_covered_market_remains_eligible(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Coverage that stops short of close (beyond tolerance) is not coverage
    -- the market must stay selectable so the gap can still be filled."""
    async with session_scope(session_factory) as session:
        await _seed_covered_market(
            session,
            "KXHIGHNY-26MAY01-T10",
            close_time=datetime(2026, 5, 1, tzinfo=UTC),
            reaches_close=False,
        )

    async with session_scope(session_factory) as session:
        selected = await select_backfill_candidates(session, exclude_covered=True)

    assert [c.market_ticker for c in selected] == ["KXHIGHNY-26MAY01-T10"]


async def test_fully_covered_market_is_excluded(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as session:
        await _seed_covered_market(
            session, "KXHIGHNY-26MAY01-T10", close_time=datetime(2026, 5, 1, tzinfo=UTC)
        )

    async with session_scope(session_factory) as session:
        assert await select_backfill_candidates(session, exclude_covered=True) == []
        # ...but the exclusion is opt-in: the historical sweep behaviour is unchanged.
        unfiltered = await select_backfill_candidates(session)
    assert [c.market_ticker for c in unfiltered] == ["KXHIGHNY-26MAY01-T10"]


async def test_coverage_exclusion_respects_resolution(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Candles stored at a different resolution do not count as coverage for
    the resolution being backfilled."""
    async with session_scope(session_factory) as session:
        await _seed_covered_market(
            session, "KXHIGHNY-26MAY01-T10", close_time=datetime(2026, 5, 1, tzinfo=UTC)
        )

    async with session_scope(session_factory) as session:
        assert await select_backfill_candidates(session, exclude_covered=True) == []
        other = await select_backfill_candidates(
            session, exclude_covered=True, period_interval_seconds=3600
        )
    assert [c.market_ticker for c in other] == ["KXHIGHNY-26MAY01-T10"]


async def test_exclude_tickers_removes_markets_from_selection(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as session:
        await _seed_market(
            session, "KXHIGHNY-26MAY01-T10", close_time=datetime(2026, 5, 1, tzinfo=UTC)
        )
        await _seed_market(
            session, "KXHIGHNY-26MAY02-T10", close_time=datetime(2026, 5, 2, tzinfo=UTC)
        )

    async with session_scope(session_factory) as session:
        selected = await select_backfill_candidates(
            session, exclude_tickers={"KXHIGHNY-26MAY01-T10"}
        )
    assert [c.market_ticker for c in selected] == ["KXHIGHNY-26MAY02-T10"]


async def test_explicit_event_invocation_keeps_covered_markets_selectable(session_factory) -> None:  # type: ignore[no-untyped-def]
    """`--event` names a target the same way `--ticker` does, so it must keep
    reaching covered markets rather than silently returning nothing."""
    async with session_scope(session_factory) as session:
        await _seed_covered_market(
            session, "KXHIGHNY-26MAY01-T10", close_time=datetime(2026, 5, 1, tzinfo=UTC)
        )

    call_count = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"candlesticks": [], "ticker": "x"})

    async with _client(counting_handler) as client:
        report = await run_price_backfill(
            session_factory=session_factory,
            client=client,
            resolution_minutes=1,
            event_ticker="KXHIGHNY-26MAY01",
        )
    assert [r.market_ticker for r in report.results] == ["KXHIGHNY-26MAY01-T10"]
    assert report.results[0].skipped_already_covered is True
    assert call_count == 0


# --- Continuous sync-loop forward progress ----------------------------------

SYNC_CLOSE = datetime(2026, 7, 15, 4, 59, tzinfo=UTC)


def _sync_handler(quiet_prefix: str = "QUIET"):  # type: ignore[no-untyped-def]
    """Serves three real candles for any ticker whose name marks it tradeable,
    and a 200-with-zero-candles for a 'quiet' one (the real-world case that
    produced 85 permanently uncovered markets on 2026-07-23)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        close_ts = int(SYNC_CLOSE.timestamp())
        if quiet_prefix in path:
            return httpx.Response(200, json={"candlesticks": [], "ticker": "quiet"})
        params = dict(request.url.params)
        start_ts, end_ts = int(params["start_ts"]), int(params["end_ts"])
        candles = [
            c
            for c in (
                _candle_payload(close_ts - 120, close_dollars="0.2500"),
                _candle_payload(close_ts - 60, close_dollars="0.3000"),
                _candle_payload(close_ts, close_dollars="0.9900"),
            )
            if start_ts <= c["end_period_ts"] <= end_ts
        ]
        return httpx.Response(200, json={"candlesticks": candles, "ticker": "t"})

    return handler


async def _run_sync_cycles(session_factory, handler, *, cycles: int, limit: int) -> None:  # type: ignore[no-untyped-def]
    await run_price_sync_loop(
        session_factory=session_factory,
        client_factory=lambda: _client(handler),
        resolution_minutes=1,
        limit_per_cycle=limit,
        interval_seconds=0,
        stop_event=asyncio.Event(),
        max_cycles=cycles,
    )


async def test_sync_loop_makes_forward_progress_past_covered_head(session_factory) -> None:  # type: ignore[no-untyped-def]
    """End-to-end regression for the reported defect: with a fully covered
    head older than the uncovered markets and limit < len(head), the loop
    previously re-selected and skipped the same head every cycle, saving
    nothing forever."""
    async with session_scope(session_factory) as session:
        for i in range(3):
            await _seed_covered_market(
                session, f"KXHIGHNY-26MAY0{i}-T10", close_time=datetime(2026, 5, 1 + i, tzinfo=UTC)
            )
        await _seed_market(session, "KXHIGHNY-26JUL14-B94.5", close_time=SYNC_CLOSE)

    await _run_sync_cycles(session_factory, _sync_handler(), cycles=1, limit=2)

    async with session_scope(session_factory) as session:
        saved = (
            await session.scalars(
                select(MarketCandlestick).where(
                    MarketCandlestick.market_ticker == "KXHIGHNY-26JUL14-B94.5"
                )
            )
        ).all()
        runs = (await session.scalars(select(CollectorRun))).all()

    assert len(saved) == 3  # the newly settled market was actually captured
    assert [r.collector for r in runs] == ["prices"]
    assert runs[0].stats_json["saved"] == 3
    assert runs[0].stats_json["markets_attempted"] == 1  # covered head cost nothing


async def test_sync_loop_quiet_markets_do_not_block_the_queue(session_factory) -> None:  # type: ignore[no-untyped-def]
    """A market that legitimately returns zero candles stays uncovered
    forever, so a coverage predicate alone would let it re-occupy the head of
    the oldest-first queue every cycle. The terminal-outcome exclusion is what
    lets a later, tradeable market be reached at all."""
    async with session_scope(session_factory) as session:
        for i in range(2):
            await _seed_market(
                session, f"KXQUIET-26MAY0{i}-T10", close_time=datetime(2026, 5, 1 + i, tzinfo=UTC)
            )
        await _seed_market(session, "KXHIGHNY-26JUL14-B94.5", close_time=SYNC_CLOSE)

    await _run_sync_cycles(
        session_factory, _sync_handler(quiet_prefix="KXQUIET"), cycles=2, limit=2
    )

    async with session_scope(session_factory) as session:
        saved = (
            await session.scalars(
                select(MarketCandlestick).where(
                    MarketCandlestick.market_ticker == "KXHIGHNY-26JUL14-B94.5"
                )
            )
        ).all()
        runs = (await session.scalars(select(CollectorRun).order_by(CollectorRun.id))).all()

    assert len(saved) == 3  # reached on cycle 2, once the quiet pair was set aside
    assert runs[0].stats_json["outcome_no_price_data"] == 2
    assert runs[0].stats_json["terminal_excluded"] == 2
    assert runs[1].stats_json["saved"] == 3


async def test_sync_loop_repeated_cycles_write_no_duplicate_candles(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Once captured, a market is covered, so later cycles neither re-fetch it
    nor write duplicate rows (candles are immutable and dedup'd on
    (market, resolution, period_end))."""
    async with session_scope(session_factory) as session:
        await _seed_market(session, "KXHIGHNY-26JUL14-B94.5", close_time=SYNC_CLOSE)

    await _run_sync_cycles(session_factory, _sync_handler(), cycles=3, limit=5)

    async with session_scope(session_factory) as session:
        rows = (await session.scalars(select(MarketCandlestick))).all()
        runs = (await session.scalars(select(CollectorRun).order_by(CollectorRun.id))).all()

    assert len(rows) == 3
    assert len({r.period_end for r in rows}) == 3
    assert runs[0].stats_json["saved"] == 3
    assert [r.stats_json["markets_attempted"] for r in runs[1:]] == [0, 0]


async def test_sync_loop_retries_transient_api_failures(session_factory) -> None:  # type: ignore[no-untyped-def]
    """api_failure is deliberately NOT terminal: a market that failed on a
    transient error must be retried on the next cycle, and captured once the
    API recovers."""
    async with session_scope(session_factory) as session:
        await _seed_market(session, "KXHIGHNY-26JUL14-B94.5", close_time=SYNC_CLOSE)

    healthy = _sync_handler()
    fail_first = True

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal fail_first
        if fail_first:
            fail_first = False
            return httpx.Response(500, json={"error": {"code": "internal"}})
        return healthy(request)

    await _run_sync_cycles(session_factory, flaky, cycles=2, limit=5)

    async with session_scope(session_factory) as session:
        rows = (await session.scalars(select(MarketCandlestick))).all()
        runs = (await session.scalars(select(CollectorRun).order_by(CollectorRun.id))).all()

    assert runs[0].stats_json["outcome_api_failure"] == 1
    assert runs[0].stats_json["terminal_excluded"] == 0  # stays retryable
    assert len(rows) == 3  # captured on the retry
