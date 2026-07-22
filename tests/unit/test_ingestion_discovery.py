import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.config import Environment
from kalshi_weather.ingestion.discovery import discover_and_snapshot_weather_markets
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.storage.models import Base, EventRecord, MarketSnapshot, SeriesRecord

SERIES_TICKER = "KXHIGHNY"
EVENT_TICKER = "KXHIGHNY-26JUL21"
VALID_MARKET_TICKER = "KXHIGHNY-26JUL21-T85"


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


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/series"):
        return httpx.Response(
            200,
            json={
                "series": [
                    {
                        "ticker": SERIES_TICKER,
                        "category": "Climate and Weather",
                        "title": "Highest temp NYC",
                        "frequency": "daily",
                    }
                ],
                "cursor": "",
            },
        )
    if path.endswith("/events"):
        return httpx.Response(
            200,
            json={
                "events": [
                    {
                        "event_ticker": EVENT_TICKER,
                        "series_ticker": SERIES_TICKER,
                        "category": "Climate and Weather",
                        "title": "Highest temp NYC on Jul 21",
                        "sub_title": "Jul 21, 2026",
                    }
                ],
                "cursor": "",
            },
        )
    if path.endswith("/markets"):
        return httpx.Response(
            200,
            json={
                "markets": [
                    {
                        "ticker": VALID_MARKET_TICKER,
                        "event_ticker": EVENT_TICKER,
                        "market_type": "binary",
                        "title": "85F or above",
                        "subtitle": "85F or above",
                        "status": "open",
                        "yes_bid": 42,
                        "yes_ask": 47,
                        "no_bid": 53,
                        "no_ask": 58,
                        "last_price": 45,
                        "volume": 10,
                        "open_interest": 5,
                        "close_time": "2026-07-21T23:59:00Z",
                        "rules_primary": "rules",
                    },
                    {
                        # invalid: empty ticker -- must be skipped, not crash discovery
                        "ticker": "",
                        "event_ticker": EVENT_TICKER,
                        "status": "open",
                    },
                    {
                        # invalid: yes_bid out of the valid [0, 100] range
                        "ticker": "KXHIGHNY-26JUL21-T90",
                        "event_ticker": EVENT_TICKER,
                        "status": "open",
                        "yes_bid": 500,
                    },
                ],
                "cursor": "",
            },
        )
    raise AssertionError(f"unexpected path in discovery test: {path}")


def _client() -> KalshiClient:
    return KalshiClient(
        base_url="https://example.invalid/trade-api/v2",
        environment=Environment.DEVELOPMENT,
        transport=httpx.MockTransport(_handler),
    )


async def test_discovery_persists_series_events_and_valid_markets(session: AsyncSession) -> None:
    async with _client() as client:
        result = await discover_and_snapshot_weather_markets(
            client, session, category="Climate and Weather", market_status="open"
        )

    assert result.market_tickers == [VALID_MARKET_TICKER]
    assert result.market_snapshots_saved == 1
    assert result.market_snapshots_duplicate == 0
    assert result.invalid_items == 2  # empty ticker + out-of-range yes_bid

    series = await session.get(SeriesRecord, SERIES_TICKER)
    assert series is not None
    assert series.category == "Climate and Weather"

    event = await session.get(EventRecord, EVENT_TICKER)
    assert event is not None
    assert event.series_ticker == SERIES_TICKER
    assert event.sub_title == "Jul 21, 2026"

    snapshot = await session.scalar(
        select(MarketSnapshot).where(MarketSnapshot.market_ticker == VALID_MARKET_TICKER)
    )
    assert snapshot is not None
    assert snapshot.yes_bid_cents == 42


async def test_discovery_second_run_deduplicates_unchanged_market(session: AsyncSession) -> None:
    async with _client() as client:
        await discover_and_snapshot_weather_markets(
            client, session, category="Climate and Weather", market_status="open"
        )
    async with _client() as client:
        result = await discover_and_snapshot_weather_markets(
            client, session, category="Climate and Weather", market_status="open"
        )

    assert result.market_snapshots_saved == 0
    assert result.market_snapshots_duplicate == 1


async def test_discovery_filters_events_by_status(session: AsyncSession) -> None:
    """An unfiltered /events call returns a recurring series' entire history
    (confirmed live: 185 events vs. 2 with status="open" for one weather
    series). Discovery must pass market_status through to list_events."""
    seen_events_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            seen_events_params.append(dict(request.url.params))
            return httpx.Response(200, json={"events": [], "cursor": ""})
        return _handler(request)

    async with KalshiClient(
        base_url="https://example.invalid/trade-api/v2",
        environment=Environment.DEVELOPMENT,
        transport=httpx.MockTransport(handler),
    ) as client:
        await discover_and_snapshot_weather_markets(
            client, session, category="Climate and Weather", market_status="open"
        )

    assert seen_events_params
    assert all(p.get("status") == "open" for p in seen_events_params)


async def test_discovery_tolerates_non_numeric_expiration_value(session: AsyncSession) -> None:
    """Operational-restoration regression (2026-07-21): a non-temperature
    climate market carrying a non-numeric expiration_value (e.g. "" or a
    categorical outcome) must be stored with expiration_value=None -- not
    abort the entire discovery cycle, which is exactly what happened on
    the first category-wide authenticated run and why collection never
    got past NYC."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/markets"):
            return httpx.Response(
                200,
                json={
                    "markets": [
                        {
                            "ticker": "KXRAINLAXM-26JUL-X",
                            "event_ticker": EVENT_TICKER,
                            "market_type": "binary",
                            "status": "settled",
                            "yes_bid": 1,
                            "yes_ask": 2,
                            "close_time": "2026-07-21T23:59:00Z",
                            "expiration_value": "",  # the real-world crash input
                        },
                        {
                            "ticker": VALID_MARKET_TICKER,
                            "event_ticker": EVENT_TICKER,
                            "market_type": "binary",
                            "status": "settled",
                            "yes_bid": 42,
                            "yes_ask": 47,
                            "close_time": "2026-07-21T23:59:00Z",
                            "expiration_value": "85.0",  # numeric: must still parse
                        },
                    ],
                    "cursor": "",
                },
            )
        return _handler(request)

    client = KalshiClient(
        base_url="https://example.invalid/trade-api/v2",
        environment=Environment.DEVELOPMENT,
        transport=httpx.MockTransport(handler),
    )
    result = await discover_and_snapshot_weather_markets(
        client, session, category="Climate and Weather", market_status="settled"
    )
    assert result.market_snapshots_saved == 2  # both stored; neither aborted the cycle

    rows = (await session.execute(select(MarketSnapshot))).scalars().all()
    by_ticker = {r.market_ticker: r for r in rows}
    assert by_ticker["KXRAINLAXM-26JUL-X"].expiration_value is None
    assert by_ticker[VALID_MARKET_TICKER].expiration_value is not None
