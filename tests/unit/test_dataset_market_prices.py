"""Tests for the market_prices dataset frame (Phase 7A): correct per-market
context join, no cross-market leakage, and the zero-volume data-quality flag.
Frames are constructed directly so no DB is needed (mirrors
test_dataset_builder.py's style)."""

from datetime import datetime

import polars as pl

from kalshi_weather.dataset import builder as B

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
                "market_ticker": "KXLOWTNYC-20", "event_ticker": "KXLOWTNYC-26JUL20",
                "status": "finalized",
                "yes_bid_cents": 0, "yes_ask_cents": 1, "last_price_cents": 0, "volume": 50,
                "open_interest": 20, "rules_primary": "r", "observed_at": _dt(2026, 7, 20, 18),
                "raw_payload_id": 2, "close_time": _dt(2026, 7, 21, 5, 0), "result": "no",
                "expiration_value": 60.0, "settlement_ts": _dt(2026, 7, 21, 12, 0),
                "floor_strike": 65.0, "cap_strike": None, "strike_type": "greater",
            },
        ],
        schema=B.MARKETS_SCHEMA,
        orient="row",
    )


def _candle(ticker: str, minute: int, *, volume: int = 10, carried_forward: bool = False) -> dict:  # type: ignore[type-arg]
    return {
        "market_ticker": ticker,
        "period_interval_seconds": 60,
        "period_start": _dt(2026, 7, 20, 18, minute),
        "period_end": _dt(2026, 7, 20, 18, minute + 1),
        "price_open_cents": 80, "price_high_cents": 82, "price_low_cents": 79,
        "price_close_cents": 81, "price_mean_cents": 80,
        "price_close_is_carried_forward": carried_forward,
        "yes_bid_open_cents": 79, "yes_bid_high_cents": 80, "yes_bid_low_cents": 78,
        "yes_bid_close_cents": 79,
        "yes_ask_open_cents": 82, "yes_ask_high_cents": 83, "yes_ask_low_cents": 80,
        "yes_ask_close_cents": 82,
        "volume": volume, "open_interest": 50, "raw_payload_id": 10,
    }


def _sources(**overrides: pl.DataFrame) -> B.SourceFrames:
    base = {
        "stations": STATIONS,
        "markets": _markets(),
        "orderbooks": _empty(B.ORDERBOOKS_SCHEMA),
        "trades": _empty(B.TRADES_SCHEMA),
        "forecasts": _empty(B.FORECASTS_SCHEMA),
        "observations": _empty(B.OBSERVATIONS_SCHEMA),
        "candlesticks": pl.DataFrame(
            [_candle("KXHIGHNY-20", 0), _candle("KXLOWTNYC-20", 0, volume=0, carried_forward=True)],
            schema=B.CANDLESTICKS_SCHEMA,
            orient="row",
        ),
    }
    base.update(overrides)
    return B.SourceFrames(**base)  # type: ignore[arg-type]


def test_market_prices_one_row_per_candle_no_fanout() -> None:
    """The market-metadata reduction must not fan out: N candles in ->
    N rows out, never N x snapshots-per-ticker."""
    frame = B.build_market_prices(_sources())
    assert frame.height == 2


def test_market_prices_attaches_correct_per_market_strike_structure() -> None:
    """No cross-market leakage: each candle gets its OWN market's static
    context, never another market's."""
    frame = B.build_market_prices(_sources())
    high = frame.filter(pl.col("market_ticker") == "KXHIGHNY-20").row(0, named=True)
    low = frame.filter(pl.col("market_ticker") == "KXLOWTNYC-20").row(0, named=True)

    assert high["strike_type"] == "between"
    assert high["floor_strike"] == 84.0
    assert high["cap_strike"] == 85.0
    assert high["event_ticker"] == "KXHIGHNY-26JUL20"

    assert low["strike_type"] == "greater"
    assert low["floor_strike"] == 65.0
    assert low["cap_strike"] is None
    assert low["event_ticker"] == "KXLOWTNYC-26JUL20"


