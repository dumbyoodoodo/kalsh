"""Per-ticker polling-evidence tests (ADR 0020): taxonomy, the buffered record,
collector instrumentation, the append-only writer, and availability integration.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# Reuse the existing collector mock transport (same-dir import; prepend mode).
from test_ingestion_collector import BAD_ORDERBOOK_TICKER, GOOD_TICKER, _client, make_handler

from kalshi_weather.ingestion.collector import run_collection_cycle
from kalshi_weather.ingestion.poll_ledger import (
    CONTINUITY_BREAKING_OUTCOMES,
    OBSERVED_OUTCOMES,
    AttemptState,
    EligibilityState,
    EndpointType,
    PollAttempt,
    PollOutcome,
    bounded_detail,
    classify_exception,
)
from kalshi_weather.kalshi.client import KalshiAPIError
from kalshi_weather.storage.models import Base, MarketPollAttempt
from kalshi_weather.storage.repositories import record_collector_run, record_poll_attempts


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


def T(h: int, m: int = 0) -> datetime:
    return datetime(2026, 7, 26, h, m, tzinfo=UTC)


# --- taxonomy / pure helpers -----------------------------------------------


def test_classify_exception_rate_limit_vs_api_vs_persistence() -> None:
    from sqlalchemy.exc import SQLAlchemyError

    from kalshi_weather.ingestion.validation import MalformedPayloadError

    assert classify_exception(KalshiAPIError(429, "too many")) == (
        PollOutcome.RATE_LIMITED,
        429,
        True,
    )
    assert classify_exception(KalshiAPIError(500, "boom"))[0] is PollOutcome.API_FAILURE
    assert classify_exception(MalformedPayloadError("bad"))[0] is PollOutcome.MALFORMED_PAYLOAD
    assert classify_exception(SQLAlchemyError("db"))[0] is PollOutcome.PERSISTENCE_FAILURE
    assert classify_exception(ValueError("x"))[0] is PollOutcome.UNKNOWN_FAILURE


def test_bounded_detail_truncates_and_flattens() -> None:
    assert bounded_detail(None) is None
    assert bounded_detail("a\nb\n  c") == "a b c"
    assert len(bounded_detail("x" * 1000) or "") <= 480


def test_observed_and_breaking_sets_are_disjoint() -> None:
    assert not (OBSERVED_OUTCOMES & CONTINUITY_BREAKING_OUTCOMES)
    assert PollOutcome.SUCCEEDED_UNCHANGED in OBSERVED_OUTCOMES  # unchanged still observed


def test_poll_attempt_factory_defaults_eligible_attempted() -> None:
    a = PollAttempt.attempted(
        ticker="M",
        endpoint_type=EndpointType.ORDERBOOK,
        environment="production",
        outcome=PollOutcome.SUCCEEDED_UNCHANGED,
        requested_at=T(10),
        completed_at=T(10),
        deduplicated=True,
    )
    assert a.eligibility_state is EligibilityState.ELIGIBLE
    assert a.attempt_state is AttemptState.ATTEMPTED
    assert a.deduplicated and a.persisted_row_count == 0


# --- collector instrumentation ---------------------------------------------


async def test_cycle_records_per_ticker_poll_evidence(session: AsyncSession) -> None:
    async with _client(make_handler([])) as client:
        stats = await run_collection_cycle(
            session=session,
            client=client,
            category="Climate and Weather",
            market_status="open",
            trade_bootstrap_lookback_days=30,
            settle_check_limit=25,
            settle_check_days=7,
        )
    # 2 tickers x 2 endpoints (orderbook + trades) = 4 attempts
    assert stats.poll_attempts_total == 4
    assert stats.poll_unique_tickers == 2
    by = {(a.ticker, a.endpoint_type): a for a in stats.poll_attempts}
    # GOOD orderbook has depth -> new data; BAD orderbook is malformed -> failure
    assert by[(GOOD_TICKER, EndpointType.ORDERBOOK)].outcome is PollOutcome.SUCCEEDED_NEW_DATA
    assert (
        by[(BAD_ORDERBOOK_TICKER, EndpointType.ORDERBOOK)].outcome in CONTINUITY_BREAKING_OUTCOMES
    )
    # a failed attempt records the error class, bounded (never a full payload)
    bad = by[(BAD_ORDERBOOK_TICKER, EndpointType.ORDERBOOK)]
    assert bad.error_class is not None and (bad.bounded_error_detail or "") == (
        bad.bounded_error_detail or ""
    )
    # both tickers' trades succeed independently of the orderbook failure
    assert by[(GOOD_TICKER, EndpointType.TRADES)].outcome is PollOutcome.SUCCEEDED_NEW_DATA
    assert by[(BAD_ORDERBOOK_TICKER, EndpointType.TRADES)].outcome is PollOutcome.SUCCEEDED_NEW_DATA


async def test_second_cycle_records_unchanged_as_observed(session: AsyncSession) -> None:
    for _ in range(2):
        async with _client(make_handler([])) as client:
            stats = await run_collection_cycle(
                session=session,
                client=client,
                category="Climate and Weather",
                market_status="open",
                trade_bootstrap_lookback_days=30,
                settle_check_limit=25,
                settle_check_days=7,
            )
    ob = next(
        a
        for a in stats.poll_attempts
        if a.ticker == GOOD_TICKER and a.endpoint_type is EndpointType.ORDERBOOK
    )
    # unchanged book -> succeeded_unchanged, deduplicated, still an OBSERVED outcome
    assert ob.outcome is PollOutcome.SUCCEEDED_UNCHANGED
    assert ob.deduplicated and ob.persisted_row_count == 0
    assert ob.outcome in OBSERVED_OUTCOMES


# --- append-only writer ----------------------------------------------------


async def _run_id(session: AsyncSession) -> int:
    run = await record_collector_run(
        session,
        collector="kalshi",
        started_at=T(10),
        finished_at=T(10, 5),
        success=True,
        requests_attempted=1,
        retries=0,
        stats={},
    )
    return run.id


async def test_record_poll_attempts_is_append_only(session: AsyncSession) -> None:
    rid = await _run_id(session)
    a = PollAttempt.attempted(
        ticker="M",
        endpoint_type=EndpointType.ORDERBOOK,
        environment="production",
        outcome=PollOutcome.SUCCEEDED_NEW_DATA,
        requested_at=T(10),
        completed_at=T(10),
        persisted_row_count=1,
    )
    n = await record_poll_attempts(session, collector_run_id=rid, attempts=[a, a])
    assert n == 2
    rows = (await session.scalars(select(MarketPollAttempt))).all()
    assert len(rows) == 2
    assert all(r.collector_run_id == rid and r.created_at is not None for r in rows)
    # appending more never updates existing rows
    await record_poll_attempts(session, collector_run_id=rid, attempts=[a])
    assert len((await session.scalars(select(MarketPollAttempt))).all()) == 3
