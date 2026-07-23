"""Chunked, resumable historical price (candlestick) backfill (Phase 7A).

Preserves the rolling ~67-day recoverable price window measured in
`docs/research/investigations/INV-20260721-price-history-recovery.md` before
it ages out: unlike the weather backfill (`ingestion/backfill.py`, one chunk
= a date range within one station), here **one chunk = one settled market**
-- each market's price history is fetched and committed independently, so a
crash loses at most one market's worth of work, and a failing market is
recorded and skipped without aborting the run (never silently discarded).

Markets are processed **oldest `close_time` first**, because the retention
window is rolling: the oldest known markets are closest to falling out of
reach permanently, so they are backfilled with priority.

A market's outcome is classified into exactly one of:

- ``complete`` -- candle coverage now reaches (or already reached) the
  market's close, within tolerance.
- ``partial`` -- some candles were fetched/stored this run, but coverage
  still does not reach close (e.g. the API returned less than expected).
- ``no_price_data`` -- the API returned zero candles for a real market (a
  true, quiet market -- not an error).
- ``expired`` -- the market has aged out of the retention window since it
  was last seen (a 404 from the candlestick endpoint for a ticker we know
  from `market_snapshots`); permanently unrecoverable, per the investigation.
- ``api_failure`` -- an unexpected, non-404 error after the client's own
  retry/backoff was exhausted.
- ``unsupported`` -- the market's ticker/series could not be resolved, or it
  has no ``close_time`` to bound a fetch window.

"Unresolved coverage gap" (a market that remains incomplete across runs for
no clear single-run reason) is deliberately **not** a per-run outcome here --
it is a query-time classification computed by `ops/price_coverage.py` over
the stored data, since it describes a persistent state, not a single
attempt's result (see docs/adr/0007-price-ingestion.md).
"""

import asyncio
import contextlib
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalshi_weather.domain.time import to_naive_utc, utc_now
from kalshi_weather.kalshi.client import KalshiAPIError, KalshiClient
from kalshi_weather.logging import get_logger
from kalshi_weather.storage.database import session_scope
from kalshi_weather.storage.models import EventRecord, MarketCandlestick, MarketSnapshot
from kalshi_weather.storage.repositories import (
    get_candlestick_coverage,
    get_latest_candlestick_period_end,
    record_collector_run,
    save_market_candlestick,
)

logger = get_logger(__name__)

DEFAULT_RESOLUTION_MINUTES = 1
#: Generous headroom before close_time to cover a market's full lifetime
#: (observed live: markets open ~1-2 days before close; 5 days is a safe
#: margin with negligible extra cost since the endpoint returns no candles
#: before a market existed -- see the investigation report).
CANDLE_LOOKBACK_DAYS = 5
#: Small trailing buffer after close_time -- settlement can trail close by
#: minutes to hours (docs/adr/0006-settlement-labels.md).
CANDLE_TRAILING_HOURS = 6
#: A market's coverage is considered complete if its latest stored candle's
#: period_end is within this many seconds of close_time (candles are
#: minute-aligned; a full hour of tolerance comfortably absorbs the market's
#: own final-candle boundary without masking a genuine gap).
COMPLETENESS_TOLERANCE_SECONDS = 3600


