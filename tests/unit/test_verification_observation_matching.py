"""Tests for kalshi_weather.verification.observation_matching."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from kalshi_weather.verification.eligibility import (
    ObservationEligibility,
    ObservationExclusionReason,
)
from kalshi_weather.verification.forecast_matching import (
    ForecastExclusionReason,
    ForecastIssuance,
    MatchedForecastCandidate,
)
from kalshi_weather.verification.observation_matching import (
    UNMATCHED_INTEGRITY_VIOLATION,
    UNMATCHED_NO_ELIGIBLE_OBSERVATION,
    build_observation_lookup,
    match_forecasts_to_observations,
)


def _forecast(d: date, issue: datetime, value: str = "80.0") -> ForecastIssuance:
    return ForecastIssuance(
        station_id="NYC",
        variable="tmax_f",
        target_date=d,
        issue_time=issue,
        valid_start=None,
        value=Decimal(value),
    )


def _candidate(
    f: ForecastIssuance, horizon: str = "24", bucket: str = "12-24h"
) -> MatchedForecastCandidate:
    return MatchedForecastCandidate(f, Decimal(horizon), bucket, None)


def _eligible(d: date, value: str, final_time: datetime) -> ObservationEligibility:
    return ObservationEligibility(
        station_id="NYC",
        variable="tmax_f",
        observation_date=d,
        eligible=True,
        reason=None,
        final_value=Decimal(value),
        final_issuance_time=final_time,
        n_issuances=2,
    )


def _ineligible(d: date, reason: ObservationExclusionReason) -> ObservationEligibility:
    return ObservationEligibility(
        station_id="NYC",
        variable="tmax_f",
        observation_date=d,
        eligible=False,
        reason=reason,
        final_value=None,
        final_issuance_time=None,
        n_issuances=1,
    )


def test_build_observation_lookup() -> None:
    d = date(2026, 1, 1)
    elig = _eligible(d, "83.0", datetime(2026, 1, 2, 6, 0))
    lookup = build_observation_lookup([elig])
    assert lookup[("NYC", "tmax_f", d)] is elig


def test_matches_eligible_observation_and_computes_error() -> None:
    d = date(2026, 1, 1)
    issue = datetime(2025, 12, 30, 12, 0)
    f = _forecast(d, issue, value="80.0")
    candidate = _candidate(f)
    lookup = build_observation_lookup([_eligible(d, "83.0", datetime(2026, 1, 2, 6, 0))])

    matched, unmatched = match_forecasts_to_observations([candidate], lookup)
    assert unmatched == []
    assert len(matched) == 1
    assert matched[0].observed_value == Decimal("83.0")
    assert matched[0].error == Decimal("80.0") - Decimal("83.0")


def test_skips_candidates_already_excluded_upstream() -> None:
    d = date(2026, 1, 1)
    f = _forecast(d, datetime(2025, 12, 30, 12, 0))
    excluded_candidate = MatchedForecastCandidate(f, None, None, ForecastExclusionReason.NULL_VALUE)
    matched, unmatched = match_forecasts_to_observations([excluded_candidate], {})
    assert matched == []
    assert unmatched == []  # not this module's concern -- already handled


def test_no_observation_at_all_is_unmatched() -> None:
    d = date(2026, 1, 1)
    f = _forecast(d, datetime(2025, 12, 30, 12, 0))
    matched, unmatched = match_forecasts_to_observations([_candidate(f)], {})
    assert matched == []
    assert len(unmatched) == 1
    assert unmatched[0].reason == f"{UNMATCHED_NO_ELIGIBLE_OBSERVATION}:no_observation"


def test_not_yet_final_observation_is_unmatched() -> None:
    d = date(2026, 1, 1)
    f = _forecast(d, datetime(2025, 12, 30, 12, 0))
    lookup = build_observation_lookup([_ineligible(d, ObservationExclusionReason.NOT_YET_FINAL)])
    matched, unmatched = match_forecasts_to_observations([_candidate(f)], lookup)
    assert matched == []
    assert unmatched[0].reason == f"{UNMATCHED_NO_ELIGIBLE_OBSERVATION}:not_yet_final"


def test_integrity_violation_when_issue_time_after_final() -> None:
    """The exact H0003 G2c scenario, generalized: a forecast issued after
    the observation's own finalizing report must never be joined."""
    d = date(2026, 7, 22)
    final_time = datetime(2026, 7, 22, 12, 31)  # same-day provisional... but
    # here we simulate it as "eligible" (e.g. a hypothetical timezone with an
    # earlier local-midnight boundary) purely to isolate the integrity check
    # from the eligibility check, which is tested separately.
    issue_after = datetime(2026, 7, 22, 18, 46)
    f = _forecast(d, issue_after)
    lookup = build_observation_lookup([_eligible(d, "80.0", final_time)])
    matched, unmatched = match_forecasts_to_observations([_candidate(f)], lookup)
    assert matched == []
    assert unmatched[0].reason == UNMATCHED_INTEGRITY_VIOLATION


def test_integrity_check_boundary_equal_times_is_violation() -> None:
    """issue_time == final_issuance_time is NOT strictly before -- flagged
    as a violation (the observation cannot have been final at the exact
    instant the forecast was issued without a definite ordering)."""
    d = date(2026, 1, 1)
    t = datetime(2026, 1, 2, 6, 0)
    f = _forecast(d, t)
    lookup = build_observation_lookup([_eligible(d, "80.0", t)])
    matched, unmatched = match_forecasts_to_observations([_candidate(f)], lookup)
    assert matched == []
    assert unmatched[0].reason == UNMATCHED_INTEGRITY_VIOLATION


def test_deterministic_order_preserved() -> None:
    d = date(2026, 1, 1)
    f1 = _forecast(d, datetime(2025, 12, 30, 0, 0), value="80.0")
    f2 = _forecast(d, datetime(2025, 12, 31, 0, 0), value="81.0")
    lookup = build_observation_lookup([_eligible(d, "83.0", datetime(2026, 1, 2, 6, 0))])
    matched1, _ = match_forecasts_to_observations([_candidate(f1), _candidate(f2)], lookup)
    matched2, _ = match_forecasts_to_observations([_candidate(f1), _candidate(f2)], lookup)
    assert matched1 == matched2
    assert [m.forecast.issue_time for m in matched1] == [f1.issue_time, f2.issue_time]
