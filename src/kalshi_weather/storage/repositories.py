"""Persistence for raw payloads and normalized snapshots.

Raw payloads are append-only and deduplicated by (source, request_key,
content_hash), matching DATA_MODEL.md. Market/orderbook snapshots are also
append-only, but a new snapshot identical to the immediately-prior one for
the same ticker is skipped rather than inserted -- see save_market_snapshot
and save_orderbook_snapshot, and docs/adr/0002-ingestion-collector.md.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.domain.time import utc_now
from kalshi_weather.kalshi.orderbook import reconstruct_best_quote
from kalshi_weather.storage.models import (
    CollectorRun,
    EventRecord,
    MarketCandlestick,
    MarketPollAttempt,
    MarketSnapshot,
    OrderbookSnapshot,
    RawApiPayload,
    SeriesRecord,
    SettlementSpecRecord,
    TradeRecord,
    WeatherCollectionAttempt,
    WeatherForecast,
    WeatherObservation,
    WeatherStation,
    content_hash,
)

if TYPE_CHECKING:
    from kalshi_weather.ingestion.poll_ledger import PollAttempt
    from kalshi_weather.ingestion.weather_attempts import (
        AttemptRecord as WeatherAttemptRecord,
    )


async def record_poll_attempts(
    session: AsyncSession,
    *,
    collector_run_id: int,
    attempts: "list[PollAttempt]",
) -> int:
    """Append per-ticker polling-evidence rows for one cycle. Append-only: never
    updates or deletes. Returns the number of rows written. Deterministic column
    mapping; ``created_at`` is stamped at write time."""
    now = utc_now()
    rows = [
        MarketPollAttempt(
            collector_run_id=collector_run_id,
            ticker=a.ticker,
            endpoint_type=a.endpoint_type.value,
            environment=a.environment,
            eligibility_state=a.eligibility_state.value,
            attempt_state=a.attempt_state.value,
            outcome=a.outcome.value,
            requested_at=a.requested_at,
            completed_at=a.completed_at,
            http_status=a.http_status,
            retry_count=a.retry_count,
            rate_limited=a.rate_limited,
            persisted_row_count=a.persisted_row_count,
            deduplicated=a.deduplicated,
            raw_payload_id=a.raw_payload_id,
            error_class=a.error_class,
            bounded_error_detail=a.bounded_error_detail,
            created_at=now,
        )
        for a in attempts
    ]
    session.add_all(rows)
    await session.flush()
    return len(rows)


@dataclass(frozen=True, slots=True)
class SaveResult[T]:
    """Result of an append-only snapshot save: the row (new or the matching
    prior one) and whether it was skipped as a duplicate of the prior row."""

    record: T
    was_duplicate: bool


async def save_raw_payload(
    session: AsyncSession,
    *,
    source: str,
    endpoint_or_channel: str,
    request_key: str,
    http_status: int,
    payload_json: Any,
    environment: str | None = None,
) -> RawApiPayload:
    """Insert a raw payload, or return the existing row if it's an exact
    duplicate. ``environment`` (ADR 0013) is the Kalshi source environment for
    Kalshi payloads; leave it None for non-Kalshi (e.g. weather) sources."""
    hash_value = content_hash(payload_json)
    existing = await session.scalar(
        select(RawApiPayload).where(
            RawApiPayload.source == source,
            RawApiPayload.request_key == request_key,
            RawApiPayload.content_hash == hash_value,
        )
    )
    if existing is not None:
        return existing

    record = RawApiPayload(
        source=source,
        endpoint_or_channel=endpoint_or_channel,
        request_key=request_key,
        received_at=utc_now(),
        http_status=http_status,
        content_hash=hash_value,
        payload_json=payload_json,
        schema_version="1",
        environment=environment,
    )
    session.add(record)
    await session.flush()
    return record


async def save_series(
    session: AsyncSession,
    *,
    series_ticker: str,
    category: str | None,
    title: str | None,
    frequency: str | None,
    settlement_source: str | None = None,
    source_updated_at: datetime | None = None,
    raw_payload_id: int | None = None,
) -> SeriesRecord:
    """Upsert a series row; unlike market/orderbook snapshots, series is not
    append-only (DATA_MODEL.md keys it by series_ticker as a single row).

    ``settlement_source`` is the JSON-serialized ``settlement_sources`` list
    from Kalshi's series payload -- the structured settlement citation the
    settlement parser reads (settlement/parser.py). ``None`` leaves any
    previously stored value in place rather than erasing it (a caller that
    doesn't know the sources must not destroy them)."""
    existing = await session.get(SeriesRecord, series_ticker)
    if existing is not None:
        existing.category = category
        existing.title = title
        existing.frequency = frequency
        if settlement_source is not None:
            existing.settlement_source = settlement_source
        existing.source_updated_at = source_updated_at
        existing.raw_payload_id = raw_payload_id
        existing.observed_at = utc_now()
        await session.flush()
        return existing

    record = SeriesRecord(
        series_ticker=series_ticker,
        category=category,
        title=title,
        frequency=frequency,
        settlement_source=settlement_source,
        source_updated_at=source_updated_at,
        raw_payload_id=raw_payload_id,
        observed_at=utc_now(),
    )
    session.add(record)
    await session.flush()
    return record


async def save_event(
    session: AsyncSession,
    *,
    event_ticker: str,
    series_ticker: str | None,
    category: str | None,
    title: str | None,
    sub_title: str | None,
    source_updated_at: datetime | None = None,
    raw_payload_id: int | None = None,
) -> EventRecord:
    """Upsert an event row; not append-only, same rationale as save_series."""
    existing = await session.get(EventRecord, event_ticker)
    if existing is not None:
        existing.series_ticker = series_ticker
        existing.category = category
        existing.title = title
        existing.sub_title = sub_title
        existing.source_updated_at = source_updated_at
        existing.raw_payload_id = raw_payload_id
        existing.observed_at = utc_now()
        await session.flush()
        return existing

    record = EventRecord(
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        category=category,
        title=title,
        sub_title=sub_title,
        source_updated_at=source_updated_at,
        raw_payload_id=raw_payload_id,
        observed_at=utc_now(),
    )
    session.add(record)
    await session.flush()
    return record


def _market_snapshot_content_hash(
    *,
    market_type: str | None,
    title: str | None,
    subtitle: str | None,
    status: str | None,
    yes_bid_cents: int | None,
    yes_ask_cents: int | None,
    last_price_cents: int | None,
    volume: int | None,
    open_interest: int | None,
    result: str | None = None,
    expiration_value: Decimal | None = None,
    expiration_time: datetime | None = None,
) -> str:
    """Hash of the fields that matter for "did this market actually change"
    -- deliberately excludes id/observed_at/raw_payload_id/event_ticker/
    close_time/rules (rules and close_time don't change meaningfully between
    polls; including them wouldn't affect dedup in practice since a market's
    identity ties them to the ticker, but they're left out to keep the hash
    focused on the fields a poller actually expects to change)."""
    return content_hash(
        {
            "market_type": market_type,
            "title": title,
            "subtitle": subtitle,
            "status": status,
            "yes_bid_cents": yes_bid_cents,
            "yes_ask_cents": yes_ask_cents,
            "last_price_cents": last_price_cents,
            "volume": volume,
            "open_interest": open_interest,
            # settlement transition must produce a (final) snapshot
            "result": result,
            "expiration_value": str(expiration_value) if expiration_value is not None else None,
            # Included deliberately (ADR 0011): if the venue moves a market's
            # finality deadline that is a meaningful change and should append a
            # snapshot. Note this changes the hash function itself, so the
            # first poll after migration 0008 appends one extra snapshot per
            # market -- additive, never a rewrite.
            "expiration_time": expiration_time.isoformat() if expiration_time else None,
        }
    )


async def save_market_snapshot(
    session: AsyncSession,
    *,
    market_ticker: str,
    event_ticker: str | None,
    market_type: str | None,
    title: str | None,
    subtitle: str | None,
    status: str | None,
    yes_bid_cents: int | None,
    yes_ask_cents: int | None,
    last_price_cents: int | None,
    volume: int | None,
    open_interest: int | None,
    close_time: Any,
    rules_primary: str | None,
    rules_secondary: str | None,
    raw_payload_id: int | None,
    source_updated_at: datetime | None = None,
    result: str | None = None,
    expiration_value: Decimal | None = None,
    settlement_ts: datetime | None = None,
    expiration_time: datetime | None = None,
    floor_strike: Decimal | None = None,
    cap_strike: Decimal | None = None,
    strike_type: str | None = None,
    environment: str | None = None,
) -> SaveResult[MarketSnapshot]:
    """Insert a market snapshot, unless it's identical to the immediately-prior
    snapshot for this ticker (see _market_snapshot_content_hash) -- in which
    case the prior row is returned with was_duplicate=True. Nothing is ever
    overwritten either way; a duplicate is simply not appended.

    ``environment`` (ADR 0013) is provenance, deliberately NOT part of the
    content hash: it records where a row came from, not what the market did, so
    it must never by itself force a new snapshot."""
    hash_value = _market_snapshot_content_hash(
        market_type=market_type,
        title=title,
        subtitle=subtitle,
        status=status,
        yes_bid_cents=yes_bid_cents,
        yes_ask_cents=yes_ask_cents,
        last_price_cents=last_price_cents,
        volume=volume,
        open_interest=open_interest,
        result=result,
        expiration_value=expiration_value,
        expiration_time=expiration_time,
    )

    latest = await session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.market_ticker == market_ticker)
        .order_by(MarketSnapshot.id.desc())
        .limit(1)
    )
    if latest is not None and latest.content_hash == hash_value:
        return SaveResult(record=latest, was_duplicate=True)

    record = MarketSnapshot(
        market_ticker=market_ticker,
        event_ticker=event_ticker,
        market_type=market_type,
        title=title,
        subtitle=subtitle,
        status=status,
        yes_bid_cents=yes_bid_cents,
        yes_ask_cents=yes_ask_cents,
        last_price_cents=last_price_cents,
        volume=volume,
        open_interest=open_interest,
        close_time=close_time,
        rules_primary=rules_primary,
        rules_secondary=rules_secondary,
        source_updated_at=source_updated_at,
        result=result,
        expiration_value=expiration_value,
        settlement_ts=settlement_ts,
        expiration_time=expiration_time,
        floor_strike=floor_strike,
        cap_strike=cap_strike,
        strike_type=strike_type,
        content_hash=hash_value,
        raw_payload_id=raw_payload_id,
        observed_at=utc_now(),
        environment=environment,
    )
    session.add(record)
    await session.flush()
    return SaveResult(record=record, was_duplicate=False)