class MarketOutcome(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NO_PRICE_DATA = "no_price_data"
    EXPIRED = "expired"
    API_FAILURE = "api_failure"
    UNSUPPORTED = "unsupported"


@dataclass(slots=True)
class MarketBackfillResult:
    market_ticker: str
    outcome: MarketOutcome
    saved: int = 0
    duplicate: int = 0
    would_save: int | None = None  # dry-run only: candles that would be saved
    candle_count_after: int = 0
    skipped_already_covered: bool = False
    error: str | None = None


@dataclass(slots=True)
class PriceBackfillReport:
    resolution_minutes: int
    dry_run: bool
    results: list[MarketBackfillResult] = field(default_factory=list)

    @property
    def failed(self) -> list[MarketBackfillResult]:
        return [r for r in self.results if r.outcome is MarketOutcome.API_FAILURE]

    @property
    def ok(self) -> bool:
        """True unless a genuine API failure occurred. Expired/no-data/
        unsupported are legitimate classifications, not run failures --
        matching the weather backfill's ``ok`` semantics (only unresolved
        errors make a run 'not ok')."""
        return not self.failed

    def totals(self) -> dict[str, int]:
        counts: dict[str, int] = {outcome.value: 0 for outcome in MarketOutcome}
        for r in self.results:
            counts[r.outcome.value] += 1
        return {
            "markets_attempted": len(self.results),
            "markets_skipped_already_covered": sum(
                1 for r in self.results if r.skipped_already_covered
            ),
            "saved": sum(r.saved for r in self.results),
            "duplicate": sum(r.duplicate for r in self.results),
            **{f"outcome_{k}": v for k, v in counts.items()},
        }


def _infer_series_ticker(
    snap: MarketSnapshot, events_by_ticker: dict[str, EventRecord]
) -> str | None:
    """Same three-tier fallback as settlement/resolver.py's
    load_parser_inputs: prefer the persisted event's series_ticker, then the
    event ticker's own prefix, then the market ticker's prefix."""
    if snap.event_ticker:
        event = events_by_ticker.get(snap.event_ticker)
        if event is not None and event.series_ticker:
            return event.series_ticker
        return snap.event_ticker.split("-")[0]
    if snap.market_ticker:
        return snap.market_ticker.split("-")[0]
    return None


def coverage_reaches_close(
    latest_period_end: datetime | None,
    close_time: datetime | None,
    tolerance: timedelta = timedelta(seconds=COMPLETENESS_TOLERANCE_SECONDS),
) -> bool:
    """This project's single definition of "a market's candle coverage is
    complete": the latest stored candle's ``period_end`` reaches the market's
    ``close_time``, within tolerance.

    Deliberately shared by `select_backfill_candidates` (which excludes
    covered markets *before* LIMIT) and `backfill_one_market` (which re-checks
    per market). If those two ever disagreed, covered markets would consume
    the sync loop's per-cycle limit and the loop would stop making progress --
    precisely the 2026-07-23 defect this function exists to prevent.

    Both values are normalized to naive UTC first. `market_snapshots.
    close_time` is TIMESTAMPTZ while `market_candlesticks.period_end` is a
    naive TIMESTAMP, so the two must never be compared raw -- and, for the
    same reason, this comparison is done in Python rather than pushed into a
    SQL predicate that would silently reinterpret the naive side against the
    server's TimeZone setting.
    """
    if latest_period_end is None or close_time is None:
        return False
    return to_naive_utc(latest_period_end) >= to_naive_utc(close_time) - tolerance


async def select_backfill_candidates(
    session: AsyncSession,
    *,
    tickers: list[str] | None = None,
    event_ticker: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int | None = None,
    period_interval_seconds: int = DEFAULT_RESOLUTION_MINUTES * 60,
    exclude_covered: bool = False,
    exclude_tickers: Collection[str] | None = None,
) -> list[MarketSnapshot]:
    """Settled markets to backfill, **oldest close_time first** -- the
    rolling retention window makes the oldest known markets the
    highest-risk (see the investigation report).

    With ``exclude_covered`` (the continuous sync loop's mode), markets whose
    coverage already reaches close are filtered out *before* ``limit`` is
    applied, so the limit is spent only on markets that still need work. The
    original ordering-then-LIMIT-then-skip shape meant the oldest fully
    covered markets consumed the entire limit every cycle and newly settled
    markets were never reached (2026-07-23; see ADR 0010).

    ``exclude_tickers`` lets a caller drop markets it has already resolved to
    a terminal, non-retryable outcome this process (no price data, expired,
    unsupported) so they cannot re-block the head of the queue -- see
    `run_price_sync_loop`.

    Ties on ``close_time`` are broken by ``market_ticker`` so the ordering is
    deterministic and a bounded run is reproducible.
    """
    latest_ids = (
        select(func.max(MarketSnapshot.id))
        .group_by(MarketSnapshot.market_ticker)
        .scalar_subquery()
    )
    filters: list[ColumnElement[bool]] = [
        MarketSnapshot.id.in_(latest_ids),
        MarketSnapshot.result.in_(["yes", "no"]),
    ]
    if tickers:
        filters.append(MarketSnapshot.market_ticker.in_(tickers))
    if event_ticker:
        filters.append(MarketSnapshot.event_ticker == event_ticker)
    if start_date is not None:
        filters.append(
            MarketSnapshot.close_time >= datetime.combine(start_date, time.min, tzinfo=UTC)
        )
    if end_date is not None:
        filters.append(
            MarketSnapshot.close_time <= datetime.combine(end_date, time.max, tzinfo=UTC)
        )

    ordering = (MarketSnapshot.close_time.asc(), MarketSnapshot.market_ticker.asc())

    if not exclude_covered and not exclude_tickers:
        stmt = select(MarketSnapshot).where(*filters).order_by(*ordering)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await session.scalars(stmt)).all())

    # Coverage-aware path. Step 1 is a narrow projection (three columns, no
    # ORM hydration) left-joined to each market's latest candle at this
    # resolution; step 2 applies the shared coverage rule in Python; only
    # then is `limit` applied and the surviving rows hydrated. Keeping the
    # tolerance comparison out of SQL avoids the TIMESTAMPTZ/TIMESTAMP
    # mismatch described in `coverage_reaches_close`.
    latest_candle = (
        select(
            MarketCandlestick.market_ticker.label("market_ticker"),
            func.max(MarketCandlestick.period_end).label("latest_period_end"),
        )
        .where(MarketCandlestick.period_interval_seconds == period_interval_seconds)
        .group_by(MarketCandlestick.market_ticker)
        .subquery()
    )
    projection = (
        select(
            MarketSnapshot.market_ticker,
            MarketSnapshot.close_time,
            latest_candle.c.latest_period_end,
        )
        .outerjoin(latest_candle, latest_candle.c.market_ticker == MarketSnapshot.market_ticker)
        .where(*filters)
        .order_by(*ordering)
    )

    excluded = set(exclude_tickers or ())
    selected: list[str] = []
    for market_ticker, close_time, latest_period_end in await session.execute(projection):
        if market_ticker in excluded:
            continue
        if exclude_covered and coverage_reaches_close(latest_period_end, close_time):
            continue
        selected.append(market_ticker)
        if limit is not None and len(selected) >= limit:
            break

    if not selected:
        return []

    rows = (
        await session.scalars(
            select(MarketSnapshot)
            .where(*filters, MarketSnapshot.market_ticker.in_(selected))
            .order_by(*ordering)
        )
    ).all()
    return list(rows)


