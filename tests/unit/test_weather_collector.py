import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.ingestion.weather_collector import (
    run_weather_collection_cycle,
    run_weather_collector_loop,
)
from kalshi_weather.storage.models import Base
from kalshi_weather.weather.models import ForecastRecord, ObservationRecord, StationMetadata
from kalshi_weather.weather.stations import Station

STATION = Station(
    station_id="NYC",
    source_location_code="NYC",
    name="Central Park, NY",
    latitude=Decimal("40.7829"),
    longitude=Decimal("-73.9654"),
    timezone="America/New_York",
    city="New York",
    wfo_site="OKX",
)

OTHER_STATION = Station(
    station_id="BAD",
    source_location_code="BAD",
    name="Broken Station",
    latitude=Decimal("0"),
    longitude=Decimal("0"),
    timezone="UTC",
    city="Nowhere",
    wfo_site="XXX",
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


def _metadata(station: Station) -> StationMetadata:
    return StationMetadata(
        station_id=station.station_id,
        provider="nws",
        source_location_code=station.source_location_code,
        office="OKX",
        latitude=station.latitude,
        longitude=station.longitude,
        name=station.name,
        timezone=station.timezone,
    )


def _observation(
    station: Station,
    *,
    variable: str,
    value: Decimal,
    observation_date: date,
    issuance_time: datetime,
    product_id: str = "prod-1",
) -> ObservationRecord:
    return ObservationRecord(
        station_id=station.station_id,
        provider="nws",
        variable=variable,
        value=value,
        unit="F",
        observation_date=observation_date,
        issuance_time=issuance_time,
        source_product_id=product_id,
        raw_payload_id=1,
    )


def _forecast(
    station: Station,
    *,
    variable: str = "temperature",
    point_estimate: Decimal = Decimal(75),
    issue_time: datetime,
    valid_start: datetime,
    valid_end: datetime,
) -> ForecastRecord:
    return ForecastRecord(
        station_id=station.station_id,
        provider="nws",
        variable=variable,
        point_estimate=point_estimate,
        unit="F",
        issue_time=issue_time,
        valid_start=valid_start,
        valid_end=valid_end,
        raw_payload_id=1,
    )


class FakeProvider:
    """Minimal stand-in for WeatherProvider, structurally satisfying the
    Protocol without going through httpx at all."""

    def __init__(
        self,
        *,
        metadata_by_station: dict[str, StationMetadata],
        observations_by_station: dict[str, list[ObservationRecord]],
        forecasts_by_station: dict[str, list[ForecastRecord]],
        raise_for_stations: set[str] | None = None,
    ) -> None:
        self.last_raw_payload_id: int | None = None
        self._metadata_by_station = metadata_by_station
        self._observations_by_station = observations_by_station
        self._forecasts_by_station = forecasts_by_station
        self._raise_for_stations = raise_for_stations or set()

    async def __aenter__(self) -> "FakeProvider":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get_station_metadata(self, station: Station) -> StationMetadata:
        if station.station_id in self._raise_for_stations:
            raise RuntimeError(f"boom: {station.station_id}")
        return self._metadata_by_station[station.station_id]

    async def get_observations(
        self, station: Station, *, start: date, end: date
    ) -> list[ObservationRecord]:
        return self._observations_by_station.get(station.station_id, [])

    async def get_forecast(self, station: Station) -> list[ForecastRecord]:
        return self._forecasts_by_station.get(station.station_id, [])


def _basic_provider() -> FakeProvider:
    issuance = datetime(2026, 7, 20, 20, 37, tzinfo=UTC)
    obs_date = date(2026, 7, 20)
    valid_start = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)
    valid_end = datetime(2026, 7, 21, 6, 0, tzinfo=UTC)
    return FakeProvider(
        metadata_by_station={STATION.station_id: _metadata(STATION)},
        observations_by_station={
            STATION.station_id: [
                _observation(
                    STATION,
                    variable="tmax_f",
                    value=Decimal(81),
                    observation_date=obs_date,
                    issuance_time=issuance,
                ),
                _observation(
                    STATION,
                    variable="tmin_f",
                    value=Decimal(63),
                    observation_date=obs_date,
                    issuance_time=issuance,
                ),
            ]
        },
        forecasts_by_station={
            STATION.station_id: [
                _forecast(
                    STATION, issue_time=issuance, valid_start=valid_start, valid_end=valid_end
                )
            ]
        },
    )


