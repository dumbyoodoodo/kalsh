"""Read-only adapter from archived production market data into replay events.

Converts explicitly-selected PostgreSQL rows (``orderbook_snapshots``,
``trades``, ``market_snapshots``) into the execution simulator's replay types
(``OrderBook``, ``Trade``, ``SettlementEvent``, ``MarketStatusEvent``), preserving
point-in-time semantics, provenance, ordering, and missingness.

This adapter NEVER writes to the database, never submits orders, never reads
H0019, and never fabricates depth or fills. It requires bounded time and market
filters (no implicit "all history"). See docs/adr/0017-historical-replay-adapter.md.

Archive semantics established by auditing the collector/persistence code
(Phase 1):

* Order books are FULL-depth independent snapshots (``get_orderbook`` is called
  with no depth limit), deduped when unchanged from the immediately-prior book
  for a ticker. ``captured_at`` is the collector's clock at fetch time -- the
  only timestamp available for a book, and effectively its ingestion time
  (Kalshi's orderbook response carries no server timestamp). The stored levels
  are resting bids on each side: ``yes_levels_json`` = YES bids, ``no_levels_json``
  = NO bids. A NO bid at price p is a YES ask at ``100 - p`` (kalshi/orderbook.py),
  so the canonical YES-referenced book is: ``yes_bids`` = yes_levels,
  ``yes_asks`` = {(100 - p, q) for (p, q) in no_levels}.
* Trades are keyed by Kalshi's immutable ``trade_id``; ``executed_at`` is the
  exchange execution time; ``count`` is quantity; ``taker_side`` (yes/no) is the
  reliably-recorded aggressor when present, else unknown.
* Settlement lives on ``market_snapshots``: ``result`` in {yes, no} (empty string
  means not yet settled), ``settlement_ts`` the determination time,
  ``expiration_time`` the venue finality marker, ``observed_at`` when we ingested
  the snapshot.
* ``environment`` (demo | production | unknown | NULL-pre-provenance) is stamped
  from the producing client's base URL, never guessed (ADR 0013/0014).
"""

from __future__ import annotations

import itertools
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.execution.market import MarketData, OrderBook, Trade
from kalshi_weather.execution.models import Side
from kalshi_weather.execution.replay import MarketStatusEvent, SettlementEvent
from kalshi_weather.storage.models import (
    MarketSnapshot,
    OrderbookSnapshot,
    RawApiPayload,
    TradeRecord,
)

CONTRACT_VALUE_CENTS = 100


def _utc(dt: datetime | None) -> datetime | None:
    """Normalize a possibly-naive stored timestamp to timezone-aware UTC.
    Stored timestamps are UTC by construction (CLAUDE.md); some columns are
    ``timestamp without time zone`` and come back naive."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _utc_req(dt: datetime) -> datetime:
    """Normalize a required (non-null) stored timestamp to UTC-aware."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class TimestampMode(StrEnum):
    """Which timestamp drives event visibility/ordering (Phase 4)."""

    #: Idealized public history: use the best exchange/event timestamp.
    EXCHANGE_TIME = "exchange-time"
    #: What this archive actually knew in real time: a record becomes visible
    #: only at its ingestion timestamp.
    COLLECTOR_AVAILABLE = "collector-available"


#: Recognized market states, normalized from Kalshi's ``status`` strings.
class MarketState(StrEnum):
    OPEN = "open"  # "active"
    CLOSED = "closed"  # "closed"
    DETERMINED = "determined"  # "determined"
    FINALIZED = "finalized"  # "finalized"/"settled"
    UNKNOWN = "unknown"


_STATUS_MAP = {
    "active": MarketState.OPEN,
    "open": MarketState.OPEN,
    "closed": MarketState.CLOSED,
    "determined": MarketState.DETERMINED,
    "finalized": MarketState.FINALIZED,
    "settled": MarketState.FINALIZED,
}


@dataclass(frozen=True)
class HistoryEnvironmentPolicy:
    """Which environments are admissible, per Phase 2. Default: production only;
    demo rejected; unknown/NULL rejected unless a data type is explicitly allowed
    (only for data types whose provenance is deterministic)."""

    allowed_environments: frozenset[str] = frozenset({"production"})
    #: Data types for which a NULL/unknown environment is explicitly allowed.
    #: Empty by default -- missing rows are never treated as zero activity.
    allow_null_for: frozenset[str] = frozenset()

    def admits(self, environment: str | None, *, data_type: str) -> bool:
        if environment in self.allowed_environments:
            return True
        if environment in (None, "unknown"):
            return data_type in self.allow_null_for
        return False  # demo (or any other) is rejected

    def to_manifest(self) -> dict[str, object]:
        return {
            "allowed_environments": sorted(self.allowed_environments),
            "allow_null_for": sorted(self.allow_null_for),
            "reject_demo": True,
            "missing_rows_are_missing_never_zero": True,
        }


