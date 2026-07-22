from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.observatory.integrity import (
    count_duplicate_candlesticks,
    count_duplicate_forecasts,
    count_duplicate_market_snapshots,
    find_monotonicity_violations,
    load_id_timestamp_pairs,
)
from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import save_market_snapshot

BASE = datetime(2026, 7, 22, 0, 0, 0)


def test_find_monotonicity_violations_empty_input() -> None:
    assert find_monotonicity_violations([], stream="market") == []


def test_find_monotonicity_violations_strictly_ascending_is_clean() -> None:
    rows = [(1, BASE), (2, BASE + timedelta(minutes=5)), (3, BASE + timedelta(minutes=10))]
    assert find_monotonicity_violations(rows, stream="market") == []


def test_find_monotonicity_violations_small_reorder_within_tolerance_is_clean() -> None:
    # Row 2's timestamp is 3 minutes behind row 1's -- within the 10-min default.
    rows = [(1, BASE), (2, BASE - timedelta(minutes=3)), (3, BASE + timedelta(minutes=5))]
    assert find_monotonicity_violations(rows, stream="market") == []


def test_find_monotonicity_violations_detects_large_regression() -> None:
    rows = [(1, BASE), (2, BASE - timedelta(minutes=30))]
    violations = find_monotonicity_violations(rows, stream="market")
    assert len(violations) == 1
    assert violations[0].row_id == 2
    assert violations[0].running_max_before == BASE
    assert violations[0].regression == timedelta(minutes=30)


def test_find_monotonicity_violations_sorts_by_id_not_input_order() -> None:
    rows = [(2, BASE - timedelta(minutes=30)), (1, BASE)]
    violations = find_monotonicity_violations(rows, stream="market")
    assert len(violations) == 1
    assert violations[0].row_id == 2


def test_find_monotonicity_violations_custom_tolerance() -> None:
    rows = [(1, BASE), (2, BASE - timedelta(minutes=3))]
    assert find_monotonicity_violations(rows, stream="market", tolerance=timedelta(minutes=1))
    assert not find_monotonicity_violations(rows, stream="market", tolerance=timedelta(minutes=5))


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


async def _snapshot(session: AsyncSession, ticker: str, price: int) -> None:
    await save_market_snapshot(
        session,
        market_ticker=ticker,
        event_ticker=None,
        market_type=None,
        title=None,
        subtitle=None,
        status=None,
        yes_bid_cents=None,
        yes_ask_cents=None,
        last_price_cents=price,
        volume=None,
        open_interest=None,
        close_time=None,
        rules_primary=None,
        rules_secondary=None,
        raw_payload_id=None,
    )


async def test_count_duplicate_market_snapshots_none_when_deduped_on_write(
    session: AsyncSession,
) -> None:
    # save_market_snapshot's own content-hash dedup means two calls with the
    # same content never produce two distinct rows -- so an honest exercise
    # of this check needs genuinely distinct content, which never collides.
    await _snapshot(session, "KXTICKER-A", 50)
    await _snapshot(session, "KXTICKER-A", 51)
    assert await count_duplicate_market_snapshots(session) == 0


async def test_count_duplicate_candlesticks_empty_store(session: AsyncSession) -> None:
    assert await count_duplicate_candlesticks(session) == 0


async def test_count_duplicate_forecasts_empty_store(session: AsyncSession) -> None:
    assert await count_duplicate_forecasts(session) == 0


async def test_load_id_timestamp_pairs_market(session: AsyncSession) -> None:
    await _snapshot(session, "KXTICKER-A", 50)
    await _snapshot(session, "KXTICKER-A", 51)
    pairs = await load_id_timestamp_pairs(session, "market")
    assert len(pairs) == 2
    assert [p[0] for p in pairs] == sorted(p[0] for p in pairs)


async def test_load_id_timestamp_pairs_unknown_stream_raises(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        await load_id_timestamp_pairs(session, "bogus")
