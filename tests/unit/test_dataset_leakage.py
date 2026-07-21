"""Explicit leakage-validation proofs for market_price_weather (Phase 7B,
requirement 4): future observations never appear, finalized/settled values
never leak into pre-settlement weather-progression columns, and a blanket
structural invariant holds over every row of a built frame -- not just a
hand-picked example."""

from datetime import UTC, date, datetime
from decimal import Decimal

import polars as pl
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.dataset import builder as B
from kalshi_weather.dataset import pipeline
from kalshi_weather.dataset.market_map import MarketMapping
from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import (
    save_market_candlestick,
    save_market_snapshot,
    save_weather_observation,
    save_weather_station,
)

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
                "expiration_value": 81.0, "settlement_ts": _dt(2026, 7, 21, 8, 0),
                "floor_strike": 80.0, "cap_strike": 82.0, "strike_type": "between",
            }
        ],
        schema=B.MARKETS_SCHEMA,
        orient="row",
    )


def _candle(pe: datetime) -> dict:  # type: ignore[type-arg]
    return {
        "market_ticker": "KXHIGHNY-20", "period_interval_seconds": 60,
        "period_start": pe, "period_end": pe,
        "price_open_cents": 80, "price_high_cents": 82, "price_low_cents": 79,
        "price_close_cents": 81, "price_mean_cents": 80,
        "price_close_is_carried_forward": False,
        "yes_bid_open_cents": 79, "yes_bid_high_cents": 80, "yes_bid_low_cents": 78,
        "yes_bid_close_cents": 79,
        "yes_ask_open_cents": 82, "yes_ask_high_cents": 83, "yes_ask_low_cents": 80,
        "yes_ask_close_cents": 82,
        "volume": 10, "open_interest": 50, "raw_payload_id": 10,
    }


# provisional tmax (79, same-day), then a big upward revision (81, next
# morning) -- the classic H0007 scenario: settlement pays 81, but nothing
# before the 06:00-on-7/21 issuance is allowed to know that.
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
                _candle(_dt(2026, 7, 20, 18, 1)),  # before the preliminary issuance
                _candle(_dt(2026, 7, 21, 3, 1)),  # after preliminary, before revision, not locked
                _candle(_dt(2026, 7, 21, 5, 1)),  # locked, still before the revision
                _candle(_dt(2026, 7, 21, 7, 1)),  # after the revision
            ],
            schema=B.CANDLESTICKS_SCHEMA,
            orient="row",
        ),
    }
    base.update(overrides)
    return B.SourceFrames(**base)  # type: ignore[arg-type]


_MAP = [MarketMapping("KXHIGHNY-20", "NYC", "tmax_f", date(2026, 7, 20))]


def test_future_observation_never_appears_in_obs_value_known() -> None:
    frame = B.build_market_price_weather(_sources(), _MAP).sort("period_end")
    before_anything = frame.row(0, named=True)  # 18:01 on 7-20
    assert before_anything["obs_value_known"] is None
    assert before_anything["obs_issuance_time_known"] is None


def test_finalized_running_extreme_never_appears_before_its_own_issuance() -> None:
    """The 81 revision issued at 06:00 on 7-21 must be invisible to every
    candle strictly before that instant -- including ones after the
    observation window has already 'locked' (locked only means the window
    closed, not that we've been told the number)."""
    frame = B.build_market_price_weather(_sources(), _MAP).sort("period_end")
    rows = frame.to_dicts()
    before_revision = [r for r in rows if r["period_end"] < _dt(2026, 7, 21, 6)]
    assert len(before_revision) == 3  # 18:01, 03:01, 05:01
    for r in before_revision:
        assert r["running_tmax_f_known"] != 81.0
        assert r["running_tmax_f_known"] in (None, 79.0)

    after_revision = [r for r in rows if r["period_end"] >= _dt(2026, 7, 21, 6)]
    assert len(after_revision) == 1
    assert after_revision[0]["running_tmax_f_known"] == 81.0


def test_locked_does_not_imply_the_final_value_is_already_known() -> None:
    """The 05:01 candle: the observation window has elapsed (locked=True)
    but the 06:00 final issuance hasn't happened yet -- these are
    deliberately independent facts, and locking must never be used as a
    shortcut to reveal the eventual value early."""
    frame = B.build_market_price_weather(_sources(), _MAP).sort("period_end")
    row = frame.filter(pl.col("period_end") == _dt(2026, 7, 21, 5, 1)).row(0, named=True)
    assert row["tmax_locked"] is True
    assert row["running_tmax_f_known"] == 79.0  # not yet 81
    assert row["obs_value_known"] == 79.0