async def save_orderbook_snapshot(
    session: AsyncSession,
    *,
    market_ticker: str,
    yes_levels: list[list[int]],
    no_levels: list[list[int]],
    raw_payload_id: int | None,
    environment: str | None = None,
) -> SaveResult[OrderbookSnapshot]:
    """Insert an order-book snapshot, unless the book (yes/no levels) is
    identical to the immediately-prior snapshot for this ticker.
    ``environment`` (ADR 0013) is provenance, not part of the dedup hash."""
    yes_bids = [(level[0], level[1]) for level in yes_levels]
    no_bids = [(level[0], level[1]) for level in no_levels]
    quote = reconstruct_best_quote(yes_bids, no_bids)
    hash_value = content_hash({"yes": yes_levels, "no": no_levels})

    latest = await session.scalar(
        select(OrderbookSnapshot)
        .where(OrderbookSnapshot.market_ticker == market_ticker)
        .order_by(OrderbookSnapshot.id.desc())
        .limit(1)
    )
    if latest is not None and latest.content_hash == hash_value:
        return SaveResult(record=latest, was_duplicate=True)

    record = OrderbookSnapshot(
        market_ticker=market_ticker,
        captured_at=utc_now(),
        yes_levels_json=yes_levels,
        no_levels_json=no_levels,
        best_yes_bid_cents=quote.best_yes_bid_cents,
        best_yes_ask_cents=quote.best_yes_ask_cents,
        best_no_bid_cents=quote.best_no_bid_cents,
        best_no_ask_cents=quote.best_no_ask_cents,
        spread_cents=quote.yes_spread_cents,
        content_hash=hash_value,
        raw_payload_id=raw_payload_id,
        environment=environment,
    )
    session.add(record)
    await session.flush()
    return SaveResult(record=record, was_duplicate=False)


