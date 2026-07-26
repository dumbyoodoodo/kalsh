"""PostgreSQL-backed proof of the canonical dataset's provenance filtering
(ADR 0014). SQLite cannot stand in for research-dataset correctness, so this
seeds demo/production/NULL rows in a throwaway PostgreSQL database and asserts,
through the real ``load_source_frames`` loader, that:

- production liquidity is included;
- demo and NULL order books / trades are dropped;
- demo/NULL snapshot & candlestick volume/OI become NULL (missing, never zero);
- NULL candlestick *prices* are kept (deterministic production);
- environment-invariant identity/result survive regardless of environment.
"""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kalshi_weather.dataset.builder import load_source_frames
from kalshi_weather.dataset.provenance import EnvironmentPolicy, ProvenanceCoverage
from kalshi_weather.storage.models import (
    Base,
    MarketCandlestick,
    MarketSnapshot,
    OrderbookSnapshot,
    TradeRecord,
)

pytestmark = pytest.mark.integration

_TEST_DB = "kalshi_canonical_it"
CLOSE = datetime(2026, 7, 26, 14, 0, tzinfo=UTC)
OBS = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _with_db(url: str, db: str) -> str:
    return make_url(url).set(database=db).render_as_string(hide_password=False)


@pytest.fixture
def throwaway_db():  # type: ignore[no-untyped-def]
    from kalshi_weather.config import get_settings

    real = str(get_settings().database_url)
    assert make_url(real).database != _TEST_DB
    admin = _with_db(real, "postgres")
    try:
        eng = create_engine(admin, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            c.execute(text("select 1"))
    except Exception:
        pytest.skip("no reachable PostgreSQL server for integration test")
    with eng.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DB}"))
        c.execute(text(f"CREATE DATABASE {_TEST_DB}"))
    prev = os.environ.get("DATABASE_URL")
    try:
        yield _with_db(admin, _TEST_DB)
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
        with eng.connect() as c:
            c.execute(
                text(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where datname=:d and pid<>pg_backend_pid()"
                ),
                {"d": _TEST_DB},
            )
            c.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DB}"))
        eng.dispose()


def _snap(ticker: str, env: str | None, volume: int) -> MarketSnapshot:
    return MarketSnapshot(
        market_ticker=ticker,
        event_ticker="E",
        market_type="binary",
        title="t",
        status="finalized",
        yes_bid_cents=40,
        yes_ask_cents=60,
        last_price_cents=50,
        volume=volume,
        open_interest=volume,
        close_time=CLOSE,
        rules_primary="r",
        observed_at=OBS,
        result="yes",
        environment=env,
    )


def _candle(ticker: str, env: str | None, volume: int) -> MarketCandlestick:
    return MarketCandlestick(
        market_ticker=ticker,
        series_ticker="S",
        period_interval_seconds=60,
        period_start=OBS,
        period_end=OBS,
        price_open_cents=50,
        price_high_cents=55,
        price_low_cents=45,
        price_close_cents=52,
        price_mean_cents=50,
        price_close_is_carried_forward=False,
        yes_bid_open_cents=49,
        yes_bid_high_cents=49,
        yes_bid_low_cents=49,
        yes_bid_close_cents=49,
        yes_ask_open_cents=51,
        yes_ask_high_cents=51,
        yes_ask_low_cents=51,
        yes_ask_close_cents=51,
        volume=volume,
        open_interest=volume,
        observed_at=OBS,
        environment=env,
    )


async def _seed_and_load(url: str, policy: EnvironmentPolicy | None):  # type: ignore[no-untyped-def]
    async_url = url.replace("postgresql://", "postgresql+psycopg://")
    engine = create_async_engine(async_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as s:
        # one production, one demo, one NULL of each liquidity type
        s.add_all(
            [_snap("PROD", "production", 500), _snap("DEMO", "demo", 0), _snap("NUL", None, 0)]
        )
        s.add_all([_candle("PROD", "production", 500), _candle("NUL", None, 0)])
        for tk, env in [("PROD", "production"), ("DEMO", "demo"), ("NUL", None)]:
            s.add(
                OrderbookSnapshot(
                    market_ticker=tk,
                    captured_at=OBS,
                    yes_levels_json=[[40, 5]],
                    no_levels_json=[[60, 5]],
                    best_yes_bid_cents=40,
                    best_yes_ask_cents=60,
                    spread_cents=20,
                    environment=env,
                )
            )
            s.add(
                TradeRecord(
                    trade_id=f"{tk}-1",
                    market_ticker=tk,
                    executed_at=OBS,
                    price_cents=50,
                    count=1,
                    taker_side="yes",
                    environment=env,
                )
            )
        await s.commit()
    cov = ProvenanceCoverage() if policy else None
    async with sf() as s:
        frames = await load_source_frames(s, env_policy=policy, coverage=cov)
    await engine.dispose()
    return frames, cov


@pytest.mark.anyio
async def test_canonical_excludes_demo_and_null_liquidity(throwaway_db: str) -> None:
    frames, cov = await _seed_and_load(throwaway_db, EnvironmentPolicy())

    # order books & trades: production only (demo + NULL dropped)
    assert frames.orderbooks["market_ticker"].to_list() == ["PROD"]
    assert frames.trades["market_ticker"].to_list() == ["PROD"]

    # snapshots: all three rows survive (identity/result invariant), but volume
    # is present only for production; demo/NULL volume is NULL, NOT zero.
    m = frames.markets.sort("market_ticker")
    by = {r["market_ticker"]: r for r in m.to_dicts()}
    assert by["PROD"]["volume"] == 500
    assert by["DEMO"]["volume"] is None, "demo volume must be missing, not 0"
    assert by["NUL"]["volume"] is None, "NULL-provenance volume must be missing, not 0"
    assert {by[t]["result"] for t in ("PROD", "DEMO", "NUL")} == {"yes"}  # result invariant

    # candlesticks: both PROD and NUL kept (price admissible), but NUL volume NULL
    c = {r["market_ticker"]: r for r in frames.candlesticks.to_dicts()}
    assert set(c) == {"PROD", "NUL"}
    assert c["PROD"]["price_close_cents"] == 52 and c["PROD"]["volume"] == 500
    assert c["NUL"]["price_close_cents"] == 52, "NULL candle price kept (deterministic production)"
    assert c["NUL"]["volume"] is None, "NULL candle volume must be missing"

    # coverage reflects the exclusions
    rep = cov.to_report()
    assert rep["liquidity_rows_excluded_non_production"]["trades"] == 2  # demo + NULL


@pytest.mark.anyio
async def test_legacy_mode_unchanged(throwaway_db: str) -> None:
    # env_policy=None -> no filtering: all liquidity rows load as before.
    frames, cov = await _seed_and_load(throwaway_db, None)
    assert cov is None
    assert sorted(frames.orderbooks["market_ticker"].to_list()) == ["DEMO", "NUL", "PROD"]
    assert sorted(frames.trades["market_ticker"].to_list()) == ["DEMO", "NUL", "PROD"]
