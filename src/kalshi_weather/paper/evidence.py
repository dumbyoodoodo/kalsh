"""Read-only live market evidence for the forward paper loop.

Queries the production research database with SELECTs only and converts
what it finds into the execution simulator's own types (via
``execution/history.normalize_book`` -- no second book-normalization
path). Every gate is fail-closed: missing, stale, or unevidenced data is
a recorded rejection, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.execution.history import normalize_book
from kalshi_weather.execution.market import OrderBook
from kalshi_weather.storage.models import MarketPollAttempt, MarketSnapshot, OrderbookSnapshot

#: Poll outcomes that PROVE the ticker was directly observed (ADR 0020).
_OBSERVED_OUTCOMES = ("succeeded_new_data", "succeeded_unchanged", "succeeded_empty")


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class MarketEvidence:
    """Everything a paper decision may know about one market right now."""

    ticker: str
    snapshot_source_id: int | None
    snapshot_observed_at: datetime | None
    market_status: str | None
    yes_bid_cents: int | None
    yes_ask_cents: int | None
    book: OrderBook | None
    book_source_id: int | None
    poll_evidence_at: datetime | None

    def snapshot_age_seconds(self, now: datetime) -> float | None:
        at = _utc(self.snapshot_observed_at)
        return None if at is None else (now - at).total_seconds()

    def book_age_seconds(self, now: datetime) -> float | None:
        return None if self.book is None else (now - _utc(self.book.captured_at)).total_seconds()  # type: ignore[operator]

    def poll_evidence_age_seconds(self, now: datetime) -> float | None:
        at = _utc(self.poll_evidence_at)
        return None if at is None else (now - at).total_seconds()


async def load_market_evidence(
    session: AsyncSession, tickers: list[str], *, now: datetime
) -> dict[str, MarketEvidence]:
    """Latest snapshot, latest order book, and latest direct-poll evidence per
    ticker. Read-only; absent rows yield None fields (gated later)."""
    out: dict[str, MarketEvidence] = {}
    for ticker in tickers:
        snap = (
            await session.scalars(
                select(MarketSnapshot)
                .where(MarketSnapshot.market_ticker == ticker)
                .order_by(MarketSnapshot.observed_at.desc())
                .limit(1)
            )
        ).first()
        book_row = (
            await session.scalars(
                select(OrderbookSnapshot)
                .where(OrderbookSnapshot.market_ticker == ticker)
                .order_by(OrderbookSnapshot.captured_at.desc())
                .limit(1)
            )
        ).first()
        poll = (
            await session.scalars(
                select(MarketPollAttempt)
                .where(
                    MarketPollAttempt.ticker == ticker,
                    MarketPollAttempt.endpoint_type == "orderbook",
                    MarketPollAttempt.outcome.in_(_OBSERVED_OUTCOMES),
                )
                .order_by(MarketPollAttempt.completed_at.desc())
                .limit(1)
            )
        ).first()
        out[ticker] = MarketEvidence(
            ticker=ticker,
            snapshot_source_id=snap.id if snap else None,
            snapshot_observed_at=_utc(snap.observed_at) if snap else None,
            market_status=snap.status if snap else None,
            yes_bid_cents=snap.yes_bid_cents if snap else None,
            yes_ask_cents=snap.yes_ask_cents if snap else None,
            book=normalize_book(book_row) if book_row else None,
            book_source_id=book_row.id if book_row else None,
            poll_evidence_at=_utc(poll.completed_at) if poll else None,
        )
    return out
