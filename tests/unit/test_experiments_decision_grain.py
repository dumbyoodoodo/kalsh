"""As-of / leakage correctness for the H0018 decision grain."""

from datetime import datetime

import polars as pl

from kalshi_weather.experiments.decision_grain import _asof_latest
from kalshi_weather.experiments.runner import _WEATHER_FEATURES, _leakage_checks


def _dt(day: int, hour: int) -> datetime:
    return datetime(2026, 7, day, hour, 0)


def test_asof_picks_latest_at_or_before_decision_never_after() -> None:
    left = pl.DataFrame(
        {
            "market_ticker": ["A", "B"],
            "station": ["CHI", "CHI"],
            "decision_time": [_dt(10, 12), _dt(10, 12)],
        }
    )
    right = pl.DataFrame(
        {
            "station": ["CHI", "CHI", "CHI"],
            "issue_time": [_dt(10, 6), _dt(10, 11), _dt(10, 18)],  # 18:00 is AFTER decision
            "value": [1.0, 2.0, 99.0],
        }
    )
    out = _asof_latest(
        left,
        right,
        on=["station"],
        time_left="decision_time",
        time_right="issue_time",
        take={"value": "obs_value", "issue_time": "obs_time"},
    ).sort("market_ticker")
    # latest <= 12:00 is the 11:00 row (value 2.0); the 18:00 (future) is never used
    assert out["obs_value"].to_list() == [2.0, 2.0]
    assert out["obs_time"].max() == _dt(10, 11)


def test_asof_returns_null_when_no_prior_record() -> None:
    left = pl.DataFrame({"market_ticker": ["A"], "station": ["CHI"], "decision_time": [_dt(10, 5)]})
    right = pl.DataFrame(
        {"station": ["CHI"], "issue_time": [_dt(10, 9)], "value": [7.0]}
    )  # only future
    out = _asof_latest(
        left,
        right,
        on=["station"],
        time_left="decision_time",
        time_right="issue_time",
        take={"value": "obs_value", "issue_time": "obs_time"},
    )
    assert out["obs_value"].to_list() == [None]


def test_asof_one_row_per_market() -> None:
    left = pl.DataFrame(
        {
            "market_ticker": ["A", "B", "C"],
            "station": ["CHI", "CHI", "DEN"],
            "decision_time": [_dt(10, 12)] * 3,
        }
    )
    right = pl.DataFrame(
        {
            "station": ["CHI", "CHI", "DEN"],
            "issue_time": [_dt(10, 6), _dt(10, 9), _dt(10, 8)],
            "value": [1.0, 2.0, 3.0],
        }
    )
    out = _asof_latest(
        left,
        right,
        on=["station"],
        time_left="decision_time",
        time_right="issue_time",
        take={"value": "v", "issue_time": "t"},
    )
    assert out.height == 3  # exactly one row per market


def test_leakage_checks_flag_a_future_feature() -> None:
    good = pl.DataFrame(
        {
            "market_ticker": ["A"],
            "y": [1],
            "decision_time": [_dt(10, 12)],
            "close_time": [_dt(11, 0)],
            "price_asof": [_dt(10, 6)],
            "fc_issue": [_dt(10, 5)],
            "obs_time": [_dt(10, 4)],
        }
    )
    assert _leakage_checks(good)["all_passed"] is True
    bad = good.with_columns(pl.lit(_dt(10, 18)).alias("fc_issue"))  # forecast AFTER decision
    assert _leakage_checks(bad)["forecast_before_decision"] is False
    assert _leakage_checks(bad)["all_passed"] is False


def test_no_liquidity_features_in_model_feature_set() -> None:
    banned = {"volume", "open_interest", "spread", "trade", "depth", "imbalance"}
    for f in _WEATHER_FEATURES:
        assert not any(b in f.lower() for b in banned), f
