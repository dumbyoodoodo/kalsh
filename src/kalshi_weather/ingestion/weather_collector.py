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
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalshi_weather.domain.time import utc_now
from kalshi_weather.ingestion import weather_attempts as wa
from kalshi_weather.ingestion.validation import (
    WEATHER_TIMESTAMP_FLOOR,
    MalformedPayloadError,
    validate_temperature_f,
    validate_timestamp,
)
from kalshi_weather.logging import get_logger
from kalshi_weather.storage.database import session_scope
from kalshi_weather.storage.repositories import (
    append_terminal_attempt,
    get_latest_observation_date,
    record_collector_run,
    save_weather_forecast,
    save_weather_observation,
    save_weather_station,
)
from kalshi_weather.weather.provider import WeatherProvider
from kalshi_weather.weather.stations import Station, list_stations

logger = get_logger(__name__)


class AttemptEvidenceIncomplete(RuntimeError):
    """Fewer terminal attempts were persisted than the cycle expected.

    Raised inside the persistence scope so the whole scope rolls back: a run
    that cannot carry complete evidence must not commit looking attributed.
    """


_OBS = wa.WeatherProductType.CLI_OBSERVATIONS
_FC = wa.WeatherProductType.GRIDPOINT_FORECAST


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


@dataclass(slots=True)
class WeatherCycleResult:
    """Everything one cycle produced: business stats AND its attempt evidence.

    Evidence is returned, never injected through a caller-owned optional list.
    The previous design let ``attempts=None`` mean "record nothing", and the
    production loop simply never passed it -- so a fully successful deployment
    recorded zero evidence (production run 3290, 0 of 14 expected attempts).
    Making the sink part of the return type removes that failure mode: a caller
    cannot forget to opt in.
    """

    stats: WeatherCycleStats
    pending_attempts: tuple[wa.PendingWeatherCollectionAttempt, ...]
    expected_pairs: tuple[tuple[str, wa.WeatherProductType], ...]
    started_at: datetime
    completed_at: datetime

    def as_dict(self) -> dict[str, int]:
        return self.stats.as_dict()


async def run_weather_collection_cycle(
    provider: WeatherProvider,
    session: AsyncSession,
    *,
    stations: list[Station] | None = None,
    backfill_days: int,
    environment: str = "production",
) -> WeatherCycleResult:
    """Run one full weather collection pass across `stations` (default: the
    whole registry). Each station is isolated: a failure for one station is
    logged and counted, and collection continues with the next station.

    When ``attempts`` and ``collector_run_id`` are supplied, one terminal
    station-level attempt record per station/product is accumulated into
    ``attempts`` for the caller to append after the cycle. Passing neither
    leaves behaviour byte-identical to the pre-instrumentation collector, so
    existing callers and tests are unaffected."""
    cycle_started = utc_now()
    stats = WeatherCycleStats()
    target_stations = stations if stations is not None else list_stations()
    pending: list[wa.PendingWeatherCollectionAttempt] = []
    expected: list[tuple[str, wa.WeatherProductType]] = []
    for station in target_stations:
        expected.append((station.station_id, _OBS))
        expected.append((station.station_id, _FC))

    for station in target_stations:
        try:
            await _collect_station(
                provider,
                session,
                station,
                backfill_days=backfill_days,
                stats=stats,
                pending=pending,
                environment=environment,
            )
            stats.stations_processed += 1
        except Exception:
            # One station's failure never erases another station's evidence:
            # any attempt already recorded for this station stays in `attempts`,
            # and the loop continues to the next station.
            logger.exception("weather_collector.station_failed", station=station.station_id)
            stats.errors += 1

    return WeatherCycleResult(
        stats=stats,
        pending_attempts=tuple(pending),
        expected_pairs=tuple(expected),
        started_at=cycle_started,
        completed_at=utc_now(),
    )


