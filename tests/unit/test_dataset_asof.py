"""Tests for the reusable point-in-time join engine (dataset/asof.py,
Phase 7B). Exercised directly, independent of any specific dataset frame, so
its guarantees (determinism, leakage-freedom, multi-key/multi-city support,
resolution-agnosticism) are proven once and reused everywhere."""

from datetime import datetime

import polars as pl

from kalshi_weather.dataset.asof import asof_count, asof_latest

BASE_SCHEMA = {"key": pl.Utf8, "t": pl.Datetime("us")}
OTHER_SCHEMA = {"key": pl.Utf8, "t": pl.Datetime("us"), "value": pl.Float64}


def _dt(h: int, mi: int = 0) -> datetime:
    return datetime(2026, 7, 20, h, mi)


def _base(rows: list[dict]) -> pl.DataFrame:  # type: ignore[type-arg]
    return pl.DataFrame(rows, schema=BASE_SCHEMA, orient="row")


def _other(rows: list[dict]) -> pl.DataFrame:  # type: ignore[type-arg]
    return pl.DataFrame(rows, schema=OTHER_SCHEMA, orient="row")


def test_asof_latest_picks_the_most_recent_row_at_or_before_left_time() -> None:
    base = _base([{"key": "A", "t": _dt(10)}])
    other = _other(
        [
            {"key": "A", "t": _dt(8), "value": 1.0},
            {"key": "A", "t": _dt(9), "value": 2.0},
            {"key": "A", "t": _dt(11), "value": 3.0},  # after left_time -- must not be picked
        ]
    )
    out = asof_latest(base, other, by=["key"], left_time="t", right_time="t", bring={"value": "v"})
    assert out.row(0, named=True)["v"] == 2.0


def test_asof_latest_is_inclusive_at_exact_timestamp_match() -> None:
    base = _base([{"key": "A", "t": _dt(9)}])
    other = _other([{"key": "A", "t": _dt(9), "value": 5.0}])
    out = asof_latest(base, other, by=["key"], left_time="t", right_time="t", bring={"value": "v"})
    assert out.row(0, named=True)["v"] == 5.0


def test_asof_latest_never_leaks_a_future_row() -> None:
    base = _base([{"key": "A", "t": _dt(8)}])
    other = _other([{"key": "A", "t": _dt(9), "value": 99.0}])
    out = asof_latest(base, other, by=["key"], left_time="t", right_time="t", bring={"value": "v"})
    assert out.row(0, named=True)["v"] is None


def test_asof_latest_multi_key_isolates_groups_multiple_cities() -> None:
    """`by` with multiple columns (or just station_id) must never let one
    city's fact answer another city's row -- the 'multiple cities' guarantee
    the milestone requires."""
    base = _base(
        [{"key": "NYC", "t": _dt(10)}, {"key": "LAX", "t": _dt(10)}]
    ).rename({"key": "station"})
    other = pl.DataFrame(
        [
            {"station": "NYC", "t": _dt(9), "value": 70.0},
            {"station": "LAX", "t": _dt(9), "value": 90.0},
        ],
        schema={"station": pl.Utf8, "t": pl.Datetime("us"), "value": pl.Float64},
        orient="row",
    )
    out = asof_latest(
        base, other, by=["station"], left_time="t", right_time="t", bring={"value": "v"}
    )
    by_station = {r["station"]: r["v"] for r in out.to_dicts()}
    assert by_station == {"NYC": 70.0, "LAX": 90.0}


def test_asof_latest_works_at_one_minute_and_daily_resolutions() -> None:
    """Resolution-agnostic: the join key is just a timestamp column, so a
    1-minute-candle base and a daily-observation `other` join correctly with
    no special-casing."""
    base = _base(
        [
            {"key": "A", "t": datetime(2026, 7, 20, 18, 1)},
            {"key": "A", "t": datetime(2026, 7, 21, 3, 1)},
        ]
    )
    other = _other([{"key": "A", "t": datetime(2026, 7, 20, 20, 0), "value": 79.0}])
    out = asof_latest(base, other, by=["key"], left_time="t", right_time="t", bring={"value": "v"})
    rows = out.sort("t").to_dicts()
    assert rows[0]["v"] is None  # 18:01, before the 20:00 issuance
    assert rows[1]["v"] == 79.0  # next day 03:01, after it


