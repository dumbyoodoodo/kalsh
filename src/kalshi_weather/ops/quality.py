"""Automated data-quality checks over the collected store (Phase 6).

Complements `dataset/validation.py` (which validates a *built dataset*): these
checks run against the database itself, on a schedule or on demand, and are
designed to catch collection problems early -- gaps, anomalies, resolution
failures, and schema drift. Failures are reported, never silently ignored;
`ok` is false only for error-severity findings (corruption/drift), while
expected operational gaps surface as warnings with counts.
"""

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.domain.time import to_naive_utc, utc_now
from kalshi_weather.settlement.resolver import ParserSettlementResolver
from kalshi_weather.settlement.spec import SettlementStatus
from kalshi_weather.storage.models import (
    MarketSnapshot,
    SettlementSpecRecord,
    TradeRecord,
    WeatherForecast,
    WeatherObservation,
)
from kalshi_weather.weather.stations import list_stations

#: The Alembic revision this codebase expects the database to be at. Bump in
#: the same change that adds a migration -- `unexpected_schema_change` fires
#: on any mismatch, in either direction (DB behind code, or code behind DB).
EXPECTED_DB_REVISION = "0005"

#: Windows for gap checks. Observations: a settled day's final CLI report
#: arrives the next local morning, so "yesterday missing" is only a gap once
#: a full day has passed.
OBSERVATION_GAP_WINDOW_DAYS = 30
FORECAST_STALE_AFTER_HOURS = 48


@dataclass(frozen=True, slots=True)
class QualityFinding:
    check: str
    severity: str  # "error" | "warning" | "info"
    count: int
    message: str
    samples: list[Any] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class QualityReport:
    generated_at: str
    findings: list[QualityFinding]

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def counts(self) -> dict[str, int]:
        return {f.check: f.count for f in self.findings}

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "ok": self.ok,
            "findings": [asdict(f) for f in self.findings],
        }


async def _db_revision(session: AsyncSession) -> str | None:
    try:
        result = await session.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:
        return None
    return str(result) if result is not None else None


