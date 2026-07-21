"""Builder tests, centered on point-in-time correctness (the highest-priority
guarantee of this phase). Frames are constructed directly so no DB is needed."""

from datetime import date, datetime

import polars as pl

from kalshi_weather.dataset import builder as B
from kalshi_weather.dataset.market_map import MarketMapping


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi)


STATIONS = pl.DataFrame(
    [{"station_id": "NYC", "timezone": "America/New_York"}],
    schema=B.STATIONS_SCHEMA,
    orient="row",
)


def _forecasts() -> pl.DataFrame:
    return pl.DataFrame(
        [
            # issuance 1 (7-18): day period high 80, night period low 64
            {"station_id": "NYC", "target_date": date(2026, 7, 20), "point_estimate": 80.0,
             "issue_time": _dt(2026, 7, 18, 12), "valid_start": _dt(2026, 7, 20, 16),
             "valid_end": _dt(2026, 7, 20, 23), "raw_payload_id": 1},
            {"station_id": "NYC", "target_date": date(2026, 7, 20), "point_estimate": 64.0,
             "issue_time": _dt(2026, 7, 18, 12), "valid_start": _dt(2026, 7, 20, 4),
             "valid_end": _dt(2026, 7, 20, 10), "raw_payload_id": 1},
            # issuance 2 (7-19): revised day high 83
            {"station_id": "NYC", "target_date": date(2026, 7, 20), "point_estimate": 83.0,
             "issue_time": _dt(2026, 7, 19, 12), "valid_start": _dt(2026, 7, 20, 16),
             "valid_end": _dt(2026, 7, 20, 23), "raw_payload_id": 2},
        ],
        schema=B.FORECASTS_SCHEMA,
        orient="row",
    )


def _observations() -> pl.DataFrame:
    return pl.DataFrame(
        [
            # provisional tmax 79 (issued same day), then final tmax 81 (next morning)
            {"station_id": "NYC", "variable": "tmax_f", "value": 79.0,
             "observation_date": date(2026, 7, 20), "issuance_time": _dt(2026, 7, 20, 20),
             "raw_payload_id": 10},
            {"station_id": "NYC", "variable": "tmax_f", "value": 81.0,
             "observation_date": date(2026, 7, 20), "issuance_time": _dt(2026, 7, 21, 6),
             "raw_payload_id": 11},
            {"station_id": "NYC", "variable": "tmin_f", "value": 63.0,
             "observation_date": date(2026, 7, 20), "issuance_time": _dt(2026, 7, 21, 6),
             "raw_payload_id": 11},
        ],
        schema=B.OBSERVATIONS_SCHEMA,
        orient="row",
    )


def _markets() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {"market_ticker": "KXHIGHNY-20", "event_ticker": "E", "status": "open",
             "yes_bid_cents": 40, "yes_ask_cents": 45, "last_price_cents": 42, "volume": 10,
             "open_interest": 5, "rules_primary": "r", "observed_at": _dt(2026, 7, 18, 18),
             "raw_payload_id": 100},
            {"market_ticker": "KXHIGHNY-20", "event_ticker": "E", "status": "open",
             "yes_bid_cents": 48, "yes_ask_cents": 52, "last_price_cents": 50, "volume": 20,
             "open_interest": 8, "rules_primary": "r", "observed_at": _dt(2026, 7, 19, 18),
             "raw_payload_id": 101},
        ],
        schema=B.MARKETS_SCHEMA,
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
        "forecasts": _forecasts(),
        "observations": _observations(),
    }
    base.update(overrides)
    return B.SourceFrames(**base)  # type: ignore[arg-type]


def test_weather_panel_settles_on_latest_issuance_and_computes_residuals() -> None:
    panel = B.build_weather_panel(_sources())
    row = panel.row(0, named=True)
    # settled value is the LATEST issuance (81), not the provisional 79
    assert row["settled_tmax_f"] == 81.0
    assert row["settled_tmin_f"] == 63.0
    assert row["n_observation_issuances"] == 3
    # final forecast is the 7-19 issuance: high 83, low null (that issuance had
    # no night period) -> residual_high = 83 - 81 = 2
    assert row["final_forecast_high_f"] == 83.0
    assert row["residual_high_f"] == 2.0
    assert row["n_forecast_issuances"] == 2


