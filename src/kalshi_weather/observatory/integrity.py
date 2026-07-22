"""Duplicate-record detection and timestamp-monotonicity checks for the
streams `ops/quality.py` does not already cover.

`ops/quality.py` already checks natural-key duplicates for
`weather_observations` and `settlement_specs`, and future-dated rows
("timestamp_anomalies") across observations/trades/markets. This module
is deliberately scoped to what that check does *not* do:

- Duplicate natural keys for `market_snapshots`, `market_candlesticks`,
  and `weather_forecasts` (each already has a DB-level unique/primary-key
  constraint that *should* make a duplicate impossible -- exactly like
  `ops/quality.py`'s existing checks, any hit here is corruption, not an
  expected condition).
- **Timestamp monotonicity**: are rows being *inserted* in roughly
  chronological order? This is a different question from "is a timestamp
  future-dated" (`ops/quality.py`'s check) -- a collector clock issue or a
  backfill-replay bug can insert a row whose own timestamp regresses well
  behind rows already stored, without that timestamp ever being in the
  future. Detected by walking rows in **insertion order** (`id`, an
  auto-incrementing surrogate key, is a faithful proxy for insertion
  order) and flagging any row whose timestamp falls more than `tolerance`
  behind the running maximum timestamp seen so far.

This module computes no research statistic. It only counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from kalshi_weather.storage.models import MarketCandlestick, MarketSnapshot, WeatherForecast

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

#: Ordinary out-of-order arrival (network retries, concurrent collector
#: requests) is expected up to this tolerance; only a larger regression is
#: flagged.
DEFAULT_MONOTONICITY_TOLERANCE = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class MonotonicityViolation:
    stream: str
    row_id: int
    timestamp: datetime
    running_max_before: datetime

    @property
    def regression(self) -> timedelta:
        return self.running_max_before - self.timestamp


def find_monotonicity_violations(
    rows: Sequence[tuple[int, datetime]],
    *,
    stream: str,
    tolerance: timedelta = DEFAULT_MONOTONICITY_TOLERANCE,
) -> list[MonotonicityViolation]:
    """`rows`: (id, timestamp) pairs, any order in -- sorted here by `id`
    (insertion order) before walking. A violation is a row whose timestamp
    is more than `tolerance` behind the maximum timestamp of every row
    inserted before it."""
    ordered = sorted(rows, key=lambda r: r[0])
    violations: list[MonotonicityViolation] = []
    running_max: datetime | None = None
    for row_id, ts in ordered:
        if running_max is not None and (running_max - ts) > tolerance:
            violations.append(
                MonotonicityViolation(
                    stream=stream, row_id=row_id, timestamp=ts, running_max_before=running_max
                )
            )
        if running_max is None or ts > running_max:
            running_max = ts
    return violations


# --- Async loaders ------------------------------------------------------------


async def count_duplicate_market_snapshots(session: AsyncSession) -> int:
    """Exact-duplicate (market_ticker, content_hash, observed_at) rows --
    the collector's own content-hash dedup should make this impossible."""
    result = await session.scalar(
        select(func.count()).select_from(
            select(MarketSnapshot.market_ticker)
            .group_by(
                MarketSnapshot.market_ticker,
                MarketSnapshot.content_hash,
                MarketSnapshot.observed_at,
            )
            .having(func.count() > 1)
            .subquery()
        )
    )
    return int(result or 0)


async def count_duplicate_candlesticks(session: AsyncSession) -> int:
    """The unique index on (market_ticker, period_interval_seconds,
    period_end) should already make this impossible at the DB level;
    checked anyway as a corruption sanity check, mirroring
    `ops/quality.py`'s precedent for observations/settlement_specs."""
    result = await session.scalar(
        select(func.count()).select_from(
            select(MarketCandlestick.market_ticker)
            .group_by(
                MarketCandlestick.market_ticker,
                MarketCandlestick.period_interval_seconds,
                MarketCandlestick.period_end,
            )
            .having(func.count() > 1)
            .subquery()
        )
    )
    return int(result or 0)


async def count_duplicate_forecasts(session: AsyncSession) -> int:
    """The unique index on (station_id, variable, issue_time, valid_start)
    should already make this impossible at the DB level."""
    result = await session.scalar(
        select(func.count()).select_from(
            select(WeatherForecast.station_id)
            .group_by(
                WeatherForecast.station_id,
                WeatherForecast.variable,
                WeatherForecast.issue_time,
                WeatherForecast.valid_start,
            )
            .having(func.count() > 1)
            .subquery()
        )
    )
    return int(result or 0)


async def load_id_timestamp_pairs(
    session: AsyncSession, stream: str, *, since_id: int = 0, limit: int = 5000
) -> list[tuple[int, datetime]]:
    """Recent (id, timestamp) pairs for one stream, ordered by id -- the
    input `find_monotonicity_violations` expects."""
    if stream == "market":
        stmt = (
            select(MarketSnapshot.id, MarketSnapshot.observed_at)
            .where(MarketSnapshot.id > since_id)
            .order_by(MarketSnapshot.id)
            .limit(limit)
        )
    elif stream == "candle":
        stmt = (
            select(MarketCandlestick.id, MarketCandlestick.observed_at)
            .where(MarketCandlestick.id > since_id)
            .order_by(MarketCandlestick.id)
            .limit(limit)
        )
    elif stream == "forecast":
        stmt = (
            select(WeatherForecast.id, WeatherForecast.observed_at)
            .where(WeatherForecast.id > since_id)
            .order_by(WeatherForecast.id)
            .limit(limit)
        )
    else:
        raise ValueError(f"unknown stream {stream!r}")
    rows = await session.execute(stmt)
    return [(r[0], r[1]) for r in rows.all()]
