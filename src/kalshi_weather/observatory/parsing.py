"""Parser-failure trend detection.

Two independent parser failure surfaces:

1. **Weather CLI parsing** -- the weather collector already counts
   `invalid_items`/`errors` per cycle in `WeatherCycleStats`
   (`ingestion/weather_collector.py`) and persists them into
   `collector_runs.stats_json` (see `storage.repositories
   .record_collector_run`). This module mines that existing history for a
   trend rather than adding new instrumentation to the collector itself.
2. **Settlement parsing** -- `ops/quality.py`'s `settlement_resolution_failures`
   finding already reports this; adapted here via `drift.adapt_quality_finding`
   rather than reimplemented (see `report.py`).

This module computes no research statistic. It only counts failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from kalshi_weather.observatory.severity import Finding, Severity
from kalshi_weather.storage.models import CollectorRun

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

#: A cycle-level parser-failure rate above this fraction, sustained over
#: the whole recent window, escalates from WARNING to CRITICAL -- isolated
#: failures happen (a single malformed product); a persistently high rate
#: suggests the parser itself is broken against a changed upstream format.
CRITICAL_FAILURE_RATE = 0.5


@dataclass(frozen=True, slots=True)
class ParserCycleFailures:
    started_at: datetime
    invalid_items: int
    errors: int


def summarize_weather_parser_failures(cycles: Sequence[ParserCycleFailures]) -> Finding:
    """Trend over the given (already time-windowed) weather-collector
    cycles: total invalid items/errors, and the fraction of cycles with
    any failure at all."""
    if not cycles:
        return Finding(
            domain="observation",
            check="weather_parser_failures",
            severity=Severity.INFO,
            count=0,
            message="no weather-collector cycles recorded in the window",
        )
    total_invalid = sum(c.invalid_items for c in cycles)
    total_errors = sum(c.errors for c in cycles)
    failing_cycles = sum(1 for c in cycles if c.invalid_items or c.errors)
    rate = failing_cycles / len(cycles)
    total = total_invalid + total_errors
    if total == 0:
        severity = Severity.INFO
    elif rate >= CRITICAL_FAILURE_RATE:
        severity = Severity.CRITICAL
    else:
        severity = Severity.WARNING
    return Finding(
        domain="observation",
        check="weather_parser_failures",
        severity=severity,
        count=total,
        message=(
            f"{total_invalid} invalid item(s) and {total_errors} error(s) across "
            f"{len(cycles)} cycles ({failing_cycles} cycles affected, "
            f"{rate:.1%})"
        ),
    )


# --- Async loader --------------------------------------------------------------


async def load_weather_parser_cycles(
    session: AsyncSession, *, since: datetime, limit: int = 500
) -> list[ParserCycleFailures]:
    rows = await session.scalars(
        select(CollectorRun)
        .where(CollectorRun.collector == "weather", CollectorRun.started_at >= since)
        .order_by(CollectorRun.started_at.desc())
        .limit(limit)
    )
    cycles: list[ParserCycleFailures] = []
    for r in rows:
        stats: dict[str, Any] = r.stats_json or {}
        cycles.append(
            ParserCycleFailures(
                started_at=r.started_at,
                invalid_items=int(stats.get("invalid_items", 0)),
                errors=int(stats.get("errors", 0)),
            )
        )
    return cycles
