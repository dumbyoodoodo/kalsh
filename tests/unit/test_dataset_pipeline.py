"""End-to-end dataset build against an in-memory SQLite database, exercising
the SQLAlchemy read path, manifest provenance, and reproducibility."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.dataset import pipeline
from kalshi_weather.dataset.market_map import MarketMapping
from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import (
    save_market_snapshot,
    save_weather_forecast,
    save_weather_observation,
    save_weather_station,
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


def _dt(y: int, mo: int, d: int, h: int = 0) -> datetime:
    return datetime(y, mo, d, h, tzinfo=UTC)


async def _seed(session: AsyncSession) -> None:
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
    # provisional then final tmax, plus tmin, for 2026-07-20
    for value, issued in ((Decimal(79), _dt(2026, 7, 20, 20)), (Decimal(81), _dt(2026, 7, 21, 6))):
        await save_weather_observation(
            session, station_id="NYC", provider="nws", variable="tmax_f", value=value,
            unit="F", observation_date=date(2026, 7, 20), issuance_time=issued,
            source_product_id=f"p-{issued.isoformat()}", raw_payload_id=None,
        )
    await save_weather_observation(
        session, station_id="NYC", provider="nws", variable="tmin_f", value=Decimal(63),
        unit="F", observation_date=date(2026, 7, 20), issuance_time=_dt(2026, 7, 21, 6),
        source_product_id="p-tmin", raw_payload_id=None,
    )
    # a daytime forecast period for 2026-07-20 (16:00 UTC = noon EDT -> local 7-20)
    await save_weather_forecast(
        session, station_id="NYC", provider="nws", variable="temperature",
        point_estimate=Decimal(83), unit="F", issue_time=_dt(2026, 7, 19, 12),
        valid_start=_dt(2026, 7, 20, 16), valid_end=_dt(2026, 7, 20, 23), raw_payload_id=None,
    )
    await save_market_snapshot(
        session, market_ticker="KXHIGHNY-20", event_ticker="E", market_type="binary",
        title="t", subtitle="s", status="open", yes_bid_cents=40, yes_ask_cents=45,
        last_price_cents=42, volume=10, open_interest=5, close_time=None,
        rules_primary="High temp in Central Park", rules_secondary=None, raw_payload_id=None,
    )
    await session.flush()


_MAP = [MarketMapping("KXHIGHNY-20", "NYC", "tmax_f", date(2026, 7, 20))]


async def test_pipeline_build_produces_valid_datasets(session: AsyncSession) -> None:
    await _seed(session)
    output = await pipeline.build(
        session, database_url="sqlite://", mappings=_MAP, which="all", version="v1"
    )

    assert output.frames["weather_panel"].height == 1
    assert output.frames["market_weather"].height == 1

    panel = output.frames["weather_panel"].row(0, named=True)
    assert panel["settled_tmax_f"] == 81.0  # latest issuance wins
    assert panel["residual_high_f"] == 2.0  # 83 forecast - 81 settled

    mw = output.frames["market_weather"].row(0, named=True)
    assert mw["forecast_high_f"] == 83.0
    assert mw["settled_value"] == 81.0

    assert output.validation.ok
    assert set(output.manifest.content_hashes) == {"weather_panel", "market_weather"}
    # sqlite test DB has no alembic_version table -> None, and no crash
    assert output.manifest.source_db_revision is None
    assert "sqlite" in output.manifest.database_url


async def test_pipeline_build_is_reproducible(session: AsyncSession) -> None:
    await _seed(session)
    first = await pipeline.build(
        session, database_url="sqlite://", mappings=_MAP, version="v1"
    )
    second = await pipeline.build(
        session, database_url="sqlite://", mappings=_MAP, version="v2"
    )
    # same source data + same mapping -> identical content hashes despite a
    # different version label and build time.
    assert first.manifest.content_hashes == second.manifest.content_hashes


async def test_pipeline_export_writes_versioned_dir(session: AsyncSession, tmp_path) -> None:  # type: ignore[no-untyped-def]
    await _seed(session)
    output = await pipeline.build(
        session, database_url="sqlite://", mappings=_MAP, version="v1"
    )
    from kalshi_weather.dataset.export import ExportFormat

    result = pipeline.export(output, root=tmp_path, version="v1", fmt=ExportFormat.PARQUET)
    assert (result.output_dir / "weather_panel.parquet").exists()
    assert (result.output_dir / "manifest.json").exists()
    assert (result.output_dir / "validation.json").exists()
    assert (result.output_dir / "stats.json").exists()
