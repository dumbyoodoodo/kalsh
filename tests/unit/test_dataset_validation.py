from datetime import UTC, date, datetime, timedelta

import polars as pl

from kalshi_weather.dataset import builder as B
from kalshi_weather.dataset.market_map import MarketMapping
from kalshi_weather.dataset.validation import validate

STATIONS = pl.DataFrame(
    [{"station_id": "NYC", "timezone": "America/New_York"}],
    schema=B.STATIONS_SCHEMA,
    orient="row",
)


def _empty(schema: dict) -> pl.DataFrame:  # type: ignore[type-arg]
    return pl.DataFrame([], schema=schema, orient="row")


def _sources(**overrides: pl.DataFrame) -> B.SourceFrames:
    base = {
        "stations": STATIONS,
        "markets": _empty(B.MARKETS_SCHEMA),
        "orderbooks": _empty(B.ORDERBOOKS_SCHEMA),
        "trades": _empty(B.TRADES_SCHEMA),
        "forecasts": _empty(B.FORECASTS_SCHEMA),
        "observations": _empty(B.OBSERVATIONS_SCHEMA),
        "candlesticks": _empty(B.CANDLESTICKS_SCHEMA),
    }
    base.update(overrides)
    return B.SourceFrames(**base)  # type: ignore[arg-type]


def _finding(report, check):  # type: ignore[no-untyped-def]
    return next(f for f in report.findings if f.check == check)


def test_impossible_timestamp_forecast_valid_start_after_end() -> None:
    forecasts = pl.DataFrame(
        [{"station_id": "NYC", "target_date": date(2026, 7, 20), "point_estimate": 80.0,
          "issue_time": datetime(2026, 7, 18, 12), "valid_start": datetime(2026, 7, 20, 23),
          "valid_end": datetime(2026, 7, 20, 4), "raw_payload_id": 1}],
        schema=B.FORECASTS_SCHEMA, orient="row",
    )
    sources = _sources(forecasts=forecasts)
    report = validate(sources, B.build_datasets(sources, []).frames, [])
    assert not report.ok
    assert _finding(report, "impossible_timestamps").count >= 1


def test_impossible_observation_issued_before_its_date() -> None:
    observations = pl.DataFrame(
        [{"station_id": "NYC", "variable": "tmax_f", "value": 80.0,
          "observation_date": date(2026, 7, 20), "issuance_time": datetime(2026, 7, 19, 12),
          "raw_payload_id": 1}],
        schema=B.OBSERVATIONS_SCHEMA, orient="row",
    )
    sources = _sources(observations=observations)
    report = validate(sources, B.build_datasets(sources, []).frames, [])
    assert _finding(report, "impossible_timestamps").count >= 1


def test_future_market_snapshot_flagged() -> None:
    future = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=2)
    markets = pl.DataFrame(
        [{"market_ticker": "X", "event_ticker": "E", "status": "open", "yes_bid_cents": 40,
          "yes_ask_cents": 45, "last_price_cents": 42, "volume": 1, "open_interest": 1,
          "rules_primary": "r", "observed_at": future, "raw_payload_id": 1}],
        schema=B.MARKETS_SCHEMA, orient="row",
    )
    sources = _sources(markets=markets)
    report = validate(sources, B.build_datasets(sources, []).frames, [])
    assert _finding(report, "impossible_timestamps").count >= 1


def test_settlement_mismatch_high_below_low() -> None:
    observations = pl.DataFrame(
        [
            {"station_id": "NYC", "variable": "tmax_f", "value": 60.0,
             "observation_date": date(2026, 7, 20), "issuance_time": datetime(2026, 7, 21, 6),
             "raw_payload_id": 1},
            {"station_id": "NYC", "variable": "tmin_f", "value": 70.0,
             "observation_date": date(2026, 7, 20), "issuance_time": datetime(2026, 7, 21, 6),
             "raw_payload_id": 1},
        ],
        schema=B.OBSERVATIONS_SCHEMA, orient="row",
    )
    sources = _sources(observations=observations)
    report = validate(sources, B.build_datasets(sources, []).frames, [])
    assert not report.ok
    assert _finding(report, "settlement_mismatches").count >= 1


def test_duplicate_join_grain_is_an_error() -> None:
    dup_panel = pl.DataFrame(
        {"station_id": ["NYC", "NYC"], "target_date": [date(2026, 7, 20), date(2026, 7, 20)]}
    )
    report = validate(_sources(), {"weather_panel": dup_panel}, [])
    assert not report.ok
    assert _finding(report, "duplicate_join_weather_panel").count == 1


def test_missing_forecast_and_missing_observation_are_warnings() -> None:
    # forecast-only day
    forecasts = pl.DataFrame(
        [{"station_id": "NYC", "target_date": date(2026, 7, 20), "point_estimate": 80.0,
          "issue_time": datetime(2026, 7, 18, 12), "valid_start": datetime(2026, 7, 20, 16),
          "valid_end": datetime(2026, 7, 20, 23), "raw_payload_id": 1}],
        schema=B.FORECASTS_SCHEMA, orient="row",
    )
    # observation-only day (different date)
    observations = pl.DataFrame(
        [{"station_id": "NYC", "variable": "tmax_f", "value": 81.0,
          "observation_date": date(2026, 7, 25), "issuance_time": datetime(2026, 7, 26, 6),
          "raw_payload_id": 1}],
        schema=B.OBSERVATIONS_SCHEMA, orient="row",
    )
    sources = _sources(forecasts=forecasts, observations=observations)
    report = validate(sources, B.build_datasets(sources, []).frames, [])
    assert report.ok  # warnings only
    assert _finding(report, "missing_weather_observations").count == 1
    assert _finding(report, "missing_forecasts").count == 1


def test_orphan_market_and_station_warnings() -> None:
    markets = pl.DataFrame(
        [{"market_ticker": "UNMAPPED", "event_ticker": "E", "status": "open",
          "yes_bid_cents": 40, "yes_ask_cents": 45, "last_price_cents": 42, "volume": 1,
          "open_interest": 1, "rules_primary": "r", "observed_at": datetime(2026, 7, 18, 12),
          "raw_payload_id": 1}],
        schema=B.MARKETS_SCHEMA, orient="row",
    )
    observations = pl.DataFrame(
        [{"station_id": "LAX", "variable": "tmax_f", "value": 81.0,
          "observation_date": date(2026, 7, 20), "issuance_time": datetime(2026, 7, 21, 6),
          "raw_payload_id": 1}],
        schema=B.OBSERVATIONS_SCHEMA, orient="row",
    )
    sources = _sources(markets=markets, observations=observations)
    mappings: list[MarketMapping] = []
    report = validate(sources, B.build_datasets(sources, mappings).frames, mappings)
    assert _finding(report, "orphaned_market_records").count == 1
    assert _finding(report, "orphaned_weather_records").count == 1
