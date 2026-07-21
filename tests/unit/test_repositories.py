from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import (
    get_latest_observation_date,
    get_latest_trade_timestamp,
    save_event,
    save_market_snapshot,
    save_orderbook_snapshot,
    save_raw_payload,
    save_series,
    save_trade,
    save_weather_forecast,
    save_weather_observation,
    save_weather_station,
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


async def _make_weather_station(session: AsyncSession, **overrides: object):  # type: ignore[no-untyped-def]
    defaults: dict[str, object] = {
        "station_id": "NYC",
        "provider": "nws",
        "source_location_code": "NYC",
        "office": "OKX",
        "latitude": Decimal("40.7829"),
        "longitude": Decimal("-73.9654"),
        "name": "Central Park, NY",
        "timezone": "America/New_York",
        "raw_payload_id": None,
    }
    defaults.update(overrides)
    return await save_weather_station(session, **defaults)  # type: ignore[arg-type]


async def test_save_weather_station_upserts(session: AsyncSession) -> None:
    first = await _make_weather_station(session)
    second = await _make_weather_station(session, office="BOX")

    assert first.station_id == second.station_id == "NYC"
    assert second.office == "BOX"


async def _make_weather_observation(session: AsyncSession, **overrides: object):  # type: ignore[no-untyped-def]
    defaults: dict[str, object] = {
        "station_id": "NYC",
        "provider": "nws",
        "variable": "tmax_f",
        "value": Decimal(81),
        "unit": "F",
        "observation_date": date(2026, 7, 20),
        "issuance_time": datetime(2026, 7, 20, 20, 37, tzinfo=UTC),
        "source_product_id": "prod-1",
        "raw_payload_id": None,
    }
    defaults.update(overrides)
    return await save_weather_observation(session, **defaults)  # type: ignore[arg-type]


async def test_save_weather_observation_inserts_new(session: AsyncSession) -> None:
    result = await _make_weather_observation(session)
    assert result.was_duplicate is False
    assert result.record.value == Decimal(81)


async def test_save_weather_observation_skips_same_issuance(session: AsyncSession) -> None:
    first = await _make_weather_observation(session)
    second = await _make_weather_observation(session, value=Decimal(999))

    assert second.was_duplicate is True
    assert second.record.id == first.record.id
    assert second.record.value == first.record.value  # not overwritten


async def test_save_weather_observation_keeps_distinct_issuances(session: AsyncSession) -> None:
    """CLI reports are reissued multiple times per day -- a later issuance
    for the same station/variable must be stored, not treated as a dup."""
    first = await _make_weather_observation(session)
    second = await _make_weather_observation(
        session,
        value=Decimal(83),
        issuance_time=datetime(2026, 7, 21, 6, 0, tzinfo=UTC),
    )

    assert second.was_duplicate is False
    assert second.record.id != first.record.id


async def _make_weather_forecast(session: AsyncSession, **overrides: object):  # type: ignore[no-untyped-def]
    defaults: dict[str, object] = {
        "station_id": "NYC",
        "provider": "nws",
        "variable": "temperature",
        "point_estimate": Decimal(75),
        "unit": "F",
        "issue_time": datetime(2026, 7, 20, 18, 0, tzinfo=UTC),
        "valid_start": datetime(2026, 7, 20, 18, 0, tzinfo=UTC),
        "valid_end": datetime(2026, 7, 21, 6, 0, tzinfo=UTC),
        "raw_payload_id": None,
    }
    defaults.update(overrides)
    return await save_weather_forecast(session, **defaults)  # type: ignore[arg-type]


async def test_save_weather_forecast_inserts_new(session: AsyncSession) -> None:
    result = await _make_weather_forecast(session)
    assert result.was_duplicate is False
    assert result.record.point_estimate == Decimal(75)


async def test_save_weather_forecast_skips_same_issue_and_valid_start(
    session: AsyncSession,
) -> None:
    first = await _make_weather_forecast(session)
    second = await _make_weather_forecast(session, point_estimate=Decimal(999))

    assert second.was_duplicate is True
    assert second.record.id == first.record.id
    assert second.record.point_estimate == first.record.point_estimate  # never overwritten


async def test_save_weather_forecast_keeps_distinct_revisions(session: AsyncSession) -> None:
    """A later forecast issuance for the same validity window must be stored
    alongside the earlier one -- forecasts are never overwritten."""
    first = await _make_weather_forecast(session)
    second = await _make_weather_forecast(
        session,
        point_estimate=Decimal(78),
        issue_time=datetime(2026, 7, 21, 0, 0, tzinfo=UTC),
    )

    assert second.was_duplicate is False
    assert second.record.id != first.record.id


async def test_get_latest_observation_date_none_when_no_observations(
    session: AsyncSession,
) -> None:
    assert await get_latest_observation_date(session, "NYC", "tmax_f") is None


async def test_get_latest_observation_date_returns_max(session: AsyncSession) -> None:
    await _make_weather_observation(
        session,
        observation_date=date(2026, 7, 18),
        issuance_time=datetime(2026, 7, 18, 20, 37, tzinfo=UTC),
    )
    await _make_weather_observation(
        session,
        observation_date=date(2026, 7, 20),
        issuance_time=datetime(2026, 7, 20, 20, 37, tzinfo=UTC),
    )

    latest = await get_latest_observation_date(session, "NYC", "tmax_f")
    assert latest == date(2026, 7, 20)
