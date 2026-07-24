"""Settled-transition capture: final snapshots for markets that left the
open list before settling.

Discovery is open-status-filtered (COLLECTOR_MARKET_STATUS=open), so a
market disappears from discovery the moment it closes -- *before* a final
result-bearing snapshot exists. Nothing downstream that keys off "latest
snapshot has a result" (price-sync candidacy, retention monitoring,
settlement labels) ever sees it. This pass re-checks a bounded number of
recently-tracked tickers that are absent from the current open list, one
`get_market` call each (ADR 0009).

Queue fairness (ADR 0012)
-------------------------
The original queue had no memory. A market checked and found *still
unsettled* -- HTTP 200 with an empty `result`, which is a perfectly normal
answer for a market awaiting settlement -- stayed eligible at identical
priority. With `ORDER BY close_time ASC` and a bounded batch, the oldest
markets that happen not to settle occupy every slot forever.

Measured in production on 2026-07-23: 80 pending `KXRAIN` markets (the
oldest by `close_time`) held all 25 head slots across 20+ consecutive cycles
-- `settle_checks=25, settled_captured=0, settle_errors=0` every time --
while 1,476 other pending markets went untouched. Ten of ten sampled starved
markets were `finalized` with real results and would have been captured
immediately. This was silent, ongoing settlement-capture loss.

The fix is a persisted attempt ledger (`settlement_attempts`) plus a
per-outcome cooldown. Eligibility is filtered *before* the batch limit, so a
market that was just checked cannot re-occupy a slot until its cooldown
elapses. Nothing is ever dropped: every non-terminal outcome returns to the
queue, and every deferral is auditable.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalshi_weather.domain.time import to_naive_utc, utc_now
from kalshi_weather.ingestion.discovery import persist_market_snapshot
from kalshi_weather.kalshi.client import KalshiAPIError, KalshiClient
from kalshi_weather.kalshi.models import Market
from kalshi_weather.logging import get_logger
from kalshi_weather.storage.models import MarketSnapshot, SettlementAttempt

logger = get_logger(__name__)


class SettlementOutcome(StrEnum):
    """Why one settlement attempt ended. Deliberately not a single generic
    "failed": the cooldown, terminality, and alerting policy differ per case,
    and collapsing them is what made the original starvation invisible."""

    SETTLED_AND_SAVED = "settled_and_saved"
    ALREADY_CAPTURED = "already_captured"
    NOT_FINAL_YET = "not_final_yet"
    RESULT_MISSING_RETRYABLE = "result_missing_retryable"
    API_FAILURE_RETRYABLE = "api_failure_retryable"
    MALFORMED_PAYLOAD = "malformed_payload"
    UNSUPPORTED_CONTRACT_TYPE = "unsupported_contract_type"
    MARKET_REMOVED = "market_removed"
    PERSISTENCE_FAILURE = "persistence_failure"
    UNEXPECTED_ERROR = "unexpected_error"


#: Retry policy per outcome: (retryable, cooldown). A terminal outcome has
#: cooldown None and never re-enters the queue.
#:
#: Cooldowns are deliberately coarse. Their job is only to stop one market
#: from re-taking a slot every cycle; precision buys nothing and a short
#: cooldown would reproduce the starvation at a slower rate.
RETRY_POLICY: dict[SettlementOutcome, tuple[bool, timedelta | None]] = {
    # Terminal successes -- the market now has a result, so the base
    # eligibility predicate excludes it anyway; recorded for auditability.
    SettlementOutcome.SETTLED_AND_SAVED: (False, None),
    SettlementOutcome.ALREADY_CAPTURED: (False, None),
    # The venue says "closed, no result yet" and the market's own
    # expiration_time has NOT passed: it is simply awaiting settlement. Normal
    # and expected -- never alert on this.
    SettlementOutcome.NOT_FINAL_YET: (True, timedelta(hours=1)),
    # Same answer, but finality has passed. Still retried (the venue may
    # settle late), just far less often.
    SettlementOutcome.RESULT_MISSING_RETRYABLE: (True, timedelta(hours=6)),
    SettlementOutcome.API_FAILURE_RETRYABLE: (True, timedelta(minutes=15)),
    SettlementOutcome.MALFORMED_PAYLOAD: (True, timedelta(hours=6)),
    # Never silently dropped: parked on a long cooldown and counted, so an
    # unsupported family is visible in metrics instead of vanishing.
    SettlementOutcome.UNSUPPORTED_CONTRACT_TYPE: (True, timedelta(hours=24)),
    # The venue no longer serves this ticker. Terminal.
    SettlementOutcome.MARKET_REMOVED: (False, None),
    SettlementOutcome.PERSISTENCE_FAILURE: (True, timedelta(minutes=15)),
    SettlementOutcome.UNEXPECTED_ERROR: (True, timedelta(minutes=15)),
}

TERMINAL_OUTCOMES = frozenset(o for o, (retry, _) in RETRY_POLICY.items() if not retry)

#: How many rows to pull before applying the batch limit. Eligibility is
#: filtered before the limit, so this only bounds how far the scan looks for
#: actionable markets in one cycle.
CANDIDATE_SCAN_LIMIT = 2000


@dataclass(slots=True)
class SettleAttemptResult:
    market_ticker: str
    outcome: SettlementOutcome
    http_status: int | None = None
    error_code: str | None = None
    detail: str | None = None


@dataclass(slots=True)
class SettleSyncStats:
    """Per-cycle settlement metrics.

    `checked`/`settled_captured`/`errors` are retained under their original
    names so existing `collector_runs` history and `ops health` keep working.
    """

    candidates_considered: int = 0
    candidates_selected: int = 0
    checked: int = 0
    settled_captured: int = 0
    still_unsettled: int = 0
    errors: int = 0
    deferred_cooldown: int = 0
    terminal_excluded: int = 0
    queue_remaining: int = 0
    unique_series_selected: int = 0
    max_markets_from_one_series: int = 0
    oldest_actionable_age_seconds: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)

    def as_stats(self) -> dict[str, int]:
        base = {
            "settle_checks": self.checked,
            "settled_captured": self.settled_captured,
            "settle_errors": self.errors,
            "settle_candidates_considered": self.candidates_considered,
            "settle_candidates_selected": self.candidates_selected,
            "settle_deferred_cooldown": self.deferred_cooldown,
            "settle_terminal_excluded": self.terminal_excluded,
            "settle_queue_remaining": self.queue_remaining,
            "settle_unique_series": self.unique_series_selected,
            "settle_max_from_one_series": self.max_markets_from_one_series,
            "settle_oldest_actionable_age_seconds": self.oldest_actionable_age_seconds,
        }
        for outcome in SettlementOutcome:
            base[f"settle_outcome_{outcome.value}"] = self.outcomes.get(outcome.value, 0)
        return base


def classify_market(market: Market, *, now: datetime) -> SettlementOutcome:
    """Classify a successfully fetched market.

    `result` in {yes, no} is a settlement. Anything else means the venue has
    not settled it yet, and the market's own `expiration_time` decides how
    hopeful we should be: still inside the window is routine
    (`not_final_yet`); past it is unusual enough to deserve a longer cooldown
    and its own metric (`result_missing_retryable`).

    A non-empty `result` that is neither yes nor no is a payload we do not
    understand -- surfaced as `malformed_payload` rather than being treated as
    "not settled", so it cannot hide.
    """
    result = (market.result or "").strip().lower()
    if result in ("yes", "no"):
        return SettlementOutcome.SETTLED_AND_SAVED
    if result:
        return SettlementOutcome.MALFORMED_PAYLOAD
    expiration = market.expiration_time
    if expiration is not None and to_naive_utc(expiration) <= to_naive_utc(now):
        return SettlementOutcome.RESULT_MISSING_RETRYABLE
    return SettlementOutcome.NOT_FINAL_YET


async def find_tickers_pending_settlement_check(
    session: AsyncSession,
    *,
    exclude_tickers: Collection[str],
    recent_days: int,
    limit: int,
    now: datetime | None = None,
) -> list[str]:
    """Actionable tickers, oldest `close_time` first, filtered **before** the
    batch limit.

    A ticker is actionable when its latest snapshot has no settlement result,
    it was observed within `recent_days`, it is not in the current open list,
    and its latest settlement attempt is neither terminal nor still inside its
    cooldown.

    Filtering before the limit is the whole point: the previous version sliced
    the oldest N and had no notion of "already tried, come back later", so
    markets that never settle held every slot indefinitely (ADR 0012).
    Chronological priority is preserved *among actionable markets*.
    """
    now = now or utc_now()
    now_naive = to_naive_utc(now)
    cutoff = now_naive - timedelta(days=recent_days)

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
        .limit(CANDIDATE_SCAN_LIMIT)
    )
    rows = list((await session.scalars(stmt)).all())

    latest_attempts = await _latest_attempts(session, rows)
    excluded = set(exclude_tickers)

    actionable: list[str] = []
    for ticker in rows:
        if ticker in excluded:
            continue
        attempt = latest_attempts.get(ticker)
        if attempt is not None:
            if not attempt.retryable:
                continue
            if (
                attempt.next_attempt_at is not None
                and to_naive_utc(attempt.next_attempt_at) > now_naive
            ):
                continue
        actionable.append(ticker)
        if len(actionable) >= limit:
            break
    return actionable


async def _latest_attempts(
    session: AsyncSession, tickers: list[str]
) -> dict[str, SettlementAttempt]:
    """Latest attempt row per ticker.

    Deliberately does **not** filter by the candidate ticker list: doing so
    generated an ``IN (...)`` with one bind parameter per candidate (up to
    `CANDIDATE_SCAN_LIMIT`), producing a multi-thousand-parameter statement.
    The ledger holds at most one row per market per attempt and is indexed on
    ``(market_ticker, attempted_at)``, so scanning its max-id-per-ticker set
    once is both smaller and faster than parameterising the candidate list.
    """
    if not tickers:
        return {}
    newest = (
        select(func.max(SettlementAttempt.id))
        .group_by(SettlementAttempt.market_ticker)
        .scalar_subquery()
    )
    rows = (
        await session.scalars(select(SettlementAttempt).where(SettlementAttempt.id.in_(newest)))
    ).all()
    wanted = set(tickers)
    return {r.market_ticker: r for r in rows if r.market_ticker in wanted}


async def record_settlement_attempt(
    session: AsyncSession,
    *,
    market_ticker: str,
    outcome: SettlementOutcome,
    now: datetime,
    http_status: int | None = None,
    error_code: str | None = None,
    detail: str | None = None,
    raw_payload_id: int | None = None,
    snapshot_id: int | None = None,
    environment: str | None = None,
) -> SettlementAttempt:
    """Append one attempt row. Never updates an existing row. ``environment``
    (ADR 0013) is the settlement client's source (currently demo)."""
    retryable, cooldown = RETRY_POLICY[outcome]
    record = SettlementAttempt(
        market_ticker=market_ticker,
        attempted_at=now,
        outcome=outcome.value,
        retryable=retryable,
        next_attempt_at=(now + cooldown) if cooldown is not None else None,
        http_status=http_status,
        error_code=error_code,
        detail=(detail or None) if detail is None else detail[:300],
        raw_payload_id=raw_payload_id,
        snapshot_id=snapshot_id,
        environment=environment,
    )
    session.add(record)
    await session.flush()
    return record


