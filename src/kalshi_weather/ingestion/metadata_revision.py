"""Post-expiration settled-metadata revision capture (ADR 0011).

Kalshi keeps revising a settled market's ``volume``, ``open_interest`` and
occasionally ``result`` *after* publishing ``status="finalized"`` with a
``settlement_ts``. The 2026-07-23 integrity investigation measured this
directly: three hours after close, an event reported ``volume=0`` for ten of
eleven markets that had in fact traded 104-1,165 contracts each, and one
market carried a ``result`` that contradicted its own strike arithmetic
(``expiration_value=85.00 > floor_strike=79.99`` under ``strike_type=greater``,
yet ``result="no"``) -- since corrected venue-side.

The archive captured all of that faithfully; field mapping, units, raw-payload
preservation, content hashing and append-only persistence were all correct.
The defect was narrower: the settlement queue keys on "no result yet", so the
instant a result-bearing snapshot lands the ticker leaves the queue forever and
the later, correct values are never fetched. Every one of the 3,089 settled
markets had exactly one result-bearing snapshot.

This module adds the missing second pass. Two concepts are kept distinct:

- **result available** -- a snapshot carries ``result`` in {yes, no}.
- **settled metadata stable** -- the venue's own ``expiration_time`` has
  passed (observed ~7 days after ``close_time``).

Nothing here rewrites history. A revision that finds changed values appends
exactly one new snapshot through the existing content-hash path; a revision
that finds nothing changed appends none, and is recorded only in
``market_metadata_verifications`` so it is not re-fetched forever.
"""

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalshi_weather.domain.time import to_naive_utc, utc_now
from kalshi_weather.ingestion.discovery import persist_market_snapshot
from kalshi_weather.kalshi.client import KalshiAPIError, KalshiClient
from kalshi_weather.logging import get_logger
from kalshi_weather.storage.database import session_scope
from kalshi_weather.storage.models import MarketMetadataVerification, MarketSnapshot
from kalshi_weather.storage.repositories import record_collector_run

logger = get_logger(__name__)

#: Fallback finality horizon for rows written before migration 0008, which
#: have `expiration_time IS NULL`. Measured, not assumed: every market sampled
#: on 2026-07-23 reported `expiration_time` between 6 and 7 days after
#: `close_time` (ADR 0011). Deliberately a separate, explicitly-testable rule
#: -- a market with a real `expiration_time` never uses it.
FALLBACK_FINALITY_DAYS = 7

DEFAULT_REVISION_LIMIT_PER_CYCLE = 50

#: Outcomes that permanently retire a market from the revision queue.
TERMINAL_OUTCOMES = frozenset({"unchanged", "changed", "market_removed", "retention_expired"})

#: Snapshot fields this pass treats as venue-mutable and reports on.
MUTABLE_FIELDS = ("volume", "open_interest", "result")


@dataclass(slots=True)
class MarketRevisionResult:
    market_ticker: str
    outcome: str
    snapshot_appended: bool = False
    changed_fields: tuple[str, ...] = ()
    error: str | None = None


@dataclass(slots=True)
class RevisionReport:
    dry_run: bool = False
    candidates_selected: int = 0
    results: list[MarketRevisionResult] = field(default_factory=list)

    def totals(self) -> dict[str, int]:
        changed = [r for r in self.results if r.outcome == "changed"]
        return {
            "candidates_selected": self.candidates_selected,
            "attempted": len(self.results),
            "changed": len(changed),
            "unchanged": sum(1 for r in self.results if r.outcome == "unchanged"),
            "snapshots_appended": sum(1 for r in self.results if r.snapshot_appended),
            "result_revisions": sum(1 for r in changed if "result" in r.changed_fields),
            "volume_revisions": sum(1 for r in changed if "volume" in r.changed_fields),
            "open_interest_revisions": sum(
                1 for r in changed if "open_interest" in r.changed_fields
            ),
            "market_removed": sum(1 for r in self.results if r.outcome == "market_removed"),
            "retention_expired": sum(1 for r in self.results if r.outcome == "retention_expired"),
            "api_failure": sum(1 for r in self.results if r.outcome == "api_failure"),
            "errors": sum(1 for r in self.results if r.error is not None),
        }