@dataclass(frozen=True)
class HistoryQuery:
    """Explicit, bounded selection of historical production data. Time range and
    ticker list are REQUIRED -- there is no implicit all-history default."""

    start: datetime
    end: datetime
    tickers: tuple[str, ...]
    mode: TimestampMode = TimestampMode.COLLECTOR_AVAILABLE
    env_policy: HistoryEnvironmentPolicy = field(default_factory=HistoryEnvironmentPolicy)
    max_book_age_seconds: float = 300.0
    include_settlement: bool = True
    data_types: frozenset[str] = frozenset(
        {"order_books", "trades", "settlements", "market_status"}
    )
    source_db_revision: str | None = None

    def __post_init__(self) -> None:
        if not self.tickers:
            raise ValueError("HistoryQuery requires an explicit non-empty ticker list")
        if self.end <= self.start:
            raise ValueError("HistoryQuery end must be after start")

    def to_manifest(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "tickers": list(self.tickers),
            "timestamp_mode": self.mode.value,
            "environment_policy": self.env_policy.to_manifest(),
            "max_book_age_seconds": self.max_book_age_seconds,
            "include_settlement": self.include_settlement,
            "data_types": sorted(self.data_types),
            "source_db_revision": self.source_db_revision,
        }


@dataclass
class ExclusionCounts:
    source_rows_read: int = 0
    events_emitted: int = 0
    exact_duplicates_removed: int = 0
    conflicts_rejected: int = 0
    excluded_by_environment: int = 0
    excluded_by_time_range: int = 0  # rows the query bound already excludes; kept for symmetry
    excluded_malformed: int = 0

    def to_manifest(self) -> dict[str, int]:
        return {
            "source_rows_read": self.source_rows_read,
            "events_emitted": self.events_emitted,
            "exact_duplicates_removed": self.exact_duplicates_removed,
            "conflicts_rejected": self.conflicts_rejected,
            "excluded_by_environment": self.excluded_by_environment,
            "excluded_by_time_range": self.excluded_by_time_range,
            "excluded_malformed": self.excluded_malformed,
        }


@dataclass
class QualityReport:
    order_book_events: int = 0
    trade_events: int = 0
    settlement_events: int = 0
    market_status_events: int = 0
    tickers_requested: int = 0
    tickers_with_books: int = 0
    tickers_with_trades: int = 0
    tickers_with_settlement: int = 0
    max_book_staleness_seconds: float | None = None
    median_book_staleness_seconds: float | None = None
    environments_seen: dict[str, int] = field(default_factory=dict)
    malformed_records: int = 0
    limitations: list[str] = field(default_factory=list)
    #: Per-ticker raw counters the emitted events can't recover (env exclusions,
    #: conflicts, malformed). Book/trade counts and gaps are derived from events.
    per_market: dict[str, dict[str, int]] = field(default_factory=dict)

    def bump(self, ticker: str, key: str, n: int = 1) -> None:
        self.per_market.setdefault(ticker, {})
        self.per_market[ticker][key] = self.per_market[ticker].get(key, 0) + n

    def to_manifest(self) -> dict[str, Any]:
        return {
            "order_book_events": self.order_book_events,
            "trade_events": self.trade_events,
            "settlement_events": self.settlement_events,
            "market_status_events": self.market_status_events,
            "tickers_requested": self.tickers_requested,
            "tickers_with_books": self.tickers_with_books,
            "tickers_with_trades": self.tickers_with_trades,
            "tickers_with_settlement": self.tickers_with_settlement,
            "max_book_staleness_seconds": self.max_book_staleness_seconds,
            "median_book_staleness_seconds": self.median_book_staleness_seconds,
            "environments_seen": self.environments_seen,
            "malformed_records": self.malformed_records,
            "per_market": self.per_market,
            "limitations": self.limitations,
        }


