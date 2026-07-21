import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.ops.snapshot import create_snapshot, default_snapshot_version
from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import (
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


async def _seed_weather(session: AsyncSession) -> None:
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
    await save_weather_observation(
        session,
        station_id="NYC",
        provider="nws",
        variable="tmax_f",
        value=Decimal(81),
        unit="F",
        observation_date=date(2026, 7, 20),
        issuance_time=datetime(2026, 7, 21, 6, tzinfo=UTC),
        source_product_id="p1",
        raw_payload_id=None,
    )


def test_default_snapshot_version_is_date_keyed() -> None:
    assert default_snapshot_version().startswith("snapshot-20")


async def test_snapshot_writes_dataset_plus_sidecars(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _seed_weather(session)
    result = await create_snapshot(
        session,
        database_url="sqlite://",
        overrides_path=None,
        root=tmp_path,
        version="snapshot-test",
        kalshi_interval_seconds=300,
        weather_interval_seconds=1800,
        stale_after_intervals=3,
    )
    d = result.output_dir
    for name in (
        "weather_panel.parquet",
        "market_weather.parquet",
        "manifest.json",
        "validation.json",
        "stats.json",
        "quality.json",
        "ops.json",
    ):
        assert (d / name).exists(), name

    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["version"] == "snapshot-test"
    assert manifest["config"]["resolver"]["parser_version"]
    assert "content_hashes" in manifest

    ops = json.loads((d / "ops.json").read_text())
    assert "health" in ops and "settlement_resolution" in ops
    assert isinstance(ops["recent_collector_runs"], list)

    quality = json.loads((d / "quality.json").read_text())
    assert "findings" in quality
    assert result.dataset_ok is True


async def test_snapshot_version_is_immutable(session: AsyncSession, tmp_path: Path) -> None:
    await _seed_weather(session)
    kwargs = dict(
        database_url="sqlite://",
        overrides_path=None,
        root=tmp_path,
        version="snapshot-test",
        kalshi_interval_seconds=300.0,
        weather_interval_seconds=1800.0,
        stale_after_intervals=3.0,
    )
    await create_snapshot(session, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(FileExistsError):
        await create_snapshot(session, **kwargs)  # type: ignore[arg-type]