async def save_trade(
    session: AsyncSession,
    *,
    trade_id: str,
    market_ticker: str,
    executed_at: datetime,
    price_cents: int,
    count: int,
    taker_side: str | None,
    raw_payload_id: int | None,
    environment: str | None = None,
) -> SaveResult[TradeRecord]:
    """Insert a trade, unless a row with this trade_id already exists --
    trades are immutable and keyed by Kalshi's own trade_id, so a repeat
    fetch (e.g. an overlapping min_ts window) is a duplicate, not new data.
    ``environment`` (ADR 0013) records the source; demo trades are near-empty."""
    existing = await session.get(TradeRecord, trade_id)
    if existing is not None:
        return SaveResult(record=existing, was_duplicate=True)

    record = TradeRecord(
        trade_id=trade_id,
        market_ticker=market_ticker,
        executed_at=executed_at,
        price_cents=price_cents,
        count=count,
        taker_side=taker_side,
        raw_payload_id=raw_payload_id,
        environment=environment,
    )
    session.add(record)
    await session.flush()
    return SaveResult(record=record, was_duplicate=False)


async def get_latest_trade_timestamp(session: AsyncSession, market_ticker: str) -> datetime | None:
    """Latest stored trade's executed_at for a ticker, used to compute an
    incremental min_ts for the next trade-collection fetch."""
    result: datetime | None = await session.scalar(
        select(TradeRecord.executed_at)
        .where(TradeRecord.market_ticker == market_ticker)
        .order_by(TradeRecord.executed_at.desc())
        .limit(1)
    )
    return result