def test_market_weather_asof_never_leaks_a_future_forecast() -> None:
    market_map = [MarketMapping("KXHIGHNY-20", "NYC", "tmax_f", date(2026, 7, 20))]
    mw = B.build_market_weather(_sources(markets=_markets()), market_map)

    early = mw.filter(pl.col("observed_at") == _dt(2026, 7, 18, 18)).row(0, named=True)
    # only the 7-18 forecast is known at 7-18; the 7-19 revision (83) must not leak
    assert early["forecast_high_f"] == 80.0
    assert early["n_forecast_issuances_known"] == 1
    # the observation isn't issued until 7-20, so nothing is known-observed yet
    assert early["obs_value_known"] is None
    # but the post-settlement label is present (final tmax)
    assert early["settled_value"] == 81.0

    late = mw.filter(pl.col("observed_at") == _dt(2026, 7, 19, 18)).row(0, named=True)
    assert late["forecast_high_f"] == 83.0  # the revision is now knowable
    assert late["n_forecast_issuances_known"] == 2


def test_market_weather_asof_trades_and_orderbook_are_point_in_time() -> None:
    orderbooks = pl.DataFrame(
        [{"market_ticker": "KXHIGHNY-20", "captured_at": _dt(2026, 7, 18, 17),
          "best_yes_bid_cents": 39, "best_yes_ask_cents": 46, "spread_cents": 7}],
        schema=B.ORDERBOOKS_SCHEMA, orient="row",
    )
    trades = pl.DataFrame(
        [
            {"market_ticker": "KXHIGHNY-20", "executed_at": _dt(2026, 7, 18, 17, 30),
             "price_cents": 41},
            {"market_ticker": "KXHIGHNY-20", "executed_at": _dt(2026, 7, 19, 10),
             "price_cents": 49},
        ],
        schema=B.TRADES_SCHEMA, orient="row",
    )
    market_map = [MarketMapping("KXHIGHNY-20", "NYC", "tmax_f", date(2026, 7, 20))]
    mw = B.build_market_weather(
        _sources(markets=_markets(), orderbooks=orderbooks, trades=trades), market_map
    )
    early = mw.filter(pl.col("observed_at") == _dt(2026, 7, 18, 18)).row(0, named=True)
    assert early["n_trades_known"] == 1  # only the 17:30 trade
    assert early["last_trade_price_cents"] == 41
    assert early["best_yes_bid_cents"] == 39
    late = mw.filter(pl.col("observed_at") == _dt(2026, 7, 19, 18)).row(0, named=True)
    assert late["n_trades_known"] == 2
    assert late["last_trade_price_cents"] == 49


def test_unmapped_market_is_excluded_and_reported_as_orphan() -> None:
    sources = _sources(markets=_markets())
    mw = B.build_market_weather(sources, [])  # no mapping
    assert mw.height == 0
    assert B.orphan_market_tickers(sources, []) == ["KXHIGHNY-20"]


def test_orphan_station_detected_when_not_in_registry() -> None:
    obs = _observations().with_columns(pl.lit("LAX").alias("station_id"))
    sources = _sources(observations=obs)
    assert "LAX" in B.orphan_station_ids(sources)


def test_empty_sources_build_without_error() -> None:
    empty = B.SourceFrames(
        STATIONS,
        _empty(B.MARKETS_SCHEMA),
        _empty(B.ORDERBOOKS_SCHEMA),
        _empty(B.TRADES_SCHEMA),
        _empty(B.FORECASTS_SCHEMA),
        _empty(B.OBSERVATIONS_SCHEMA),
    )
    assert B.build_weather_panel(empty).height == 0
    assert B.build_market_weather(empty, []).height == 0


def test_build_datasets_selection() -> None:
    built = B.build_datasets(_sources(), [], which="weather_panel")
    assert set(built.frames) == {"weather_panel"}
    built_all = B.build_datasets(_sources(markets=_markets()), [], which="all")
    assert set(built_all.frames) == {"weather_panel", "market_weather"}