def finality_time(
    expiration_time: datetime | None,
    close_time: datetime | None,
    *,
    fallback_days: int = FALLBACK_FINALITY_DAYS,
) -> datetime | None:
    """When this market's settled metadata should have stopped moving.

    Prefers the venue's own ``expiration_time``. Falls back to
    ``close_time + fallback_days`` only for rows written before migration 0008
    (which have no ``expiration_time``); returns None when neither is known, in
    which case the market is simply never eligible -- a market we cannot prove
    is final must not be treated as final.
    """
    if expiration_time is not None:
        return to_naive_utc(expiration_time)
    if close_time is not None:
        return to_naive_utc(close_time) + timedelta(days=fallback_days)
    return None


def classify_missing_market(
    close_time: datetime | None, *, now: datetime, retention_days: int
) -> str:
    """A 404 from the market endpoint means the venue no longer serves this
    ticker, but for two very different reasons. A market older than the
    observed candle/market retention horizon has aged out (``retention_expired``
    -- expected, and its data was captured while it existed). A recent one was
    removed or re-struck by the venue (``market_removed``), which the
    2026-07-23 investigation observed for extreme-strike contracts within hours
    of close. Both are terminal, but conflating them would hide the second.
    """
    if close_time is None:
        return "market_removed"
    age_days = (now - to_naive_utc(close_time)).total_seconds() / 86400
    return "retention_expired" if age_days > retention_days else "market_removed"


async def select_revision_candidates(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    tickers: list[str] | None = None,
    fallback_days: int = FALLBACK_FINALITY_DAYS,
) -> list[tuple[str, datetime | None]]:
    """Settled markets whose venue finality time has passed and which have no
    terminal verification yet, oldest-finality first.

    Eligibility is applied **before** ``limit``, and already-verified markets
    are excluded by an anti-join in the query rather than by filtering
    afterwards. That ordering matters: the price-sync defect fixed in ADR 0010
    was exactly a fixed head selected first and filtered second, which let
    ineligible markets consume the entire per-cycle budget forever. Exclusion
    lives in SQL, not an in-process set, so nothing accumulates in memory and a
    restart changes nothing.

    Returns ``(market_ticker, finality_at)`` so the caller can record which
    rule admitted each market.
    """
    now = now or utc_now()
    now_naive = to_naive_utc(now)

    latest_ids = (
        select(func.max(MarketSnapshot.id)).group_by(MarketSnapshot.market_ticker).scalar_subquery()
    )
    verified = (
        select(MarketMetadataVerification.market_ticker)
        .where(MarketMetadataVerification.outcome.in_(sorted(TERMINAL_OUTCOMES)))
        .distinct()
        .scalar_subquery()
    )
    filters: list[ColumnElement[bool]] = [
        MarketSnapshot.id.in_(latest_ids),
        MarketSnapshot.result.in_(["yes", "no"]),
        MarketSnapshot.market_ticker.not_in(verified),
    ]
    if tickers:
        filters.append(MarketSnapshot.market_ticker.in_(tickers))

    rows = await session.execute(
        select(
            MarketSnapshot.market_ticker,
            MarketSnapshot.expiration_time,
            MarketSnapshot.close_time,
        ).where(*filters)
    )

    # The finality comparison is done in Python, not SQL: `close_time` and
    # `expiration_time` are TIMESTAMPTZ while other datetimes in this schema
    # are naive, and the fallback needs interval arithmetic that differs
    # between PostgreSQL and the SQLite used by unit tests. Same reasoning as
    # `price_backfill.coverage_reaches_close`.
    eligible: list[tuple[str, datetime]] = []
    for ticker, expiration, close in rows:
        final_at = finality_time(expiration, close, fallback_days=fallback_days)
        if final_at is None or final_at > now_naive:
            continue
        eligible.append((ticker, final_at))

    # Deterministic: oldest finality first, ties broken by ticker.
    eligible.sort(key=lambda pair: (pair[1], pair[0]))
    if limit is not None:
        eligible = eligible[:limit]
    return [(t, f) for t, f in eligible]


