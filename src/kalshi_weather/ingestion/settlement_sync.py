"""Settled-transition capture: final snapshots for markets that left the
open list before settling.

Discovery is open-status-filtered (COLLECTOR_MARKET_STATUS=open), so a
market disappears from discovery the moment it closes -- *before* a final
result-bearing snapshot exists. Nothing downstream that keys off "latest
snapshot has a result" (price-sync candidacy, retention monitoring,
settlement labels) ever sees it. That structural starvation stalled candle
ingestion and settlement capture for every market settling after
2026-07-21 (production-gap investigation, 2026-07-23).

Each collection cycle re-checks a *bounded* number of recently-tracked
tickers that are absent from the current open list, via one `get_market`
call each. The pending queue is derived purely from stored data -- a
ticker whose latest snapshot has no `result` yet -- so it is idempotent
and self-draining: capturing a settled snapshot removes the ticker from
the queue (its latest snapshot now bears a result), and content-hash dedup
in `save_market_snapshot` makes re-checks no-ops. A recency window bounds
retries for markets that vanish without ever exposing a settlement
(delisted/expired unsettled), so the queue cannot grow without bound and
the full historical settled archive is never re-fetched.
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.domain.time import utc_now
from kalshi_weather.ingestion.discovery import persist_market_snapshot
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.logging import get_logger
from kalshi_weather.storage.models import MarketSnapshot

logger = get_logger(__name__)


@dataclass(slots=True)
class SettleSyncStats:
    checked: int = 0
    settled_captured: int = 0
    still_unsettled: int = 0
    errors: int = 0


async def find_tickers_pending_settlement_check(
    session: AsyncSession,
    *,
    exclude_tickers: set[str],
    recent_days: int,
    limit: int,
) -> list[str]:
    """Tickers whose latest stored snapshot carries no settlement result yet,
    was observed within `recent_days`, and which are absent from the current
    open-market discovery (`exclude_tickers`) -- i.e. recently tracked, now
    gone from the open list, settlement not yet captured. Oldest close_time
    first (mirrors price-sync's retention-priority ordering)."""
    cutoff = (utc_now() - timedelta(days=recent_days)).replace(tzinfo=None)
    latest_ids = (
        select(func.max(MarketSnapshot.id)).group_by(MarketSnapshot.market_ticker).scalar_subquery()
    )
    stmt = (
        select(MarketSnapshot.market_ticker)
        .where(
            MarketSnapshot.id.in_(latest_ids),
            MarketSnapshot.result.is_(None) | (MarketSnapshot.result == ""),
            MarketSnapshot.observed_at >= cutoff,
        )
        .order_by(MarketSnapshot.close_time.asc().nulls_last())
    )
    rows = (await session.scalars(stmt)).all()
    pending = [t for t in rows if t not in exclude_tickers]
    return pending[:limit]


async def capture_settled_transitions(
    client: KalshiClient,
    session: AsyncSession,
    *,
    open_tickers: set[str],
    limit: int,
    recent_days: int,
) -> SettleSyncStats:
    """Fetch each pending ticker once and persist whatever state it reports.

    A settled market yields the final result-bearing snapshot (new row via
    content-hash change); a still-open/closed-unsettled market dedups or
    appends normally and stays queued until it settles or ages past the
    recency window. Per-ticker failures (404 for a delisted market, a
    transient API error) are logged and counted, never raised -- one bad
    ticker must not abort the cycle, matching the collector's existing
    isolation contract."""
    stats = SettleSyncStats()
    pending = await find_tickers_pending_settlement_check(
        session, exclude_tickers=open_tickers, recent_days=recent_days, limit=limit
    )
    for ticker in pending:
        stats.checked += 1
        try:
            market = await client.get_market(ticker)
            save_result = await persist_market_snapshot(
                session, market, raw_payload_id=client.last_raw_payload_id
            )
            if market.result in ("yes", "no"):
                stats.settled_captured += 1
                logger.info(
                    "collector.settle_capture.final_snapshot",
                    ticker=ticker,
                    result=market.result,
                    settlement_ts=(
                        market.settlement_ts.isoformat() if market.settlement_ts else None
                    ),
                    was_duplicate=save_result.was_duplicate,
                )
            else:
                stats.still_unsettled += 1
        except Exception as exc:
            stats.errors += 1
            logger.warning(
                "collector.settle_capture.failed",
                ticker=ticker,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
    return stats
