"""Orchestrates one collection cycle (discovery -> market snapshots -> order
books -> trades) and, for continuous operation, a loop around it with
graceful shutdown. See docs/runbooks/collector.md for start/stop procedure.
"""

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalshi_weather.ingestion.discovery import discover_and_snapshot_weather_markets
from kalshi_weather.ingestion.validation import MalformedPayloadError, validate_price_cents
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.logging import get_logger
from kalshi_weather.storage.database import session_scope
from kalshi_weather.storage.repositories import (
    get_latest_trade_timestamp,
    save_orderbook_snapshot,
    save_trade,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class CycleStats:
    markets_discovered: int = 0
    market_snapshots_saved: int = 0
    market_snapshots_duplicate: int = 0
    orderbooks_saved: int = 0
    orderbooks_duplicate: int = 0
    trades_saved: int = 0
    trades_duplicate: int = 0
    invalid_items: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


async def run_collection_cycle(
    client: KalshiClient,
    session: AsyncSession,
    *,
    category: str,
    market_status: str,
) -> CycleStats:
    """Run one full discovery + snapshot + trade collection pass.

    Each tracked market's order-book and trade collection is isolated: a
    failure or malformed item for one market is logged and counted, and
    collection continues with the next market rather than aborting the
    cycle.
    """
    stats = CycleStats()

    discovery = await discover_and_snapshot_weather_markets(
        client, session, category=category, market_status=market_status
    )
    stats.markets_discovered = len(discovery.market_tickers)
    stats.market_snapshots_saved = discovery.market_snapshots_saved
    stats.market_snapshots_duplicate = discovery.market_snapshots_duplicate
    stats.invalid_items += discovery.invalid_items

    for ticker in discovery.market_tickers:
        try:
            book = await client.get_orderbook(ticker)
            orderbook_result = await save_orderbook_snapshot(
                session,
                market_ticker=ticker,
                yes_levels=book.orderbook.yes,
                no_levels=book.orderbook.no,
                raw_payload_id=client.last_raw_payload_id,
            )
            if orderbook_result.was_duplicate:
                stats.orderbooks_duplicate += 1
            else:
                stats.orderbooks_saved += 1
        except Exception:
            logger.exception("collector.orderbook.failed", ticker=ticker)
            stats.errors += 1

        try:
            latest_trade_at = await get_latest_trade_timestamp(session, ticker)
            min_ts = int(latest_trade_at.timestamp()) if latest_trade_at is not None else None
            trades = await client.list_trades(ticker=ticker, min_ts=min_ts)
            for trade in trades:
                if trade.created_time is None or trade.yes_price is None or trade.count is None:
                    logger.warning("collector.trade.missing_fields", trade_id=trade.trade_id)
                    stats.invalid_items += 1
                    continue
                try:
                    validate_price_cents(trade.yes_price, field_name="trade.yes_price")
                    validate_price_cents(trade.no_price, field_name="trade.no_price")
                except MalformedPayloadError as exc:
                    logger.warning(
                        "collector.trade.invalid", trade_id=trade.trade_id, error=str(exc)
                    )
                    stats.invalid_items += 1
                    continue

                trade_result = await save_trade(
                    session,
                    trade_id=trade.trade_id,
                    market_ticker=ticker,
                    executed_at=trade.created_time,
                    price_cents=trade.yes_price,
                    count=trade.count,
                    taker_side=trade.taker_side,
                    raw_payload_id=client.last_raw_payload_id,
                )
                if trade_result.was_duplicate:
                    stats.trades_duplicate += 1
                else:
                    stats.trades_saved += 1
        except Exception:
            logger.exception("collector.trades.failed", ticker=ticker)
            stats.errors += 1

    return stats


async def run_collector_loop(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    client_factory: Callable[[AsyncSession], KalshiClient],
    category: str,
    market_status: str,
    interval_seconds: float,
    stop_event: asyncio.Event,
    max_cycles: int | None = None,
) -> None:
    """Run collection cycles until `stop_event` is set (or `max_cycles` is
    reached, for `--once`/testing). A fresh session and client are used per
    cycle so a stale DB/HTTP connection can never persist across months of
    runtime. A whole-cycle failure (DB down, network down) is logged and the
    loop waits for the next interval rather than crashing the process.
    """
    cycle_number = 0
    while not stop_event.is_set():
        cycle_number += 1
        try:
            async with session_scope(session_factory) as session:
                client = client_factory(session)
                async with client:
                    stats = await run_collection_cycle(
                        client, session, category=category, market_status=market_status
                    )
            logger.info("collector.cycle_complete", cycle=cycle_number, **stats.as_dict())
        except Exception:
            logger.exception("collector.cycle_failed", cycle=cycle_number)

        if max_cycles is not None and cycle_number >= max_cycles:
            return

        # normal case: interval elapses (TimeoutError, suppressed) and the
        # next cycle proceeds; stop_event being set instead exits the loop.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