def test_structural_invariant_no_row_ever_has_a_future_known_issuance_time() -> None:
    """Blanket check over every row of a built frame (not a hand-picked
    example): every *_issuance_time_known column, where non-null, must be
    <= that row's own period_end."""
    frame = B.build_market_price_weather(_sources(), _MAP)
    for col in (
        "obs_issuance_time_known",
        "running_tmax_issuance_time_known",
        "running_tmin_issuance_time_known",
    ):
        violations = frame.filter(pl.col(col).is_not_null() & (pl.col(col) > pl.col("period_end")))
        assert violations.height == 0, f"{col} leaked a future issuance in {violations}"


@pytest.fixture
async def session():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_settlement_outcome_never_leaks_into_pre_settlement_weather_progression(
    session: AsyncSession,
) -> None:
    """End-to-end through the real pipeline (DB -> builder -> settlement-
    label join): a market settles at 81 (the label the pipeline attaches to
    every row as an outcome column, exactly like market_weather already
    does), but the weather-PROGRESSION columns for a candle well before the
    revision issuance must reflect only what was truly knowable then (79),
    never silently equal the eventual settlement value."""
    await save_weather_station(
        session, station_id="NYC", provider="nws", source_location_code="NYC", office="OKX",
        latitude=Decimal("40.7829"), longitude=Decimal("-73.9654"), name="Central Park, NY",
        timezone="America/New_York",
    )
    for value, issued in ((Decimal(79), _dt(2026, 7, 20, 20)), (Decimal(81), _dt(2026, 7, 21, 6))):
        await save_weather_observation(
            session, station_id="NYC", provider="nws", variable="tmax_f", value=value,
            unit="F", observation_date=date(2026, 7, 20), issuance_time=issued,
            source_product_id=f"p-{issued.isoformat()}", raw_payload_id=None,
        )
    await save_market_snapshot(
        session, market_ticker="KXHIGHNY-20", event_ticker="E", market_type="binary",
        title="t", subtitle="s", status="finalized", yes_bid_cents=0, yes_ask_cents=1,
        last_price_cents=0, volume=1, open_interest=1,
        close_time=_dt(2026, 7, 21, 4, 59).replace(tzinfo=UTC),
        rules_primary="High temp in Central Park", rules_secondary=None, raw_payload_id=None,
        result="yes", expiration_value=Decimal(81),
        settlement_ts=_dt(2026, 7, 21, 8, 0).replace(tzinfo=UTC),
        floor_strike=Decimal(80), cap_strike=Decimal(82), strike_type="between",
    )
    for pe in (_dt(2026, 7, 20, 18, 1), _dt(2026, 7, 21, 3, 1)):
        await save_market_candlestick(
            session, market_ticker="KXHIGHNY-20", series_ticker="KXHIGHNY",
            period_interval_seconds=60, period_start=pe, period_end=pe,
            price_open_cents=80, price_high_cents=82, price_low_cents=79,
            price_close_cents=81, price_mean_cents=80, price_close_is_carried_forward=False,
            yes_bid_open_cents=79, yes_bid_high_cents=80, yes_bid_low_cents=78,
            yes_bid_close_cents=79, yes_ask_open_cents=82, yes_ask_high_cents=83,
            yes_ask_low_cents=80, yes_ask_close_cents=82, volume=10, open_interest=50,
            raw_payload_id=None,
        )
    await session.flush()

    mapping = [MarketMapping("KXHIGHNY-20", "NYC", "tmax_f", date(2026, 7, 20))]
    output = await pipeline.build(
        session, database_url="sqlite://", mappings=mapping,
        which="market_price_weather", version="v1",
    )
    frame = output.frames["market_price_weather"].sort("period_end")
    assert frame.height == 2
    # the outcome column is present (labelled, post-hoc -- same design as
    # market_weather's settled_value) ...
    assert (frame["value_at_settlement"] == 81.0).all()
    # ... but the point-in-time progression columns never equal it before
    # the revision that actually reveals it was ever issued.
    pre_revision = frame.row(0, named=True)
    assert pre_revision["obs_value_known"] != 81.0
    assert pre_revision["obs_value_known"] in (None, 79.0)