async def save_weather_station(
    session: AsyncSession,
    *,
    station_id: str,
    provider: str,
    source_location_code: str,
    office: str | None,
    latitude: Decimal,
    longitude: Decimal,
    name: str,
    timezone: str,
    raw_payload_id: int | None = None,
) -> WeatherStation:
    """Upsert a station row; station identity doesn't change, so this is not
    append-only (same rationale as save_series)."""
    existing = await session.get(WeatherStation, station_id)
    if existing is not None:
        existing.provider = provider
        existing.source_location_code = source_location_code
        existing.office = office
        existing.latitude = latitude
        existing.longitude = longitude
        existing.name = name
        existing.timezone = timezone
        existing.raw_payload_id = raw_payload_id
        existing.observed_at = utc_now()
        await session.flush()
        return existing

    record = WeatherStation(
        station_id=station_id,
        provider=provider,
        source_location_code=source_location_code,
        office=office,
        latitude=latitude,
        longitude=longitude,
        name=name,
        timezone=timezone,
        raw_payload_id=raw_payload_id,
        observed_at=utc_now(),
    )
    session.add(record)
    await session.flush()
    return record


async def save_weather_observation(
    session: AsyncSession,
    *,
    station_id: str,
    provider: str,
    variable: str,
    value: Decimal,
    unit: str,
    observation_date: date,
    issuance_time: datetime,
    source_product_id: str,
    raw_payload_id: int | None,
) -> SaveResult[WeatherObservation]:
    """Insert an observation, unless this exact (station, variable,
    issuance_time) has already been stored -- CLI reports are reissued
    multiple times per day and every issuance is kept, never overwritten
    (see storage/models.py WeatherObservation)."""
    existing = await session.scalar(
        select(WeatherObservation).where(
            WeatherObservation.station_id == station_id,
            WeatherObservation.variable == variable,
            WeatherObservation.issuance_time == issuance_time,
        )
    )
    if existing is not None:
        return SaveResult(record=existing, was_duplicate=True)

    record = WeatherObservation(
        station_id=station_id,
        provider=provider,
        variable=variable,
        value=value,
        unit=unit,
        observation_date=observation_date,
        issuance_time=issuance_time,
        source_product_id=source_product_id,
        raw_payload_id=raw_payload_id,
        observed_at=utc_now(),
    )
    session.add(record)
    await session.flush()
    return SaveResult(record=record, was_duplicate=False)


async def save_weather_forecast(
    session: AsyncSession,
    *,
    station_id: str,
    provider: str,
    variable: str,
    point_estimate: Decimal,
    unit: str,
    issue_time: datetime,
    valid_start: datetime,
    valid_end: datetime,
    raw_payload_id: int | None,
) -> SaveResult[WeatherForecast]:
    """Insert a forecast period, unless this exact (station, variable,
    issue_time, valid_start) has already been stored -- never overwrite a
    forecast with a later one (DATA_MODEL.md)."""
    existing = await session.scalar(
        select(WeatherForecast).where(
            WeatherForecast.station_id == station_id,
            WeatherForecast.variable == variable,
            WeatherForecast.issue_time == issue_time,
            WeatherForecast.valid_start == valid_start,
        )
    )
    if existing is not None:
        return SaveResult(record=existing, was_duplicate=True)

    record = WeatherForecast(
        station_id=station_id,
        provider=provider,
        variable=variable,
        point_estimate=point_estimate,
        unit=unit,
        issue_time=issue_time,
        valid_start=valid_start,
        valid_end=valid_end,
        raw_payload_id=raw_payload_id,
        observed_at=utc_now(),
    )
    session.add(record)
    await session.flush()
    return SaveResult(record=record, was_duplicate=False)


async def get_latest_observation_date(
    session: AsyncSession, station_id: str, variable: str
) -> date | None:
    """Latest stored observation_date for a station/variable, used to decide
    where incremental observation collection should resume from."""
    result: date | None = await session.scalar(
        select(WeatherObservation.observation_date)
        .where(
            WeatherObservation.station_id == station_id,
            WeatherObservation.variable == variable,
        )
        .order_by(WeatherObservation.observation_date.desc())
        .limit(1)
    )
    return result


