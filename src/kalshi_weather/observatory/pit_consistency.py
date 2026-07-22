"""Point-in-time consistency, forecast eligibility rates, and observation
completeness -- powered directly by `kalshi_weather.verification` (the
reusable framework closing the exact gap H0003's first execution surfaced,
2026-07-22).

This module is a thin observatory-facing wrapper: it loads recent
forecasts/observations, runs them through `verification`'s eligibility and
matching primitives, and reports the results as `Finding`s. It adds no new
eligibility or matching logic of its own -- that logic already exists,
is already tested, and must not be duplicated a second time.

This module computes no research statistic. A forecast eligibility rate
or an integrity-violation count is an operational data-quality signal, not
a scientific claim about forecast skill.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import polars as pl

from kalshi_weather.observatory.severity import Finding, Severity
from kalshi_weather.verification import (
    build_observation_lookup,
    eligibility_table_from_frame,
    forecasts_from_frame,
    match_forecasts_to_observations,
    select_forecasts,
)
from kalshi_weather.verification.eligibility import ObservationExclusionReason

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from kalshi_weather.dataset.builder import SourceFrames

#: How far back to load for this observatory check. Deliberately a small
#: recent window (not full history): the observatory must stay cheap
#: enough to run frequently; forecast eligibility over the last few weeks
#: is exactly the window in which a genuine regression would first appear.
DEFAULT_WINDOW_DAYS = 45


def summarize_forecast_eligibility(observations: pl.DataFrame) -> Finding:
    """Fraction of (station, variable, observation_date) triples in the
    window that are eligible (genuinely finalized) vs not yet final vs
    entirely absent -- the same classification `verification.eligibility`
    uses everywhere else, surfaced here as a trend signal, not a
    confirmatory statistic."""
    table = eligibility_table_from_frame(observations)
    if not table:
        return Finding(
            domain="observation",
            check="forecast_eligibility_rate",
            severity=Severity.INFO,
            count=0,
            message="no observation-date cells in the window",
        )
    eligible = sum(1 for e in table if e.eligible)
    not_yet_final = sum(1 for e in table if e.reason == ObservationExclusionReason.NOT_YET_FINAL)
    unknown_station = sum(
        1 for e in table if e.reason == ObservationExclusionReason.UNKNOWN_STATION
    )
    rate = eligible / len(table)
    severity = Severity.WARNING if unknown_station else Severity.INFO
    return Finding(
        domain="observation",
        check="forecast_eligibility_rate",
        severity=severity,
        count=len(table),
        message=(
            f"{eligible}/{len(table)} station/variable/date cells eligible "
            f"({rate:.1%}); {not_yet_final} not yet final; "
            f"{unknown_station} unknown-station"
        ),
    )


def summarize_point_in_time_integrity(
    forecasts: pl.DataFrame, observations: pl.DataFrame
) -> Finding:
    """Count of forecast rows whose `issue_time` does not strictly precede
    their observation's finalizing issuance -- the exact H0003 G2c
    scenario, generalized. Any occurrence is CRITICAL: it is a leakage
    risk masquerading as a join, not a benign data gap."""
    eligibility_table = eligibility_table_from_frame(observations)
    lookup = build_observation_lookup(eligibility_table)
    candidates, _forecast_excluded = select_forecasts(forecasts_from_frame(forecasts))
    _matched, unmatched = match_forecasts_to_observations(candidates, lookup)
    violations = [u for u in unmatched if u.reason == "integrity_violation_issue_after_final"]
    return Finding(
        domain="platform",
        check="point_in_time_integrity",
        severity=Severity.CRITICAL if violations else Severity.INFO,
        count=len(violations),
        message=(
            f"{len(violations)} forecast(s) issued after their own observation's finalizing report"
        ),
        samples=tuple(
            f"{v.forecast.station_id}/{v.forecast.variable}/{v.forecast.target_date}"
            for v in violations[:10]
        ),
    )


def adapt_completeness(completeness: dict[str, dict[str, Any]]) -> list[Finding]:
    """Fold `ops/health.py`'s per-station `dataset_completeness` dict into
    `Finding`s -- reused, not recomputed."""
    findings = []
    for station_id, info in sorted(completeness.items()):
        coverage: float | None = info.get("coverage")
        if coverage is None or coverage >= 0.98:
            severity = Severity.INFO
        elif coverage >= 0.90:
            severity = Severity.WARNING
        else:
            severity = Severity.CRITICAL
        findings.append(
            Finding(
                domain="observation",
                check=f"observation_completeness_{station_id}",
                severity=severity,
                count=int(info.get("observation_days") or 0),
                message=f"{station_id}: coverage={coverage}, range={info.get('range')}",
            )
        )
    return findings


# --- Async loader --------------------------------------------------------------


async def load_pit_frames(
    session: AsyncSession, *, window_days: int = DEFAULT_WINDOW_DAYS
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(forecasts, observations) frames for the recent window. Forecasts
    are station-local `target_date`-derived and high/low-aggregated via
    `dataset.builder.build_forecast_horizon_frame` -- reused, not
    reimplemented a third time (see that function's docstring for why a
    third implementation would otherwise have been tempting here)."""
    from kalshi_weather.dataset.builder import build_forecast_horizon_frame, load_source_frames
    from kalshi_weather.domain.time import utc_now

    start = (utc_now().replace(tzinfo=None) - timedelta(days=window_days)).date()
    sources: SourceFrames = await load_source_frames(session, start=start)
    forecasts = build_forecast_horizon_frame(sources.forecasts)
    return forecasts, sources.observations