async def test_run_weather_collection_cycle_saves_observations_and_forecasts(
    session: AsyncSession,
) -> None:
    provider = _basic_provider()
    stats = (
        await run_weather_collection_cycle(provider, session, stations=[STATION], backfill_days=30)
    ).stats

    assert stats.stations_processed == 1
    assert stats.observations_saved == 2
    assert stats.observations_duplicate == 0
    assert stats.forecasts_saved == 1
    assert stats.forecasts_duplicate == 0
    assert stats.invalid_items == 0
    assert stats.errors == 0


async def test_run_weather_collection_cycle_second_run_dedupes(session: AsyncSession) -> None:
    provider = _basic_provider()
    await run_weather_collection_cycle(provider, session, stations=[STATION], backfill_days=30)

    provider2 = _basic_provider()
    stats = (
        await run_weather_collection_cycle(provider2, session, stations=[STATION], backfill_days=30)
    ).stats

    assert stats.observations_saved == 0
    assert stats.observations_duplicate == 2
    assert stats.forecasts_saved == 0
    assert stats.forecasts_duplicate == 1


async def test_run_weather_collection_cycle_rejects_implausible_temperature(
    session: AsyncSession,
) -> None:
    issuance = datetime(2026, 7, 20, 20, 37, tzinfo=UTC)
    obs_date = date(2026, 7, 20)
    provider = FakeProvider(
        metadata_by_station={STATION.station_id: _metadata(STATION)},
        observations_by_station={
            STATION.station_id: [
                _observation(
                    STATION,
                    variable="tmax_f",
                    value=Decimal(999),
                    observation_date=obs_date,
                    issuance_time=issuance,
                ),
            ]
        },
        forecasts_by_station={},
    )

    stats = (
        await run_weather_collection_cycle(provider, session, stations=[STATION], backfill_days=30)
    ).stats

    assert stats.observations_saved == 0
    assert stats.invalid_items == 1


async def test_run_weather_collection_cycle_isolates_per_station_errors(
    session: AsyncSession,
) -> None:
    good = _basic_provider()
    provider = FakeProvider(
        metadata_by_station={STATION.station_id: good._metadata_by_station[STATION.station_id]},
        observations_by_station=good._observations_by_station,
        forecasts_by_station=good._forecasts_by_station,
        raise_for_stations={OTHER_STATION.station_id},
    )

    stats = (
        await run_weather_collection_cycle(
            provider, session, stations=[STATION, OTHER_STATION], backfill_days=30
        )
    ).stats

    assert stats.stations_processed == 1
    assert stats.errors == 1
    assert stats.observations_saved == 2


async def test_run_weather_collector_loop_respects_max_cycles() -> None:
    call_count = 0

    def provider_factory(_session: AsyncSession) -> FakeProvider:
        nonlocal call_count
        call_count += 1
        return _basic_provider()

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    stop_event = asyncio.Event()
    await run_weather_collector_loop(
        session_factory=session_factory,
        provider_factory=provider_factory,
        stations=[STATION],
        backfill_days=30,
        interval_seconds=0,
        stop_event=stop_event,
        max_cycles=2,
    )
    await engine.dispose()

    assert call_count == 2


async def test_run_weather_collector_loop_honors_already_set_stop_event() -> None:
    call_count = 0

    def provider_factory(_session: AsyncSession) -> FakeProvider:
        nonlocal call_count
        call_count += 1
        return _basic_provider()

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    stop_event = asyncio.Event()
    stop_event.set()
    await run_weather_collector_loop(
        session_factory=session_factory,
        provider_factory=provider_factory,
        stations=[STATION],
        backfill_days=30,
        interval_seconds=60,
        stop_event=stop_event,
        max_cycles=None,
    )
    await engine.dispose()

    assert call_count == 0


async def test_run_weather_collector_loop_records_run_metrics() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    await run_weather_collector_loop(
        session_factory=session_factory,
        provider_factory=lambda _s: _basic_provider(),
        stations=[STATION],
        backfill_days=30,
        interval_seconds=0,
        stop_event=asyncio.Event(),
        max_cycles=1,
    )

    from sqlalchemy import select

    from kalshi_weather.storage.models import CollectorRun

    async with session_factory() as session:
        runs = (await session.scalars(select(CollectorRun))).all()
    await engine.dispose()

    assert len(runs) == 1
    assert runs[0].collector == "weather"
    assert runs[0].success
    assert runs[0].stats_json["observations_saved"] == 2
