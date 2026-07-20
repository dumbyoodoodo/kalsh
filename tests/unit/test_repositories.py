import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import (
    save_market_snapshot,
    save_orderbook_snapshot,
    save_raw_payload,
)


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


async def test_save_raw_payload_deduplicates(session: AsyncSession) -> None:
    payload = {"ticker": "ABC", "value": 1}
    first = await save_raw_payload(
        session,
        source="kalshi_rest",
        endpoint_or_channel="/series",
        request_key="GET:/series:{}",
        http_status=200,
        payload_json=payload,
    )
    second = await save_raw_payload(
        session,
        source="kalshi_rest",
        endpoint_or_channel="/series",
        request_key="GET:/series:{}",
        http_status=200,
        payload_json=payload,
    )
    assert first.id == second.id


async def test_save_market_snapshot_persists_fields(session: AsyncSession) -> None:
    record = await save_market_snapshot(
        session,
        market_ticker="ABC-1",
        event_ticker=None,
        market_type="binary",
        title="title",
        subtitle="sub",
        status="open",
        yes_bid_cents=45,
        yes_ask_cents=47,
        last_price_cents=46,
        volume=10,
        open_interest=5,
        close_time=None,
        rules_primary="rules",
        rules_secondary=None,
        raw_payload_id=None,
    )
    assert record.id is not None
    assert record.market_ticker == "ABC-1"


async def test_save_orderbook_snapshot_computes_best_quote(session: AsyncSession) -> None:
    record = await save_orderbook_snapshot(
        session,
        market_ticker="ABC-1",
        yes_levels=[[45, 10]],
        no_levels=[[53, 5]],
        raw_payload_id=None,
    )
    assert record.best_yes_bid_cents == 45
    assert record.best_no_bid_cents == 53
    assert record.best_yes_ask_cents == 47
    assert record.best_no_ask_cents == 55