def _price_block_kwargs(prefix: str, block: Any) -> dict[str, Any]:
    return {
        f"{prefix}_open_cents": block.open_cents,
        f"{prefix}_high_cents": block.high_cents,
        f"{prefix}_low_cents": block.low_cents,
        f"{prefix}_close_cents": block.close_cents,
    }


async def backfill_one_market(
    session: AsyncSession,
    client: KalshiClient,
    snap: MarketSnapshot,
    *,
    events_by_ticker: dict[str, EventRecord],
    resolution_minutes: int,
    skip_covered: bool,
    dry_run: bool,
) -> MarketBackfillResult:
    """Fetch and store one market's candlestick history. Never raises for an
    expected outcome (expired/no-data/unsupported/api-failure) -- those are
    all returned as a classified result, never a silent discard."""
    result = MarketBackfillResult(market_ticker=snap.market_ticker, outcome=MarketOutcome.PARTIAL)
    period_interval_seconds = resolution_minutes * 60

    series_ticker = _infer_series_ticker(snap, events_by_ticker)
    close_time = to_naive_utc(snap.close_time) if snap.close_time else None
    if series_ticker is None or close_time is None:
        result.outcome = MarketOutcome.UNSUPPORTED
        result.error = "could not resolve series_ticker or close_time"
        return result

    tolerance = timedelta(seconds=COMPLETENESS_TOLERANCE_SECONDS)

    if skip_covered:
        latest = await get_latest_candlestick_period_end(
            session, snap.market_ticker, period_interval_seconds
        )
        # Same rule the candidate query used, so a market excluded there is
        # never re-admitted here (and vice versa) -- see coverage_reaches_close.
        if coverage_reaches_close(latest, close_time, tolerance):
            coverage = await get_candlestick_coverage(
                session, snap.market_ticker, period_interval_seconds
            )
            result.outcome = MarketOutcome.COMPLETE
            result.skipped_already_covered = True
            result.candle_count_after = coverage["candle_count"]
            return result

    start_ts = int((close_time - timedelta(days=CANDLE_LOOKBACK_DAYS)).timestamp())
    end_ts = int((close_time + timedelta(hours=CANDLE_TRAILING_HOURS)).timestamp())

    try:
        candles = await client.list_candlesticks(
            series_ticker=series_ticker,
            ticker=snap.market_ticker,
            start_ts=start_ts,
            end_ts=end_ts,
            period_interval=resolution_minutes,
        )
    except KalshiAPIError as exc:
        result.outcome = (
            MarketOutcome.EXPIRED if exc.status_code == 404 else MarketOutcome.API_FAILURE
        )
        result.error = str(exc)
        return result
    except Exception as exc:
        result.outcome = MarketOutcome.API_FAILURE
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    if not candles:
        result.outcome = MarketOutcome.NO_PRICE_DATA
        return result

    if dry_run:
        result.would_save = len(candles)
        fetched_max = max(
            datetime.fromtimestamp(c.end_period_ts, tz=UTC).replace(tzinfo=None) for c in candles
        )
        result.outcome = (
            MarketOutcome.COMPLETE
            if coverage_reaches_close(fetched_max, close_time, tolerance)
            else MarketOutcome.PARTIAL
        )
        return result

    raw_payload_id = client.last_raw_payload_id
    for candle in candles:
        period_end = datetime.fromtimestamp(candle.end_period_ts, tz=UTC).replace(tzinfo=None)
        period_start = period_end - timedelta(seconds=period_interval_seconds)

        price_close_cents = candle.price.close_cents
        carried_forward = False
        if price_close_cents is None and candle.price.previous_cents is not None:
            # Zero-volume period: the API's own backward carry-forward
            # value, already known at this period's own timestamp -- never
            # a future value. See MarketCandlestick's docstring.
            price_close_cents = candle.price.previous_cents
            carried_forward = True

        save_result = await save_market_candlestick(
            session,
            market_ticker=snap.market_ticker,
            series_ticker=series_ticker,
            period_interval_seconds=period_interval_seconds,
            period_start=period_start,
            period_end=period_end,
            price_open_cents=candle.price.open_cents,
            price_high_cents=candle.price.high_cents,
            price_low_cents=candle.price.low_cents,
            price_close_cents=price_close_cents,
            price_mean_cents=candle.price.mean_cents,
            price_close_is_carried_forward=carried_forward,
            volume=candle.volume or 0,
            open_interest=candle.open_interest,
            raw_payload_id=raw_payload_id,
            **_price_block_kwargs("yes_bid", candle.yes_bid),
            **_price_block_kwargs("yes_ask", candle.yes_ask),
        )
        if save_result.was_duplicate:
            result.duplicate += 1
        else:
            result.saved += 1

    latest_after = await get_latest_candlestick_period_end(
        session, snap.market_ticker, period_interval_seconds
    )
    coverage = await get_candlestick_coverage(session, snap.market_ticker, period_interval_seconds)
    result.candle_count_after = coverage["candle_count"]
    result.outcome = (
        MarketOutcome.COMPLETE
        if coverage_reaches_close(latest_after, close_time, tolerance)
        else MarketOutcome.PARTIAL
    )
    return result