@dataclass
class HistoricalReplayData:
    market_data: MarketData
    settlements: list[SettlementEvent]
    market_status: list[MarketStatusEvent]
    counts: ExclusionCounts
    quality: QualityReport


# --- book normalization ----------------------------------------------------


def _levels(raw: Any) -> list[tuple[int, int]]:
    """Coerce a stored levels JSON ([[price, qty], ...]) into int tuples."""
    out: list[tuple[int, int]] = []
    for lvl in raw or []:
        out.append((int(lvl[0]), int(lvl[1])))
    return out


def normalize_book(row: OrderbookSnapshot) -> OrderBook:
    """Canonical YES-referenced book. yes_bids from yes_levels; yes_asks from
    no_levels via the (100 - p) NO->YES transform. No depth is invented."""
    yes_bids = tuple(_levels(row.yes_levels_json))
    yes_asks = tuple((CONTRACT_VALUE_CENTS - p, q) for p, q in _levels(row.no_levels_json))
    return OrderBook(
        ticker=row.market_ticker,
        captured_at=_utc_req(row.captured_at),
        yes_asks=yes_asks,
        yes_bids=yes_bids,
        source_ref=f"orderbook_snapshots:{row.id}",
    )


# --- loaders (bounded, read-only) ------------------------------------------


async def _received_at_map(session: AsyncSession, payload_ids: set[int]) -> dict[int, datetime]:
    """received_at (ingestion time) for a set of raw_payload ids."""
    if not payload_ids:
        return {}
    rows = (
        await session.execute(
            select(RawApiPayload.id, RawApiPayload.received_at).where(
                RawApiPayload.id.in_(payload_ids)
            )
        )
    ).all()
    return {rid: _utc(rat) for rid, rat in rows}  # type: ignore[misc]


async def load_order_books(
    session: AsyncSession, query: HistoryQuery, counts: ExclusionCounts, quality: QualityReport
) -> list[OrderBook]:
    rows = list(
        await session.scalars(
            select(OrderbookSnapshot)
            .where(
                OrderbookSnapshot.market_ticker.in_(query.tickers),
                OrderbookSnapshot.captured_at >= query.start,
                OrderbookSnapshot.captured_at <= query.end,
            )
            .order_by(OrderbookSnapshot.captured_at, OrderbookSnapshot.id)
        )
    )
    counts.source_rows_read += len(rows)
    books: list[OrderBook] = []
    # dedup/conflict on (ticker, captured_at): identical content -> exact dup;
    # different content at the same instant -> irreconcilable conflict.
    seen: dict[tuple[str, datetime], str | None] = {}
    conflict_keys: set[tuple[str, datetime]] = set()
    per_ticker: dict[str, list[datetime]] = defaultdict(list)
    for r in rows:
        quality.environments_seen[str(r.environment)] = (
            quality.environments_seen.get(str(r.environment), 0) + 1
        )
        if not query.env_policy.admits(r.environment, data_type="order_books"):
            counts.excluded_by_environment += 1
            quality.bump(r.market_ticker, "env_excluded_books")
            continue
        if not r.yes_levels_json and not r.no_levels_json:
            # an empty book is legitimate (no resting depth); not malformed. Keep.
            pass
        key = (r.market_ticker, _utc_req(r.captured_at))
        if key in conflict_keys:
            counts.conflicts_rejected += 1
            continue
        if key in seen:
            if seen[key] == r.content_hash:
                counts.exact_duplicates_removed += 1
            else:
                # remove the earlier-emitted event for this instant and reject both
                books = [b for b in books if not (b.ticker == key[0] and b.captured_at == key[1])]
                conflict_keys.add(key)
                counts.conflicts_rejected += 2
                quality.bump(r.market_ticker, "book_conflicts", 2)
            continue
        seen[key] = r.content_hash
        if r.environment != "production":
            quality.bump(r.market_ticker, "admitted_nonproduction")
        books.append(normalize_book(r))
        per_ticker[r.market_ticker].append(key[1])
    quality.order_book_events = len(books)
    quality.tickers_with_books = len(per_ticker)
    _book_staleness(per_ticker, query, quality)
    return books


def _book_staleness(
    per_ticker: dict[str, list[datetime]], query: HistoryQuery, quality: QualityReport
) -> None:
    gaps: list[float] = []
    for _tk, times in per_ticker.items():
        ordered = sorted(times)
        for a, b in itertools.pairwise(ordered):
            gaps.append((b - a).total_seconds())
    if gaps:
        quality.max_book_staleness_seconds = max(gaps)
        quality.median_book_staleness_seconds = statistics.median(gaps)


