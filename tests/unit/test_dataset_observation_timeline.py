"""Tests for observation-timeline reconstruction (dataset/observation_timeline.py,
Phase 7B): the running-extreme cummax/cummin and the calendar-lock
computation, both pure and model-free."""

from datetime import date, datetime

import polars as pl

from kalshi_weather.dataset.builder import OBSERVATIONS_SCHEMA, STATIONS_SCHEMA
from kalshi_weather.dataset.observation_timeline import (
    attach_calendar_lock,
    compute_running_extremes,
)


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi)


def _obs(rows: list[dict]) -> pl.DataFrame:  # type: ignore[type-arg]
    return pl.DataFrame(rows, schema=OBSERVATIONS_SCHEMA, orient="row")


def _row(station: str, variable: str, value: float, obs_date: date, issued: datetime) -> dict:  # type: ignore[type-arg]
    return {
        "station_id": station,
        "variable": variable,
        "value": value,
        "observation_date": obs_date,
        "issuance_time": issued,
        "raw_payload_id": 1,
    }


def test_running_tmax_is_cumulative_max_across_same_day_issuances() -> None:
    observations = _obs(
        [
            _row("NYC", "tmax_f", 75.0, date(2026, 7, 20), _dt(2026, 7, 20, 16)),
            _row("NYC", "tmax_f", 79.0, date(2026, 7, 20), _dt(2026, 7, 20, 20)),
            # a later issuance revises DOWN (rare, per H0002) -- cummax must
            # not decrease: the earlier 79 reading is still a valid hard bound.
            _row("NYC", "tmax_f", 78.0, date(2026, 7, 20), _dt(2026, 7, 21, 6)),
        ]
    )
    out = compute_running_extremes(observations).sort("issuance_time")
    assert out["running_extreme_f"].to_list() == [75.0, 79.0, 79.0]


def test_running_tmin_is_cumulative_min_across_same_day_issuances() -> None:
    observations = _obs(
        [
            _row("NYC", "tmin_f", 65.0, date(2026, 7, 20), _dt(2026, 7, 20, 10)),
            _row("NYC", "tmin_f", 60.0, date(2026, 7, 20), _dt(2026, 7, 20, 14)),
            # a later issuance revises UP -- cummin must not increase.
            _row("NYC", "tmin_f", 62.0, date(2026, 7, 20), _dt(2026, 7, 21, 6)),
        ]
    )
    out = compute_running_extremes(observations).sort("issuance_time")
    assert out["running_extreme_f"].to_list() == [65.0, 60.0, 60.0]


def test_running_extreme_is_isolated_per_station_variable_and_date() -> None:
    """No cross-group leakage: a different station, variable, or date must
    never contribute to another group's running value."""
    observations = _obs(
        [
            _row("NYC", "tmax_f", 90.0, date(2026, 7, 20), _dt(2026, 7, 20, 12)),
            _row("LAX", "tmax_f", 60.0, date(2026, 7, 20), _dt(2026, 7, 20, 12)),
            _row("NYC", "tmin_f", 55.0, date(2026, 7, 20), _dt(2026, 7, 20, 12)),
            _row("NYC", "tmax_f", 70.0, date(2026, 7, 21), _dt(2026, 7, 21, 12)),
        ]
    )
    out = compute_running_extremes(observations)
    by_group = {
        (r["station_id"], r["variable"], r["observation_date"]): r["running_extreme_f"]
        for r in out.to_dicts()
    }
    assert by_group[("NYC", "tmax_f", date(2026, 7, 20))] == 90.0
    assert by_group[("LAX", "tmax_f", date(2026, 7, 20))] == 60.0
    assert by_group[("NYC", "tmin_f", date(2026, 7, 20))] == 55.0
    assert by_group[("NYC", "tmax_f", date(2026, 7, 21))] == 70.0