@dataclass(slots=True)
class _AttemptContext:
    """Mutable in-memory state for ONE logical attempt.

    Retries mutate this object; exactly one terminal record is appended from it
    at the end, so a retried request never produces two rows.
    """

    station: Station
    product_type: wa.WeatherProductType
    endpoint: str
    requested_at: datetime
    target_local_date: date | None = None
    stage: wa.AttemptStage = wa.AttemptStage.REQUEST_STARTED
    outcome: wa.AttemptOutcome = wa.AttemptOutcome.UNKNOWN_FAILURE
    availability: wa.SourceAvailability = wa.SourceAvailability.UNKNOWN
    retry_count: int = 0
    http_status: int | None = None
    raw_payload_id: int | None = None
    parsed: int = 0
    persisted: int = 0
    duplicate: int = 0
    parser_error_type: str | None = None
    parser_error_message: str | None = None
    persistence_error_type: str | None = None
    persistence_error_message: str | None = None
    source_product_id: str | None = None


def _station_local_date(station: Station, moment: datetime) -> date:
    """Station-LOCAL calendar date, never a UTC day. PHX is fixed UTC-7."""
    return moment.astimezone(ZoneInfo(station.timezone)).date()


def _build_pending(ctx: _AttemptContext, *, environment: str) -> wa.PendingWeatherCollectionAttempt:
    """Materialize the single PENDING record for one logical attempt.

    No collector_run_id: it does not exist yet and a placeholder would be a
    lie that could reach the database.
    """
    key = wa.logical_request_key(
        collector_run_id=0,  # excluded from identity below; see target_window
        environment=environment,
        station_code=ctx.station.station_id,
        product_type=ctx.product_type,
        target_window=(ctx.target_local_date.isoformat() if ctx.target_local_date else "n/a"),
    )
    # The run id is not known yet, so identity is keyed on the request itself.
    # The run scopes it at persistence time via the uniqueness constraint.
    key = key.split("|", 1)[1]
    now = utc_now()
    return wa.PendingWeatherCollectionAttempt(
        attempt_id=wa.attempt_id_for(f"{key}|{ctx.requested_at.isoformat()}"),
        environment=environment,
        station_code=ctx.station.station_id,
        wfo=ctx.station.wfo_site,
        product_type=ctx.product_type,
        logical_request_key=key,
        source_endpoint=ctx.endpoint,
        stage=ctx.stage,
        outcome=ctx.outcome,
        source_availability=ctx.availability,
        requested_at=ctx.requested_at,
        completed_at=now,
        observed_at=now,
        target_station_local_date=ctx.target_local_date,
        http_status=ctx.http_status,
        retry_count=ctx.retry_count,
        parser_name="nws_cli_parser" if ctx.product_type is _OBS else None,
        parser_version=wa.ATTEMPT_SCHEMA_VERSION,
        parser_error_type=ctx.parser_error_type,
        parser_error_message=wa.sanitize_error(ctx.parser_error_message),
        raw_payload_id=ctx.raw_payload_id,
        parsed_entity_count=ctx.parsed,
        persisted_entity_count=ctx.persisted,
        duplicate_entity_count=ctx.duplicate,
        persistence_error_type=ctx.persistence_error_type,
        persistence_error_message=wa.sanitize_error(ctx.persistence_error_message),
        source_product_id=ctx.source_product_id,
    )


def _terminalize(ctx: _AttemptContext) -> None:
    """Derive the terminal outcome from committed counts.

    Precedence when a request yields BOTH new and duplicate entities:
    ``SUCCEEDED_NEW_DATA`` wins, because new data is the stronger operational
    fact. Both counts are preserved on the record regardless -- neither is
    discarded (see the runbook's mixed-success note).
    """
    if ctx.outcome is not wa.AttemptOutcome.UNKNOWN_FAILURE:
        return  # an explicit failure outcome was already set
    ctx.availability = wa.SourceAvailability.AVAILABLE
    if ctx.persisted > 0:
        ctx.outcome = wa.AttemptOutcome.SUCCEEDED_NEW_DATA
    elif ctx.duplicate > 0:
        ctx.outcome = wa.AttemptOutcome.SUCCEEDED_DUPLICATE
    else:
        ctx.outcome = wa.AttemptOutcome.SUCCEEDED_NO_DATA
    ctx.stage = wa.AttemptStage.NORMALIZED_PERSISTED