async def load_trades(
    session: AsyncSession, query: HistoryQuery, counts: ExclusionCounts, quality: QualityReport
) -> list[Trade]:
    rows = list(
        await session.scalars(
            select(TradeRecord)
            .where(
                TradeRecord.market_ticker.in_(query.tickers),
                TradeRecord.executed_at >= query.start,
                TradeRecord.executed_at <= query.end,
            )
            .order_by(TradeRecord.executed_at, TradeRecord.trade_id)
        )
    )
    counts.source_rows_read += len(rows)
    # collector-available mode requires known ingestion provenance for a trade.
    rat_map: dict[int, datetime] = {}
    if query.mode is TimestampMode.COLLECTOR_AVAILABLE:
        rat_map = await _received_at_map(
            session, {r.raw_payload_id for r in rows if r.raw_payload_id is not None}
        )
    trades: list[Trade] = []
    seen_ids: set[str] = set()
    per_ticker: set[str] = set()
    for r in rows:
        quality.environments_seen[str(r.environment)] = (
            quality.environments_seen.get(str(r.environment), 0) + 1
        )
        if not query.env_policy.admits(r.environment, data_type="trades"):
            counts.excluded_by_environment += 1
            quality.bump(r.market_ticker, "env_excluded_trades")
            continue
        if r.trade_id in seen_ids:  # trade_id is the immutable dedup key
            counts.exact_duplicates_removed += 1
            continue
        if r.price_cents is None or r.count is None or r.count <= 0:
            counts.excluded_malformed += 1
            quality.malformed_records += 1
            quality.bump(r.market_ticker, "malformed_trades")
            continue
        if query.mode is TimestampMode.COLLECTOR_AVAILABLE and (
            r.raw_payload_id is None or r.raw_payload_id not in rat_map
        ):
            # cannot establish when the archive knew this trade -> exclude in the
            # what-we-knew-in-real-time mode (do not guess).
            counts.excluded_malformed += 1
            quality.malformed_records += 1
            quality.bump(r.market_ticker, "malformed_trades")
            continue
        seen_ids.add(r.trade_id)
        if r.environment != "production":
            quality.bump(r.market_ticker, "admitted_nonproduction")
        taker = Side(r.taker_side) if r.taker_side in ("yes", "no") else None
        trades.append(
            Trade(
                ticker=r.market_ticker,
                executed_at=_utc_req(r.executed_at),
                yes_price_cents=int(r.price_cents),
                quantity=int(r.count),
                taker_side=taker,
                source_ref=f"trades:{r.trade_id}",
            )
        )
        per_ticker.add(r.market_ticker)
    quality.trade_events = len(trades)
    quality.tickers_with_trades = len(per_ticker)
    return trades


async def load_settlements(
    session: AsyncSession, query: HistoryQuery, counts: ExclusionCounts, quality: QualityReport
) -> list[SettlementEvent]:
    """One settlement per ticker, from the market snapshot bearing a final
    result. available_at is mode-dependent: the exchange determination time
    (exchange-time) or the ingestion time of the finalizing snapshot
    (collector-available)."""
    if not query.include_settlement or "settlements" not in query.data_types:
        return []
    rows = list(
        await session.scalars(
            select(MarketSnapshot)
            .where(
                MarketSnapshot.market_ticker.in_(query.tickers),
                MarketSnapshot.result.in_(("yes", "no")),
            )
            .order_by(MarketSnapshot.market_ticker, MarketSnapshot.observed_at)
        )
    )
    counts.source_rows_read += len(rows)
    # earliest finalizing snapshot per ticker; detect conflicting results.
    chosen: dict[str, MarketSnapshot] = {}
    result_of: dict[str, str] = {}
    conflicts: set[str] = set()
    for r in rows:
        # result/identity are environment-invariant (ADR 0013), but honour the
        # configured policy; production is the default admit.
        if not query.env_policy.admits(r.environment, data_type="settlements") and (
            r.environment is not None or "settlements" not in query.env_policy.allow_null_for
        ):
            counts.excluded_by_environment += 1
            continue
        if r.market_ticker in result_of and result_of[r.market_ticker] != r.result:
            conflicts.add(r.market_ticker)
            continue
        result_of.setdefault(r.market_ticker, r.result or "")
        chosen.setdefault(r.market_ticker, r)
    for tk in conflicts:
        counts.conflicts_rejected += 1
        chosen.pop(tk, None)
    settlements: list[SettlementEvent] = []
    for tk, snap in chosen.items():
        if snap.result not in ("yes", "no"):  # query already filters, narrow for typing
            continue
        if query.mode is TimestampMode.EXCHANGE_TIME:
            available = (
                _utc(snap.settlement_ts) or _utc(snap.expiration_time) or _utc_req(snap.observed_at)
            )
        else:
            available = _utc_req(snap.observed_at)
        settlements.append(
            SettlementEvent(ticker=tk, result=Side(snap.result), available_at=available)
        )
    quality.settlement_events = len(settlements)
    quality.tickers_with_settlement = len(settlements)
    return settlements


