"""Observation-side matching: final-observation lookup, the deterministic
forecast-to-observation join, and its point-in-time integrity check.

This is where H0003's G2c finding becomes a reusable primitive: joining a
forecast to an observation is only valid once the observation is eligible
(`eligibility.py`) *and* the forecast's own `issue_time` strictly precedes
the observation's finalizing issuance. A violation here is exactly the
scenario G2c caught (a forecast issued for the current, still-open day,
matched against a same-day provisional reading) -- this module flags it
per-row rather than silently joining; a hypothesis's own frozen gate
decides whether any violation blocks the whole run (that policy belongs
to each pre-registration, not to this library).

This module computes no scientific conclusion. It only joins and flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from kalshi_weather.verification.eligibility import (
    ObservationEligibility,
    ObservationExclusionReason,
)
from kalshi_weather.verification.forecast_matching import (
    ForecastIssuance,
    MatchedForecastCandidate,
)
from kalshi_weather.verification.metrics import signed_error

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Reasons a forecast candidate, having already survived forecast-side
#: selection, still failed to produce a matched observation.
UNMATCHED_NO_ELIGIBLE_OBSERVATION = "observation_not_eligible"
UNMATCHED_INTEGRITY_VIOLATION = "integrity_violation_issue_after_final"


@dataclass(frozen=True, slots=True)
class MatchedObservation:
    """A forecast successfully joined to its eligible, finalized truth."""

    forecast: ForecastIssuance
    horizon_hours: Decimal
    bucket: str
    observed_value: Decimal
    observed_finalized_at: datetime
    error: Decimal


@dataclass(frozen=True, slots=True)
class UnmatchedForecast:
    """A forecast candidate that survived forecast-side selection but could
    not be matched to a finalized observation."""

    forecast: ForecastIssuance
    reason: str  # UNMATCHED_* constant, optionally suffixed with detail


def build_observation_lookup(
    eligibility_table: Sequence[ObservationEligibility],
) -> dict[tuple[str, str, date], ObservationEligibility]:
    """(station_id, variable, observation_date) -> ObservationEligibility."""
    return {(e.station_id, e.variable, e.observation_date): e for e in eligibility_table}


def match_forecasts_to_observations(
    candidates: Sequence[MatchedForecastCandidate],
    lookup: Mapping[tuple[str, str, date], ObservationEligibility],
) -> tuple[list[MatchedObservation], list[UnmatchedForecast]]:
    """Join already-selected forecast candidates (from
    `forecast_matching.select_forecasts`'s first return value -- candidates
    with `excluded is None`) to finalized observations. Deterministic:
    iterates `candidates` in their given order and never reorders ties.
    """
    matched: list[MatchedObservation] = []
    unmatched: list[UnmatchedForecast] = []

    for candidate in candidates:
        if candidate.excluded is not None:
            continue  # already excluded upstream; not this module's concern
        f = candidate.forecast
        assert f.value is not None and candidate.horizon_hours is not None
        assert candidate.bucket is not None

        elig = lookup.get((f.station_id, f.variable, f.target_date))
        if elig is None:
            unmatched.append(
                UnmatchedForecast(
                    f,
                    f"{UNMATCHED_NO_ELIGIBLE_OBSERVATION}:{ObservationExclusionReason.NO_OBSERVATION.value}",
                )
            )
            continue
        if not elig.eligible:
            reason = elig.reason.value if elig.reason is not None else "unknown"
            unmatched.append(UnmatchedForecast(f, f"{UNMATCHED_NO_ELIGIBLE_OBSERVATION}:{reason}"))
            continue

        assert elig.final_value is not None and elig.final_issuance_time is not None
        if elig.final_issuance_time <= f.issue_time:
            unmatched.append(UnmatchedForecast(f, UNMATCHED_INTEGRITY_VIOLATION))
            continue

        matched.append(
            MatchedObservation(
                forecast=f,
                horizon_hours=candidate.horizon_hours,
                bucket=candidate.bucket,
                observed_value=elig.final_value,
                observed_finalized_at=elig.final_issuance_time,
                error=signed_error(f.value, elig.final_value),
            )
        )

    return matched, unmatched
