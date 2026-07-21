from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import (
    get_latest_trade_timestamp,
    save_event,
    save_market_snapshot,
    save_orderbook_snapshot,
    save_raw_payload,
    save_series,
    save_trade,
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


async def _make_market_snapshot(session: AsyncSession, **overrides: object):  # type: ignore[no-untyped-def]
    defaults: dict[str, object] = {
        "market_ticker": "ABC-1",
        "event_ticker": None,
        "market_type": "binary",
        "title": "title",
        "subtitle": "sub",
        "status": "open",
        "yes_bid_cents": 45,
        "yes_ask_cents": 47,
        "last_price_cents": 46,
        "volume": 10,
        "open_interest": 5,
        "close_time": None,
        "rules_primary": "rules",
        "rules_secondary": None,
        "raw_payload_id": None,
    }
    defaults.update(overrides)
    return await save_market_snapshot(session, **defaults)  # type: ignore[arg-type]


async def test_save_market_snapshot_persists_fields(session: AsyncSession) -> None:
    result = await _make_market_snapshot(session)
    assert result.was_duplicate is False
    assert result.record.id is not None
    assert result.record.market_ticker == "ABC-1"


async def test_save_market_snapshot_skips_exact_duplicate(session: AsyncSession) -> None:
    first = await _make_market_snapshot(session)
    second = await _make_market_snapshot(session)

    assert first.was_duplicate is False
    assert second.was_duplicate is True
    assert second.record.id == first.record.id


async def test_save_market_snapshot_inserts_when_fields_change(session: AsyncSession) -> None:
    first = await _make_market_snapshot(session)
    second = await _make_market_snapshot(session, yes_bid_cents=50)

    assert second.was_duplicate is False
    assert second.record.id != first.record.id


async def test_save_orderbook_snapshot_computes_best_quote(session: AsyncSession) -> None:
    result = await save_orderbook_snapshot(
        session,
        market_ticker="ABC-1",
        yes_levels=[[45, 10]],
        no_levels=[[53, 5]],
        raw_payload_id=None,
    )
    assert result.was_duplicate is False
    assert result.record.best_yes_bid_cents == 45
    assert result.record.best_no_bid_cents == 53
    assert result.record.best_yes_ask_cents == 47
    assert result.record.best_no_ask_cents == 55


async def test_save_orderbook_snapshot_skips_exact_duplicate(session: AsyncSession) -> None:
    first = await save_orderbook_snapshot(
        session, market_ticker="ABC-1", yes_levels=[[45, 10]], no_levels=[], raw_payload_id=None
    )
    second = await save_orderbook_snapshot(
        session, market_ticker="ABC-1", yes_levels=[[45, 10]], no_levels=[], raw_payload_id=None
    )
    third = await save_orderbook_snapshot(
        session, market_ticker="ABC-1", yes_levels=[[46, 10]], no_levels=[], raw_payload_id=None
    )

    assert first.was_duplicate is False
    assert second.was_duplicate is True
    assert second.record.id == first.record.id
    assert third.was_duplicate is False
    assert third.record.id != first.record.id


async def test_save_series_upserts(session: AsyncSession) -> None:
    common = {"series_ticker": "KXHIGHNY", "category": "Climate and Weather", "frequency": "daily"}
    first = await save_series(session, title="t1", **common)  # type: ignore[arg-type]
    second = await save_series(session, title="t2", **common)  # type: ignore[arg-type]
    assert first.series_ticker == second.series_ticker
    assert second.title == "t2"


async def test_save_event_upserts(session: AsyncSession) -> None:
    first = await save_event(
        session,
        event_ticker="KXHIGHNY-26JUL21",
        series_ticker="KXHIGHNY",
        category="Climate and Weather",
        title="t1",
        sub_title="s1",
    )
    second = await save_event(
        session,
        event_ticker="KXHIGHNY-26JUL21",
        series_ticker="KXHIGHNY",
        category="Climate and Weather",
        title="t2",
        sub_title="s2",
    )
    assert first.event_ticker == second.event_ticker
    assert second.title == "t2"
    assert second.sub_title == "s2"


async def test_save_trade_inserts_new(session: AsyncSession) -> None:
    result = await save_trade(
        session,
        trade_id="trade-1",
        market_ticker="ABC-1",
        executed_at=datetime(2026, 7, 20, tzinfo=UTC),
        price_cents=45,
        count=2,
        taker_side="yes",
        raw_payload_id=None,
    )
    assert result.was_duplicate is False
    assert result.record.trade_id == "trade-1"


async def test_save_trade_skips_existing_trade_id(session: AsyncSession) -> None:
    first = await save_trade(
        session,
        trade_id="trade-1",
        market_ticker="ABC-1",
        executed_at=datetime(2026, 7, 20, tzinfo=UTC),
        price_cents=45,
        count=2,
        taker_side="yes",
        raw_payload_id=None,
    )
    second = await save_trade(
        session,
        trade_id="trade-1",
        market_ticker="ABC-1",
        executed_at=datetime(2026, 7, 20, tzinfo=UTC),
        price_cents=99,
        count=99,
        taker_side="no",
        raw_payload_id=None,
    )
    assert second.was_duplicate is True
    assert second.record.price_cents == first.record.price_cents  # not overwritten


async def test_get_latest_trade_timestamp_none_when_no_trades(session: AsyncSession) -> None:
    assert await get_latest_trade_timestamp(session, "ABC-1") is None


async def test_get_latest_trade_timestamp_returns_max(session: AsyncSession) -> None:
    await save_trade(
        session,
        trade_id="t1",
        market_ticker="ABC-1",
        executed_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
        price_cents=45,
        count=1,
        taker_side="yes",
        raw_payload_id=None,
    )
    await save_trade(
        session,
        trade_id="t2",
        market_ticker="ABC-1",
        executed_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        price_cents=46,
        count=1,
        taker_side="yes",
        raw_payload_id=None,
    )
    latest = await get_latest_trade_timestamp(session, "ABC-1")
    assert latest is not None
    assert latest.hour == 12
