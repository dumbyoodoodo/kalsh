"""Tests for kalshi_weather.verification.reporting."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from kalshi_weather.verification.forecast_matching import (
    DEFAULT_HORIZON_BUCKETS,
    ForecastExclusionReason,
    ForecastIssuance,
    MatchedForecastCandidate,
)
from kalshi_weather.verification.observation_matching import MatchedObservation, UnmatchedForecast
from kalshi_weather.verification.reporting import (
    eligible_forecasts_table,
    excluded_forecasts_table,
    exclusion_reason_summary,
    forecast_coverage_report,
    horizon_coverage_report,
)


def _forecast(
    station: str, variable: str, d: date, issue: datetime, value: str = "80.0"
) -> ForecastIssuance:
    return ForecastIssuance(station, variable, d, issue, None, Decimal(value))


def _matched(
    station: str, variable: str, d: date, issue: datetime, bucket: str = "12-24h"
) -> MatchedObservation:
    f = _forecast(station, variable, d, issue)
    return MatchedObservation(
        f, Decimal("18"), bucket, Decimal("83.0"), datetime(2026, 1, 2, 6, 0), Decimal("-3.0")
    )


def test_eligible_forecasts_table_sorted_and_json_safe() -> None:
    m1 = _matched("NYC", "tmax_f", date(2026, 1, 2), datetime(2026, 1, 1, 6, 0))
    m2 = _matched("CHI", "tmax_f", date(2026, 1, 1), datetime(2026, 1, 1, 0, 0))
    table = eligible_forecasts_table([m1, m2])
    assert table[0]["station_id"] == "CHI"  # sorted before NYC
    assert table[1]["station_id"] == "NYC"
    json.dumps(table)  # must not raise


def test_excluded_forecasts_table_combines_both_sources() -> None:
    d = date(2026, 1, 1)
    forecast_side = [
        MatchedForecastCandidate(
            _forecast("NYC", "tmax_f", d, datetime(2026, 1, 1, 0, 0)),
            None,
            None,
            ForecastExclusionReason.NULL_VALUE,
        )
    ]
    obs_side = [
        UnmatchedForecast(
            _forecast("CHI", "tmax_f", d, datetime(2026, 1, 1, 0, 0)),
            "observation_not_eligible:not_yet_final",
        )
    ]
    table = excluded_forecasts_table(forecast_side, obs_side)
    assert len(table) == 2
    reasons = {row["station_id"]: row["reason"] for row in table}
    assert reasons["NYC"] == "null_value"
    assert reasons["CHI"] == "observation_not_eligible:not_yet_final"
    json.dumps(table)


def test_exclusion_reason_summary_counts_and_sorts() -> None:
    d = date(2026, 1, 1)
    forecast_side = [
        MatchedForecastCandidate(
            _forecast("NYC", "tmax_f", d, datetime(2026, 1, 1, 0, 0)),
            None,
            None,
            ForecastExclusionReason.NULL_VALUE,
        ),
        MatchedForecastCandidate(
            _forecast("NYC", "tmin_f", d, datetime(2026, 1, 1, 0, 0)),
            None,
            None,
            ForecastExclusionReason.NULL_VALUE,
        ),
    ]
    obs_side = [
        UnmatchedForecast(
            _forecast("CHI", "tmax_f", d, datetime(2026, 1, 1, 0, 0)),
            "integrity_violation_issue_after_final",
        )
    ]
    summary = exclusion_reason_summary(forecast_side, obs_side)
    assert summary == {
        "integrity_violation_issue_after_final": 1,
        "null_value": 2,
    }
    assert list(summary.keys()) == sorted(summary.keys())


def test_forecast_coverage_report_totals_and_fraction() -> None:
    d = date(2026, 1, 1)
    matched = [_matched("NYC", "tmax_f", d, datetime(2026, 1, 1, 0, 0))]
    forecast_side = [
        MatchedForecastCandidate(
            _forecast("NYC", "tmin_f", d, datetime(2026, 1, 1, 0, 0)),
            None,
            None,
            ForecastExclusionReason.NULL_VALUE,
        )
    ]
    obs_side: list[UnmatchedForecast] = []
    report = forecast_coverage_report(matched, forecast_side, obs_side)
    assert report["total_candidates"] == 2
    assert report["eligible"] == 1
    assert report["excluded"] == 1
    assert report["eligible_of_total"] == "1/2"
    assert report["coverage_fraction"] == str(Decimal(1) / Decimal(2))
    assert report["by_station_variable"]["NYC|tmax_f"] == {"eligible": 1, "excluded": 0}
    assert report["by_station_variable"]["NYC|tmin_f"] == {"eligible": 0, "excluded": 1}
    json.dumps(report)


def test_forecast_coverage_report_handles_zero_total() -> None:
    report = forecast_coverage_report([], [], [])
    assert report["total_candidates"] == 0
    assert report["coverage_fraction"] is None


def test_horizon_coverage_report_uses_bucket_order_and_zero_fills() -> None:
    d = date(2026, 1, 1)
    matched = [_matched("NYC", "tmax_f", d, datetime(2026, 1, 1, 0, 0), bucket="24-48h")]
    report = horizon_coverage_report(matched)
    assert report["bucket_order"] == [b.name for b in DEFAULT_HORIZON_BUCKETS]
    row = report["by_station_variable"]["NYC|tmax_f"]
    assert row["24-48h"] == 1
    assert row["0-12h"] == 0  # zero-filled, not absent
    json.dumps(report)


def test_reports_are_byte_identical_across_reordered_input() -> None:
    d = date(2026, 1, 1)
    m1 = _matched("NYC", "tmax_f", d, datetime(2026, 1, 1, 0, 0))
    m2 = _matched("CHI", "tmax_f", d, datetime(2026, 1, 1, 0, 0))
    t1 = eligible_forecasts_table([m1, m2])
    t2 = eligible_forecasts_table([m2, m1])
    assert json.dumps(t1, sort_keys=True) == json.dumps(t2, sort_keys=True)
