"""Orchestrates weather data collection: for each registered station,
refresh station metadata, collect observations (bounded initial backfill,
then incremental), and collect the current forecast. See
docs/runbooks/weather_collector.md for start/stop and
docs/adr/0003-weather-data-source.md for the source/design.
"""

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalshi_weather.domain.time import utc_now
from kalshi_weather.ingestion.validation import (
    WEATHER_TIMESTAMP_FLOOR,
    MalformedPayloadError,
    validate_temperature_f,
    validate_timestamp,
)
from kalshi_weather.logging import get_logger
from kalshi_weather.storage.database import session_scope
from kalshi_weather.storage.repositories import (
    get_latest_observation_date,
    save_weather_forecast,
    save_weather_observation,
    save_weather_station,
)
from kalshi_weather.weather.provider import WeatherProvider
from kalshi_weather.weather.stations import Station, list_stations

logger = get_logger(__name__)


@dataclass(slots=True)
class WeatherCycleStats:
    stations_processed: int = 0
    observations_saved: int = 0
    observations_duplicate: int = 0
    forecasts_saved: int = 0
    forecasts_duplicate: int = 0
    invalid_items: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


async def run_weather_collection_cycle(
    provider: WeatherProvider,
    session: AsyncSession,
    *,
    stations: list[Station] | None = None,
    backfill_days: int,
) -> WeatherCycleStats:
    """Run one full weather collection pass across `stations` (default: the
    whole registry). Each station is isolated: a failure for one station is
    logged and counted, and collection continues with the next station."""
    stats = WeatherCycleStats()
    target_stations = stations if stations is not None else list_stations()

    for station in target_stations:
        try:
            await _collect_station(
                provider, session, station, backfill_days=backfill_days, stats=stats
            )
            stats.stations_processed += 1
        except Exception:
            logger.exception("weather_collector.station_failed", station=station.station_id)
            stats.errors += 1

    return stats


async def _collect_station(
    provider: WeatherProvider,
    session: AsyncSession,
    station: Station,
    *,
    backfill_days: int,
    stats: WeatherCycleStats,
) -> None:
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

    today = utc_now().date()
    latest_date = await get_latest_observation_date(session, station.station_id, "tmax_f")
    # If we've seen this station before, re-fetch the most recently seen day
    # too, in case a more authoritative issuance (e.g. the final
    # "YESTERDAY" report superseding an earlier "TODAY" one) has since been
    # published for it -- both are stored (append-only), not merged.
    start = today - timedelta(days=backfill_days) if latest_date is None else latest_date

    observations = await provider.get_observations(station, start=start, end=today)
    for obs in observations:
        try:
            validate_timestamp(
                obs.issuance_time, field_name="issuance_time", floor=WEATHER_TIMESTAMP_FLOOR
            )
            validate_temperature_f(obs.value, field_name=f"observation.{obs.variable}")
        except MalformedPayloadError as exc:
            logger.warning(
                "weather_collector.observation_invalid",
                station=station.station_id,
                error=str(exc),
            )
            stats.invalid_items += 1
            continue

        obs_result = await save_weather_observation(
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
        if obs_result.was_duplicate:
            stats.observations_duplicate += 1
        else:
            stats.observations_saved += 1

    forecasts = await provider.get_forecast(station)
    for fc in forecasts:
        try:
            validate_timestamp(
                fc.issue_time, field_name="issue_time", floor=WEATHER_TIMESTAMP_FLOOR
            )
            validate_temperature_f(fc.point_estimate, field_name=f"forecast.{fc.variable}")
        except MalformedPayloadError as exc:
            logger.warning(
                "weather_collector.forecast_invalid", station=station.station_id, error=str(exc)
            )
            stats.invalid_items += 1
            continue

        forecast_result = await save_weather_forecast(
            session,
            station_id=fc.station_id,
            provider=fc.provider,
            variable=fc.variable,
            point_estimate=fc.point_estimate,
            unit=fc.unit,
            issue_time=fc.issue_time,
            valid_start=fc.valid_start,
            valid_end=fc.valid_end,
            raw_payload_id=fc.raw_payload_id,
        )
        if forecast_result.was_duplicate:
            stats.forecasts_duplicate += 1
        else:
            stats.forecasts_saved += 1


async def run_weather_collector_loop(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    provider_factory: Callable[[AsyncSession], WeatherProvider],
    stations: list[Station] | None,
    backfill_days: int,
    interval_seconds: float,
    stop_event: asyncio.Event,
    max_cycles: int | None = None,
) -> None:
    """Run weather collection cycles until `stop_event` is set (or
    `max_cycles` is reached, for `--once`/testing). A fresh session and
    provider are used per cycle; a whole-cycle failure is logged and the
    loop waits for the next interval rather than crashing the process --
    same shape as ingestion/collector.py's run_collector_loop."""
    cycle_number = 0
    while not stop_event.is_set():
        cycle_number += 1
        try:
            async with session_scope(session_factory) as session:
                provider = provider_factory(session)
                async with provider:
                    stats = await run_weather_collection_cycle(
                        provider, session, stations=stations, backfill_days=backfill_days
                    )
            logger.info("weather_collector.cycle_complete", cycle=cycle_number, **stats.as_dict())
        except Exception:
            logger.exception("weather_collector.cycle_failed", cycle=cycle_number)

        if max_cycles is not None and cycle_number >= max_cycles:
            return

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
