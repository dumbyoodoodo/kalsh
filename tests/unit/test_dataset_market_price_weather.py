"""Tests for the market_price_weather dataset frame (Phase 7B): grain, join
correctness, and column preservation. Frames are constructed directly so no
DB is needed (mirrors test_dataset_market_prices.py's style). Leakage proofs
specifically live in test_dataset_leakage.py."""

from datetime import date, datetime

import polars as pl

from kalshi_weather.dataset import builder as B
from kalshi_weather.dataset.market_map import MarketMapping

STATIONS = pl.DataFrame(
    [{"station_id": "NYC", "timezone": "America/New_York"}], schema=B.STATIONS_SCHEMA, orient="row"
)


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi)


def _empty(schema: dict) -> pl.DataFrame:  # type: ignore[type-arg]
    return pl.DataFrame([], schema=schema, orient="row")


def _markets() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "market_ticker": "KXHIGHNY-20", "event_ticker": "KXHIGHNY-26JUL20",
                "status": "finalized",
                "yes_bid_cents": 0, "yes_ask_cents": 1, "last_price_cents": 0, "volume": 100,
                "open_interest": 50, "rules_primary": "r", "observed_at": _dt(2026, 7, 20, 18),
                "raw_payload_id": 1, "close_time": _dt(2026, 7, 21, 4, 59), "result": "yes",
                "expiration_value": 85.0, "settlement_ts": _dt(2026, 7, 21, 12, 0),
                "floor_strike": 84.0, "cap_strike": 85.0, "strike_type": "between",
            },
            {
                "market_ticker": "UNMAPPED-20", "event_ticker": "OTHER-26JUL20",
                "status": "finalized",
                "yes_bid_cents": 0, "yes_ask_cents": 1, "last_price_cents": 0, "volume": 10,
                "open_interest": 5, "rules_primary": "r", "observed_at": _dt(2026, 7, 20, 18),
                "raw_payload_id": 2, "close_time": _dt(2026, 7, 21, 4, 59), "result": "no",
                "expiration_value": 10.0, "settlement_ts": _dt(2026, 7, 21, 12, 0),
                "floor_strike": 9.0, "cap_strike": None, "strike_type": "greater",
            },
        ],
        schema=B.MARKETS_SCHEMA,
        orient="row",
    )


def _candle(ticker: str, pe: datetime, *, volume: int = 10) -> dict:  # type: ignore[type-arg]
    return {
        "market_ticker": ticker,
        "period_interval_seconds": 60,
        "period_start": pe,
        "period_end": pe,
        "price_open_cents": 80, "price_high_cents": 82, "price_low_cents": 79,
        "price_close_cents": 81, "price_mean_cents": 80,
        "price_close_is_carried_forward": False,
        "yes_bid_open_cents": 79, "yes_bid_high_cents": 80, "yes_bid_low_cents": 78,
        "yes_bid_close_cents": 79,
        "yes_ask_open_cents": 82, "yes_ask_high_cents": 83, "yes_ask_low_cents": 80,
        "yes_ask_close_cents": 82,
        "volume": volume, "open_interest": 50, "raw_payload_id": 10,
    }


def _observations() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {"station_id": "NYC", "variable": "tmax_f", "value": 79.0,
             "observation_date": date(2026, 7, 20), "issuance_time": _dt(2026, 7, 20, 20),
             "raw_payload_id": 100},
            {"station_id": "NYC", "variable": "tmax_f", "value": 81.0,
             "observation_date": date(2026, 7, 20), "issuance_time": _dt(2026, 7, 21, 6),
             "raw_payload_id": 101},
            {"station_id": "NYC", "variable": "tmin_f", "value": 63.0,
             "observation_date": date(2026, 7, 20), "issuance_time": _dt(2026, 7, 21, 6),
             "raw_payload_id": 102},
        ],
        schema=B.OBSERVATIONS_SCHEMA,
        orient="row",
    )


def _sources(**overrides: pl.DataFrame) -> B.SourceFrames:
    base = {
        "stations": STATIONS,
        "markets": _markets(),
        "orderbooks": _empty(B.ORDERBOOKS_SCHEMA),
        "trades": _empty(B.TRADES_SCHEMA),
        "forecasts": _empty(B.FORECASTS_SCHEMA),
        "observations": _observations(),
        "candlesticks": pl.DataFrame(
            [
                _candle("KXHIGHNY-20", _dt(2026, 7, 20, 18, 1)),
                _candle("KXHIGHNY-20", _dt(2026, 7, 21, 3, 1)),
                _candle("UNMAPPED-20", _dt(2026, 7, 20, 18, 1)),
            ],
            schema=B.CANDLESTICKS_SCHEMA,
            orient="row",
        ),
    }
    base.update(overrides)
    return B.SourceFrames(**base)  # type: ignore[arg-type]


