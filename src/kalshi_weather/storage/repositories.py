"""Persistence for raw payloads and normalized snapshots.

Raw payloads are append-only and deduplicated by (source, request_key,
content_hash), matching DATA_MODEL.md.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.domain.time import utc_now
from kalshi_weather.kalshi.orderbook import reconstruct_best_quote
from kalshi_weather.storage.models import (
    MarketSnapshot,
    OrderbookSnapshot,
    RawApiPayload,
    SeriesRecord,
    content_hash,
)


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
    raw_payload_id: int | None = None,
) -> SeriesRecord:
    """Upsert a series row; unlike market/orderbook snapshots, series is not
    append-only (DATA_MODEL.md keys it by series_ticker as a single row)."""
    existing = await session.get(SeriesRecord, series_ticker)
    if existing is not None:
        existing.category = category
        existing.title = title
        existing.frequency = frequency
        existing.raw_payload_id = raw_payload_id
        existing.observed_at = utc_now()
        await session.flush()
        return existing

    record = SeriesRecord(
        series_ticker=series_ticker,
        category=category,
        title=title,
        frequency=frequency,
        raw_payload_id=raw_payload_id,
        observed_at=utc_now(),
    )
    session.add(record)
    await session.flush()
    return record


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
) -> MarketSnapshot:
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
        raw_payload_id=raw_payload_id,
        observed_at=utc_now(),
    )
    session.add(record)
    await session.flush()
    return record


async def save_orderbook_snapshot(
    session: AsyncSession,
    *,
    market_ticker: str,
    yes_levels: list[list[int]],
    no_levels: list[list[int]],
    raw_payload_id: int | None,
) -> OrderbookSnapshot:
    yes_bids = [(level[0], level[1]) for level in yes_levels]
    no_bids = [(level[0], level[1]) for level in no_levels]
    quote = reconstruct_best_quote(yes_bids, no_bids)

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
        raw_payload_id=raw_payload_id,
    )
    session.add(record)
    await session.flush()
    return record
