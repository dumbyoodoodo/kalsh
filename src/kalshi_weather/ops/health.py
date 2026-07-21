"""Platform health report (Phase 6): one place where operational problems
become immediately obvious.

Assembles collector liveness (from collector_runs), data freshness, coverage,
settlement resolution state, and the quality-check summary into a single
typed report, renderable as text or JSON by the CLI.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.domain.time import to_naive_utc, utc_now
from kalshi_weather.ops.quality import QualityReport, run_quality_checks
from kalshi_weather.settlement.resolver import ParserSettlementResolver
from kalshi_weather.settlement.spec import SettlementStatus
from kalshi_weather.storage.models import (
    CollectorRun,
    MarketSnapshot,
    OrderbookSnapshot,
    TradeRecord,
    WeatherForecast,
    WeatherObservation,
)
from kalshi_weather.weather.stations import list_stations


@dataclass(frozen=True, slots=True)
class CollectorHealth:
    collector: str
    last_run_at: str | None
    last_success: bool | None
    stale: bool
    runs_recorded: int
    success_rate_recent: float | None  # over the most recent <=50 runs
    mean_duration_seconds: float | None


@dataclass(frozen=True, slots=True)
class HealthReport:
    generated_at: str
    collectors: list[CollectorHealth]
    newest_data: dict[str, str | None]
    database_size_bytes: int | None
    station_coverage: dict[str, Any]
    settlement_coverage: dict[str, Any]
    dataset_completeness: dict[str, Any]
    quality_ok: bool
    quality_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def _collector_health(
    session: AsyncSession, collector: str, *, interval_seconds: float, stale_after_intervals: float
) -> CollectorHealth:
    runs = (
        await session.scalars(
            select(CollectorRun)
            .where(CollectorRun.collector == collector)
            .order_by(CollectorRun.started_at.desc())
            .limit(50)
        )
    ).all()
    total = await session.scalar(
        select(func.count()).where(CollectorRun.collector == collector)
    )
    if not runs:
        return CollectorHealth(
            collector=collector,
            last_run_at=None,
            last_success=None,
            stale=True,  # a collector that has never run is operationally stale
            runs_recorded=0,
            success_rate_recent=None,
            mean_duration_seconds=None,
        )
    newest = runs[0]
    age = (utc_now().replace(tzinfo=None) - to_naive_utc(newest.started_at)).total_seconds()
    return CollectorHealth(
        collector=collector,
        last_run_at=_iso(newest.started_at),
        last_success=newest.success,
        stale=age > interval_seconds * stale_after_intervals,
        runs_recorded=int(total or 0),
        success_rate_recent=sum(1 for r in runs if r.success) / len(runs),
        mean_duration_seconds=sum(r.duration_seconds for r in runs) / len(runs),
    )


async def _database_size_bytes(session: AsyncSession) -> int | None:
    """Postgres only; other dialects (e.g. sqlite tests) return None."""
    try:
        result = await session.scalar(
            text("SELECT pg_database_size(current_database())")
        )
        return int(result) if result is not None else None
    except Exception:
        return None


async def build_health_report(
    session: AsyncSession,
    *,
    kalshi_interval_seconds: float,
    weather_interval_seconds: float,
    stale_after_intervals: float,
    quality: QualityReport | None = None,
) -> HealthReport:
    collectors = [
        await _collector_health(
            session,
            "kalshi",
            interval_seconds=kalshi_interval_seconds,
            stale_after_intervals=stale_after_intervals,
        ),
        await _collector_health(
            session,
            "weather",
            interval_seconds=weather_interval_seconds,
            stale_after_intervals=stale_after_intervals,
        ),
    ]

    newest_data = {
        "market_snapshot": _iso(await session.scalar(select(func.max(MarketSnapshot.observed_at)))),
        "orderbook_snapshot": _iso(
            await session.scalar(select(func.max(OrderbookSnapshot.captured_at)))
        ),
        "trade": _iso(await session.scalar(select(func.max(TradeRecord.executed_at)))),
        "observation_issuance": _iso(
            await session.scalar(select(func.max(WeatherObservation.issuance_time)))
        ),
        "forecast_issuance": _iso(
            await session.scalar(select(func.max(WeatherForecast.issue_time)))
        ),
    }

    registry = list_stations()
    stations_with_data = [
        s.station_id
        for s in registry
        if await session.scalar(
            select(func.count()).where(WeatherObservation.station_id == s.station_id)
        )
    ]
    station_coverage = {
        "registry": len(registry),
        "with_observations": len(stations_with_data),
        "stations": stations_with_data,
    }

    specs = await ParserSettlementResolver().resolve_specs(session)
    resolved = sum(1 for s in specs if s.status is SettlementStatus.RESOLVED)
    settlement_coverage = {
        "markets": len(specs),
        "resolved": resolved,
        "unresolved_backlog": len(specs) - resolved,
        "confidence": {
            level: sum(1 for s in specs if s.confidence.value == level)
            for level in ("high", "medium", "none")
        },
    }

    # Dataset completeness: recent-window observation day coverage per station
    # (the joinable-days measure the research datasets ultimately inherit).
    completeness: dict[str, Any] = {}
    for s in registry:
        days = await session.scalar(
            select(func.count(func.distinct(WeatherObservation.observation_date))).where(
                WeatherObservation.station_id == s.station_id
            )
        )
        first = await session.scalar(
            select(func.min(WeatherObservation.observation_date)).where(
                WeatherObservation.station_id == s.station_id
            )
        )
        last = await session.scalar(
            select(func.max(WeatherObservation.observation_date)).where(
                WeatherObservation.station_id == s.station_id
            )
        )
        expected = (last - first).days + 1 if first is not None and last is not None else 0
        completeness[s.station_id] = {
            "observation_days": int(days or 0),
            "range": f"{first} .. {last}" if first is not None else None,
            "coverage": round(int(days or 0) / expected, 4) if expected else None,
        }

    quality_report = quality if quality is not None else await run_quality_checks(session)

    return HealthReport(
        generated_at=utc_now().isoformat(),
        collectors=collectors,
        newest_data=newest_data,
        database_size_bytes=await _database_size_bytes(session),
        station_coverage=station_coverage,
        settlement_coverage=settlement_coverage,
        dataset_completeness=completeness,
        quality_ok=quality_report.ok,
        quality_counts=quality_report.counts(),
    )
