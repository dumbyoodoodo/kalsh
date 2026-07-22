"""Observation eligibility: has a target date received a genuinely final
report, or only a same-day provisional reading?

This module exists because of a specific, real defect it closes: H0003's
first execution (2026-07-22) blocked at its point-in-time integrity gate
because the platform's pre-existing "settled = latest issuance so far"
convention (`dataset.builder._settled_observations`) is sound only once a
target date has actually concluded. For a day still in progress, "latest
issuance so far" is a same-day provisional reading, not a final truth, and
using it to score a forecast is a leakage risk masquerading as a join.

The fix: an observation is eligible for verification use only once its
latest stored issuance was recorded at or after local midnight of the day
following the target date -- the same boundary convention H0011/H0013/
H0015 each independently froze for their own (closed) hypotheses. This
module is the canonical, reusable form of that convention; those
experiment scripts remain unmodified (each is a frozen artifact of an
already-executed, closed study).

This module computes no scientific conclusion. It only classifies.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from kalshi_weather.weather.stations import UnknownStationError, get_station

if TYPE_CHECKING:
    from collections.abc import Sequence

    import polars as pl


class ObservationExclusionReason(StrEnum):
    """Why a (station, variable, observation_date) is not eligible."""

    NO_OBSERVATION = "no_observation"
    NOT_YET_FINAL = "not_yet_final"
    UNKNOWN_STATION = "unknown_station"


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """One stored CLI issuance, as loaded from `weather_observations`."""

    station_id: str
    variable: str
    observation_date: date
    issuance_time: datetime  # naive, interpreted as UTC (platform convention)
    value: Decimal


@dataclass(frozen=True, slots=True)
class ObservationEligibility:
    """The deterministic eligibility decision for one (station, variable,
    observation_date), given every stored issuance for it.

    `final_value`/`final_issuance_time` are populated **only** when
    `eligible` is `True` -- structurally, a caller cannot obtain a value
    from this type without it having passed the finality check. When
    issuances exist but the day is not yet eligible, `latest_seen_value`/
    `latest_seen_issuance_time` expose the same-day provisional reading for
    diagnostic/reporting use only; they must never be used as ground
    truth (that is exactly the mistake this module exists to prevent).
    """

    station_id: str
    variable: str
    observation_date: date
    eligible: bool
    reason: ObservationExclusionReason | None
    final_value: Decimal | None
    final_issuance_time: datetime | None
    n_issuances: int
    latest_seen_value: Decimal | None = None
    latest_seen_issuance_time: datetime | None = None


def local_midnight_boundary(observation_date: date, timezone: str) -> datetime:
    """00:00:00 local time on `observation_date + 1 day`, as an aware
    datetime in `timezone`. An observation is genuinely final only once an
    issuance has been recorded at or after this instant."""
    next_day = observation_date + timedelta(days=1)
    return datetime(next_day.year, next_day.month, next_day.day, tzinfo=ZoneInfo(timezone))


def _to_local(naive_utc: datetime, timezone: str) -> datetime:
    return naive_utc.replace(tzinfo=UTC).astimezone(ZoneInfo(timezone))


def check_eligibility(
    station_id: str,
    variable: str,
    observation_date: date,
    issuances: Sequence[ObservationRecord],
) -> ObservationEligibility:
    """Deterministic eligibility decision for one (station, variable,
    observation_date), given every stored issuance for it (already filtered
    to that key by the caller)."""
    if not issuances:
        return ObservationEligibility(
            station_id=station_id,
            variable=variable,
            observation_date=observation_date,
            eligible=False,
            reason=ObservationExclusionReason.NO_OBSERVATION,
            final_value=None,
            final_issuance_time=None,
            n_issuances=0,
        )

    latest = max(issuances, key=lambda i: i.issuance_time)

    try:
        station = get_station(station_id)
    except UnknownStationError:
        return ObservationEligibility(
            station_id=station_id,
            variable=variable,
            observation_date=observation_date,
            eligible=False,
            reason=ObservationExclusionReason.UNKNOWN_STATION,
            final_value=None,
            final_issuance_time=None,
            n_issuances=len(issuances),
            latest_seen_value=latest.value,
            latest_seen_issuance_time=latest.issuance_time,
        )

    boundary = local_midnight_boundary(observation_date, station.timezone)
    if _to_local(latest.issuance_time, station.timezone) >= boundary:
        return ObservationEligibility(
            station_id=station_id,
            variable=variable,
            observation_date=observation_date,
            eligible=True,
            reason=None,
            final_value=latest.value,
            final_issuance_time=latest.issuance_time,
            n_issuances=len(issuances),
            latest_seen_value=latest.value,
            latest_seen_issuance_time=latest.issuance_time,
        )

    return ObservationEligibility(
        station_id=station_id,
        variable=variable,
        observation_date=observation_date,
        eligible=False,
        reason=ObservationExclusionReason.NOT_YET_FINAL,
        final_value=None,
        final_issuance_time=None,
        n_issuances=len(issuances),
        latest_seen_value=latest.value,
        latest_seen_issuance_time=latest.issuance_time,
    )


def build_eligibility_table(
    records: Sequence[ObservationRecord],
) -> list[ObservationEligibility]:
    """Group `records` by (station_id, variable, observation_date) and
    return one `ObservationEligibility` per group, sorted deterministically."""
    groups: dict[tuple[str, str, date], list[ObservationRecord]] = defaultdict(list)
    for r in records:
        groups[(r.station_id, r.variable, r.observation_date)].append(r)
    return [
        check_eligibility(station_id, variable, observation_date, issuances)
        for (station_id, variable, observation_date), issuances in sorted(groups.items())
    ]


def eligible_lookup(
    table: Sequence[ObservationEligibility],
) -> dict[tuple[str, str, date], ObservationEligibility]:
    """(station_id, variable, observation_date) -> ObservationEligibility,
    for O(1) downstream joins (observation_matching.py)."""
    return {(e.station_id, e.variable, e.observation_date): e for e in table}


def eligibility_table_from_frame(observations: pl.DataFrame) -> list[ObservationEligibility]:
    """Adapter: build the eligibility table from a polars DataFrame with
    columns (station_id, variable, observation_date, issuance_time, value)
    -- the platform's existing `weather_observations` / dataset-builder
    `OBSERVATIONS_SCHEMA` shape. Values are constructed via `Decimal(str(v))`
    (never `Decimal(v)` directly) to avoid importing a float's binary-exact
    expansion."""
    records = [
        ObservationRecord(
            station_id=row["station_id"],
            variable=row["variable"],
            observation_date=row["observation_date"],
            issuance_time=row["issuance_time"],
            value=Decimal(str(row["value"])),
        )
        for row in observations.iter_rows(named=True)
    ]
    return build_eligibility_table(records)