def _record_attempt(
    pending: list[wa.PendingWeatherCollectionAttempt],
    ctx: _AttemptContext,
    environment: str,
) -> None:
    """Append exactly one pending terminal record for a logical attempt.

    A malformed attempt is logged and dropped rather than raised into the
    station loop: losing one station's weather data to an evidence bug would be
    strictly worse than losing that evidence. The loss is NOT silent -- the
    post-cycle reconciliation compares pending count against expected pairs and
    fails loudly if any are missing.
    """
    try:
        pending.append(_build_pending(ctx, environment=environment))
    except Exception:
        logger.exception(
            "weather_collector.attempt_record_failed",
            station=ctx.station.station_id,
            product=str(ctx.product_type),
        )


async def _collect_station(
    provider: WeatherProvider,
    session: AsyncSession,
    station: Station,
    *,
    backfill_days: int,
    stats: WeatherCycleStats,
    pending: list[wa.PendingWeatherCollectionAttempt],
    environment: str = "production",
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

    # --- CLI observations: one logical attempt, terminalized once -----------
    obs_ctx = _AttemptContext(
        station=station,
        product_type=_OBS,
        endpoint="nws:cli-observations",
        requested_at=utc_now(),
        target_local_date=_station_local_date(station, utc_now()),
    )
    try:
        observations = await provider.get_observations(station, start=start, end=today)
        obs_ctx.stage = wa.AttemptStage.RESPONSE_RECEIVED
        obs_ctx.raw_payload_id = getattr(provider, "last_raw_payload_id", None)
        if obs_ctx.raw_payload_id is not None:
            obs_ctx.stage = wa.AttemptStage.RAW_PAYLOAD_PERSISTED
    except Exception as exc:  # provider error: no body, source not proven down
        obs_ctx.outcome = wa.AttemptOutcome.REQUEST_FAILED
        obs_ctx.availability = wa.SourceAvailability.UNKNOWN
        obs_ctx.parser_error_message = wa.sanitize_error(f"{type(exc).__name__}: {exc}")
        _record_attempt(pending, obs_ctx, environment)
        raise

    for obs in observations:
        obs_ctx.parsed += 1
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
            # Parser rejected a body we DID retrieve: preserve the rejection and
            # the payload that was refused. This is the evidence the pilot
            # review could not previously attribute to a station.
            obs_ctx.outcome = wa.AttemptOutcome.PARSER_REJECTED
            obs_ctx.availability = wa.SourceAvailability.AVAILABLE
            obs_ctx.parser_error_type = type(exc).__name__
            obs_ctx.parser_error_message = str(exc)
            obs_ctx.stage = wa.AttemptStage.PARSED
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
            obs_ctx.duplicate += 1
        else:
            stats.observations_saved += 1
            obs_ctx.persisted += 1
        obs_ctx.source_product_id = obs.source_product_id

    _terminalize(obs_ctx)
    _record_attempt(pending, obs_ctx, environment)

    # --- gridpoint forecast: an independent logical attempt ------------------
    fc_ctx = _AttemptContext(
        station=station,
        product_type=_FC,
        endpoint="nws:gridpoint-forecast",
        requested_at=utc_now(),
        target_local_date=_station_local_date(station, utc_now()),
    )
    try:
        forecasts = await provider.get_forecast(station)
        fc_ctx.stage = wa.AttemptStage.RESPONSE_RECEIVED
        fc_ctx.raw_payload_id = getattr(provider, "last_raw_payload_id", None)
        if fc_ctx.raw_payload_id is not None:
            fc_ctx.stage = wa.AttemptStage.RAW_PAYLOAD_PERSISTED
    except Exception as exc:
        fc_ctx.outcome = wa.AttemptOutcome.REQUEST_FAILED
        fc_ctx.availability = wa.SourceAvailability.UNKNOWN
        fc_ctx.parser_error_message = wa.sanitize_error(f"{type(exc).__name__}: {exc}")
        _record_attempt(pending, fc_ctx, environment)
        raise

    for fc in forecasts:
        fc_ctx.parsed += 1
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
            fc_ctx.outcome = wa.AttemptOutcome.PARSER_REJECTED
            fc_ctx.availability = wa.SourceAvailability.AVAILABLE
            fc_ctx.parser_error_type = type(exc).__name__
            fc_ctx.parser_error_message = str(exc)
            fc_ctx.stage = wa.AttemptStage.PARSED
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
            fc_ctx.duplicate += 1
        else:
            stats.forecasts_saved += 1
            fc_ctx.persisted += 1

    _terminalize(fc_ctx)
    _record_attempt(pending, fc_ctx, environment)


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
        started_at = utc_now()
        run_stats: dict[str, Any] = {}
        run_requests = run_retries = 0
        run_error: str | None = None
        cycle_result: WeatherCycleResult | None = None
        try:
            async with session_scope(session_factory) as session:
                provider = provider_factory(session)
                async with provider:
                    cycle_result = await run_weather_collection_cycle(
                        provider, session, stations=stations, backfill_days=backfill_days
                    )
                    run_stats = cycle_result.as_dict()
                    # Prospective instrumentation marker. No historical run
                    # carries it, so "this run should have attempts" is a
                    # recorded fact rather than a timestamp guess.
                    run_stats[wa.ATTEMPT_INSTRUMENTATION_KEY] = int(
                        wa.ATTEMPT_INSTRUMENTATION_VERSION
                    )
                    run_requests = getattr(provider, "requests_attempted", 0)
                    run_retries = getattr(provider, "retries", 0)
            logger.info(
                "weather_collector.cycle_complete", cycle=cycle_number, **cycle_result.as_dict()
            )
        except Exception as exc:
            run_error = f"{type(exc).__name__}: {exc}"
            logger.exception("weather_collector.cycle_failed", cycle=cycle_number)

        # Phase 2: persist the collector run, then materialize and append this
        # cycle's attempt evidence with the REAL run id.
        #
        # Transaction choice: one session_scope owns both writes. The run record
        # is flushed first so the FK target exists, then every attempt is
        # appended, then the scope commits. A failure anywhere rolls the whole
        # scope back -- so a failed run insert can never leave orphan attempts,
        # and a failed attempt append never commits a run that falsely looks
        # fully attributed. Business weather data committed in phase 1 is NOT
        # rolled back: collection succeeding while its evidence fails is a real,
        # separately-visible state, not something to conceal.
        attempt_error: str | None = None
        try:
            async with session_scope(session_factory) as session:
                run_record = await record_collector_run(
                    session,
                    collector="weather",
                    started_at=started_at,
                    finished_at=utc_now(),
                    success=run_error is None,
                    requests_attempted=run_requests,
                    retries=run_retries,
                    stats=run_stats,
                    error=run_error,
                )
                run_id = int(run_record.id)
                pending = cycle_result.pending_attempts if cycle_result is not None else ()
                created_at = utc_now()
                appended = 0
                for item in pending:
                    await append_terminal_attempt(
                        session, item.materialize(run_id, created_at=created_at)
                    )
                    appended += 1
                expected_n = len(cycle_result.expected_pairs) if cycle_result is not None else 0
                if expected_n and appended != expected_n:
                    # Loud: partial evidence must never read as complete.
                    raise AttemptEvidenceIncomplete(
                        f"run {run_id}: appended {appended} of {expected_n} expected attempts"
                    )
                logger.info(
                    "weather_collector.attempts_recorded",
                    cycle=cycle_number,
                    collector_run_id=run_id,
                    attempts=appended,
                    expected=expected_n,
                )
        except Exception as exc:
            attempt_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "weather_collector.attempt_evidence_failed",
                cycle=cycle_number,
                error=attempt_error,
            )

        if max_cycles is not None and cycle_number >= max_cycles:
            return

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
