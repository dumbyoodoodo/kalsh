"""Forecast verification framework: reusable, deterministic infrastructure
for joining point-in-time forecasts to finalized observations without
leakage.

This package computes no scientific conclusion, fits no model, and owns no
hypothesis's decision thresholds. It exists so that H0003 retries, M-01,
and H0006 share one correct, tested implementation of eligibility,
matching, error arithmetic, and reporting rather than each reimplementing
it inline (as the original H0003 experiment script did, before its
2026-07-22 execution surfaced the eligibility gap this package closes).

See `docs/runbooks/forecast_verification.md` for usage guidance and the
eligibility/leakage-prevention rules this package enforces.
"""

from kalshi_weather.verification import metrics, reporting
from kalshi_weather.verification.eligibility import (
    ObservationEligibility,
    ObservationExclusionReason,
    ObservationRecord,
    build_eligibility_table,
    check_eligibility,
    eligibility_table_from_frame,
    eligible_lookup,
    local_midnight_boundary,
)
from kalshi_weather.verification.forecast_matching import (
    DEFAULT_HORIZON_BUCKETS,
    DEFAULT_MAX_HORIZON_HOURS,
    DEFAULT_SLACK_HOURS,
    ForecastExclusionReason,
    ForecastIssuance,
    HorizonBucket,
    MatchedForecastCandidate,
    TemporalValidationIssue,
    classify_horizon,
    compute_horizon_hours,
    find_duplicate_keys,
    forecasts_from_frame,
    select_forecasts,
    validate_temporal_ordering,
)
from kalshi_weather.verification.observation_matching import (
    MatchedObservation,
    UnmatchedForecast,
    build_observation_lookup,
    match_forecasts_to_observations,
)

__all__ = [
    "DEFAULT_HORIZON_BUCKETS",
    "DEFAULT_MAX_HORIZON_HOURS",
    "DEFAULT_SLACK_HOURS",
    "ForecastExclusionReason",
    "ForecastIssuance",
    "HorizonBucket",
    "MatchedForecastCandidate",
    "MatchedObservation",
    "ObservationEligibility",
    "ObservationExclusionReason",
    "ObservationRecord",
    "TemporalValidationIssue",
    "UnmatchedForecast",
    "build_eligibility_table",
    "build_observation_lookup",
    "check_eligibility",
    "classify_horizon",
    "compute_horizon_hours",
    "eligibility_table_from_frame",
    "eligible_lookup",
    "find_duplicate_keys",
    "forecasts_from_frame",
    "local_midnight_boundary",
    "match_forecasts_to_observations",
    "metrics",
    "reporting",
    "select_forecasts",
    "validate_temporal_ordering",
]