async def _attempt_one(
    session: AsyncSession,
    client: KalshiClient,
    ticker: str,
    *,
    now: datetime,
) -> SettleAttemptResult:
    """Fetch and persist one market inside the caller's transaction.

    Snapshot persistence and the attempt row are written in the same
    transaction, so they commit together or not at all -- a persisted snapshot
    can never be paired with a missing attempt record, and a recorded terminal
    success can never exist without its snapshot.
    """
    env = client.source_environment  # ADR 0013: stamp rows with this client's source
    try:
        market = await client.get_market(ticker)
    except KalshiAPIError as exc:
        if exc.status_code == 404:
            outcome = SettlementOutcome.MARKET_REMOVED
        else:
            outcome = SettlementOutcome.API_FAILURE_RETRYABLE
        await record_settlement_attempt(
            session,
            market_ticker=ticker,
            outcome=outcome,
            now=now,
            http_status=exc.status_code,
            error_code=type(exc).__name__,
            detail=str(exc)[:300],
            environment=env,
        )
        return SettleAttemptResult(ticker, outcome, http_status=exc.status_code)
    except Exception as exc:
        await record_settlement_attempt(
            session,
            market_ticker=ticker,
            outcome=SettlementOutcome.API_FAILURE_RETRYABLE,
            now=now,
            error_code=type(exc).__name__,
            detail=str(exc)[:300],
            environment=env,
        )
        return SettleAttemptResult(
            ticker, SettlementOutcome.API_FAILURE_RETRYABLE, error_code=type(exc).__name__
        )

    outcome = classify_market(market, now=now)
    raw_id = client.last_raw_payload_id

    try:
        save = await persist_market_snapshot(
            session, market, raw_payload_id=raw_id, environment=env
        )
    except Exception as exc:
        # Persistence failed: the attempt must NOT be recorded as a success.
        # Re-raise so the per-market transaction rolls back entirely; the
        # caller records a persistence_failure in a fresh transaction, leaving
        # the market retryable rather than falsely terminal.
        raise _PersistenceFailure(type(exc).__name__, str(exc)[:300]) from exc

    if outcome is SettlementOutcome.SETTLED_AND_SAVED and save.was_duplicate:
        outcome = SettlementOutcome.ALREADY_CAPTURED

    await record_settlement_attempt(
        session,
        market_ticker=ticker,
        outcome=outcome,
        now=now,
        http_status=200,
        raw_payload_id=raw_id,
        snapshot_id=save.record.id,
        environment=env,
    )
    return SettleAttemptResult(ticker, outcome, http_status=200)


