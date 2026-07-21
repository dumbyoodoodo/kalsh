import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.domain.time import utc_now
from kalshi_weather.ops.quality import EXPECTED_DB_REVISION, run_quality_checks
from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import (
    save_market_snapshot,
    save_series,
    save_weather_observation,
    save_weather_station,
)

NYC_CLI_URL = "https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC"


@pytest.fixture
async def session():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # simulate a correctly migrated DB so the schema check passes
        await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        await conn.execute(
            text(f"INSERT INTO alembic_version VALUES ('{EXPECTED_DB_REVISION}')")
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _finding(report, check):  # type: ignore[no-untyped-def]
    return next(f for f in report.findings if f.check == check)


async def _seed_station(session: AsyncSession) -> None:
    await save_weather_station(
        session,
        station_id="NYC",
        provider="nws",
        source_location_code="NYC",
        office="OKX",
        latitude=Decimal("40.7829"),
        longitude=Decimal("-73.9654"),
        name="Central Park, NY",
        timezone="America/New_York",
    )


async def test_clean_empty_store_is_ok_with_gap_warnings(session: AsyncSession) -> None:
    report = await run_quality_checks(session)
    assert report.ok  # no errors -- gaps are warnings, not corruption
    assert _finding(report, "missing_observations_NYC").count > 0
    assert _finding(report, "missing_forecasts").severity == "info"
    assert _finding(report, "duplicate_records").count == 0
    assert _finding(report, "timestamp_anomalies").count == 0
    assert json.dumps(report.to_dict())  # machine-readable


async def test_schema_mismatch_is_an_error(session: AsyncSession) -> None:
    await session.execute(text("UPDATE alembic_version SET version_num = '0001'"))
    report = await run_quality_checks(session)
    assert not report.ok
    finding = _finding(report, "unexpected_schema_change")
    assert finding.severity == "error"
    assert "0001" in finding.message


async def test_future_timestamp_is_an_error(session: AsyncSession) -> None:
    await _seed_station(session)
    await save_weather_observation(
        session,
        station_id="NYC",
        provider="nws",
        variable="tmax_f",
        value=Decimal(80),
        unit="F",
        observation_date=date(2026, 7, 20),
        issuance_time=utc_now() + timedelta(days=3),  # future-dated
        source_product_id="p-future",
        raw_payload_id=None,
    )
    report = await run_quality_checks(session)
    assert not report.ok
    assert _finding(report, "timestamp_anomalies").count == 1


async def test_recent_observations_shrink_the_gap_count(session: AsyncSession) -> None:
    await _seed_station(session)
    yesterday = (utc_now() - timedelta(days=1)).date()
    await save_weather_observation(
        session,
        station_id="NYC",
        provider="nws",
        variable="tmax_f",
        value=Decimal(80),
        unit="F",
        observation_date=yesterday,
        issuance_time=utc_now() - timedelta(hours=2),
        source_product_id="p-yday",
        raw_payload_id=None,
    )
    report = await run_quality_checks(session)
    assert _finding(report, "missing_observations_NYC").count == 29  # 30-day window - 1


async def test_unresolvable_market_appears_in_resolution_failures(
    session: AsyncSession,
) -> None:
    await save_series(
        session,
        series_ticker="KXNYCSNOWM",
        category="Climate and Weather",
        title="NYC Snowfall monthly",
        frequency="monthly",
        settlement_source=json.dumps([{"url": "https://www.weather.gov"}]),
    )
    await save_market_snapshot(
        session,
        market_ticker="KXNYCSNOWM-26AUG-A",
        event_ticker="KXNYCSNOWM-26AUG",
        market_type="binary",
        title="t",
        subtitle=None,
        status="open",
        yes_bid_cents=None,
        yes_ask_cents=None,
        last_price_cents=None,
        volume=None,
        open_interest=None,
        close_time=datetime(2026, 9, 1, tzinfo=UTC),
        rules_primary="If total snowfall...",
        rules_secondary=None,
        raw_payload_id=None,
    )
    report = await run_quality_checks(session)
    finding = _finding(report, "settlement_resolution_failures")
    assert finding.count == 1
    assert finding.severity == "warning"
    assert "KXNYCSNOWM-26AUG-A" in finding.samples