async def run_price_backfill(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    client: KalshiClient,
    tickers: list[str] | None = None,
    event_ticker: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    resolution_minutes: int = DEFAULT_RESOLUTION_MINUTES,
    limit: int | None = None,
    skip_covered: bool = True,
    dry_run: bool = False,
    exclude_tickers: Collection[str] | None = None,
) -> PriceBackfillReport:
    """Backfill candlestick history for settled markets matching the given
    filters, oldest-close-time first, one committed transaction per market.
    Resumable: an interrupted run's completed markets are skipped on the
    next invocation via ``skip_covered`` (default); a failed market is
    recorded and the run continues -- never silently discarded.

    Covered markets are excluded from *candidate selection* (not merely
    skipped after selection) for sweep-shaped runs -- the continuous sync and
    any bounded `prices backfill` without an explicit market/event selector --
    so ``limit`` is spent only on markets that still need work.

    Naming an explicit ``tickers`` or ``event_ticker`` opts out of that
    exclusion: an operator who names a market is inspecting or deliberately
    re-fetching it, and must still be able to reach it once it is covered.
    ``skip_covered`` continues to govern whether such a run re-fetches or
    reports it as already complete, exactly as before. ``--no-skip-covered``
    likewise disables the exclusion, since it asks for an unconditional
    re-fetch. Date bounds (``start_date``/``end_date``) narrow a sweep rather
    than name markets, so they keep the exclusion.
    """
    report = PriceBackfillReport(resolution_minutes=resolution_minutes, dry_run=dry_run)
    exclude_covered = skip_covered and not tickers and not event_ticker

    async with session_scope(session_factory) as session:
        candidates = await select_backfill_candidates(
            session,
            tickers=tickers,
            event_ticker=event_ticker,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            period_interval_seconds=resolution_minutes * 60,
            exclude_covered=exclude_covered,
            exclude_tickers=exclude_tickers,
        )
        events_by_ticker = {
            e.event_ticker: e for e in (await session.scalars(select(EventRecord))).all()
        }

    for i, snap in enumerate(candidates, start=1):
        try:
            async with session_scope(session_factory) as session:
                result = await backfill_one_market(
                    session,
                    client,
                    snap,
                    events_by_ticker=events_by_ticker,
                    resolution_minutes=resolution_minutes,
                    skip_covered=skip_covered,
                    dry_run=dry_run,
                )
        except Exception as exc:
            result = MarketBackfillResult(
                market_ticker=snap.market_ticker,
                outcome=MarketOutcome.API_FAILURE,
                error=f"{type(exc).__name__}: {exc}",
            )
            logger.exception("price_backfill.market_failed", market=snap.market_ticker)

        report.results.append(result)
        logger.info(
            "price_backfill.market_complete",
            market=snap.market_ticker,
            progress=f"{i}/{len(candidates)}",
            outcome=result.outcome.value,
            saved=result.saved,
            duplicate=result.duplicate,
            skipped=result.skipped_already_covered,
        )

    return report