class _PersistenceFailure(Exception):
    def __init__(self, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail


async def capture_settled_transitions(
    client: KalshiClient,
    session: AsyncSession,
    *,
    open_tickers: Collection[str],
    limit: int,
    recent_days: int,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    now: datetime | None = None,
) -> SettleSyncStats:
    """Run one bounded settlement pass.

    Each market is processed inside its own SAVEPOINT on the caller's
    `session`, so one market's failure rolls back only that market while the
    rest of the cycle -- and the outer transaction -- continue.

    `session_factory` is accepted but no longer used to open a session per
    market. That approach failed in production: a separate session cannot see
    the `raw_api_payloads` row the client's sink wrote into the caller's still
    open transaction, so every newly reached market violated the
    `raw_payload_id` foreign key. The parameter is retained so the collector's
    call site keeps working and so the intent (per-market isolation) stays
    explicit at the boundary.
    """
    now = now or utc_now()
    stats = SettleSyncStats()

    pending = await find_tickers_pending_settlement_check(
        session, exclude_tickers=open_tickers, recent_days=recent_days, limit=limit, now=now
    )
    stats.candidates_selected = len(pending)
    stats.checked = len(pending)

    if pending:
        series = [t.split("-")[0] for t in pending]
        stats.unique_series_selected = len(set(series))
        stats.max_markets_from_one_series = max(series.count(s) for s in set(series))
        oldest = await session.scalar(
            select(func.min(MarketSnapshot.close_time)).where(
                MarketSnapshot.market_ticker.in_(pending)
            )
        )
        if oldest is not None:
            stats.oldest_actionable_age_seconds = max(
                0, int((to_naive_utc(now) - to_naive_utc(oldest)).total_seconds())
            )

    for ticker in pending:
        try:
            # SAVEPOINT per market, not a separate session. A separate session
            # cannot see the `raw_api_payloads` row the client's sink just
            # wrote into *this* session's open transaction, so every newly
            # reached market failed its raw_payload_id foreign key (observed
            # in production, cycle 2 of the ADR 0012 deploy: 10/10 new markets
            # hit ForeignKeyViolation). A nested transaction gives the same
            # per-market isolation -- a rollback undoes only this market --
            # while keeping the raw payload visible.
            async with session.begin_nested():
                result = await _attempt_one(session, client, ticker, now=now)
        except _PersistenceFailure as exc:
            result = SettleAttemptResult(
                ticker,
                SettlementOutcome.PERSISTENCE_FAILURE,
                error_code=exc.error_code,
                detail=exc.detail,
            )
            try:
                # The savepoint above already rolled back this market's writes;
                # the outer transaction is still usable, so the attempt row is
                # recorded normally and the market stays retryable.
                async with session.begin_nested():
                    await record_settlement_attempt(
                        session,
                        market_ticker=ticker,
                        outcome=SettlementOutcome.PERSISTENCE_FAILURE,
                        now=now,
                        error_code=exc.error_code,
                        detail=exc.detail,
                        environment=client.source_environment,
                    )
            except Exception:
                logger.exception("collector.settle_capture.attempt_record_failed", ticker=ticker)
            logger.warning(
                "collector.settle_capture.persistence_failed",
                ticker=ticker,
                error=exc.error_code,
            )
        except Exception as exc:
            result = SettleAttemptResult(
                ticker, SettlementOutcome.UNEXPECTED_ERROR, error_code=type(exc).__name__
            )
            logger.exception("collector.settle_capture.failed", ticker=ticker)

        stats.outcomes[result.outcome.value] = stats.outcomes.get(result.outcome.value, 0) + 1
        if result.outcome is SettlementOutcome.SETTLED_AND_SAVED:
            stats.settled_captured += 1
            logger.info("collector.settle_capture.final_snapshot", ticker=ticker)
        elif result.outcome in (
            SettlementOutcome.NOT_FINAL_YET,
            SettlementOutcome.RESULT_MISSING_RETRYABLE,
        ):
            stats.still_unsettled += 1
        elif result.outcome in (
            SettlementOutcome.API_FAILURE_RETRYABLE,
            SettlementOutcome.PERSISTENCE_FAILURE,
            SettlementOutcome.UNEXPECTED_ERROR,
            SettlementOutcome.MALFORMED_PAYLOAD,
        ):
            stats.errors += 1

    (
        stats.candidates_considered,
        stats.deferred_cooldown,
        stats.terminal_excluded,
    ) = await _queue_shape(session, exclude_tickers=open_tickers, recent_days=recent_days, now=now)
    stats.queue_remaining = max(0, stats.candidates_considered - stats.candidates_selected)
    return stats


async def _queue_shape(
    session: AsyncSession,
    *,
    exclude_tickers: Collection[str],
    recent_days: int,
    now: datetime,
) -> tuple[int, int, int]:
    """(considered, deferred_by_cooldown, terminal_excluded) for observability."""
    now_naive = to_naive_utc(now)
    cutoff = now_naive - timedelta(days=recent_days)
    latest_ids = (
        select(func.max(MarketSnapshot.id)).group_by(MarketSnapshot.market_ticker).scalar_subquery()
    )
    rows = list(
        (
            await session.scalars(
                select(MarketSnapshot.market_ticker)
                .where(
                    MarketSnapshot.id.in_(latest_ids),
                    MarketSnapshot.result.is_(None) | (MarketSnapshot.result == ""),
                    MarketSnapshot.observed_at >= cutoff,
                )
                .limit(CANDIDATE_SCAN_LIMIT)
            )
        ).all()
    )
    excluded = set(exclude_tickers)
    rows = [t for t in rows if t not in excluded]
    attempts = await _latest_attempts(session, rows)
    cooldown = terminal = 0
    for ticker in rows:
        a = attempts.get(ticker)
        if a is None:
            continue
        if not a.retryable:
            terminal += 1
        elif a.next_attempt_at is not None and to_naive_utc(a.next_attempt_at) > now_naive:
            cooldown += 1
    return len(rows), cooldown, terminal


__all__ = [
    "CANDIDATE_SCAN_LIMIT",
    "RETRY_POLICY",
    "TERMINAL_OUTCOMES",
    "SettleAttemptResult",
    "SettleSyncStats",
    "SettlementOutcome",
    "capture_settled_transitions",
    "classify_market",
    "find_tickers_pending_settlement_check",
    "record_settlement_attempt",
]
