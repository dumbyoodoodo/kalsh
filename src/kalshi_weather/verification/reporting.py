"""Deterministic reporting: eligible/excluded forecast tables, exclusion
reason summaries, and coverage reports.

Every function here returns plain, JSON-serializable structures (dicts and
lists of dicts; `Decimal`/`date`/`datetime` values are rendered as strings
via `str()`, matching the program-wide manifest-serialization pattern) so a
caller can drop the result directly into an experiment's own results
manifest. Rows and dict keys are always produced in a fixed, sorted order
-- two calls over the same inputs are byte-identical after
`json.dumps(..., sort_keys=True)`.

This module computes no scientific conclusion. It only tabulates counts
and values already computed elsewhere.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Any

from kalshi_weather.verification.forecast_matching import DEFAULT_HORIZON_BUCKETS

DECIMAL_CONTEXT_PRECISION = 50

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kalshi_weather.verification.forecast_matching import (
        HorizonBucket,
        MatchedForecastCandidate,
    )
    from kalshi_weather.verification.observation_matching import (
        MatchedObservation,
        UnmatchedForecast,
    )


def _sort_key(
    station_id: str, variable: str, target_date: object, issue_time: object
) -> tuple[str, str, str, str]:
    return (station_id, variable, str(target_date), str(issue_time))


def eligible_forecasts_table(matched: Sequence[MatchedObservation]) -> list[dict[str, Any]]:
    """One row per matched forecast, sorted by (station, variable,
    target_date, issue_time)."""
    rows = [
        {
            "station_id": m.forecast.station_id,
            "variable": m.forecast.variable,
            "target_date": str(m.forecast.target_date),
            "issue_time": str(m.forecast.issue_time),
            "horizon_hours": str(m.horizon_hours),
            "bucket": m.bucket,
            "forecast_value": str(m.forecast.value),
            "observed_value": str(m.observed_value),
            "observed_finalized_at": str(m.observed_finalized_at),
            "error": str(m.error),
        }
        for m in matched
    ]
    rows.sort(
        key=lambda r: _sort_key(r["station_id"], r["variable"], r["target_date"], r["issue_time"])
    )
    return rows


def excluded_forecasts_table(
    forecast_side_excluded: Sequence[MatchedForecastCandidate],
    observation_side_unmatched: Sequence[UnmatchedForecast],
) -> list[dict[str, Any]]:
    """One row per excluded forecast, from either exclusion source, sorted
    by (station, variable, target_date, issue_time)."""
    rows: list[dict[str, Any]] = []
    for c in forecast_side_excluded:
        assert c.excluded is not None
        rows.append(
            {
                "station_id": c.forecast.station_id,
                "variable": c.forecast.variable,
                "target_date": str(c.forecast.target_date),
                "issue_time": str(c.forecast.issue_time),
                "reason": c.excluded.value,
                "horizon_hours": str(c.horizon_hours) if c.horizon_hours is not None else None,
            }
        )
    for u in observation_side_unmatched:
        rows.append(
            {
                "station_id": u.forecast.station_id,
                "variable": u.forecast.variable,
                "target_date": str(u.forecast.target_date),
                "issue_time": str(u.forecast.issue_time),
                "reason": u.reason,
                "horizon_hours": None,
            }
        )
    rows.sort(
        key=lambda r: _sort_key(r["station_id"], r["variable"], r["target_date"], r["issue_time"])
    )
    return rows


def exclusion_reason_summary(
    forecast_side_excluded: Sequence[MatchedForecastCandidate],
    observation_side_unmatched: Sequence[UnmatchedForecast],
) -> dict[str, int]:
    """Count of excluded forecasts per reason, sorted by reason string."""
    counts: dict[str, int] = {}
    for c in forecast_side_excluded:
        assert c.excluded is not None
        counts[c.excluded.value] = counts.get(c.excluded.value, 0) + 1
    for u in observation_side_unmatched:
        counts[u.reason] = counts.get(u.reason, 0) + 1
    return dict(sorted(counts.items()))


def forecast_coverage_report(
    matched: Sequence[MatchedObservation],
    forecast_side_excluded: Sequence[MatchedForecastCandidate],
    observation_side_unmatched: Sequence[UnmatchedForecast],
) -> dict[str, Any]:
    """Overall and per-(station, variable) coverage: how many candidate
    forecasts ended up eligible vs excluded, and why."""
    total = len(matched) + len(forecast_side_excluded) + len(observation_side_unmatched)
    per_cell: dict[tuple[str, str], dict[str, int]] = {}

    def cell(station_id: str, variable: str) -> dict[str, int]:
        return per_cell.setdefault((station_id, variable), {"eligible": 0, "excluded": 0})

    for m in matched:
        cell(m.forecast.station_id, m.forecast.variable)["eligible"] += 1
    for c in forecast_side_excluded:
        cell(c.forecast.station_id, c.forecast.variable)["excluded"] += 1
    for u in observation_side_unmatched:
        cell(u.forecast.station_id, u.forecast.variable)["excluded"] += 1

    coverage_fraction: str | None = None
    if total:
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            coverage_fraction = str(Decimal(len(matched)) / Decimal(total))

    return {
        "total_candidates": total,
        "eligible": len(matched),
        "excluded": total - len(matched),
        "eligible_of_total": f"{len(matched)}/{total}",
        "coverage_fraction": coverage_fraction,
        "exclusion_reasons": exclusion_reason_summary(
            forecast_side_excluded, observation_side_unmatched
        ),
        "by_station_variable": {f"{s}|{v}": counts for (s, v), counts in sorted(per_cell.items())},
    }


def horizon_coverage_report(
    matched: Sequence[MatchedObservation],
    buckets: Sequence[HorizonBucket] = DEFAULT_HORIZON_BUCKETS,
) -> dict[str, Any]:
    """Per (station, variable, bucket) matched-forecast counts, in the
    given bucket order (default: the bucket scheme's own declared order)."""
    bucket_order = [b.name for b in buckets]
    per_cell: dict[tuple[str, str], dict[str, int]] = {}
    for m in matched:
        key = (m.forecast.station_id, m.forecast.variable)
        row = per_cell.setdefault(key, dict.fromkeys(bucket_order, 0))
        row[m.bucket] = row.get(m.bucket, 0) + 1
    return {
        "bucket_order": bucket_order,
        "by_station_variable": {f"{s}|{v}": counts for (s, v), counts in sorted(per_cell.items())},
    }
