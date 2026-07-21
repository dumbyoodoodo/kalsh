"""Chunked, resumable historical observation backfill (Phase 6 operations).

The collector's built-in backfill (`WEATHER_BACKFILL_DAYS`) fetches its whole
window inside one cycle/transaction -- fine for ~30 days, fragile for years: a
failure at day 900 of 1095 rolls back everything, and there is no progress
visibility. This module runs a deep backfill as an explicit operation:

- the requested range is split into chunks (default 30 days), each fetched,
  validated, and **committed independently** -- a crash loses at most one
  chunk of work;
- a failing chunk is recorded and skipped, and the backfill continues
  (partial-failure tolerance); the run reports exactly which chunks failed;
- re-running the same command is safe (dedup by issuance keys) and cheap:
  chunks whose every day already has stored observations are skipped by
  default (``skip_covered``) -- this is what makes an interrupted backfill
  resumable without refetching completed work;
- per-chunk progress (coverage: days-with-data / days-in-chunk) is reported
  as it goes and verified incrementally against the store.

Runtime expectations (documented in docs/runbooks/operations.md): IEM serves
~1 listing request per day plus ~1 text request per issuance (~2/day), so one
station-year is ~1,100 requests; at the default 0.1s spacing plus network
latency, plan on roughly 5-15 minutes per station-year. Storage is trivial:
~1,500 observation rows per station-year plus raw payloads.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalshi_weather.ingestion.validation import (
    WEATHER_TIMESTAMP_FLOOR,
    MalformedPayloadError,
    validate_temperature_f,
    validate_timestamp,
)
from kalshi_weather.logging import get_logger
from kalshi_weather.storage.database import session_scope
from kalshi_weather.storage.models import WeatherObservation
from kalshi_weather.storage.repositories import save_weather_observation, save_weather_station
from kalshi_weather.weather.provider import WeatherProvider
from kalshi_weather.weather.stations import Station

logger = get_logger(__name__)

DEFAULT_CHUNK_DAYS = 30


@dataclass(slots=True)
class ChunkResult:
    start: date
    end: date
    saved: int = 0
    duplicate: int = 0
    invalid: int = 0
    days_covered: int = 0
    skipped: bool = False
    error: str | None = None


@dataclass(slots=True)
class BackfillReport:
    station_id: str
    start: date
    end: date
    chunks: list[ChunkResult] = field(default_factory=list)

    @property
    def failed_chunks(self) -> list[ChunkResult]:
        return [c for c in self.chunks if c.error is not None]

    @property
    def ok(self) -> bool:
        return not self.failed_chunks

    def totals(self) -> dict[str, int]:
        return {
            "chunks": len(self.chunks),
            "chunks_skipped": sum(1 for c in self.chunks if c.skipped),
            "chunks_failed": len(self.failed_chunks),
            "saved": sum(c.saved for c in self.chunks),
            "duplicate": sum(c.duplicate for c in self.chunks),
            "invalid": sum(c.invalid for c in self.chunks),
            "days_covered": sum(c.days_covered for c in self.chunks),
        }


def chunk_ranges(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    """Split [start, end] (inclusive) into consecutive chunks of at most
    chunk_days, oldest first."""
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    if chunk_days < 1:
        raise ValueError("chunk_days must be >= 1")
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


async def _covered_days(
    session: AsyncSession, station_id: str, start: date, end: date
) -> int:
    """Distinct observation dates already stored for the station in-range."""
    result = await session.scalar(
        select(func.count(func.distinct(WeatherObservation.observation_date))).where(
            WeatherObservation.station_id == station_id,
            WeatherObservation.observation_date >= start,
            WeatherObservation.observation_date <= end,
        )
    )
    return int(result or 0)


async def run_backfill(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    provider: WeatherProvider,
    station: Station,
    start: date,
    end: date,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    skip_covered: bool = True,
    stop_event: asyncio.Event | None = None,
) -> BackfillReport:
    """Backfill observations for one station across [start, end], one
    committed transaction per chunk. Returns a full per-chunk report; failed
    chunks are recorded and skipped, never silently ignored."""
    report = BackfillReport(station_id=station.station_id, start=start, end=end)
    chunks = chunk_ranges(start, end, chunk_days)

    # Station metadata upsert once, in its own small transaction.
    async with session_scope(session_factory) as session:
        metadata = await provider.get_station_metadata(station)
        await save_weather_station(
            session,
            station_id=metadata.station_id,
            provider=metadata.provider,
            source_location_code=metadata.source_location_code,
            office=metadata.office,
            latitude=metadata.latitude,
            longitude=metadata.longitude,
            name=metadata.name,
            timezone=metadata.timezone,
        )

    for i, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        if stop_event is not None and stop_event.is_set():
            logger.info(
                "backfill.stopped_early",
                station=station.station_id,
                completed_chunks=i - 1,
                total_chunks=len(chunks),
            )
            break
        result = ChunkResult(start=chunk_start, end=chunk_end)
        report.chunks.append(result)
        chunk_len = (chunk_end - chunk_start).days + 1

        try:
            async with session_scope(session_factory) as session:
                if skip_covered:
                    covered = await _covered_days(
                        session, station.station_id, chunk_start, chunk_end
                    )
                    if covered >= chunk_len:
                        result.skipped = True
                        result.days_covered = covered
                        logger.info(
                            "backfill.chunk_skipped_covered",
                            station=station.station_id,
                            chunk=f"{chunk_start}..{chunk_end}",
                            progress=f"{i}/{len(chunks)}",
                        )
                        continue

                observations = await provider.get_observations(
                    station, start=chunk_start, end=chunk_end
                )
                for obs in observations:
                    try:
                        validate_timestamp(
                            obs.issuance_time,
                            field_name="issuance_time",
                            floor=WEATHER_TIMESTAMP_FLOOR,
                        )
                        validate_temperature_f(
                            obs.value, field_name=f"observation.{obs.variable}"
                        )
                    except MalformedPayloadError as exc:
                        logger.warning(
                            "backfill.observation_invalid",
                            station=station.station_id,
                            error=str(exc),
                        )
                        result.invalid += 1
                        continue
                    save_result = await save_weather_observation(
                        session,
                        station_id=obs.station_id,
                        provider=obs.provider,
                        variable=obs.variable,
                        value=obs.value,
                        unit=obs.unit,
                        observation_date=obs.observation_date,
                        issuance_time=obs.issuance_time,
                        source_product_id=obs.source_product_id,
                        raw_payload_id=obs.raw_payload_id,
                    )
                    if save_result.was_duplicate:
                        result.duplicate += 1
                    else:
                        result.saved += 1
                # Incremental verification: how much of the chunk is now covered.
                result.days_covered = await _covered_days(
                    session, station.station_id, chunk_start, chunk_end
                )
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "backfill.chunk_failed",
                station=station.station_id,
                chunk=f"{chunk_start}..{chunk_end}",
            )
            continue

        logger.info(
            "backfill.chunk_complete",
            station=station.station_id,
            chunk=f"{chunk_start}..{chunk_end}",
            progress=f"{i}/{len(chunks)}",
            saved=result.saved,
            duplicate=result.duplicate,
            invalid=result.invalid,
            days_covered=f"{result.days_covered}/{chunk_len}",
        )

    return report
