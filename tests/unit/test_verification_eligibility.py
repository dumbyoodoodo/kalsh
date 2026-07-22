"""Tests for kalshi_weather.verification.eligibility."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import polars as pl
import pytest

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
from kalshi_weather.weather.stations import UnknownStationError


def _rec(
    station: str, variable: str, d: date, issuance: datetime, value: str = "60.0"
) -> ObservationRecord:
    return ObservationRecord(
        station_id=station,
        variable=variable,
        observation_date=d,
        issuance_time=issuance,
        value=Decimal(value),
    )


# --- local_midnight_boundary --------------------------------------------------


def test_local_midnight_boundary_is_start_of_next_day_nyc() -> None:
    boundary = local_midnight_boundary(date(2026, 1, 15), "America/New_York")
    assert boundary.year == 2026
    assert boundary.month == 1
    assert boundary.day == 16
    assert boundary.hour == 0 and boundary.minute == 0 and boundary.second == 0


def test_local_midnight_boundary_respects_dst_offset() -> None:
    # 2026-03-08 -> 2026-03-09 spans the US spring-forward transition; the
    # boundary itself (00:00 local) is unaffected (never inside the 2 AM gap).
    boundary = local_midnight_boundary(date(2026, 3, 8), "America/New_York")
    assert boundary.utcoffset() is not None


# --- check_eligibility: no observation ----------------------------------------


def test_no_issuances_is_not_eligible() -> None:
    result = check_eligibility("NYC", "tmax_f", date(2026, 1, 1), [])
    assert result.eligible is False
    assert result.reason == ObservationExclusionReason.NO_OBSERVATION
    assert result.n_issuances == 0
    assert result.final_value is None


# --- check_eligibility: same-day (in-progress) --------------------------------


def test_same_day_provisional_only_is_not_eligible() -> None:
    """The exact H0003 G2c scenario: a same-day preliminary reading, no
    post-midnight final report yet."""
    d = date(2026, 7, 22)
    # 12:31 UTC same day -- well before local midnight of 2026-07-23 in any
    # US station timezone.
    same_day = datetime(2026, 7, 22, 12, 31)
    result = check_eligibility("DEN", "tmax_f", d, [_rec("DEN", "tmax_f", d, same_day)])
    assert result.eligible is False
    assert result.reason == ObservationExclusionReason.NOT_YET_FINAL
    assert result.final_value is None
    assert result.final_issuance_time is None
    # diagnostic-only fields still populated, for reporting -- never for use
    # as ground truth:
    assert result.latest_seen_value == Decimal("60.0")
    assert result.latest_seen_issuance_time == same_day


def test_final_report_after_local_midnight_is_eligible() -> None:
    d = date(2026, 7, 21)
    # NYC is UTC-4 (EDT) in July; local midnight of 07-22 is 04:00 UTC.
    final_time = datetime(2026, 7, 22, 6, 27)  # 02:27 EDT -- after local midnight
    result = check_eligibility(
        "NYC",
        "tmax_f",
        d,
        [
            _rec("NYC", "tmax_f", d, datetime(2026, 7, 21, 20, 49), "81.0"),
            _rec("NYC", "tmax_f", d, final_time, "83.0"),
        ],
    )
    assert result.eligible is True
    assert result.reason is None
    assert result.final_value == Decimal("83.0")
    assert result.final_issuance_time == final_time
    assert result.n_issuances == 2


def test_final_report_exactly_at_local_midnight_is_eligible() -> None:
    """At-or-after: an issuance at exactly local midnight belongs to the
    next day and counts as final."""
    from datetime import UTC

    d = date(2026, 1, 15)
    boundary_local = local_midnight_boundary(d, "America/Chicago")
    naive_utc_at_boundary = boundary_local.astimezone(UTC).replace(tzinfo=None)
    result = check_eligibility(
        "CHI", "tmax_f", d, [_rec("CHI", "tmax_f", d, naive_utc_at_boundary)]
    )
    assert result.eligible is True


def test_final_report_one_second_before_local_midnight_is_not_eligible() -> None:
    from datetime import UTC, timedelta

    d = date(2026, 1, 15)
    boundary_local = local_midnight_boundary(d, "America/Chicago")
    just_before = (boundary_local - timedelta(seconds=1)).astimezone(UTC).replace(tzinfo=None)
    result = check_eligibility("CHI", "tmax_f", d, [_rec("CHI", "tmax_f", d, just_before)])
    assert result.eligible is False
    assert result.reason == ObservationExclusionReason.NOT_YET_FINAL


# --- check_eligibility: unknown station ---------------------------------------


def test_unknown_station_is_not_eligible() -> None:
    d = date(2026, 1, 1)
    result = check_eligibility(
        "ZZZ", "tmax_f", d, [_rec("ZZZ", "tmax_f", d, datetime(2026, 1, 2, 6, 0))]
    )
    assert result.eligible is False
    assert result.reason == ObservationExclusionReason.UNKNOWN_STATION
    assert result.n_issuances == 1


def test_get_station_raises_for_reference() -> None:
    # documents the exception check_eligibility catches internally
    with pytest.raises(UnknownStationError):
        from kalshi_weather.weather.stations import get_station

        get_station("ZZZ")


# --- build_eligibility_table / eligible_lookup --------------------------------


def test_build_eligibility_table_groups_and_sorts() -> None:
    records = [
        _rec("NYC", "tmin_f", date(2026, 1, 2), datetime(2026, 1, 3, 6, 0)),
        _rec("CHI", "tmax_f", date(2026, 1, 1), datetime(2026, 1, 2, 6, 0)),
        _rec("NYC", "tmax_f", date(2026, 1, 1), datetime(2026, 1, 2, 6, 0)),
    ]
    table = build_eligibility_table(records)
    keys = [(r.station_id, r.variable, r.observation_date) for r in table]
    assert keys == sorted(keys)
    assert len(table) == 3
    assert all(r.eligible for r in table)


def test_eligible_lookup_keys_by_station_variable_date() -> None:
    d = date(2026, 1, 1)
    table = [
        ObservationEligibility(
            "NYC", "tmax_f", d, True, None, Decimal("80"), datetime(2026, 1, 2, 6, 0), 1
        )
    ]
    lookup = eligible_lookup(table)
    assert lookup[("NYC", "tmax_f", d)].eligible is True
    assert ("CHI", "tmax_f", d) not in lookup


# --- eligibility_table_from_frame (polars adapter) ----------------------------


def test_eligibility_table_from_frame() -> None:
    schema = {
        "station_id": pl.Utf8,
        "variable": pl.Utf8,
        "observation_date": pl.Date,
        "issuance_time": pl.Datetime("us"),
        "value": pl.Float64,
    }
    rows = [
        {
            "station_id": "NYC",
            "variable": "tmax_f",
            "observation_date": date(2026, 1, 1),
            "issuance_time": datetime(2026, 1, 2, 6, 0),
            "value": 81.5,
        },
        {
            "station_id": "NYC",
            "variable": "tmax_f",
            "observation_date": date(2026, 1, 1),
            "issuance_time": datetime(2026, 1, 1, 20, 0),
            "value": 80.0,
        },
    ]
    df = pl.DataFrame(rows, schema=schema, orient="row")
    table = eligibility_table_from_frame(df)
    assert len(table) == 1
    assert table[0].eligible is True
    assert table[0].final_value == Decimal("81.5")
    assert table[0].n_issuances == 2


def test_eligibility_table_from_frame_uses_decimal_str_construction() -> None:
    """0.1 has no exact binary representation; Decimal(str(0.1)) must be
    used, never Decimal(0.1) directly."""
    schema = {
        "station_id": pl.Utf8,
        "variable": pl.Utf8,
        "observation_date": pl.Date,
        "issuance_time": pl.Datetime("us"),
        "value": pl.Float64,
    }
    df = pl.DataFrame(
        [
            {
                "station_id": "NYC",
                "variable": "tmax_f",
                "observation_date": date(2026, 1, 1),
                "issuance_time": datetime(2026, 1, 2, 6, 0),
                "value": 60.1,
            }
        ],
        schema=schema,
        orient="row",
    )
    table = eligibility_table_from_frame(df)
    assert table[0].final_value == Decimal(str(60.1))
    direct = Decimal(60.1)  # noqa: RUF032 -- the point of this test is this exact call
    assert table[0].final_value != direct  # the binary-exact expansion differs
