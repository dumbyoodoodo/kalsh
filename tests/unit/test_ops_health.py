from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.domain.time import utc_now
from kalshi_weather.ops.health import build_health_report
from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import record_collector_run


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


async def _record(
    session: AsyncSession, collector: str, *, minutes_ago: float, success: bool = True
) -> None:
    started = (utc_now() - timedelta(minutes=minutes_ago)).replace(tzinfo=None)
    await record_collector_run(
        session,
        collector=collector,
        started_at=started,
        finished_at=started + timedelta(seconds=30),
        success=success,
        requests_attempted=10,
        retries=1,
        stats={"markets_discovered": 2},
    )


async def test_never_run_collectors_are_stale(session: AsyncSession) -> None:
    report = await build_health_report(
        session,
        kalshi_interval_seconds=300,
        weather_interval_seconds=1800,
        stale_after_intervals=3,
    )
    by_name = {c.collector: c for c in report.collectors}
    assert by_name["kalshi"].stale is True
    assert by_name["kalshi"].last_run_at is None
    assert by_name["weather"].runs_recorded == 0
    assert report.newest_data["observation_issuance"] is None
    assert report.database_size_bytes is None  # sqlite -> n/a, no crash


async def test_recent_run_is_live_old_run_is_stale(session: AsyncSession) -> None:
    await _record(session, "kalshi", minutes_ago=2)  # within 3x 300s
    await _record(session, "weather", minutes_ago=600)  # far beyond 3x 1800s
    report = await build_health_report(
        session,
        kalshi_interval_seconds=300,
        weather_interval_seconds=1800,
        stale_after_intervals=3,
    )
    by_name = {c.collector: c for c in report.collectors}
    assert by_name["kalshi"].stale is False
    assert by_name["weather"].stale is True


async def test_success_rate_and_duration_summaries(session: AsyncSession) -> None:
    await _record(session, "kalshi", minutes_ago=10, success=True)
    await _record(session, "kalshi", minutes_ago=7, success=False)
    await _record(session, "kalshi", minutes_ago=4, success=True)
    report = await build_health_report(
        session,
        kalshi_interval_seconds=300,
        weather_interval_seconds=1800,
        stale_after_intervals=100,  # not testing staleness here
    )
    kalshi = next(c for c in report.collectors if c.collector == "kalshi")
    assert kalshi.runs_recorded == 3
    assert kalshi.success_rate_recent == pytest.approx(2 / 3)
    assert kalshi.mean_duration_seconds == pytest.approx(30.0)
    assert kalshi.last_success is True  # newest run succeeded