def test_asof_latest_empty_other_returns_typed_nulls() -> None:
    base = _base([{"key": "A", "t": _dt(10)}])
    other = _other([])
    out = asof_latest(base, other, by=["key"], left_time="t", right_time="t", bring={"value": "v"})
    assert out.height == 1
    assert out.row(0, named=True)["v"] is None
    assert out.schema["v"] == pl.Float64


def test_asof_latest_empty_base_returns_empty_typed_frame() -> None:
    base = _base([])
    other = _other([{"key": "A", "t": _dt(9), "value": 1.0}])
    out = asof_latest(base, other, by=["key"], left_time="t", right_time="t", bring={"value": "v"})
    assert out.height == 0
    assert "v" in out.columns


def test_asof_latest_is_deterministic_regardless_of_input_row_order() -> None:
    base = _base([{"key": "A", "t": _dt(10)}])
    other_forward = _other(
        [{"key": "A", "t": _dt(8), "value": 1.0}, {"key": "A", "t": _dt(9), "value": 2.0}]
    )
    other_reversed = _other(
        [{"key": "A", "t": _dt(9), "value": 2.0}, {"key": "A", "t": _dt(8), "value": 1.0}]
    )
    out1 = asof_latest(
        base, other_forward, by=["key"], left_time="t", right_time="t", bring={"value": "v"}
    )
    out2 = asof_latest(
        base, other_reversed, by=["key"], left_time="t", right_time="t", bring={"value": "v"}
    )
    assert out1.row(0, named=True)["v"] == out2.row(0, named=True)["v"] == 2.0


def test_asof_count_counts_only_rows_at_or_before_left_time() -> None:
    base = _base([{"key": "A", "t": _dt(10)}])
    other = _other(
        [
            {"key": "A", "t": _dt(8), "value": 1.0},
            {"key": "A", "t": _dt(9), "value": 2.0},
            {"key": "A", "t": _dt(11), "value": 3.0},  # future -- must not count
        ]
    )
    out = asof_count(base, other, by=["key"], left_time="t", right_time="t", out="n")
    assert out.row(0, named=True)["n"] == 2


def test_asof_count_multi_city_isolated() -> None:
    base = _base(
        [{"key": "NYC", "t": _dt(10)}, {"key": "LAX", "t": _dt(10)}]
    ).rename({"key": "station"})
    other = pl.DataFrame(
        [
            {"station": "NYC", "t": _dt(9), "value": 1.0},
            {"station": "NYC", "t": _dt(8), "value": 1.0},
            {"station": "LAX", "t": _dt(9), "value": 1.0},
        ],
        schema={"station": pl.Utf8, "t": pl.Datetime("us"), "value": pl.Float64},
        orient="row",
    )
    out = asof_count(base, other, by=["station"], left_time="t", right_time="t", out="n")
    by_station = {r["station"]: r["n"] for r in out.to_dicts()}
    assert by_station == {"NYC": 2, "LAX": 1}


def test_asof_count_empty_other_returns_zero() -> None:
    base = _base([{"key": "A", "t": _dt(10)}])
    other = _other([])
    out = asof_count(base, other, by=["key"], left_time="t", right_time="t", out="n")
    assert out.row(0, named=True)["n"] == 0


def test_asof_count_never_counts_a_future_row() -> None:
    base = _base([{"key": "A", "t": _dt(8)}])
    other = _other([{"key": "A", "t": _dt(9), "value": 1.0}])
    out = asof_count(base, other, by=["key"], left_time="t", right_time="t", out="n")
    assert out.row(0, named=True)["n"] == 0
