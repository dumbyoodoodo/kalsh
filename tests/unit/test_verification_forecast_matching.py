"""Tests for kalshi_weather.verification.forecast_matching."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import polars as pl

from kalshi_weather.verification.forecast_matching import (
    DEFAULT_HORIZON_BUCKETS,
    ForecastExclusionReason,
    ForecastIssuance,
    HorizonBucket,
    classify_horizon,
    compute_horizon_hours,
    find_duplicate_keys,
    forecasts_from_frame,
    select_forecasts,
    validate_temporal_ordering,
)


def _fc(
    station: str,
    variable: str,
    d: date,
    issue: datetime,
    valid_start: datetime | None,
    value: str | None,
    valid_end: datetime | None = None,
) -> ForecastIssuance:
    return ForecastIssuance(
        station_id=station,
        variable=variable,
        target_date=d,
        issue_time=issue,
        valid_start=valid_start,
        value=Decimal(value) if value is not None else None,
        valid_end=valid_end,
    )


# --- compute_horizon_hours -----------------------------------------------------


def test_compute_horizon_hours_basic() -> None:
    issue = datetime(2026, 7, 30, 12, 0)
    valid_start = datetime(2026, 8, 1, 6, 0)
    assert compute_horizon_hours(issue, valid_start) == Decimal(42)


def test_compute_horizon_hours_can_be_negative() -> None:
    issue = datetime(2026, 8, 1, 14, 0)
    valid_start = datetime(2026, 8, 1, 12, 0)
    assert compute_horizon_hours(issue, valid_start) == Decimal(-2)


# --- classify_horizon / bucket boundaries --------------------------------------


def test_bucket_boundaries_exact() -> None:
    assert classify_horizon(Decimal(0)) == "0-12h"
    assert classify_horizon(Decimal("11.99")) == "0-12h"
    assert classify_horizon(Decimal(12)) == "12-24h"
    assert classify_horizon(Decimal("23.99")) == "12-24h"
    assert classify_horizon(Decimal(24)) == "24-48h"
    assert classify_horizon(Decimal("47.99")) == "24-48h"
    assert classify_horizon(Decimal(48)) == "48h+"
    assert classify_horizon(Decimal(240)) == "48h+"  # inclusive at the default cap


def test_horizon_out_of_range_above_cap() -> None:
    assert classify_horizon(Decimal("240.01")) is None
    assert classify_horizon(Decimal(1000)) is None


def test_negative_horizon_within_slack_classifies_lowest_bucket() -> None:
    assert classify_horizon(Decimal("-0.5")) == "0-12h"


def test_negative_horizon_beyond_slack_is_invalid() -> None:
    assert classify_horizon(Decimal("-1.0")) is None  # exclusive boundary
    assert classify_horizon(Decimal("-2.0")) is None


def test_custom_bucket_scheme_and_max_horizon() -> None:
    buckets = (
        HorizonBucket("short", Decimal(0), Decimal(6)),
        HorizonBucket("long", Decimal(6), Decimal("Infinity")),
    )
    assert classify_horizon(Decimal(3), buckets) == "short"
    assert classify_horizon(Decimal(10), buckets) == "long"
    assert classify_horizon(Decimal(100), buckets, max_horizon_hours=Decimal(50)) is None


# --- find_duplicate_keys --------------------------------------------------------


def test_find_duplicate_keys_detects_exact_key_collision() -> None:
    issue = datetime(2026, 1, 1, 12, 0)
    d = date(2026, 1, 2)
    forecasts = [
        _fc("NYC", "tmax_f", d, issue, datetime(2026, 1, 2, 0, 0), "80.0"),
        _fc("NYC", "tmax_f", d, issue, datetime(2026, 1, 2, 0, 0), "81.0"),  # same key!
        _fc("NYC", "tmin_f", d, issue, datetime(2026, 1, 2, 0, 0), "60.0"),
    ]
    dupes = find_duplicate_keys(forecasts)
    assert dupes == {("NYC", "tmax_f", d, issue): 2}


def test_find_duplicate_keys_empty_when_all_unique() -> None:
    d = date(2026, 1, 2)
    forecasts = [
        _fc("NYC", "tmax_f", d, datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 2, 0, 0), "80.0"),
        _fc("NYC", "tmax_f", d, datetime(2026, 1, 1, 6, 0), datetime(2026, 1, 2, 0, 0), "81.0"),
    ]
    assert find_duplicate_keys(forecasts) == {}


# --- validate_temporal_ordering -------------------------------------------------


def test_validate_temporal_ordering_flags_invalid_period() -> None:
    d = date(2026, 1, 2)
    bad = _fc(
        "NYC",
        "tmax_f",
        d,
        datetime(2026, 1, 1, 0, 0),
        datetime(2026, 1, 2, 12, 0),
        "80.0",
        valid_end=datetime(2026, 1, 2, 6, 0),  # end before start!
    )
    issues = validate_temporal_ordering([bad])
    assert len(issues) == 1
    assert issues[0].problem == "valid_end <= valid_start"


def test_validate_temporal_ordering_same_day_forecast_is_not_flagged() -> None:
    """An ordinary same-day forecast (valid_start before issue_time) is
    normal and must not be flagged as an ordering problem."""
    d = date(2026, 1, 1)
    normal = _fc(
        "NYC",
        "tmax_f",
        d,
        datetime(2026, 1, 1, 14, 0),
        datetime(2026, 1, 1, 12, 0),
        "80.0",
        valid_end=datetime(2026, 1, 1, 18, 0),
    )
    assert validate_temporal_ordering([normal]) == []


# --- select_forecasts: exclusion reasons ----------------------------------------


def test_select_forecasts_excludes_null_value() -> None:
    d = date(2026, 1, 2)
    f = _fc("NYC", "tmax_f", d, datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 2, 0, 0), None)
    selected, excluded = select_forecasts([f])
    assert selected == []
    assert len(excluded) == 1
    assert excluded[0].excluded == ForecastExclusionReason.NULL_VALUE


def test_select_forecasts_excludes_missing_valid_start() -> None:
    d = date(2026, 1, 2)
    f = _fc("NYC", "tmax_f", d, datetime(2026, 1, 1, 0, 0), None, "80.0")
    selected, excluded = select_forecasts([f])
    assert selected == []
    assert excluded[0].excluded == ForecastExclusionReason.MISSING_VALID_START


def test_select_forecasts_excludes_horizon_out_of_range() -> None:
    d = date(2026, 1, 2)
    f = _fc(
        "NYC", "tmax_f", d, datetime(2025, 1, 1, 0, 0), datetime(2026, 1, 2, 0, 0), "80.0"
    )  # ~1 year lead time
    selected, excluded = select_forecasts([f])
    assert selected == []
    assert excluded[0].excluded == ForecastExclusionReason.HORIZON_OUT_OF_RANGE
    assert excluded[0].horizon_hours is not None  # still reported, for diagnostics


# --- select_forecasts: latest-per-cell selection --------------------------------


def test_select_forecasts_picks_latest_issuance_in_same_bucket() -> None:
    d = date(2026, 1, 2)
    # valid_start fixed at 00:00 Jan 2; issue at 06:00/08:00 Jan 1 -> 18h/16h
    # lead time -- both land in the 12-24h bucket.
    earlier_issue = _fc(
        "NYC", "tmax_f", d, datetime(2026, 1, 1, 6, 0), datetime(2026, 1, 2, 0, 0), "80.0"
    )
    later_issue = _fc(
        "NYC", "tmax_f", d, datetime(2026, 1, 1, 8, 0), datetime(2026, 1, 2, 0, 0), "81.0"
    )
    selected, excluded = select_forecasts([earlier_issue, later_issue])
    assert excluded == []
    assert len(selected) == 1
    assert selected[0].forecast.value == Decimal("81.0")  # the later (08:00) issuance wins


def test_select_forecasts_earliest_selection_mode() -> None:
    d = date(2026, 1, 2)
    earlier_issue = _fc(
        "NYC", "tmax_f", d, datetime(2026, 1, 1, 6, 0), datetime(2026, 1, 2, 0, 0), "80.0"
    )
    later_issue = _fc(
        "NYC", "tmax_f", d, datetime(2026, 1, 1, 8, 0), datetime(2026, 1, 2, 0, 0), "81.0"
    )
    selected, _ = select_forecasts([earlier_issue, later_issue], selection="earliest")
    assert selected[0].forecast.value == Decimal("80.0")


def test_select_forecasts_different_buckets_both_kept() -> None:
    d = date(2026, 1, 3)
    bucket_a = _fc(
        "NYC", "tmax_f", d, datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 3, 0, 0), "80.0"
    )  # exactly 48h -> "48h+" (lower bound inclusive)
    bucket_b = _fc(
        "NYC", "tmax_f", d, datetime(2026, 1, 2, 12, 0), datetime(2026, 1, 3, 0, 0), "81.0"
    )  # exactly 12h -> "12-24h" (lower bound inclusive)
    selected, excluded = select_forecasts([bucket_a, bucket_b])
    assert excluded == []
    assert len(selected) == 2
    buckets = {c.bucket for c in selected}
    assert buckets == {"48h+", "12-24h"}


def test_select_forecasts_deterministic_across_reordered_input() -> None:
    d = date(2026, 1, 2)
    a = _fc("NYC", "tmax_f", d, datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 2, 0, 0), "80.0")
    b = _fc("NYC", "tmax_f", d, datetime(2026, 1, 1, 6, 0), datetime(2026, 1, 2, 0, 0), "81.0")
    c = _fc("NYC", "tmin_f", d, datetime(2026, 1, 1, 3, 0), datetime(2026, 1, 2, 0, 0), "60.0")
    result1, _ = select_forecasts([a, b, c])
    result2, _ = select_forecasts([c, b, a])
    assert result1 == result2


def test_select_forecasts_exact_duplicate_key_resolved_deterministically() -> None:
    """Two rows sharing the identical natural key (a data-quality anomaly
    `find_duplicate_keys` would also flag) must still resolve to one
    deterministic selection, not an arbitrary one."""
    d = date(2026, 1, 2)
    issue = datetime(2026, 1, 1, 0, 0)
    valid_start = datetime(2026, 1, 2, 0, 0)
    dup1 = _fc("NYC", "tmax_f", d, issue, valid_start, "80.0")
    dup2 = _fc("NYC", "tmax_f", d, issue, valid_start, "82.0")
    result1, _ = select_forecasts([dup1, dup2])
    result2, _ = select_forecasts([dup2, dup1])
    assert result1 == result2


# --- forecasts_from_frame (polars adapter) --------------------------------------


def test_forecasts_from_frame() -> None:
    schema = {
        "station_id": pl.Utf8,
        "variable": pl.Utf8,
        "target_date": pl.Date,
        "issue_time": pl.Datetime("us"),
        "valid_start": pl.Datetime("us"),
        "forecast_value": pl.Float64,
    }
    df = pl.DataFrame(
        [
            {
                "station_id": "NYC",
                "variable": "tmax_f",
                "target_date": date(2026, 1, 2),
                "issue_time": datetime(2026, 1, 1, 0, 0),
                "valid_start": datetime(2026, 1, 2, 0, 0),
                "forecast_value": 80.5,
            }
        ],
        schema=schema,
        orient="row",
    )
    forecasts = forecasts_from_frame(df)
    assert len(forecasts) == 1
    assert forecasts[0].value == Decimal(str(80.5))
    assert forecasts[0].valid_start == datetime(2026, 1, 2, 0, 0)


def test_forecasts_from_frame_null_value_becomes_none() -> None:
    schema = {
        "station_id": pl.Utf8,
        "variable": pl.Utf8,
        "target_date": pl.Date,
        "issue_time": pl.Datetime("us"),
        "valid_start": pl.Datetime("us"),
        "forecast_value": pl.Float64,
    }
    df = pl.DataFrame(
        [
            {
                "station_id": "NYC",
                "variable": "tmax_f",
                "target_date": date(2026, 1, 2),
                "issue_time": datetime(2026, 1, 1, 0, 0),
                "valid_start": datetime(2026, 1, 2, 0, 0),
                "forecast_value": None,
            }
        ],
        schema=schema,
        orient="row",
    )
    forecasts = forecasts_from_frame(df)
    assert forecasts[0].value is None


def test_default_bucket_scheme_names_match_h0003_precedent() -> None:
    assert [b.name for b in DEFAULT_HORIZON_BUCKETS] == ["0-12h", "12-24h", "24-48h", "48h+"]