def _snapshot_diff(latest: MarketSnapshot, market: object) -> tuple[str, ...]:
    """Which venue-mutable fields moved since our last stored snapshot."""
    changed: list[str] = []
    for name in MUTABLE_FIELDS:
        old = getattr(latest, name, None)
        new = getattr(market, name, None)
        if name == "result":
            if (old or "") != (new or ""):
                changed.append(name)
        elif (old or 0) != (new or 0):
            changed.append(name)
    return tuple(changed)


async def revise_one_market(
    session: AsyncSession,
    client: KalshiClient,
    market_ticker: str,
    finality_at: datetime | None,
    *,
    now: datetime,
    retention_days: int,
    dry_run: bool,
) -> MarketRevisionResult:
    """Re-fetch one settled market and append a snapshot only if it changed.

    Never raises for an expected outcome. A transient failure deliberately
    records **no** verification row, so the market is naturally re-selected on
    a later cycle; only success and the two 404 classifications are terminal.
    """
    latest = await session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.market_ticker == market_ticker)
        .order_by(MarketSnapshot.id.desc())
        .limit(1)
    )
    close_time = latest.close_time if latest is not None else None

    try:
        market = await client.get_market(market_ticker)
    except KalshiAPIError as exc:
        if exc.status_code == 404:
            outcome = classify_missing_market(
                close_time, now=to_naive_utc(now), retention_days=retention_days
            )
            result = MarketRevisionResult(market_ticker=market_ticker, outcome=outcome)
            if not dry_run:
                await record_metadata_verification(
                    session,
                    market_ticker=market_ticker,
                    verified_at=now,
                    finality_at=finality_at,
                    outcome=outcome,
                    snapshot_appended=False,
                    changed_fields=None,
                    raw_payload_id=None,
                )
            return result
        return MarketRevisionResult(
            market_ticker=market_ticker, outcome="api_failure", error=str(exc)[:300]
        )
    except Exception as exc:
        return MarketRevisionResult(
            market_ticker=market_ticker,
            outcome="api_failure",
            error=f"{type(exc).__name__}: {exc}"[:300],
        )

    changed_fields = _snapshot_diff(latest, market) if latest is not None else ()

    if dry_run:
        return MarketRevisionResult(
            market_ticker=market_ticker,
            outcome="changed" if changed_fields else "unchanged",
            snapshot_appended=bool(changed_fields),
            changed_fields=changed_fields,
        )

    save = await persist_market_snapshot(session, market, raw_payload_id=client.last_raw_payload_id)
    appended = not save.was_duplicate
    # `outcome` reports whether a *venue-mutable* field moved; `appended`
    # separately reports whether a row was written. They are not the same
    # question, and conflating them made the metrics lie: a snapshot is also
    # appended when some unrelated field differs -- most commonly
    # `expiration_time` itself, which is NULL on every pre-0008 row, so the
    # first verification of a historical market always appends. Reporting that
    # as "changed" with no changed_fields would overstate `result_revisions`
    # and friends on essentially the entire backfill. Caught by bounded live
    # validation, not by the unit tests, which had matching expiration_times.
    outcome = "changed" if changed_fields else "unchanged"
    await record_metadata_verification(
        session,
        market_ticker=market_ticker,
        verified_at=now,
        finality_at=finality_at,
        outcome=outcome,
        snapshot_appended=appended,
        changed_fields=",".join(changed_fields) or None,
        raw_payload_id=client.last_raw_payload_id,
    )
    if appended and changed_fields:
        logger.info(
            "metadata_revision.revised",
            ticker=market_ticker,
            changed=",".join(changed_fields),
        )
    return MarketRevisionResult(
        market_ticker=market_ticker,
        outcome=outcome,
        snapshot_appended=appended,
        changed_fields=changed_fields,
    )