def test_running_extreme_empty_observations_builds_without_error() -> None:
    out = compute_running_extremes(_obs([]))
    assert out.height == 0
    assert out.schema["running_extreme_f"] == pl.Float64


STATIONS = pl.DataFrame(
    [{"station_id": "NYC", "timezone": "America/New_York"}], schema=STATIONS_SCHEMA, orient="row"
)


def _price_like(rows: list[dict]) -> pl.DataFrame:  # type: ignore[type-arg]
    return pl.DataFrame(
        rows,
        schema={"station_id": pl.Utf8, "t": pl.Datetime("us"), "target_date": pl.Date},
        orient="row",
    )


def test_calendar_lock_false_before_local_midnight_true_after() -> None:
    """America/New_York is UTC-4 in July (EDT): 2026-07-21 03:01 UTC is
    2026-07-20 23:01 local (still the target day -- not elapsed);
    2026-07-21 05:01 UTC is 2026-07-21 01:01 local (the target day has
    elapsed)."""
    frame = _price_like(
        [
            {"station_id": "NYC", "t": _dt(2026, 7, 21, 3, 1), "target_date": date(2026, 7, 20)},
            {"station_id": "NYC", "t": _dt(2026, 7, 21, 5, 1), "target_date": date(2026, 7, 20)},
        ]
    )
    out = attach_calendar_lock(
        frame, STATIONS, timestamp_col="t", target_date_col="target_date"
    ).sort("t")
    assert out["observation_window_elapsed"].to_list() == [False, True]


def test_calendar_lock_false_while_still_on_the_target_date_itself() -> None:
    frame = _price_like(
        [{"station_id": "NYC", "t": _dt(2026, 7, 20, 18, 0), "target_date": date(2026, 7, 20)}]
    )
    out = attach_calendar_lock(frame, STATIONS, timestamp_col="t", target_date_col="target_date")
    assert out.row(0, named=True)["observation_window_elapsed"] is False


def test_calendar_lock_isolates_multiple_cities_with_different_timezones() -> None:
    """A second station in a different (non-UTC-offset) timezone must not
    share NYC's boundary -- the 'multiple cities' guarantee."""
    stations = pl.concat(
        [
            STATIONS,
            pl.DataFrame(
                [{"station_id": "LAX", "timezone": "America/Los_Angeles"}],
                schema=STATIONS_SCHEMA,
                orient="row",
            ),
        ]
    )
    # 2026-07-21 05:01 UTC: NYC (UTC-4) local is 01:01 on 7-21 -> elapsed.
    # LAX (UTC-7) local is 22:01 on 7-20 -> NOT elapsed.
    t = _dt(2026, 7, 21, 5, 1)
    frame = pl.concat(
        [
            _price_like([{"station_id": "NYC", "t": t, "target_date": date(2026, 7, 20)}]),
            _price_like([{"station_id": "LAX", "t": t, "target_date": date(2026, 7, 20)}]),
        ]
    )
    out = attach_calendar_lock(frame, stations, timestamp_col="t", target_date_col="target_date")
    by_station = {r["station_id"]: r["observation_window_elapsed"] for r in out.to_dicts()}
    assert by_station == {"NYC": True, "LAX": False}


def test_calendar_lock_unregistered_station_yields_null_not_a_guess() -> None:
    frame = _price_like(
        [{"station_id": "GHOST", "t": _dt(2026, 7, 21, 5, 1), "target_date": date(2026, 7, 20)}]
    )
    out = attach_calendar_lock(frame, STATIONS, timestamp_col="t", target_date_col="target_date")
    assert out.row(0, named=True)["observation_window_elapsed"] is None


def test_calendar_lock_empty_frame_builds_without_error() -> None:
    out = attach_calendar_lock(
        _price_like([]), STATIONS, timestamp_col="t", target_date_col="target_date"
    )
    assert out.height == 0
    assert out.schema["observation_window_elapsed"] == pl.Boolean
