"""Price (candlestick) coverage reporting and retention-risk monitoring
(Phase 7A, docs/adr/0007-price-ingestion.md).

Two things live here, both read-only over the store:

- `build_price_coverage_report` -- the post-backfill measurement required by
  the milestone: markets attempted/captured, candle counts, coverage by
  market/event-date/variable/resolution, missing intervals, duplicate count,
  and settlement-label overlap. This is where "unresolved coverage gap" (a
  market that remains incomplete across runs, independent of any single run's
  outcome) is computed -- a query-time classification over stored data, not a
  per-run backfill outcome (see ingestion/price_backfill.py's module
  docstring for why that split was made).
- `retention_risk_summary` -- the smaller, cheaper subset `ops health` embeds
  on every call: oldest uncaptured market, markets nearing the observed
  retention cutoff, incomplete-coverage count. The observed retention window
  is a measured finding (~67 days as of the 2026-07-21 investigation), not a
  documented API guarantee -- passed in as a parameter, never hardcoded, so
  it can be re-verified and adjusted without a code change.

`verify_sample_against_live` is the one function here that isn't DB-only: it
re-fetches a sample of markets from the live API and diffs candle counts
against storage -- the "database counts and report counts must agree
exactly" validation the milestone requires, run for real (not just coded)
during Phase 7A -- see docs/research/investigations/
INV-20260721-price-history-recovery.md and the H0007 readiness artifact for
the measured result.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.domain.time import to_naive_utc
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.settlement.resolver import ParserSettlementResolver
from kalshi_weather.settlement.spec import SettlementStatus
from kalshi_weather.storage.models import MarketCandlestick, MarketSnapshot


@dataclass(frozen=True, slots=True)
class PriceCoverageReport:
    generated_at: str
    resolution_minutes: int
    markets_attempted: int  # settled markets known in the archive
    markets_captured: int  # settled markets with >=1 stored candle
    markets_complete: int  # coverage reaches close_time (within tolerance)
    markets_incomplete: int  # captured but coverage does not reach close_time
    markets_never_captured: int
    total_candles: int
    earliest_candle: str | None
    latest_candle: str | None
    coverage_by_event_date: list[dict[str, Any]]
    coverage_by_variable: dict[str, dict[str, int]]
    markets_with_missing_intervals: list[dict[str, Any]]
    likely_lost_to_retention: list[dict[str, Any]]
    duplicate_row_count: int
    settlement_label_overlap: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


COMPLETENESS_TOLERANCE_SECONDS = 3600


async def _settled_markets(session: AsyncSession) -> list[MarketSnapshot]:
    latest_ids = (
        select(func.max(MarketSnapshot.id)).group_by(MarketSnapshot.market_ticker).scalar_subquery()
    )
    rows = await session.scalars(
        select(MarketSnapshot).where(
            MarketSnapshot.id.in_(latest_ids), MarketSnapshot.result.in_(["yes", "no"])
        )
    )
    return list(rows.all())


def _coverage_status(close_time: datetime | None, latest_candle: datetime | None) -> str:
    if latest_candle is None:
        return "never_captured"
    if close_time is None:
        return "incomplete"  # can't judge completeness without a close_time
    tolerance = timedelta(seconds=COMPLETENESS_TOLERANCE_SECONDS)
    return "complete" if latest_candle >= to_naive_utc(close_time) - tolerance else "incomplete"


async def build_price_coverage_report(
    session: AsyncSession,
    *,
    resolution_minutes: int,
    observed_retention_days: int,
    now: datetime | None = None,
    sample_limit: int = 10,
) -> PriceCoverageReport:
    """Measured coverage over the store -- every number here is a real query
    result, never an estimate (the milestone's own requirement: 'do not
    estimate, measure')."""
    period_interval_seconds = resolution_minutes * 60
    moment = now or datetime.now(UTC)
    markets = await _settled_markets(session)

    # Per-market latest/earliest candle + count, in one grouped query.
    coverage_rows = (
        await session.execute(
            select(
                MarketCandlestick.market_ticker,
                func.count(MarketCandlestick.id),
                func.min(MarketCandlestick.period_start),
                func.max(MarketCandlestick.period_end),
            )
            .where(MarketCandlestick.period_interval_seconds == period_interval_seconds)
            .group_by(MarketCandlestick.market_ticker)
        )
    ).all()
    by_ticker = {
        ticker: {"count": count, "earliest": earliest, "latest": latest}
        for ticker, count, earliest, latest in coverage_rows
    }

    total_candles = sum(c["count"] for c in by_ticker.values())
    all_earliest = [c["earliest"] for c in by_ticker.values() if c["earliest"] is not None]
    all_latest = [c["latest"] for c in by_ticker.values() if c["latest"] is not None]

    captured = complete = incomplete = never_captured = 0
    missing_intervals: list[dict[str, Any]] = []
    likely_lost: list[dict[str, Any]] = []
    by_date: dict[str, dict[str, int]] = {}

    for snap in markets:
        info = by_ticker.get(snap.market_ticker)
        close_time = to_naive_utc(snap.close_time) if snap.close_time else None
        status = _coverage_status(snap.close_time, info["latest"] if info else None)

        if info is not None:
            captured += 1
            expected = (
                int((info["latest"] - info["earliest"]).total_seconds() / period_interval_seconds)
                + 1
                if info["latest"] and info["earliest"]
                else 0
            )
            if expected > 0 and info["count"] < expected:
                missing_intervals.append(
                    {
                        "market_ticker": snap.market_ticker,
                        "expected_candles": expected,
                        "actual_candles": info["count"],
                        "missing": expected - info["count"],
                    }
                )
        else:
            never_captured += 1
            if close_time is not None:
                age_days = (moment - close_time.replace(tzinfo=UTC)).days
                if age_days > observed_retention_days:
                    likely_lost.append(
                        {
                            "market_ticker": snap.market_ticker,
                            "close_time": close_time.isoformat(),
                            "age_days": age_days,
                            "note": (
                                "inferred from absence + age past the observed retention "
                                "window -- not a directly observed 404; re-run backfill to "
                                "confirm"
                            ),
                        }
                    )

        if status == "complete":
            complete += 1
        elif status == "incomplete":
            incomplete += 1

        if close_time is not None:
            key = close_time.date().isoformat()
            bucket = by_date.setdefault(key, {"complete": 0, "incomplete": 0, "never_captured": 0})
            bucket[status] += 1

    # By variable: needs the settlement parser (station_id/variable per market).
    specs = await ParserSettlementResolver().resolve_specs(session)
    variable_by_ticker = {s.market_ticker: s.variable for s in specs}
    by_variable: dict[str, dict[str, int]] = {}
    for snap in markets:
        variable = variable_by_ticker.get(snap.market_ticker) or "unresolved"
        info = by_ticker.get(snap.market_ticker)
        bucket = by_variable.setdefault(variable, {"captured": 0, "never_captured": 0})
        bucket["captured" if info is not None else "never_captured"] += 1

    # Duplicate check: should always be 0 (unique index) -- verified, not assumed.
    total_rows = await session.scalar(
        select(func.count(MarketCandlestick.id)).where(
            MarketCandlestick.period_interval_seconds == period_interval_seconds
        )
    )
    # Multi-column DISTINCT via func.distinct(a, b) uses row-value syntax
    # that SQLite rejects (used by the in-memory test suite) -- group-by is
    # the portable equivalent across both dialects.
    distinct_groups = (
        select(MarketCandlestick.market_ticker, MarketCandlestick.period_end)
        .where(MarketCandlestick.period_interval_seconds == period_interval_seconds)
        .group_by(MarketCandlestick.market_ticker, MarketCandlestick.period_end)
        .subquery()
    )
    distinct_rows = await session.scalar(select(func.count()).select_from(distinct_groups))
    duplicate_row_count = int(total_rows or 0) - int(distinct_rows or 0)

    # Settlement-label overlap: markets with price coverage AND a resolved spec.
    resolved_tickers = {s.market_ticker for s in specs if s.status is SettlementStatus.RESOLVED}
    captured_tickers = set(by_ticker.keys())
    settlement_label_overlap = {
        "captured_and_resolved": len(captured_tickers & resolved_tickers),
        "captured_only": len(captured_tickers - resolved_tickers),
        "resolved_only": len(resolved_tickers - captured_tickers),
    }

    return PriceCoverageReport(
        generated_at=moment.isoformat(),
        resolution_minutes=resolution_minutes,
        markets_attempted=len(markets),
        markets_captured=captured,
        markets_complete=complete,
        markets_incomplete=incomplete,
        markets_never_captured=never_captured,
        total_candles=total_candles,
        earliest_candle=min(all_earliest).isoformat() if all_earliest else None,
        latest_candle=max(all_latest).isoformat() if all_latest else None,
        coverage_by_event_date=[{"date": d, **counts} for d, counts in sorted(by_date.items())],
        coverage_by_variable=by_variable,
        markets_with_missing_intervals=sorted(missing_intervals, key=lambda m: -m["missing"])[
            :sample_limit
        ],
        likely_lost_to_retention=likely_lost[:sample_limit],
        duplicate_row_count=duplicate_row_count,
        settlement_label_overlap=settlement_label_overlap,
    )


@dataclass(frozen=True, slots=True)
class PriceRetentionHealth:
    oldest_uncaptured_market: dict[str, Any] | None
    markets_nearing_expiry: int
    markets_incomplete_coverage: int
    ingestion_lag_days: int | None  # age of the oldest still-recoverable gap

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def retention_risk_summary(
    session: AsyncSession,
    *,
    resolution_minutes: int,
    observed_retention_days: int,
    warning_buffer_days: int,
    now: datetime | None = None,
) -> PriceRetentionHealth:
    """The cheap subset `ops health` embeds on every call -- oldest gap,
    at-risk count, incomplete count. Kept separate from the full coverage
    report so `ops health` stays fast even with a large archive."""
    period_interval_seconds = resolution_minutes * 60
    moment = (now or datetime.now(UTC)).replace(tzinfo=None)
    warning_cutoff_days = observed_retention_days - warning_buffer_days

    markets = await _settled_markets(session)
    captured = await session.scalars(
        select(MarketCandlestick.market_ticker)
        .where(MarketCandlestick.period_interval_seconds == period_interval_seconds)
        .distinct()
    )
    captured_tickers = set(captured.all())

    uncaptured = [
        (snap, (moment - to_naive_utc(snap.close_time)).days)
        for snap in markets
        if snap.market_ticker not in captured_tickers and snap.close_time is not None
    ]
    oldest = max(uncaptured, key=lambda pair: pair[1], default=None)
    nearing_expiry = sum(1 for _, age in uncaptured if age >= warning_cutoff_days)

    incomplete = 0
    for snap in markets:
        if snap.market_ticker not in captured_tickers:
            continue
        latest = await session.scalar(
            select(func.max(MarketCandlestick.period_end)).where(
                MarketCandlestick.market_ticker == snap.market_ticker,
                MarketCandlestick.period_interval_seconds == period_interval_seconds,
            )
        )
        if _coverage_status(snap.close_time, latest) == "incomplete":
            incomplete += 1

    return PriceRetentionHealth(
        oldest_uncaptured_market=(
            {"market_ticker": oldest[0].market_ticker, "age_days": oldest[1]}
            if oldest is not None
            else None
        ),
        markets_nearing_expiry=nearing_expiry,
        markets_incomplete_coverage=incomplete,
        ingestion_lag_days=oldest[1] if oldest is not None else None,
    )


async def verify_sample_against_live(
    session: AsyncSession,
    client: KalshiClient,
    *,
    tickers: list[str],
    series_by_ticker: dict[str, str],
    resolution_minutes: int,
) -> dict[str, Any]:
    """Re-fetch a sample of markets live and diff candle counts against
    storage -- the milestone's required 'validate a sample directly against
    live API responses' / 'database counts and report counts must agree
    exactly' check. Returns per-ticker (db_count, live_count, agree)."""
    period_interval_seconds = resolution_minutes * 60
    results: dict[str, Any] = {}
    for ticker in tickers:
        db_count = await session.scalar(
            select(func.count(MarketCandlestick.id)).where(
                MarketCandlestick.market_ticker == ticker,
                MarketCandlestick.period_interval_seconds == period_interval_seconds,
            )
        )
        earliest, latest = (
            await session.execute(
                select(
                    func.min(MarketCandlestick.period_start),
                    func.max(MarketCandlestick.period_end),
                ).where(
                    MarketCandlestick.market_ticker == ticker,
                    MarketCandlestick.period_interval_seconds == period_interval_seconds,
                )
            )
        ).one()
        if earliest is None or latest is None:
            results[ticker] = {"db_count": 0, "live_count": None, "agree": None}
            continue
        series_ticker = series_by_ticker.get(ticker, ticker.split("-")[0])
        live_candles = await client.list_candlesticks(
            series_ticker=series_ticker,
            ticker=ticker,
            start_ts=int(earliest.replace(tzinfo=UTC).timestamp()),
            end_ts=int(latest.replace(tzinfo=UTC).timestamp()),
            period_interval=resolution_minutes,
        )
        results[ticker] = {
            "db_count": int(db_count or 0),
            "live_count": len(live_candles),
            "agree": int(db_count or 0) == len(live_candles),
        }
    return results