async def record_metadata_verification(
    session: AsyncSession,
    *,
    market_ticker: str,
    verified_at: datetime,
    finality_at: datetime | None,
    outcome: str,
    snapshot_appended: bool,
    changed_fields: str | None,
    raw_payload_id: int | None,
) -> MarketMetadataVerification:
    """Append one verification record. Never updates an existing row."""
    record = MarketMetadataVerification(
        market_ticker=market_ticker,
        verified_at=verified_at,
        finality_at=finality_at,
        outcome=outcome,
        snapshot_appended=snapshot_appended,
        changed_fields=changed_fields,
        raw_payload_id=raw_payload_id,
    )
    session.add(record)
    await session.flush()
    return record


async def run_metadata_revision(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    client: KalshiClient,
    limit: int | None = None,
    tickers: list[str] | None = None,
    retention_days: int = 67,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RevisionReport:
    """One bounded revision pass, one committed transaction per market."""
    report = RevisionReport(dry_run=dry_run)
    now = now or utc_now()

    async with session_scope(session_factory) as session:
        candidates = await select_revision_candidates(
            session, now=now, limit=limit, tickers=tickers
        )
    report.candidates_selected = len(candidates)

    for ticker, finality_at in candidates:
        try:
            async with session_scope(session_factory) as session:
                result = await revise_one_market(
                    session,
                    client,
                    ticker,
                    finality_at,
                    now=now,
                    retention_days=retention_days,
                    dry_run=dry_run,
                )
        except Exception as exc:
            result = MarketRevisionResult(
                market_ticker=ticker,
                outcome="api_failure",
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
            logger.exception("metadata_revision.market_failed", market=ticker)
        report.results.append(result)
    return report


async def run_metadata_revision_loop(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    client_factory: Callable[[], KalshiClient],
    limit_per_cycle: int = DEFAULT_REVISION_LIMIT_PER_CYCLE,
    retention_days: int = 67,
    interval_seconds: float,
    stop_event: asyncio.Event,
    max_cycles: int | None = None,
) -> None:
    """Periodic bounded revision pass, recorded under ``collector="revision"``.

    Reuses the exact interval-loop shape of the Kalshi, weather and price-sync
    loops -- no new scheduler, and the client's existing request throttle and
    bounded retries apply unchanged. A cycle with no eligible market issues
    zero API calls.
    """
    cycle_number = 0
    while not stop_event.is_set():
        cycle_number += 1
        started_at = utc_now()
        run_stats: dict[str, int] = {}
        run_error: str | None = None
        try:
            client = client_factory()
            async with client:
                report = await run_metadata_revision(
                    session_factory=session_factory,
                    client=client,
                    limit=limit_per_cycle,
                    retention_days=retention_days,
                )
                run_stats = report.totals()
            logger.info("metadata_revision.cycle_complete", cycle=cycle_number, **run_stats)
        except Exception as exc:
            run_error = f"{type(exc).__name__}: {exc}"
            logger.exception("metadata_revision.cycle_failed", cycle=cycle_number)

        try:
            async with session_scope(session_factory) as session:
                await record_collector_run(
                    session,
                    collector="revision",
                    started_at=started_at,
                    finished_at=utc_now(),
                    success=run_error is None,
                    requests_attempted=0,
                    retries=0,
                    stats=run_stats,
                    error=run_error,
                )
        except Exception:
            logger.exception("metadata_revision.run_record_failed", cycle=cycle_number)

        if max_cycles is not None and cycle_number >= max_cycles:
            return

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)


__all__ = [
    "DEFAULT_REVISION_LIMIT_PER_CYCLE",
    "FALLBACK_FINALITY_DAYS",
    "TERMINAL_OUTCOMES",
    "MarketRevisionResult",
    "RevisionReport",
    "classify_missing_market",
    "finality_time",
    "record_metadata_verification",
    "revise_one_market",
    "run_metadata_revision",
    "run_metadata_revision_loop",
    "select_revision_candidates",
]
