import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.ingestion.backfill import chunk_ranges, run_backfill
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


class BackfillFakeProvider:
    """Yields one tmax observation per requested day; optionally fails when a
    configured date range is requested (to exercise partial-failure paths)."""

    def __init__(self, *, fail_ranges: list[tuple[date, date]] | None = None) -> None:
        self.last_raw_payload_id: int | None = None
        self.calls: list[tuple[date, date]] = []
        self._fail_ranges = fail_ranges or []

    async def __aenter__(self) -> "BackfillFakeProvider":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get_station_metadata(self, station: Station) -> StationMetadata:
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

    async def get_observations(
        self, station: Station, *, start: date, end: date
    ) -> list[ObservationRecord]:
        self.calls.append((start, end))
        for fail_start, fail_end in self._fail_ranges:
            if start <= fail_end and end >= fail_start:
                raise RuntimeError("simulated IEM outage")
        records = []
        day = start
        while day <= end:
            records.append(
                ObservationRecord(
                    station_id=station.station_id,
                    provider="nws",
                    variable="tmax_f",
                    value=Decimal(80),
                    unit="F",
                    observation_date=day,
                    issuance_time=datetime(day.year, day.month, day.day, 6, tzinfo=UTC)
                    + timedelta(days=1),
                    source_product_id=f"p-{day.isoformat()}",
                    raw_payload_id=None,
                )
            )
            day += timedelta(days=1)
        return records

    async def get_forecast(self, station: Station) -> list[ForecastRecord]:
        return []


def test_chunk_ranges_splits_inclusive_ranges() -> None:
    chunks = chunk_ranges(date(2026, 1, 1), date(2026, 1, 10), 4)
    assert chunks == [
        (date(2026, 1, 1), date(2026, 1, 4)),
        (date(2026, 1, 5), date(2026, 1, 8)),
        (date(2026, 1, 9), date(2026, 1, 10)),
    ]


def test_chunk_ranges_single_day_and_validation() -> None:
    assert chunk_ranges(date(2026, 1, 1), date(2026, 1, 1), 30) == [
        (date(2026, 1, 1), date(2026, 1, 1))
    ]
    with pytest.raises(ValueError):
        chunk_ranges(date(2026, 1, 2), date(2026, 1, 1), 30)
    with pytest.raises(ValueError):
        chunk_ranges(date(2026, 1, 1), date(2026, 1, 2), 0)


async def test_backfill_saves_all_chunks(session_factory) -> None:  # type: ignore[no-untyped-def]
    provider = BackfillFakeProvider()
    report = await run_backfill(
        session_factory=session_factory,
        provider=provider,
        station=STATION,
        start=date(2026, 1, 1),
        end=date(2026, 1, 20),
        chunk_days=7,
    )
    assert report.ok
    totals = report.totals()
    assert totals["chunks"] == 3
    assert totals["saved"] == 20
    assert totals["days_covered"] == 20
    assert totals["chunks_failed"] == 0


async def test_backfill_partial_failure_is_isolated_and_reported(session_factory) -> None:  # type: ignore[no-untyped-def]
    provider = BackfillFakeProvider(fail_ranges=[(date(2026, 1, 8), date(2026, 1, 14))])
    report = await run_backfill(
        session_factory=session_factory,
        provider=provider,
        station=STATION,
        start=date(2026, 1, 1),
        end=date(2026, 1, 21),
        chunk_days=7,
    )
    assert not report.ok
    assert len(report.failed_chunks) == 1
    assert report.failed_chunks[0].start == date(2026, 1, 8)
    # the chunks around the failure still completed
    assert report.totals()["saved"] == 14


async def test_backfill_rerun_skips_covered_chunks_and_fills_gaps(session_factory) -> None:  # type: ignore[no-untyped-def]
    # first run: middle chunk fails
    failing = BackfillFakeProvider(fail_ranges=[(date(2026, 1, 8), date(2026, 1, 14))])
    await run_backfill(
        session_factory=session_factory,
        provider=failing,
        station=STATION,
        start=date(2026, 1, 1),
        end=date(2026, 1, 21),
        chunk_days=7,
    )
    # second run (resume): healthy provider; covered chunks skipped, gap filled
    healthy = BackfillFakeProvider()
    report = await run_backfill(
        session_factory=session_factory,
        provider=healthy,
        station=STATION,
        start=date(2026, 1, 1),
        end=date(2026, 1, 21),
        chunk_days=7,
    )
    assert report.ok
    totals = report.totals()
    assert totals["chunks_skipped"] == 2  # the two chunks completed in run 1
    assert totals["saved"] == 7  # only the failed chunk was fetched and saved
    # only the gap chunk was actually re-fetched
    assert healthy.calls == [(date(2026, 1, 8), date(2026, 1, 14))]


async def test_backfill_rerun_is_idempotent(session_factory) -> None:  # type: ignore[no-untyped-def]
    provider = BackfillFakeProvider()
    await run_backfill(
        session_factory=session_factory,
        provider=provider,
        station=STATION,
        start=date(2026, 1, 1),
        end=date(2026, 1, 10),
        chunk_days=5,
    )
    rerun = await run_backfill(
        session_factory=session_factory,
        provider=BackfillFakeProvider(),
        station=STATION,
        start=date(2026, 1, 1),
        end=date(2026, 1, 10),
        chunk_days=5,
        skip_covered=False,  # force refetch: everything must dedupe, not duplicate
    )
    totals = rerun.totals()
    assert totals["saved"] == 0
    assert totals["duplicate"] == 10


async def test_backfill_stop_event_exits_between_chunks(session_factory) -> None:  # type: ignore[no-untyped-def]
    stop = asyncio.Event()
    stop.set()
    report = await run_backfill(
        session_factory=session_factory,
        provider=BackfillFakeProvider(),
        station=STATION,
        start=date(2026, 1, 1),
        end=date(2026, 3, 31),
        chunk_days=7,
        stop_event=stop,
    )
    assert report.chunks == []  # stopped before the first chunk