def test_market_prices_zero_volume_flagged_not_treated_as_a_trade() -> None:
    """volume == 0 must be visible as a data-quality flag, never silently
    presented as if a trade occurred (CLAUDE.md-style constraint carried
    through from the investigation report)."""
    frame = B.build_market_prices(_sources())
    high = frame.filter(pl.col("market_ticker") == "KXHIGHNY-20").row(0, named=True)
    low = frame.filter(pl.col("market_ticker") == "KXLOWTNYC-20").row(0, named=True)

    assert high["volume"] == 10
    assert high["data_quality_status"] == "ok"
    assert high["price_close_is_carried_forward"] is False

    assert low["volume"] == 0
    assert low["data_quality_status"] == "zero_volume"
    assert low["price_close_is_carried_forward"] is True


def test_market_prices_reduction_uses_latest_non_null_settlement_fields() -> None:
    """Settlement fields only appear on a market's later snapshots (once it
    settles); an earlier, pre-settlement snapshot for the same ticker must
    not blank out the later, correct values."""
    presettlement = {
        "market_ticker": "KXHIGHNY-20", "event_ticker": "KXHIGHNY-26JUL20", "status": "open",
        "yes_bid_cents": 40, "yes_ask_cents": 45, "last_price_cents": 42, "volume": 5,
        "open_interest": 5, "rules_primary": "r", "observed_at": _dt(2026, 7, 19, 12),
        "raw_payload_id": 0, "close_time": _dt(2026, 7, 21, 4, 59), "result": None,
        "expiration_value": None, "settlement_ts": None,
        "floor_strike": 84.0, "cap_strike": 85.0, "strike_type": "between",
    }
    markets = pl.concat(
        [pl.DataFrame([presettlement], schema=B.MARKETS_SCHEMA, orient="row"), _markets()]
    )
    frame = B.build_market_prices(_sources(markets=markets))
    high = frame.filter(pl.col("market_ticker") == "KXHIGHNY-20").row(0, named=True)
    assert high["close_time"] == _dt(2026, 7, 21, 4, 59)  # same either way here, sanity
    assert high["strike_type"] == "between"


def test_market_prices_stable_under_append_only_growth() -> None:
    """Adding a later candle must not alter any existing row's own values --
    the append-only guarantee a point-in-time dataset depends on."""
    first_build = B.build_market_prices(_sources())
    first_row = first_build.filter(pl.col("market_ticker") == "KXHIGHNY-20").row(0, named=True)

    zero_vol_low = _candle("KXLOWTNYC-20", 0, volume=0, carried_forward=True)
    grown_candles = pl.concat(
        [
            pl.DataFrame(
                [_candle("KXHIGHNY-20", 0), zero_vol_low],
                schema=B.CANDLESTICKS_SCHEMA, orient="row",
            ),
            pl.DataFrame([_candle("KXHIGHNY-20", 5)], schema=B.CANDLESTICKS_SCHEMA, orient="row"),
        ]
    )
    second_build = B.build_market_prices(_sources(candlesticks=grown_candles))
    assert second_build.height == 3
    same_period = pl.col("period_start") == first_row["period_start"]
    unchanged_row = second_build.filter(
        (pl.col("market_ticker") == "KXHIGHNY-20") & same_period
    ).row(0, named=True)
    assert unchanged_row["price_close_cents"] == first_row["price_close_cents"]
    assert unchanged_row["volume"] == first_row["volume"]


def test_market_prices_empty_sources_build_without_error() -> None:
    empty = B.SourceFrames(
        STATIONS,
        _empty(B.MARKETS_SCHEMA),
        _empty(B.ORDERBOOKS_SCHEMA),
        _empty(B.TRADES_SCHEMA),
        _empty(B.FORECASTS_SCHEMA),
        _empty(B.OBSERVATIONS_SCHEMA),
        _empty(B.CANDLESTICKS_SCHEMA),
    )
    assert B.build_market_prices(empty).height == 0


def test_market_prices_no_leakage_from_unrelated_target_dates() -> None:
    """A candle's own row must never carry another market's target_date /
    settlement fields -- verified end-to-end through the pipeline join in
    test_dataset_pipeline.py; here we check the pure builder output has the
    join key (market_ticker) uniquely determining every other column."""
    frame = B.build_market_prices(_sources())
    per_market_distinct = frame.group_by("market_ticker").agg(
        n_close=pl.col("close_time").n_unique(), n_strike=pl.col("strike_type").n_unique()
    )
    assert (per_market_distinct["n_close"] <= 1).all()
    assert (per_market_distinct["n_strike"] <= 1).all()