async def run_quality_checks(session: AsyncSession) -> QualityReport:
    findings: list[QualityFinding] = []
    now = utc_now().replace(tzinfo=None)

    # 1. Unexpected schema changes: DB revision vs what this code expects.
    revision = await _db_revision(session)
    if revision != EXPECTED_DB_REVISION:
        findings.append(
            QualityFinding(
                check="unexpected_schema_change",
                severity="error",
                count=1,
                message=(
                    f"database revision {revision!r} != expected {EXPECTED_DB_REVISION!r} "
                    "(run migrations, or update ops/quality.py alongside the new migration)"
                ),
            )
        )

    # 2. Missing weather observations: per registry station, days in the
    #    recent window (excluding the last day, whose final report may not
    #    have been issued yet) with no stored observation.
    window_end = (now - timedelta(days=1)).date()
    window_start = window_end - timedelta(days=OBSERVATION_GAP_WINDOW_DAYS - 1)
    expected_days = {
        window_start + timedelta(days=i)
        for i in range((window_end - window_start).days + 1)
    }
    for station in list_stations():
        rows = await session.scalars(
            select(WeatherObservation.observation_date)
            .where(
                WeatherObservation.station_id == station.station_id,
                WeatherObservation.observation_date >= window_start,
                WeatherObservation.observation_date <= window_end,
            )
            .distinct()
        )
        missing = sorted(expected_days - set(rows.all()))
        findings.append(
            QualityFinding(
                check=f"missing_observations_{station.station_id}",
                severity="warning" if missing else "info",
                count=len(missing),
                message=(
                    f"station {station.station_id}: {len(missing)} of "
                    f"{len(expected_days)} days in the last {OBSERVATION_GAP_WINDOW_DAYS} "
                    "days have no stored observation"
                ),
                samples=[d.isoformat() for d in missing[:5]],
            )
        )

    # 3. Missing forecasts: no issuance stored recently (only meaningful once
    #    any forecast has ever been collected -- before that it's info).
    latest_issue = await session.scalar(select(func.max(WeatherForecast.issue_time)))
    if latest_issue is None:
        findings.append(
            QualityFinding(
                check="missing_forecasts",
                severity="info",
                count=0,
                message="no forecasts collected yet",
            )
        )
    else:
        age_hours = (now - to_naive_utc(latest_issue)).total_seconds() / 3600
        stale = age_hours > FORECAST_STALE_AFTER_HOURS
        findings.append(
            QualityFinding(
                check="missing_forecasts",
                severity="warning" if stale else "info",
                count=1 if stale else 0,
                message=(
                    f"latest forecast issuance is {age_hours:.1f}h old "
                    f"(stale threshold {FORECAST_STALE_AFTER_HOURS}h)"
                ),
            )
        )

    # 4. Missing markets: markets seen historically but absent from recent
    #    snapshots are expected (markets settle); the meaningful gap is *no*
    #    market snapshot at all in a store that has market history.
    total_markets = await session.scalar(
        select(func.count(func.distinct(MarketSnapshot.market_ticker)))
    )
    latest_market_obs = await session.scalar(select(func.max(MarketSnapshot.observed_at)))
    if total_markets and latest_market_obs is not None:
        age_hours = (now - to_naive_utc(latest_market_obs)).total_seconds() / 3600
        stale = age_hours > 24
        findings.append(
            QualityFinding(
                check="missing_markets",
                severity="warning" if stale else "info",
                count=1 if stale else 0,
                message=f"latest market snapshot is {age_hours:.1f}h old (stale threshold 24h)",
            )
        )
    else:
        findings.append(
            QualityFinding(
                check="missing_markets",
                severity="info",
                count=0,
                message="no market snapshots collected yet",
            )
        )

    # 5. Duplicate records: natural-key duplicates the unique indexes should
    #    make impossible -- any hit is corruption, hence error severity.
    dup_obs = await session.scalar(
        select(func.count()).select_from(
            select(WeatherObservation.station_id)
            .group_by(
                WeatherObservation.station_id,
                WeatherObservation.variable,
                WeatherObservation.issuance_time,
            )
            .having(func.count() > 1)
            .subquery()
        )
    )
    dup_specs = await session.scalar(
        select(func.count()).select_from(
            select(SettlementSpecRecord.market_ticker)
            .group_by(
                SettlementSpecRecord.market_ticker,
                SettlementSpecRecord.parser_version,
                SettlementSpecRecord.rules_hash,
            )
            .having(func.count() > 1)
            .subquery()
        )
    )
    duplicates = int(dup_obs or 0) + int(dup_specs or 0)
    findings.append(
        QualityFinding(
            check="duplicate_records",
            severity="error" if duplicates else "info",
            count=duplicates,
            message="natural-key duplicate groups (should be impossible; corruption if >0)",
        )
    )

    # 6. Timestamp anomalies: future-dated source rows (beyond clock skew).
    skew_limit = now + timedelta(minutes=10)
    future_obs = await session.scalar(
        select(func.count()).where(WeatherObservation.issuance_time > skew_limit)
    )
    future_trades = await session.scalar(
        select(func.count()).where(TradeRecord.executed_at > skew_limit)
    )
    future_markets = await session.scalar(
        select(func.count()).where(MarketSnapshot.observed_at > skew_limit)
    )
    anomalies = int(future_obs or 0) + int(future_trades or 0) + int(future_markets or 0)
    findings.append(
        QualityFinding(
            check="timestamp_anomalies",
            severity="error" if anomalies else "info",
            count=anomalies,
            message="rows timestamped in the future beyond clock-skew tolerance",
        )
    )

    # 7. Settlement resolution failures: parse every market currently in the
    #    store and count non-resolved outcomes (the live backlog, not just
    #    what happens to be persisted in settlement_specs).
    specs = await ParserSettlementResolver().resolve_specs(session)
    unresolved = [s for s in specs if s.status is not SettlementStatus.RESOLVED]
    findings.append(
        QualityFinding(
            check="settlement_resolution_failures",
            severity="warning" if unresolved else "info",
            count=len(unresolved),
            message=(
                f"{len(unresolved)} of {len(specs)} markets do not resolve "
                "(see `settlement report` for reasons)"
            ),
            samples=[s.market_ticker for s in unresolved[:5]],
        )
    )

    return QualityReport(generated_at=utc_now().isoformat(), findings=findings)