_MAP = [MarketMapping("KXHIGHNY-20", "NYC", "tmax_f", date(2026, 7, 20))]


def test_grain_is_one_row_per_candle_no_fanout() -> None:
    frame = B.build_market_price_weather(_sources(), _MAP)
    assert frame.height == 2  # only KXHIGHNY-20's two candles


def test_unmapped_market_excluded_same_convention_as_market_weather() -> None:
    frame = B.build_market_price_weather(_sources(), _MAP)
    assert "UNMAPPED-20" not in frame["market_ticker"].to_list()


def test_no_mappings_yields_empty_frame_not_an_error() -> None:
    frame = B.build_market_price_weather(_sources(), [])
    assert frame.height == 0
    # every documented output column is still present, correctly typed
    for col in B.MARKET_PRICE_WEATHER_EXTRA_SCHEMA:
        assert col in frame.columns


def test_preserves_market_metadata_and_strike() -> None:
    frame = B.build_market_price_weather(_sources(), _MAP)
    row = frame.row(0, named=True)
    assert row["event_ticker"] == "KXHIGHNY-26JUL20"
    assert row["strike_type"] == "between"
    assert row["floor_strike"] == 84.0
    assert row["cap_strike"] == 85.0


def test_preserves_price_side_columns_and_data_quality_status() -> None:
    frame = B.build_market_price_weather(_sources(), _MAP)
    row = frame.row(0, named=True)
    assert row["price_close_cents"] == 81
    assert row["data_quality_status"] == "ok"


def test_observation_progression_matches_the_markets_own_variable() -> None:
    frame = B.build_market_price_weather(_sources(), _MAP).sort("period_end")
    early = frame.row(0, named=True)  # 18:01 on 7-20, before the 20:00 preliminary
    later = frame.row(1, named=True)  # 03:01 on 7-21, after it
    assert early["obs_value_known"] is None
    assert early["observation_quality_status"] == "unknown"
    assert later["obs_value_known"] == 79.0
    assert later["observation_quality_status"] == "known"
    expected_age = (_dt(2026, 7, 21, 3, 1) - _dt(2026, 7, 20, 20)).total_seconds()
    assert later["obs_age_seconds"] == expected_age


def test_running_extremes_attached_regardless_of_the_markets_own_variable() -> None:
    """KXHIGHNY-20 settles on tmax_f, but running_tmin_f_known must still be
    populated (once its own issuance -- 06:00 on 7-21 -- has occurred) --
    both extremes are attached to every mapped market, not just the market's
    own settlement variable."""
    after_tmin_issuance = pl.DataFrame(
        [_candle("KXHIGHNY-20", _dt(2026, 7, 21, 7, 1))], schema=B.CANDLESTICKS_SCHEMA, orient="row"
    )
    grown = pl.concat([_sources().candlesticks, after_tmin_issuance])
    frame = B.build_market_price_weather(_sources(candlesticks=grown), _MAP).sort("period_end")
    later = frame.row(-1, named=True)
    assert later["running_tmax_f_known"] == 81.0  # its own final issuance, also at 06:00
    assert later["running_tmin_f_known"] == 63.0


def test_theoretical_remaining_range_is_zero_only_once_locked() -> None:
    frame = B.build_market_price_weather(_sources(), _MAP).sort("period_end")
    early = frame.row(0, named=True)  # still 7-20 locally -> not locked
    assert early["tmax_locked"] is False
    assert early["theoretical_remaining_range_high_f"] is None

    locked_candle = pl.DataFrame(
        [_candle("KXHIGHNY-20", _dt(2026, 7, 21, 5, 1))], schema=B.CANDLESTICKS_SCHEMA, orient="row"
    )
    grown = pl.concat([_sources().candlesticks, locked_candle])
    frame2 = B.build_market_price_weather(_sources(candlesticks=grown), _MAP).sort("period_end")
    late = frame2.row(-1, named=True)  # 01:01 local on 7-21 -> locked
    assert late["tmax_locked"] is True
    assert late["theoretical_remaining_range_high_f"] == 0.0


def test_empty_sources_build_without_error() -> None:
    empty = B.SourceFrames(
        STATIONS,
        _empty(B.MARKETS_SCHEMA),
        _empty(B.ORDERBOOKS_SCHEMA),
        _empty(B.TRADES_SCHEMA),
        _empty(B.FORECASTS_SCHEMA),
        _empty(B.OBSERVATIONS_SCHEMA),
        _empty(B.CANDLESTICKS_SCHEMA),
    )
    frame = B.build_market_price_weather(empty, [])
    assert frame.height == 0


def test_registered_in_build_datasets_dispatch() -> None:
    built = B.build_datasets(_sources(), _MAP, which="market_price_weather")
    assert set(built.frames) == {"market_price_weather"}
    assert built.frames["market_price_weather"].height == 2