async def save_settlement_spec(
    session: AsyncSession,
    *,
    market_ticker: str,
    series_ticker: str,
    event_ticker: str | None,
    status: str,
    confidence: str,
    city: str | None,
    station_id: str | None,
    variable: str | None,
    target_date: date | None,
    settlement_source: str | None,
    source_url: str | None,
    wfo_site: str | None,
    source_location_code: str | None,
    unit: str | None,
    observation_window: str | None,
    rounding_rule: str | None,
    market_close_time: datetime | None,
    notes: list[str],
    parser_version: str,
    rules_hash: str,
) -> SaveResult[SettlementSpecRecord]:
    """Insert a settlement resolution, unless this exact (market,
    parser_version, rules_hash) is already stored -- re-parsing unchanged
    rules with an unchanged parser is a no-op; a parser upgrade or a rules
    change appends a new row (never overwrites history)."""
    existing = await session.scalar(
        select(SettlementSpecRecord).where(
            SettlementSpecRecord.market_ticker == market_ticker,
            SettlementSpecRecord.parser_version == parser_version,
            SettlementSpecRecord.rules_hash == rules_hash,
        )
    )
    if existing is not None:
        return SaveResult(record=existing, was_duplicate=True)

    record = SettlementSpecRecord(
        market_ticker=market_ticker,
        series_ticker=series_ticker,
        event_ticker=event_ticker,
        status=status,
        confidence=confidence,
        city=city,
        station_id=station_id,
        variable=variable,
        target_date=target_date,
        settlement_source=settlement_source,
        source_url=source_url,
        wfo_site=wfo_site,
        source_location_code=source_location_code,
        unit=unit,
        observation_window=observation_window,
        rounding_rule=rounding_rule,
        market_close_time=market_close_time,
        notes_json=notes,
        parser_version=parser_version,
        rules_hash=rules_hash,
        observed_at=utc_now(),
    )
    session.add(record)
    await session.flush()
    return SaveResult(record=record, was_duplicate=False)


async def get_latest_settlement_specs(
    session: AsyncSession,
) -> list[SettlementSpecRecord]:
    """The most recent stored resolution per market (max id -- rows are
    append-only, so the highest id is the newest)."""
    latest_ids = (
        select(func.max(SettlementSpecRecord.id))
        .group_by(SettlementSpecRecord.market_ticker)
        .scalar_subquery()
    )
    rows = await session.scalars(
        select(SettlementSpecRecord).where(SettlementSpecRecord.id.in_(latest_ids))
    )
    return list(rows.all())


async def record_collector_run(
    session: AsyncSession,
    *,
    collector: str,
    started_at: datetime,
    finished_at: datetime,
    success: bool,
    requests_attempted: int,
    retries: int,
    stats: dict[str, Any],
    error: str | None = None,
) -> CollectorRun:
    """Append one collection cycle's operational record (never updated)."""
    record = CollectorRun(
        collector=collector,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=(finished_at - started_at).total_seconds(),
        success=success,
        requests_attempted=requests_attempted,
        retries=retries,
        stats_json=stats,
        error=error,
    )
    session.add(record)
    await session.flush()
    return record


async def get_recent_collector_runs(
    session: AsyncSession, *, collector: str | None = None, limit: int = 100
) -> list[CollectorRun]:
    """Most recent run records, newest first, optionally for one collector."""
    stmt = select(CollectorRun).order_by(CollectorRun.started_at.desc()).limit(limit)
    if collector is not None:
        stmt = stmt.where(CollectorRun.collector == collector)
    rows = await session.scalars(stmt)
    return list(rows.all())


async def save_market_candlestick(
    session: AsyncSession,
    *,
    market_ticker: str,
    series_ticker: str,
    period_interval_seconds: int,
    period_start: datetime,
    period_end: datetime,
    price_open_cents: int | None,
    price_high_cents: int | None,
    price_low_cents: int | None,
    price_close_cents: int | None,
    price_mean_cents: int | None,
    price_close_is_carried_forward: bool,
    yes_bid_open_cents: int | None,
    yes_bid_high_cents: int | None,
    yes_bid_low_cents: int | None,
    yes_bid_close_cents: int | None,
    yes_ask_open_cents: int | None,
    yes_ask_high_cents: int | None,
    yes_ask_low_cents: int | None,
    yes_ask_close_cents: int | None,
    volume: int,
    open_interest: int | None,
    raw_payload_id: int | None,
    environment: str | None = None,
) -> SaveResult[MarketCandlestick]:
    """Insert a candle, unless this exact (market, resolution, period_end)
    is already stored -- candles are immutable once elapsed (Kalshi's
    endpoint never revises a past period), so a repeat fetch is a duplicate,
    never an overwrite. ``environment`` (ADR 0013) is the price client's source
    (production for the price-sync loop)."""
    existing = await session.scalar(
        select(MarketCandlestick).where(
            MarketCandlestick.market_ticker == market_ticker,
            MarketCandlestick.period_interval_seconds == period_interval_seconds,
            MarketCandlestick.period_end == period_end,
        )
    )
    if existing is not None:
        return SaveResult(record=existing, was_duplicate=True)

    record = MarketCandlestick(
        market_ticker=market_ticker,
        series_ticker=series_ticker,
        period_interval_seconds=period_interval_seconds,
        period_start=period_start,
        period_end=period_end,
        price_open_cents=price_open_cents,
        price_high_cents=price_high_cents,
        price_low_cents=price_low_cents,
        price_close_cents=price_close_cents,
        price_mean_cents=price_mean_cents,
        price_close_is_carried_forward=price_close_is_carried_forward,
        yes_bid_open_cents=yes_bid_open_cents,
        yes_bid_high_cents=yes_bid_high_cents,
        yes_bid_low_cents=yes_bid_low_cents,
        yes_bid_close_cents=yes_bid_close_cents,
        yes_ask_open_cents=yes_ask_open_cents,
        yes_ask_high_cents=yes_ask_high_cents,
        yes_ask_low_cents=yes_ask_low_cents,
        yes_ask_close_cents=yes_ask_close_cents,
        volume=volume,
        open_interest=open_interest,
        raw_payload_id=raw_payload_id,
        observed_at=utc_now(),
        environment=environment,
    )
    session.add(record)
    await session.flush()
    return SaveResult(record=record, was_duplicate=False)


