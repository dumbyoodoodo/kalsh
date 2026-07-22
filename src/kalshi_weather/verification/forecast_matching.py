"""Forecast-side matching: point-in-time selection, lead-time computation,
issuance tracking, and temporal validation.

Operates on already-derived per-issuance forecast values (e.g. one row per
(station, variable, target_date, issue_time) carrying a single point
estimate and its `valid_start`) -- it does not re-derive a daily high/low
from raw NWS forecast periods; that aggregation is a distinct, existing
concern (`dataset.builder._forecast_issue_high_low`, ADR 0004) upstream of
this module. Keeping that boundary makes this module provider-agnostic:
any forecast source reducible to (issue_time, valid_start, value) can use
it, not only NWS period forecasts.

This module computes no scientific conclusion. It only selects, computes
lead time, and validates.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    import polars as pl

DECIMAL_CONTEXT_PRECISION = 50


class ForecastExclusionReason(StrEnum):
    """Why a forecast candidate was excluded before it ever reached
    observation matching."""

    NULL_VALUE = "null_value"
    MISSING_VALID_START = "missing_valid_start"
    HORIZON_OUT_OF_RANGE = "horizon_out_of_range"


@dataclass(frozen=True, slots=True)
class ForecastIssuance:
    """One forecast issuance's prediction for one day's high or low."""

    station_id: str
    variable: str
    target_date: date
    issue_time: datetime
    valid_start: datetime | None
    value: Decimal | None
    valid_end: datetime | None = None
    raw_payload_id: int | None = None


@dataclass(frozen=True, slots=True)
class HorizonBucket:
    name: str
    lower_hours: Decimal
    upper_hours: Decimal  # exclusive; Decimal("Infinity") for open-ended


#: The bucket scheme H0003 pre-registered for its own (closed) hypothesis.
#: Provided as a convenient default only -- callers with a different
#: pre-registered scheme must pass their own `buckets` argument; this
#: library does not own any hypothesis's frozen thresholds.
DEFAULT_HORIZON_BUCKETS: tuple[HorizonBucket, ...] = (
    HorizonBucket("0-12h", Decimal(0), Decimal(12)),
    HorizonBucket("12-24h", Decimal(12), Decimal(24)),
    HorizonBucket("24-48h", Decimal(24), Decimal(48)),
    HorizonBucket("48h+", Decimal(48), Decimal("Infinity")),
)

#: Default collection-latency tolerance below zero lead time (matching the
#: H0003 precedent), and the default open-ended upper cap.
DEFAULT_SLACK_HOURS = Decimal(1)
DEFAULT_MAX_HORIZON_HOURS = Decimal(240)


@dataclass(frozen=True, slots=True)
class MatchedForecastCandidate:
    """A forecast after horizon computation and bucket classification, but
    before observation matching. `excluded` is set (and `horizon_hours`/
    `bucket` are `None`) for a candidate that never reaches observation
    matching."""

    forecast: ForecastIssuance
    horizon_hours: Decimal | None
    bucket: str | None
    excluded: ForecastExclusionReason | None


@dataclass(frozen=True, slots=True)
class TemporalValidationIssue:
    forecast: ForecastIssuance
    problem: str


def compute_horizon_hours(issue_time: datetime, valid_start: datetime) -> Decimal:
    """Lead time in hours: `valid_start - issue_time`. Both are metadata
    carried on the forecast product itself, known at issuance -- this never
    references `target_date` or any observation-side timestamp, so it is
    leak-free by construction (the standard NWS-verification lead-time
    convention; a naive "target-day-midnight" reference instead produces
    negative horizons for ordinary same-day forecasts)."""
    delta = valid_start - issue_time
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        return Decimal(delta.days) * 24 + Decimal(delta.seconds) / Decimal(3600)


def classify_horizon(
    horizon_hours: Decimal,
    buckets: Sequence[HorizonBucket] = DEFAULT_HORIZON_BUCKETS,
    *,
    slack_hours: Decimal = DEFAULT_SLACK_HOURS,
    max_horizon_hours: Decimal = DEFAULT_MAX_HORIZON_HOURS,
) -> str | None:
    """`None` means the horizon falls outside every bucket's coverage --
    caller should treat this as `HORIZON_OUT_OF_RANGE`. A horizon within
    `(-slack_hours, 0)` is classified into the lowest bucket (near-zero
    lead time, tolerating collection-latency artifacts) rather than
    reported as out of range."""
    if not (-slack_hours < horizon_hours <= max_horizon_hours):
        return None
    effective = max(horizon_hours, Decimal(0))
    for bucket in buckets:
        if bucket.lower_hours <= effective < bucket.upper_hours:
            return bucket.name
    return None


def find_duplicate_keys(
    forecasts: Sequence[ForecastIssuance],
) -> dict[tuple[str, str, date, datetime], int]:
    """(station_id, variable, target_date, issue_time) keys appearing more
    than once -- a genuine data-integrity anomaly (this natural key should
    be unique), distinct from the ordinary case of multiple *different*
    issue_times covering one target_date."""
    counts: dict[tuple[str, str, date, datetime], int] = defaultdict(int)
    for f in forecasts:
        counts[(f.station_id, f.variable, f.target_date, f.issue_time)] += 1
    return {k: v for k, v in counts.items() if v > 1}


def validate_temporal_ordering(
    forecasts: Sequence[ForecastIssuance],
) -> list[TemporalValidationIssue]:
    """Flags structurally invalid periods (`valid_end <= valid_start`).
    A `valid_start` before `issue_time` is *not* flagged here -- an ordinary
    same-day forecast describing an already-started period is normal and is
    instead handled by `classify_horizon`'s slack tolerance."""
    issues = []
    for f in forecasts:
        if f.valid_start is not None and f.valid_end is not None and f.valid_end <= f.valid_start:
            issues.append(TemporalValidationIssue(f, "valid_end <= valid_start"))
    return issues