DEFAULT_SYNC_LIMIT_PER_CYCLE = 50

#: Outcomes that mean "this market will not yield more candles by being asked
#: again in a moment". Excluding them from the *next* cycle's selection is
#: what stops a permanently quiet market (`no_price_data`) or one that 404s
#: (`expired`) from re-occupying the head of the oldest-first queue forever
#: and starving newly settled markets -- the same starvation the coverage
#: predicate fixes for already-covered markets. `api_failure` is deliberately
#: absent: a transient error must stay retryable.
TERMINAL_SYNC_OUTCOMES = frozenset(
    {MarketOutcome.NO_PRICE_DATA, MarketOutcome.EXPIRED, MarketOutcome.UNSUPPORTED}
)

#: Upper bound on the in-process terminal set, so a long-lived loop cannot
#: grow it without limit. Reaching it simply restores the pre-existing
#: behaviour for the excess (they get retried), never an error.
MAX_TERMINAL_SYNC_TICKERS = 20_000


def _terminal_for_this_process(result: MarketBackfillResult) -> bool:
    """A market this cycle proved is not worth re-asking immediately.

    Either it reported a terminal outcome, or it was fetched and stored
    nothing new while still not reaching close (``partial`` with no forward
    progress) -- asking again on the next cycle would repeat the same request
    for the same nothing. A `partial` result that *did* save candles stays
    eligible, because it is making progress toward coverage.
    """
    if result.outcome in TERMINAL_SYNC_OUTCOMES:
        return True
    return result.outcome is MarketOutcome.PARTIAL and result.saved == 0


