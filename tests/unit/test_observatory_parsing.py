from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.observatory.parsing import (
    ParserCycleFailures,
    load_weather_parser_cycles,
    summarize_weather_parser_failures,
)
from kalshi_weather.observatory.severity import Severity
from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import record_collector_run


def test_summarize_weather_parser_failures_empty_is_info() -> None:
    finding = summarize_weather_parser_failures([])
    assert finding.severity == Severity.INFO
    assert finding.count == 0


def test_summarize_weather_parser_failures_clean_cycles_is_info() -> None:
    cycles = [
        ParserCycleFailures(started_at=datetime(2026, 7, 22), invalid_items=0, errors=0)
        for _ in range(5)
    ]
    finding = summarize_weather_parser_failures(cycles)
    assert finding.severity == Severity.INFO
    assert finding.count == 0


def test_summarize_weather_parser_failures_low_rate_is_warning() -> None:
    # 1 of 10 cycles has a failure -- below the 0.5 critical-rate threshold.
    cycles = [
        ParserCycleFailures(started_at=datetime(2026, 7, 22), invalid_items=0, errors=0)
        for _ in range(9)
    ] + [ParserCycleFailures(started_at=datetime(2026, 7, 22), invalid_items=1, errors=0)]
    finding = summarize_weather_parser_failures(cycles)
    assert finding.severity == Severity.WARNING
    assert finding.count == 1


def test_summarize_weather_parser_failures_high_rate_is_critical() -> None:
    # 5 of 8 cycles failing -- at/above the 0.5 critical-rate threshold.
    cycles = [
        ParserCycleFailures(started_at=datetime(2026, 7, 22), invalid_items=1, errors=0)
        for _ in range(5)
    ] + [
        ParserCycleFailures(started_at=datetime(2026, 7, 22), invalid_items=0, errors=0)
        for _ in range(3)
    ]
    finding = summarize_weather_parser_failures(cycles)
    assert finding.severity == Severity.CRITICAL
    assert finding.count == 5


def test_summarize_weather_parser_failures_counts_invalid_and_errors_separately() -> None:
    cycles = [ParserCycleFailures(started_at=datetime(2026, 7, 22), invalid_items=2, errors=3)]
    finding = summarize_weather_parser_failures(cycles)
    assert finding.count == 5
    assert "2 invalid item(s)" in finding.message
    assert "3 error(s)" in finding.message


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


async def test_load_weather_parser_cycles_mines_stats_json(session: AsyncSession) -> None:
    started = datetime(2026, 7, 22, 0, 0, 0)
    await record_collector_run(
        session,
        collector="weather",
        started_at=started,
        finished_at=started + timedelta(seconds=10),
        success=True,
        requests_attempted=4,
        retries=0,
        stats={"invalid_items": 2, "errors": 1},
    )
    cycles = await load_weather_parser_cycles(session, since=started - timedelta(hours=1))
    assert len(cycles) == 1
    assert cycles[0].invalid_items == 2
    assert cycles[0].errors == 1


async def test_load_weather_parser_cycles_defaults_missing_stats_to_zero(
    session: AsyncSession,
) -> None:
    started = datetime(2026, 7, 22, 0, 0, 0)
    await record_collector_run(
        session,
        collector="weather",
        started_at=started,
        finished_at=started + timedelta(seconds=10),
        success=True,
        requests_attempted=4,
        retries=0,
        stats={},
    )
    cycles = await load_weather_parser_cycles(session, since=started - timedelta(hours=1))
    assert cycles[0].invalid_items == 0
    assert cycles[0].errors == 0


async def test_load_weather_parser_cycles_ignores_other_collectors(
    session: AsyncSession,
) -> None:
    started = datetime(2026, 7, 22, 0, 0, 0)
    await record_collector_run(
        session,
        collector="kalshi",
        started_at=started,
        finished_at=started + timedelta(seconds=10),
        success=True,
        requests_attempted=4,
        retries=0,
        stats={"invalid_items": 9},
    )
    cycles = await load_weather_parser_cycles(session, since=started - timedelta(hours=1))
    assert cycles == []
