"""The data quality observatory: one deterministic report assembling every
check -- new and adapted -- into a single, severity-classified view of
whether incoming data remain scientifically usable.

Reused, not reimplemented: `ops/quality.py` (schema drift, observation/
forecast/market staleness, natural-key duplicates on observations/
settlement specs, future-dated rows, settlement resolution), `ops/
forecast_cadence.py` (forecast issuance cadence, missing/duplicate/
out-of-order issuances), `ops/health.py` (collector liveness, dataset
completeness) -- each folded into this module's unified `Finding` type via
the adapters in `drift.py`/`pit_consistency.py`.

New here: archive continuity and missed collection cycles across all five
streams (`continuity.py`); duplicate detection extended to markets/
candles/forecasts and timestamp monotonicity (`integrity.py`); unexpected
station drift (`drift.py`); weather-parser failure trends
(`parsing.py`); point-in-time integrity and forecast eligibility rate,
powered by `kalshi_weather.verification` (`pit_consistency.py`).

**No new persisted table.** "Historical trends" (a deliverable this task
names) come from windows over data the platform already stores
append-only (`collector_runs`, and every monitored table itself) -- not
from a new `observatory_runs` table recording each report. That would be
infrastructure the observatory does not yet need: every check here already
looks back over a historical window, which is exactly what a trend is.

This module computes no research statistic and performs no statistical
inference. It exists solely to certify dataset quality for the research
program that depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from kalshi_weather.domain.time import utc_now
from kalshi_weather.observatory import continuity, drift, integrity, parsing, pit_consistency
from kalshi_weather.observatory.severity import Finding, Severity, overall_severity
from kalshi_weather.ops.forecast_cadence import CadenceConfig, run_forecast_cadence
from kalshi_weather.ops.health import build_health_report
from kalshi_weather.ops.quality import run_quality_checks

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: How far back "recent" means for the continuity/integrity/parsing checks
#: in this report. Independent of `pit_consistency`'s own window (which is
#: tuned separately -- see that module).
DEFAULT_CONTINUITY_WINDOW_DAYS = 14
DEFAULT_INTEGRITY_ROW_LIMIT = 5000


@dataclass(frozen=True, slots=True)
class ObservatoryConfig:
    kalshi_interval_seconds: float
    weather_interval_seconds: float
    price_sync_interval_seconds: float
    stale_after_intervals: float
    continuity_window_days: int = DEFAULT_CONTINUITY_WINDOW_DAYS
    pit_window_days: int = pit_consistency.DEFAULT_WINDOW_DAYS


@dataclass(frozen=True, slots=True)
class ObservatoryReport:
    generated_at: str
    findings: tuple[Finding, ...]

    @property
    def status(self) -> Severity:
        return overall_severity(list(self.findings))

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    def by_domain(self, domain: str) -> list[Finding]:
        return [f for f in self.findings if f.domain == domain]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "status": self.status.value,
            "counts_by_severity": {sev.value: len(self.by_severity(sev)) for sev in Severity},
            "findings": [f.to_dict() for f in self.findings],
        }


async def build_observatory_report(
    session: AsyncSession, config: ObservatoryConfig
) -> ObservatoryReport:
    findings: list[Finding] = []
    now = utc_now().replace(tzinfo=None)

    # --- Reused: schema drift, staleness, duplicates (obs/specs), future
    # timestamps, settlement resolution -- ops/quality.py, unmodified.
    quality = await run_quality_checks(session)
    _QUALITY_DOMAIN_OVERRIDES = {
        "missing_forecasts": "forecast",
        "missing_markets": "market",
        "unexpected_schema_change": "platform",
        "duplicate_records": "platform",
        "timestamp_anomalies": "platform",
        "settlement_resolution_failures": "platform",
    }
    for qf in quality.findings:
        domain = next(
            (v for k, v in _QUALITY_DOMAIN_OVERRIDES.items() if qf.check.startswith(k)),
            "observation",  # the remaining quality.py checks are missing_observations_*
        )
        findings.append(drift.adapt_quality_finding(qf, domain=domain))

    # --- Reused: forecast issuance cadence (missing/duplicate/out-of-order/
    # delayed/abnormal cadence, collector outage) -- ops/forecast_cadence.py.
    cadence = await run_forecast_cadence(session, config=CadenceConfig())
    for alert in cadence.alerts:
        findings.append(drift.adapt_cadence_alert(alert))

    # --- Reused: collector liveness + dataset completeness -- ops/health.py
    # (which itself reuses run_quality_checks -- pass ours through so it is
    # computed exactly once per report).
    health = await build_health_report(
        session,
        kalshi_interval_seconds=config.kalshi_interval_seconds,
        weather_interval_seconds=config.weather_interval_seconds,
        stale_after_intervals=config.stale_after_intervals,
        price_sync_interval_seconds=config.price_sync_interval_seconds,
        quality=quality,
    )
    findings.extend(pit_consistency.adapt_completeness(health.dataset_completeness))

    # --- New: archive continuity + missed collection cycles, all 5 streams.
    continuity_since = now - timedelta(days=config.continuity_window_days)
    stream_domain = {
        "forecast": "forecast",
        "observation": "observation",
        "market": "market",
        "trade": "trade",
        "candle": "candle",
    }
    for stream, domain in stream_domain.items():
        timestamps = await continuity.load_stream_timestamps(
            session, stream, since=continuity_since
        )
        gaps = continuity.find_continuity_gaps(
            timestamps,
            stream=stream,
            max_gap_hours=continuity.DEFAULT_MAX_GAP_HOURS[stream],
            now=now,
        )
        findings.append(
            Finding(
                domain=domain,
                check=f"archive_continuity_{stream}",
                severity=Severity.WARNING if gaps else Severity.INFO,
                count=len(gaps),
                message=(
                    f"{len(gaps)} gap(s) exceeding "
                    f"{continuity.DEFAULT_MAX_GAP_HOURS[stream]}h in the last "
                    f"{config.continuity_window_days} days"
                    if timestamps
                    else "no data in the window"
                ),
                samples=tuple(f"{g.gap_start} .. {g.gap_end}" for g in gaps[:5]),
            )
        )

    run_records = await continuity.load_run_records(session, since=continuity_since)
    collector_intervals = {
        "kalshi": config.kalshi_interval_seconds,
        "weather": config.weather_interval_seconds,
        "prices": config.price_sync_interval_seconds,
    }
    for collector, interval in collector_intervals.items():
        missed = continuity.find_missed_run_cycles(
            run_records, collector=collector, interval_seconds=interval, now=now
        )
        findings.append(
            Finding(
                domain="platform",
                check=f"missed_collection_cycles_{collector}",
                severity=Severity.CRITICAL if missed else Severity.INFO,
                count=len(missed),
                message=(
                    f"{len(missed)} window(s) with no {collector} run recorded well "
                    "beyond its configured interval"
                ),
                samples=tuple(f"{m.gap_start} .. {m.gap_end}" for m in missed[:5]),
            )
        )

    # --- New: duplicate records + timestamp monotonicity for the streams
    # ops/quality.py does not already cover.
    dup_market = await integrity.count_duplicate_market_snapshots(session)
    dup_candle = await integrity.count_duplicate_candlesticks(session)
    dup_forecast = await integrity.count_duplicate_forecasts(session)
    for domain, check, count in (
        ("market", "duplicate_market_snapshots", dup_market),
        ("candle", "duplicate_candlesticks", dup_candle),
        ("forecast", "duplicate_forecasts", dup_forecast),
    ):
        findings.append(
            Finding(
                domain=domain,
                check=check,
                severity=Severity.CRITICAL if count else Severity.INFO,
                count=count,
                message="natural-key duplicate groups (should be impossible; corruption if >0)",
            )
        )

    for stream, domain in (("market", "market"), ("candle", "candle"), ("forecast", "forecast")):
        pairs = await integrity.load_id_timestamp_pairs(
            session, stream, limit=DEFAULT_INTEGRITY_ROW_LIMIT
        )
        violations = integrity.find_monotonicity_violations(pairs, stream=stream)
        findings.append(
            Finding(
                domain=domain,
                check=f"timestamp_monotonicity_{stream}",
                severity=Severity.WARNING if violations else Severity.INFO,
                count=len(violations),
                message=(
                    f"{len(violations)} row(s) inserted with a timestamp regressing "
                    f"beyond tolerance, of {len(pairs)} sampled"
                ),
            )
        )

    # --- New: unexpected station drift.
    registry_ids = drift.registry_station_ids()
    obs_orphans = drift.find_orphan_stations(
        registry_ids, await drift.load_observation_station_ids(session)
    )
    fc_orphans = drift.find_orphan_stations(
        registry_ids, await drift.load_forecast_station_ids(session)
    )
    findings.append(
        drift.orphan_station_finding(obs_orphans, domain="observation", source="observations")
    )
    findings.append(drift.orphan_station_finding(fc_orphans, domain="forecast", source="forecasts"))

    # --- New: weather-parser failure trend.
    parser_cycles = await parsing.load_weather_parser_cycles(session, since=continuity_since)
    findings.append(parsing.summarize_weather_parser_failures(parser_cycles))

    # --- New: point-in-time integrity + forecast eligibility rate, powered
    # by kalshi_weather.verification.
    pit_forecasts, pit_observations = await pit_consistency.load_pit_frames(
        session, window_days=config.pit_window_days
    )
    findings.append(pit_consistency.summarize_forecast_eligibility(pit_observations))
    findings.append(
        pit_consistency.summarize_point_in_time_integrity(pit_forecasts, pit_observations)
    )

    return ObservatoryReport(generated_at=utc_now().isoformat(), findings=tuple(findings))