async def get_latest_candlestick_period_end(
    session: AsyncSession, market_ticker: str, period_interval_seconds: int
) -> datetime | None:
    """Latest stored candle's period_end for a market/resolution -- used to
    decide whether a market's coverage already reaches its settlement close
    (skip-covered resume logic in ingestion/price_backfill.py)."""
    result: datetime | None = await session.scalar(
        select(MarketCandlestick.period_end)
        .where(
            MarketCandlestick.market_ticker == market_ticker,
            MarketCandlestick.period_interval_seconds == period_interval_seconds,
        )
        .order_by(MarketCandlestick.period_end.desc())
        .limit(1)
    )
    return result


async def get_candlestick_coverage(
    session: AsyncSession, market_ticker: str, period_interval_seconds: int
) -> dict[str, Any]:
    """Count plus earliest/latest stored candle for a market/resolution --
    the building block for coverage reports (ops/price_coverage.py)."""
    row = (
        await session.execute(
            select(
                func.count(MarketCandlestick.id),
                func.min(MarketCandlestick.period_start),
                func.max(MarketCandlestick.period_end),
            ).where(
                MarketCandlestick.market_ticker == market_ticker,
                MarketCandlestick.period_interval_seconds == period_interval_seconds,
            )
        )
    ).one()
    count, earliest, latest = row
    return {"candle_count": int(count or 0), "earliest": earliest, "latest": latest}


