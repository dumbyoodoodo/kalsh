"""Weather-market discovery: finds active weather series/events/markets and
persists series/event metadata plus a market snapshot for each market found.

Settlement-rule parsing (mapping a market to its station/variable/threshold)
is explicitly out of scope here -- see TASKS.md Milestone 2b.
"""

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.ingestion.validation import (
    MalformedPayloadError,
    validate_price_cents,
    validate_ticker,
)
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.kalshi.models import Market
from kalshi_weather.logging import get_logger
from kalshi_weather.storage.models import MarketSnapshot
from kalshi_weather.storage.repositories import (
    SaveResult,
    save_event,
    save_market_snapshot,
    save_series,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class DiscoveryResult:
    market_tickers: list[str] = field(default_factory=list)
    market_snapshots_saved: int = 0
    market_snapshots_duplicate: int = 0
    invalid_items: int = 0


async def persist_market_snapshot(
    session: AsyncSession,
    market: Market,
    *,
    raw_payload_id: int | None,
    environment: str | None = None,
) -> SaveResult[MarketSnapshot]:
    """Persist one `Market` as a snapshot row -- the single shared mapping
    from the wire model to `save_market_snapshot`, used by open-market
    discovery, settled-transition capture (ingestion/settlement_sync.py), and
    metadata revision (ingestion/metadata_revision.py) so the paths can never
    drift apart. ``environment`` (ADR 0013) is the calling client's source, so
    a demo-collected snapshot and a production-revision snapshot are
    distinguishable even under the same ticker.

    Non-temperature climate markets can carry a non-numeric
    expiration_value (e.g. empty string or a categorical outcome); a
    malformed value is logged and stored as None, never raised -- one such
    market must never abort the whole cycle (it did, pre-2026-07-21)."""
    expiration_value: Decimal | None = None
    if market.expiration_value is not None:
        try:
            expiration_value = Decimal(market.expiration_value)
        except (InvalidOperation, ValueError):
            logger.warning(
                "discovery.market.expiration_value_not_numeric",
                ticker=market.ticker,
                value=market.expiration_value,
            )

    return await save_market_snapshot(
        session,
        market_ticker=market.ticker,
        event_ticker=market.event_ticker,
        market_type=market.market_type,
        title=market.title,
        subtitle=market.subtitle,
        status=market.status,
        yes_bid_cents=market.yes_bid,
        yes_ask_cents=market.yes_ask,
        last_price_cents=market.last_price,
        volume=market.volume,
        open_interest=market.open_interest,
        close_time=market.close_time,
        rules_primary=market.rules_primary,
        rules_secondary=market.rules_secondary,
        source_updated_at=market.updated_time,
        result=market.result,
        expiration_value=expiration_value,
        settlement_ts=market.settlement_ts,
        expiration_time=market.expiration_time,
        floor_strike=(
            Decimal(str(market.floor_strike)) if market.floor_strike is not None else None
        ),
        cap_strike=(Decimal(str(market.cap_strike)) if market.cap_strike is not None else None),
        strike_type=market.strike_type,
        raw_payload_id=raw_payload_id,
        environment=environment,
    )


async def discover_and_snapshot_weather_markets(
    client: KalshiClient,
    session: AsyncSession,
    *,
    category: str,
    market_status: str,
) -> DiscoveryResult:
    """Discover weather series/events/markets for `category` (status-filtered
    by `market_status`), persist series/event metadata and a market snapshot
    for each market found.

    A malformed item (per ingestion/validation.py) is logged and skipped,
    not raised -- one bad series/event/market must not abort discovery.
    """
    result = DiscoveryResult()

    series_list = await client.list_series(category=category)
    for series in series_list:
        try:
            validate_ticker(series.ticker, field_name="series.ticker")
        except MalformedPayloadError as exc:
            logger.warning("discovery.series.invalid", error=str(exc))
            result.invalid_items += 1
            continue

        await save_series(
            session,
            series_ticker=series.ticker,
            category=series.category,
            title=series.title,
            frequency=series.frequency,
            # The structured settlement citation (list of {name, url}) the
            # settlement parser reads -- persisted verbatim as JSON.
            settlement_source=(
                json.dumps(series.settlement_sources)
                if series.settlement_sources is not None
                else None
            ),
            source_updated_at=series.last_updated_ts,
            raw_payload_id=client.last_raw_payload_id,
        )

        # status-filtered: an unfiltered call returns a recurring series'
        # entire event history (confirmed live: 185 events vs. 2 with
        # status="open" for one weather series -- see
        # docs/adr/0002-ingestion-collector.md).
        events = await client.list_events(series_ticker=series.ticker, status=market_status)
        for event in events:
            try:
                validate_ticker(event.event_ticker, field_name="event.event_ticker")
            except MalformedPayloadError as exc:
                logger.warning("discovery.event.invalid", error=str(exc))
                result.invalid_items += 1
                continue

            await save_event(
                session,
                event_ticker=event.event_ticker,
                series_ticker=event.series_ticker,
                category=event.category,
                title=event.title,
                sub_title=event.sub_title,
                source_updated_at=event.last_updated_ts,
                raw_payload_id=client.last_raw_payload_id,
            )

            markets = await client.list_markets(
                event_ticker=event.event_ticker, status=market_status
            )
            for market in markets:
                try:
                    validate_ticker(market.ticker, field_name="market.ticker")
                    validate_price_cents(market.yes_bid, field_name="market.yes_bid")
                    validate_price_cents(market.yes_ask, field_name="market.yes_ask")
                    validate_price_cents(market.no_bid, field_name="market.no_bid")
                    validate_price_cents(market.no_ask, field_name="market.no_ask")
                except MalformedPayloadError as exc:
                    logger.warning("discovery.market.invalid", ticker=market.ticker, error=str(exc))
                    result.invalid_items += 1
                    continue

                save_result = await persist_market_snapshot(
                    session,
                    market,
                    raw_payload_id=client.last_raw_payload_id,
                    environment=client.source_environment,
                )
                if save_result.was_duplicate:
                    result.market_snapshots_duplicate += 1
                else:
                    result.market_snapshots_saved += 1
                result.market_tickers.append(market.ticker)

    return result