def select_forecasts(
    forecasts: Sequence[ForecastIssuance],
    *,
    buckets: Sequence[HorizonBucket] = DEFAULT_HORIZON_BUCKETS,
    slack_hours: Decimal = DEFAULT_SLACK_HOURS,
    max_horizon_hours: Decimal = DEFAULT_MAX_HORIZON_HOURS,
    selection: Literal["latest", "earliest"] = "latest",
) -> tuple[list[MatchedForecastCandidate], list[MatchedForecastCandidate]]:
    """Classify every forecast by horizon bucket, then select one issuance
    per (station, variable, target_date, bucket) cell -- `selection`
    controls which (default: latest, the freshest information available at
    that horizon depth; avoids pseudo-replication from near-duplicate
    same-bucket reissues). Returns (selected, excluded); excluded entries
    never reach observation matching. Deterministic: exact-duplicate keys
    are broken by `(issue_time, value)` ordering, never by insertion order.
    """
    excluded: list[MatchedForecastCandidate] = []
    cells: dict[tuple[str, str, date, str], list[MatchedForecastCandidate]] = defaultdict(list)

    for f in forecasts:
        if f.value is None:
            excluded.append(
                MatchedForecastCandidate(f, None, None, ForecastExclusionReason.NULL_VALUE)
            )
            continue
        if f.valid_start is None:
            excluded.append(
                MatchedForecastCandidate(f, None, None, ForecastExclusionReason.MISSING_VALID_START)
            )
            continue
        horizon = compute_horizon_hours(f.issue_time, f.valid_start)
        bucket = classify_horizon(
            horizon, buckets, slack_hours=slack_hours, max_horizon_hours=max_horizon_hours
        )
        if bucket is None:
            excluded.append(
                MatchedForecastCandidate(
                    f, horizon, None, ForecastExclusionReason.HORIZON_OUT_OF_RANGE
                )
            )
            continue
        candidate = MatchedForecastCandidate(f, horizon, bucket, None)
        cells[(f.station_id, f.variable, f.target_date, bucket)].append(candidate)

    selected: list[MatchedForecastCandidate] = []
    for key in sorted(cells):
        group = cells[key]
        group.sort(
            key=lambda c: (c.forecast.issue_time, c.forecast.value),
            reverse=(selection == "latest"),
        )
        selected.append(group[0])

    selected.sort(
        key=lambda c: (
            c.forecast.station_id,
            c.forecast.variable,
            c.forecast.target_date,
            c.forecast.issue_time,
        )
    )
    return selected, excluded


def forecasts_from_frame(forecasts: pl.DataFrame) -> list[ForecastIssuance]:
    """Adapter: build `ForecastIssuance` records from a polars DataFrame
    with columns (station_id, variable, target_date, issue_time,
    valid_start, forecast_value) and optionally (valid_end,
    raw_payload_id) -- the shape produced by
    `scripts/build_h0003_forecast_extract.py`."""
    columns = set(forecasts.columns)
    out = []
    for row in forecasts.iter_rows(named=True):
        value = row["forecast_value"]
        out.append(
            ForecastIssuance(
                station_id=row["station_id"],
                variable=row["variable"],
                target_date=row["target_date"],
                issue_time=row["issue_time"],
                valid_start=row.get("valid_start") or row.get("earliest_valid_start"),
                value=Decimal(str(value)) if value is not None else None,
                valid_end=row["valid_end"] if "valid_end" in columns else None,
                raw_payload_id=row["raw_payload_id"] if "raw_payload_id" in columns else None,
            )
        )
    return out