async def list_candlesticks_for_market(
    session: AsyncSession,
    market_ticker: str,
    period_interval_seconds: int,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[MarketCandlestick]:
    """Stored candles for one market/resolution, optionally bounded by
    period_end, oldest first -- used by the research dataset layer."""
    stmt = (
        select(MarketCandlestick)
        .where(
            MarketCandlestick.market_ticker == market_ticker,
            MarketCandlestick.period_interval_seconds == period_interval_seconds,
        )
        .order_by(MarketCandlestick.period_end)
    )
    if start is not None:
        stmt = stmt.where(MarketCandlestick.period_end >= start)
    if end is not None:
        stmt = stmt.where(MarketCandlestick.period_end <= end)
    rows = await session.scalars(stmt)
    return list(rows.all())


async def get_candlestick_covered_tickers(
    session: AsyncSession, period_interval_seconds: int
) -> set[str]:
    """Market tickers that have at least one stored candle at this
    resolution -- the cheap 'already attempted' set the backfill job uses
    for its default skip-covered pass (a stronger per-market completeness
    check, comparing against market close_time, still runs per-market)."""
    rows = await session.scalars(
        select(MarketCandlestick.market_ticker)
        .where(MarketCandlestick.period_interval_seconds == period_interval_seconds)
        .distinct()
    )
    return set(rows.all())


# --- station-level weather collection attempts (migration 0012) --------------


class AttemptAppendResult(NamedTuple):
    """Outcome of an append. ``already_recorded`` means an identical attempt
    was present; the ledger is append-only, so nothing was rewritten."""

    attempt_id: str
    already_recorded: bool


class AttemptConflictError(RuntimeError):
    """A conflicting write was refused. The ledger never silently mutates."""


#: Fields compared when deciding whether a repeated append is byte-equivalent.
_ATTEMPT_IDENTITY_FIELDS = (
    "collector_run_id",
    "environment",
    "station_code",
    "product_type",
    "logical_request_key",
    "stage",
    "outcome",
    "source_availability",
    "parsed_entity_count",
    "persisted_entity_count",
    "duplicate_entity_count",
    "raw_payload_id",
)


async def append_terminal_attempt(
    session: AsyncSession, attempt: "WeatherAttemptRecord"
) -> AttemptAppendResult:
    """Append ONE validated terminal attempt. Never updates or deletes.

    Idempotent by ``attempt_id``: repeating an identical append returns
    ``already_recorded=True``. Repeating the same ``attempt_id`` with different
    content raises ``AttemptConflictError`` rather than overwriting — a
    contradiction in the evidence must surface, not be resolved silently.
    """
    attempt.require_valid()
    existing = (
        await session.execute(
            select(WeatherCollectionAttempt).where(
                WeatherCollectionAttempt.attempt_id == attempt.attempt_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        for field_name in _ATTEMPT_IDENTITY_FIELDS:
            stored = getattr(existing, field_name)
            incoming = getattr(attempt, field_name)
            if isinstance(incoming, StrEnum):
                incoming = str(incoming)
            if stored != incoming:
                raise AttemptConflictError(
                    f"attempt_id {attempt.attempt_id!r} already recorded with a different "
                    f"{field_name}: stored {stored!r} vs incoming {incoming!r}"
                )
        return AttemptAppendResult(attempt.attempt_id, already_recorded=True)

    session.add(
        WeatherCollectionAttempt(
            attempt_id=attempt.attempt_id,
            collector_run_id=attempt.collector_run_id,
            environment=attempt.environment,
            station_code=attempt.station_code,
            wfo=attempt.wfo,
            product_type=str(attempt.product_type),
            logical_request_key=attempt.logical_request_key,
            source_request_id=attempt.source_request_id,
            source_product_id=attempt.source_product_id,
            requested_at=attempt.requested_at,
            completed_at=attempt.completed_at,
            observed_at=attempt.observed_at,
            created_at=attempt.created_at,
            target_station_local_date=attempt.target_station_local_date,
            source_endpoint=attempt.source_endpoint,
            http_status=attempt.http_status,
            retry_count=attempt.retry_count,
            stage=str(attempt.stage),
            outcome=str(attempt.outcome),
            source_availability=str(attempt.source_availability),
            parser_name=attempt.parser_name,
            parser_version=attempt.parser_version,
            parser_error_type=attempt.parser_error_type,
            parser_error_message=attempt.parser_error_message,
            raw_payload_id=attempt.raw_payload_id,
            parsed_entity_count=attempt.parsed_entity_count,
            persisted_entity_count=attempt.persisted_entity_count,
            duplicate_entity_count=attempt.duplicate_entity_count,
            persistence_error_type=attempt.persistence_error_type,
            persistence_error_message=attempt.persistence_error_message,
            schema_version=attempt.schema_version,
        )
    )
    await session.flush()
    return AttemptAppendResult(attempt.attempt_id, already_recorded=False)


async def list_attempts_for_collector_run(
    session: AsyncSession, collector_run_id: int
) -> list[WeatherCollectionAttempt]:
    rows = await session.execute(
        select(WeatherCollectionAttempt)
        .where(WeatherCollectionAttempt.collector_run_id == collector_run_id)
        .order_by(WeatherCollectionAttempt.requested_at)
    )
    return list(rows.scalars().all())


async def list_attempts_for_station_window(
    session: AsyncSession,
    station_code: str,
    *,
    start: datetime,
    end: datetime,
) -> list[WeatherCollectionAttempt]:
    rows = await session.execute(
        select(WeatherCollectionAttempt)
        .where(
            WeatherCollectionAttempt.station_code == station_code,
            WeatherCollectionAttempt.requested_at >= start,
            WeatherCollectionAttempt.requested_at <= end,
        )
        .order_by(WeatherCollectionAttempt.requested_at)
    )
    return list(rows.scalars().all())


async def list_attempts_for_product_window(
    session: AsyncSession,
    product_type: str,
    *,
    start: datetime,
    end: datetime,
) -> list[WeatherCollectionAttempt]:
    rows = await session.execute(
        select(WeatherCollectionAttempt)
        .where(
            WeatherCollectionAttempt.product_type == product_type,
            WeatherCollectionAttempt.requested_at >= start,
            WeatherCollectionAttempt.requested_at <= end,
        )
        .order_by(WeatherCollectionAttempt.requested_at)
    )
    return list(rows.scalars().all())


async def find_attempt_by_id(
    session: AsyncSession, attempt_id: str
) -> WeatherCollectionAttempt | None:
    return (
        await session.execute(
            select(WeatherCollectionAttempt).where(
                WeatherCollectionAttempt.attempt_id == attempt_id
            )
        )
    ).scalar_one_or_none()


async def detect_duplicate_logical_attempts(
    session: AsyncSession, collector_run_id: int
) -> list[str]:
    """Logical keys appearing more than once in a run (should be structurally
    impossible via the unique constraint; checked as defence in depth)."""
    rows = await session.execute(
        select(WeatherCollectionAttempt.logical_request_key, func.count())
        .where(WeatherCollectionAttempt.collector_run_id == collector_run_id)
        .group_by(WeatherCollectionAttempt.logical_request_key)
        .having(func.count() > 1)
    )
    return [str(k) for k, _ in rows.all()]


@dataclass(frozen=True, slots=True)
class AttemptValidationContext:
    """Everything one attempt-integrity validation pass needs, loaded coherently.

    The defect this replaces: attempts were selected by a fixed ROW LIMIT while
    collector runs were selected by a 24-HOUR window. Under degraded collection
    cadence 500 rows reached back ~40h, so attempts legitimately referencing
    runs older than 24h found their run absent from the validation context and
    were reported as ``foreign_collector_run_lineage`` -- 122 CRITICAL findings
    against provably clean data on 2026-08-20.

    The run set is deliberately a UNION of two things, because each catches a
    defect the other cannot:

    - runs STARTED inside the window, so a completed instrumented run with zero
      attempts is still detectable (it references nothing, so a
      referenced-runs-only set would silently disable that CRITICAL rule);
    - runs REFERENCED by the loaded attempts, so lineage context is complete
      even when an attempt's run began before the window or was pushed out by
      the row limit.

    ``truncated`` reports whether the row limit bounded the attempt set, so a
    caller can say "inspected a bounded sample" rather than implying full
    window coverage.
    """

    window_start: datetime
    window_end: datetime
    attempts: tuple[WeatherCollectionAttempt, ...]
    runs: tuple[CollectorRun, ...]
    referenced_run_ids: frozenset[int]
    truncated: bool

    @property
    def runs_in_window(self) -> tuple[CollectorRun, ...]:
        return tuple(r for r in self.runs if self.window_start <= _aware(r.started_at))

    @property
    def coverage_start(self) -> datetime:
        """Earliest instant whose attempts are FULLY represented.

        Without truncation that is ``window_start``. With truncation it is the
        oldest retained attempt: anything older may have had rows dropped, so
        completeness cannot be judged there.
        """
        if not self.truncated or not self.attempts:
            return self.window_start
        return min(_aware(a.requested_at) for a in self.attempts)

    @property
    def completeness_eligible_run_ids(self) -> frozenset[int]:
        """Runs whose attempt set is fully loaded, so completeness may be judged.

        Truncation must not be able to make a run that recorded all its attempts
        look like it recorded none -- that would be the same class of false
        positive this loader exists to remove, only mirrored.
        """
        start = self.coverage_start
        return frozenset(int(r.id) for r in self.runs if _aware(r.started_at) >= start)

    @property
    def missing_referenced_run_ids(self) -> frozenset[int]:
        """Referenced runs that genuinely do not exist in the database.

        After this loader, a non-empty result is a REAL lineage defect rather
        than a query-window artifact.
        """
        return self.referenced_run_ids - {int(r.id) for r in self.runs}


def _aware(value: datetime) -> datetime:
    """Postgres returns naive UTC for these columns; compare in UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def load_attempt_validation_context(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    limit: int = 500,
    collector: str = "weather",
) -> AttemptValidationContext:
    """Load attempts and every collector run needed to validate their lineage.

    ``window_end`` is supplied by the caller and used for BOTH queries, so a run
    or attempt created while validation executes cannot be seen by one query and
    missed by the other.

    ``limit`` bounds attempt ROWS only. Whatever it retains, the runs those rows
    reference are always loaded -- truncation can never manufacture a foreign
    lineage finding.
    """
    attempt_rows = await session.scalars(
        select(WeatherCollectionAttempt)
        .where(
            WeatherCollectionAttempt.requested_at >= window_start,
            WeatherCollectionAttempt.requested_at <= window_end,
        )
        .order_by(WeatherCollectionAttempt.requested_at.desc())
        .limit(limit)
    )
    attempts = tuple(attempt_rows.all())
    truncated = len(attempts) == limit

    referenced = frozenset(
        int(a.collector_run_id) for a in attempts if a.collector_run_id is not None
    )

    window_runs = await session.scalars(
        select(CollectorRun)
        .where(
            CollectorRun.collector == collector,
            CollectorRun.started_at >= window_start,
            CollectorRun.started_at <= window_end,
        )
        .order_by(CollectorRun.started_at)
    )
    runs_by_id: dict[int, CollectorRun] = {int(r.id): r for r in window_runs.all()}

    outstanding = sorted(referenced - set(runs_by_id))
    if outstanding:
        extra = await session.scalars(select(CollectorRun).where(CollectorRun.id.in_(outstanding)))
        for run in extra.all():
            runs_by_id[int(run.id)] = run

    return AttemptValidationContext(
        window_start=window_start,
        window_end=window_end,
        attempts=attempts,
        runs=tuple(sorted(runs_by_id.values(), key=lambda r: _aware(r.started_at))),
        referenced_run_ids=referenced,
        truncated=truncated,
    )