async def load_market_status(
    session: AsyncSession, query: HistoryQuery, counts: ExclusionCounts, quality: QualityReport
) -> list[MarketStatusEvent]:
    if "market_status" not in query.data_types:
        return []
    rows = list(
        await session.scalars(
            select(MarketSnapshot)
            .where(
                MarketSnapshot.market_ticker.in_(query.tickers),
                MarketSnapshot.observed_at >= query.start,
                MarketSnapshot.observed_at <= query.end,
            )
            .order_by(MarketSnapshot.market_ticker, MarketSnapshot.observed_at)
        )
    )
    counts.source_rows_read += len(rows)
    events: list[MarketStatusEvent] = []
    last_state: dict[str, MarketState] = {}
    for r in rows:
        if not query.env_policy.admits(r.environment, data_type="market_status") and not (
            r.environment is None and "market_status" in query.env_policy.allow_null_for
        ):
            counts.excluded_by_environment += 1
            continue
        state = _STATUS_MAP.get((r.status or "").lower(), MarketState.UNKNOWN)
        if state is MarketState.UNKNOWN:
            continue
        if last_state.get(r.market_ticker) == state:
            counts.exact_duplicates_removed += 1
            continue
        last_state[r.market_ticker] = state
        at = _utc_req(r.observed_at)
        events.append(
            MarketStatusEvent(
                ticker=r.market_ticker,
                state=state.value,
                at=at,
                source_ref=f"market_snapshots:{r.id}",
            )
        )
    quality.market_status_events = len(events)
    return events


async def load_historical_replay_data(
    session: AsyncSession, query: HistoryQuery
) -> HistoricalReplayData:
    """Load and convert one bounded historical interval into replay events.
    Read-only; applies environment policy, timestamp mode, dedup/conflict
    handling, and computes a data-quality report."""
    counts = ExclusionCounts()
    quality = QualityReport(tickers_requested=len(query.tickers))

    books = (
        await load_order_books(session, query, counts, quality)
        if "order_books" in query.data_types
        else []
    )
    trades = (
        await load_trades(session, query, counts, quality) if "trades" in query.data_types else []
    )
    settlements = await load_settlements(session, query, counts, quality)
    status = await load_market_status(session, query, counts, quality)

    counts.events_emitted = len(books) + len(trades) + len(settlements) + len(status)
    _add_limitations(query, quality)
    return HistoricalReplayData(
        market_data=MarketData(order_books=books, trades=trades),
        settlements=settlements,
        market_status=status,
        counts=counts,
        quality=quality,
    )


def _add_limitations(query: HistoryQuery, quality: QualityReport) -> None:
    lim = quality.limitations
    lim.append(
        "Order-book 'captured_at' is the collector clock at fetch (the only book "
        "timestamp available); exchange-time and collector-available modes "
        "coincide for books."
    )
    lim.append(
        "Historical fills cannot reconstruct real queue position; passive fills "
        "are a conservative subsequent-volume proxy."
    )
    if quality.tickers_with_books < quality.tickers_requested:
        lim.append(
            f"{quality.tickers_requested - quality.tickers_with_books} of "
            f"{quality.tickers_requested} requested tickers had no admissible order book."
        )
    if quality.tickers_with_trades == 0:
        lim.append("No admissible trades in range -- passive-fill simulation is unavailable.")
    if (
        quality.max_book_staleness_seconds is not None
        and quality.max_book_staleness_seconds > query.max_book_age_seconds
    ):
        lim.append(
            f"Max book staleness {quality.max_book_staleness_seconds:.0f}s exceeds "
            f"max_book_age {query.max_book_age_seconds:.0f}s; some intervals have stale books."
        )
