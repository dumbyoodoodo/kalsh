"""Persistence for raw payloads and normalized snapshots.

Raw payloads are append-only and deduplicated by (source, request_key,
content_hash), matching DATA_MODEL.md. Market/orderbook snapshots are also
append-only, but a new snapshot identical to the immediately-prior one for
the same ticker is skipped rather than inserted -- see save_market_snapshot
and save_orderbook_snapshot, and docs/adr/0002-ingestion-collector.md.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.domain.time import utc_now
from kalshi_weather.kalshi.orderbook import reconstruct_best_quote
from kalshi_weather.storage.models import (
    EventRecord,
    MarketSnapshot,
    OrderbookSnapshot,
    RawApiPayload,
    SeriesRecord,
    SettlementSpecRecord,
    TradeRecord,
    WeatherForecast,
    WeatherObservation,
    WeatherStation,
    content_hash,
)


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
) -> RawApiPayload:
    """Insert a raw payload, or return the existing row if it's an exact duplicate."""
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
) -> SaveResult[MarketSnapshot]:
    """Insert a market snapshot, unless it's identical to the immediately-prior
    snapshot for this ticker (see _market_snapshot_content_hash) -- in which
    case the prior row is returned with was_duplicate=True. Nothing is ever
    overwritten either way; a duplicate is simply not appended."""
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
        content_hash=hash_value,
        raw_payload_id=raw_payload_id,
        observed_at=utc_now(),
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
) -> SaveResult[OrderbookSnapshot]:
    """Insert an order-book snapshot, unless the book (yes/no levels) is
    identical to the immediately-prior snapshot for this ticker."""
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
) -> SaveResult[TradeRecord]:
    """Insert a trade, unless a row with this trade_id already exists --
    trades are immutable and keyed by Kalshi's own trade_id, so a repeat
    fetch (e.g. an overlapping min_ts window) is a duplicate, not new data."""
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
    )
    session.add(record)
    await session.flush()
    return SaveResult(record=record, was_duplicate=False)


async def get_latest_trade_timestamp(
    session: AsyncSession, market_ticker: str
) -> datetime | None:
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
