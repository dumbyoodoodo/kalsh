"""Archive continuity and missing-collection-cycle detection, generalized
across all five collection streams (forecasts, observations, markets,
trades, candles).

Two distinct questions, both schedule-agnostic (no assumption about *when*
in the day data should arrive -- see `ops/forecast_cadence.py` for the one
stream, forecasts, that also gets a schedule-*aware* check on top of this):

1. **Archive continuity** (`find_continuity_gaps`): has the *data itself*
   stopped arriving? Operates on the stream's own stored timestamps.
2. **Missing collection cycles** (`find_missed_run_cycles`): has the
   *collector process* stopped running, independent of whether upstream
   data exists? Operates on `collector_runs` -- a collector can run
   successfully every cycle while upstream has nothing new to report;
   that is stream silence, not a missed cycle. Conflating the two would
   misattribute an upstream lull as a collector outage.

Every check here is a pure function over in-memory timestamp sequences;
thin async loaders at the bottom adapt the database (the same split
`ops/forecast_cadence.py` established).

This module computes no research statistic. It only detects gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from kalshi_weather.domain.time import to_naive_utc
from kalshi_weather.storage.models import (
    CollectorRun,
    MarketCandlestick,
    MarketSnapshot,
    TradeRecord,
    WeatherForecast,
    WeatherObservation,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

#: Default schedule-agnostic max-gap thresholds per stream, in hours.
#: Deliberately coarse backstops -- forecasts already have a much more
#: precise, schedule-aware check in `ops/forecast_cadence.py`; this module
#: exists for the streams that don't (and as a second, independent signal
#: for forecasts too).
DEFAULT_MAX_GAP_HOURS: dict[str, float] = {
    "forecast": 30.0,
    "observation": 48.0,  # a station's CLI report cycle is daily
    "market": 2.0,  # the kalshi collector's default interval is 30 min
    "trade": 48.0,  # trading activity can legitimately pause; less strict
    "candle": 2.0,
}

#: A collector loop is considered to have missed a cycle once the gap
#: between consecutive runs exceeds its configured interval by this factor
#: -- distinguishes "one slow cycle" from "the loop stopped."
DEFAULT_MISSED_CYCLE_MULTIPLIER = 3.0


@dataclass(frozen=True, slots=True)
class TimestampGap:
    stream: str
    gap_start: datetime
    gap_end: datetime

    @property
    def hours(self) -> float:
        return (self.gap_end - self.gap_start).total_seconds() / 3600.0


@dataclass(frozen=True, slots=True)
class RunRecord:
    collector: str
    started_at: datetime
    success: bool


@dataclass(frozen=True, slots=True)
class MissedCycleWindow:
    collector: str
    gap_start: datetime
    gap_end: datetime
    expected_interval_seconds: float

    @property
    def hours(self) -> float:
        return (self.gap_end - self.gap_start).total_seconds() / 3600.0


def find_continuity_gaps(
    timestamps: Sequence[datetime],
    *,
    stream: str,
    max_gap_hours: float,
    now: datetime,
) -> list[TimestampGap]:
    """Gaps exceeding `max_gap_hours` between consecutive ascending
    timestamps, *including* the trailing gap between the newest timestamp
    and `now` (a stream that stopped entirely must still be caught -- there
    is no "next" timestamp to compare against otherwise). An empty
    `timestamps` sequence returns no gaps -- "never collected yet" is a
    distinct condition from "collection stopped," and is the caller's
    judgment to make (mirroring `ops/quality.py`'s "no forecasts collected
    yet" convention), not this function's."""
    ordered = sorted(timestamps)
    gaps: list[TimestampGap] = []
    for earlier, later in pairwise(ordered):
        hours = (later - earlier).total_seconds() / 3600.0
        if hours > max_gap_hours:
            gaps.append(TimestampGap(stream=stream, gap_start=earlier, gap_end=later))
    if ordered:
        trailing_hours = (now - ordered[-1]).total_seconds() / 3600.0
        if trailing_hours > max_gap_hours:
            gaps.append(TimestampGap(stream=stream, gap_start=ordered[-1], gap_end=now))
    return gaps


def find_missed_run_cycles(
    runs: Sequence[RunRecord],
    *,
    collector: str,
    interval_seconds: float,
    now: datetime,
    missed_cycle_multiplier: float = DEFAULT_MISSED_CYCLE_MULTIPLIER,
) -> list[MissedCycleWindow]:
    """Gaps between consecutive `collector_runs` rows (any status -- a
    failing-but-running loop is not a missed cycle; that is
    `ops/quality.py`/`ops/health.py` territory) exceeding
    `interval_seconds * missed_cycle_multiplier`, including the trailing
    gap to `now`."""
    ordered = sorted((r for r in runs if r.collector == collector), key=lambda r: r.started_at)
    threshold_hours = interval_seconds * missed_cycle_multiplier / 3600.0
    windows: list[MissedCycleWindow] = []
    for earlier, later in pairwise(ordered):
        hours = (later.started_at - earlier.started_at).total_seconds() / 3600.0
        if hours > threshold_hours:
            windows.append(
                MissedCycleWindow(
                    collector=collector,
                    gap_start=earlier.started_at,
                    gap_end=later.started_at,
                    expected_interval_seconds=interval_seconds,
                )
            )
    if ordered:
        trailing_hours = (now - ordered[-1].started_at).total_seconds() / 3600.0
        if trailing_hours > threshold_hours:
            windows.append(
                MissedCycleWindow(
                    collector=collector,
                    gap_start=ordered[-1].started_at,
                    gap_end=now,
                    expected_interval_seconds=interval_seconds,
                )
            )
    return windows


# --- Async loaders (thin; no logic beyond fetching) --------------------------


async def load_stream_timestamps(
    session: AsyncSession, stream: str, *, since: datetime
) -> list[datetime]:
    """Recent timestamps for one of the five monitored streams."""
    if stream == "forecast":
        stmt = select(WeatherForecast.issue_time).where(WeatherForecast.issue_time >= since)
    elif stream == "observation":
        stmt = select(WeatherObservation.issuance_time).where(
            WeatherObservation.issuance_time >= since
        )
    elif stream == "market":
        stmt = select(MarketSnapshot.observed_at).where(MarketSnapshot.observed_at >= since)
    elif stream == "trade":
        stmt = select(TradeRecord.executed_at).where(TradeRecord.executed_at >= since)
    elif stream == "candle":
        stmt = select(MarketCandlestick.period_end).where(MarketCandlestick.period_end >= since)
    else:
        raise ValueError(f"unknown stream {stream!r}")
    rows = await session.scalars(stmt)
    return [to_naive_utc(ts) for ts in rows.all()]


async def load_run_records(session: AsyncSession, *, since: datetime) -> list[RunRecord]:
    result = await session.execute(
        select(CollectorRun.collector, CollectorRun.started_at, CollectorRun.success).where(
            CollectorRun.started_at >= since
        )
    )
    return [
        RunRecord(collector=r[0], started_at=to_naive_utc(r[1]), success=r[2]) for r in result.all()
    ]


async def newest_timestamp(session: AsyncSession, stream: str) -> datetime | None:
    column = {
        "forecast": WeatherForecast.issue_time,
        "observation": WeatherObservation.issuance_time,
        "market": MarketSnapshot.observed_at,
        "trade": TradeRecord.executed_at,
        "candle": MarketCandlestick.period_end,
    }[stream]
    result = await session.scalar(select(func.max(column)))
    return to_naive_utc(result) if result is not None else None
