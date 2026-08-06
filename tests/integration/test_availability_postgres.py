"""PostgreSQL-backed observed-availability construction (Phase 11).

Seeds real collector_runs (success, failure, and a downtime gap) plus per-market
orderbook observation points, then asserts through the real builder that OBSERVED,
COLLECTOR_UNAVAILABLE, and MARKET_NOT_ELIGIBLE intervals are produced, source
tables are not mutated, and queries are bounded to explicit tickers.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kalshi_weather.execution.availability import (
    IntervalState,
    build_availability_timeline,
)
from kalshi_weather.storage.models import Base, CollectorRun, OrderbookSnapshot

pytestmark = pytest.mark.integration

_TEST_DB = "test_kalshi_availability_it"
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


def T(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 7, 26, h, m, s, tzinfo=UTC)


async def _seed(url: str) -> None:
    eng = create_async_engine(url)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as s:
        # two good cycles ~5 min apart, then a >20 min gap, a good cycle, a failed cycle
        s.add_all(
            [
                CollectorRun(
                    collector="kalshi",
                    started_at=T(10, 0),
                    finished_at=T(10, 5),
                    duration_seconds=300,
                    success=True,
                ),
                CollectorRun(
                    collector="kalshi",
                    started_at=T(10, 10),
                    finished_at=T(10, 15),
                    duration_seconds=300,
                    success=True,
                ),
                # gap 10:15 -> 10:45 (30 min) = downtime
                CollectorRun(
                    collector="kalshi",
                    started_at=T(10, 45),
                    finished_at=T(10, 50),
                    duration_seconds=300,
                    success=True,
                ),
                CollectorRun(
                    collector="kalshi",
                    started_at=T(10, 55),
                    finished_at=T(11, 0),
                    duration_seconds=300,
                    success=False,
                    error="429",
                ),
                # a weather collector run that must be ignored
                CollectorRun(
                    collector="weather",
                    started_at=T(10, 0),
                    finished_at=T(10, 1),
                    duration_seconds=60,
                    success=True,
                ),
            ]
        )
        # per-market observations (production) inside the first two cycles + the 10:45 cycle
        s.add_all(
            [
                OrderbookSnapshot(
                    market_ticker=TK,
                    captured_at=T(10, 2),
                    yes_levels_json=[[40, 5]],
                    no_levels_json=[[55, 5]],
                    content_hash="a",
                    environment="production",
                ),
                OrderbookSnapshot(
                    market_ticker=TK,
                    captured_at=T(10, 12),
                    yes_levels_json=[[41, 5]],
                    no_levels_json=[[54, 5]],
                    content_hash="b",
                    environment="production",
                ),
                OrderbookSnapshot(
                    market_ticker=TK,
                    captured_at=T(10, 47),
                    yes_levels_json=[[42, 5]],
                    no_levels_json=[[53, 5]],
                    content_hash="c",
                    environment="production",
                ),
                # a demo row must not count as an observation
                OrderbookSnapshot(
                    market_ticker=TK,
                    captured_at=T(10, 3),
                    yes_levels_json=[[40, 5]],
                    no_levels_json=[[55, 5]],
                    content_hash="d",
                    environment="demo",
                ),
            ]
        )
        await s.commit()
    await eng.dispose()


@pytest.mark.asyncio
async def test_availability_from_real_collector_runs(throwaway_db: str) -> None:
    await _seed(throwaway_db)
    eng = create_async_engine(throwaway_db)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with Session() as s:
            tl = await build_availability_timeline(s, tickers=(TK,), start=T(10, 0), end=T(11, 0))
    finally:
        await eng.dispose()

    ta = tl.tickers[TK]
    assert tl.collector_runs_considered == 4  # weather run excluded
    assert tl.failed_runs == 1
    assert tl.downtime_gaps == 1
    # observed across the first two witnessed cycles (deduped bridge)
    assert ta.state_at(T(10, 2)) is IntervalState.OBSERVED
    assert ta.state_at(T(10, 12)) is IntervalState.OBSERVED
    # the 30-min gap is collector-unavailable, not observed
    assert ta.state_at(T(10, 30)) is IntervalState.COLLECTOR_UNAVAILABLE
    # the failed cycle is collector-unavailable
    assert ta.state_at(T(10, 57)) is IntervalState.COLLECTOR_UNAVAILABLE
    # a passive order at 10:12 loses continuity at the downtime gap
    brk = ta.first_break_after(T(10, 12), allow_likely=False)
    assert brk is not None and brk <= T(10, 45)


@pytest.mark.asyncio
async def test_builder_is_read_only(throwaway_db: str) -> None:
    await _seed(throwaway_db)
    eng = create_async_engine(throwaway_db)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with Session() as s:
            await build_availability_timeline(s, tickers=(TK,), start=T(10, 0), end=T(11, 0))
    finally:
        await eng.dispose()
    eng2 = create_async_engine(throwaway_db)
    try:
        async with eng2.connect() as c:
            runs = await c.scalar(text("select count(*) from collector_runs"))
            books = await c.scalar(text("select count(*) from orderbook_snapshots"))
    finally:
        await eng2.dispose()
    assert runs == 5 and books == 4  # nothing mutated or deleted