async def run_price_sync_loop(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    client_factory: Callable[[], KalshiClient],
    resolution_minutes: int = DEFAULT_RESOLUTION_MINUTES,
    limit_per_cycle: int = DEFAULT_SYNC_LIMIT_PER_CYCLE,
    interval_seconds: float,
    stop_event: asyncio.Event,
    max_cycles: int | None = None,
) -> None:
    """Continuous preservation (section 5): a periodic, bounded backfill pass
    over newly-settled markets, oldest-close-time-first, so nothing ages out
    of the rolling retention window uncaptured. Reuses the exact interval-
    loop shape already used by the Kalshi and weather collectors -- this is
    not a new scheduler, just another instance of the same one -- including
    best-effort `collector_runs` recording under ``collector="prices"`` for
    `ops health`.

    Forward progress has two guards, because a bounded oldest-first queue can
    be starved from the head by two different kinds of market (ADR 0010):

    - already-covered markets are excluded in the candidate query, before
      ``limit`` (`select_backfill_candidates`);
    - markets that just proved terminal for now (quiet, expired, unsupported,
      or partial-with-no-progress) are excluded from the next cycles via an
      in-process set.

    The terminal set is deliberately in-process, not persisted: it needs no
    migration, and forgetting it on restart is the desirable failure mode --
    a market that was quiet an hour ago gets one more chance after a
    restart, and the set drains again within a few cycles.
    """
    cycle_number = 0
    terminal_tickers: set[str] = set()
    while not stop_event.is_set():
        cycle_number += 1
        started_at = utc_now()
        run_stats: dict[str, int] = {}
        run_error: str | None = None
        try:
            client = client_factory()
            async with client:
                report = await run_price_backfill(
                    session_factory=session_factory,
                    client=client,
                    resolution_minutes=resolution_minutes,
                    limit=limit_per_cycle,
                    skip_covered=True,
                    exclude_tickers=terminal_tickers,
                )
                run_stats = report.totals()
            if len(terminal_tickers) < MAX_TERMINAL_SYNC_TICKERS:
                terminal_tickers.update(
                    r.market_ticker for r in report.results if _terminal_for_this_process(r)
                )
            run_stats["terminal_excluded"] = len(terminal_tickers)
            logger.info("price_sync.cycle_complete", cycle=cycle_number, **run_stats)
        except Exception as exc:
            run_error = f"{type(exc).__name__}: {exc}"
            logger.exception("price_sync.cycle_failed", cycle=cycle_number)

        try:
            async with session_scope(session_factory) as session:
                await record_collector_run(
                    session,
                    collector="prices",
                    started_at=started_at,
                    finished_at=utc_now(),
                    success=run_error is None,
                    requests_attempted=0,
                    retries=0,
                    stats=run_stats,
                    error=run_error,
                )
        except Exception:
            logger.exception("price_sync.run_record_failed", cycle=cycle_number)

        if max_cycles is not None and cycle_number >= max_cycles:
            return

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
