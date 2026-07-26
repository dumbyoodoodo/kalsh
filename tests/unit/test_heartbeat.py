"""External uptime heartbeat / dead-man's-switch (ops/heartbeat.py).

The whole point is that the ping is WITHHELD whenever the collector isn't
demonstrably healthy, so an external monitor alerts on power-off, wedge, or DB
outage. These tests pin that: a ping fires only on the healthy path, and every
unhealthy path (no data, stale, DB error, ping failure, unconfigured) results
in ``emitted=False``.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.config import Settings
from kalshi_weather.ops.heartbeat import (
    PING_FAILED,
    PINGED_HEALTHY,
    SKIPPED_NOT_CONFIGURED,
    WITHHELD_DB_ERROR,
    WITHHELD_NO_DATA,
    WITHHELD_STALE,
    heartbeat_decision,
    run_heartbeat,
)
from kalshi_weather.storage.models import Base, MarketSnapshot

NOW = datetime(2026, 7, 26, 20, 0, tzinfo=UTC)
MAX = timedelta(seconds=900)


# --- pure decision ---------------------------------------------------------


@pytest.mark.parametrize(
    "newest, expected_ping, expected_reason",
    [
        (None, False, WITHHELD_NO_DATA),
        (NOW - timedelta(seconds=60), True, PINGED_HEALTHY),
        (NOW - timedelta(seconds=899), True, PINGED_HEALTHY),
        (NOW - timedelta(seconds=901), False, WITHHELD_STALE),
        (NOW - timedelta(hours=14), False, WITHHELD_STALE),  # the power-loss case
    ],
)
def test_heartbeat_decision(newest, expected_ping, expected_reason) -> None:  # type: ignore[no-untyped-def]
    ping, reason = heartbeat_decision(newest, NOW, MAX)
    assert ping is expected_ping
    assert reason == expected_reason


# --- runner ----------------------------------------------------------------


def _settings(url: str | None) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        HEARTBEAT_URL=url,
        HEARTBEAT_STALE_AFTER_SECONDS=900,
    )


@pytest.fixture
async def session_factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_snapshot(factory, observed_at: datetime) -> None:  # type: ignore[no-untyped-def]
    async with factory() as s:
        s.add(
            MarketSnapshot(
                market_ticker="T",
                event_ticker="E",
                market_type="binary",
                title="t",
                status="open",
                yes_bid_cents=0,
                yes_ask_cents=1,
                last_price_cents=0,
                volume=0,
                open_interest=0,
                close_time=NOW,
                observed_at=observed_at,
            )
        )
        await s.commit()


class _Recorder:
    def __init__(self, status: int) -> None:
        self.status = status
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(self.status)


async def test_not_configured_never_pings(session_factory) -> None:  # type: ignore[no-untyped-def]
    rec = _Recorder(200)
    result = await run_heartbeat(
        _settings(None), session_factory, now=NOW, transport=httpx.MockTransport(rec)
    )
    assert result.outcome == SKIPPED_NOT_CONFIGURED
    assert result.emitted is False
    assert rec.calls == 0


async def test_healthy_collector_emits_ping(session_factory) -> None:  # type: ignore[no-untyped-def]
    await _seed_snapshot(session_factory, NOW - timedelta(seconds=120))
    rec = _Recorder(200)
    result = await run_heartbeat(
        _settings("https://hc.example/ping/abc"),
        session_factory,
        now=NOW,
        transport=httpx.MockTransport(rec),
    )
    assert result.outcome == PINGED_HEALTHY
    assert result.emitted is True
    assert rec.calls == 1


async def test_stale_collector_withholds_ping(session_factory) -> None:  # type: ignore[no-untyped-def]
    await _seed_snapshot(session_factory, NOW - timedelta(hours=14))  # power-loss gap
    rec = _Recorder(200)
    result = await run_heartbeat(
        _settings("https://hc.example/ping/abc"),
        session_factory,
        now=NOW,
        transport=httpx.MockTransport(rec),
    )
    assert result.outcome == WITHHELD_STALE
    assert result.emitted is False
    assert rec.calls == 0, "a stale collector must NOT ping -- that is the dead-man's-switch"


async def test_no_data_withholds_ping(session_factory) -> None:  # type: ignore[no-untyped-def]
    rec = _Recorder(200)
    result = await run_heartbeat(
        _settings("https://hc.example/ping/abc"),
        session_factory,
        now=NOW,
        transport=httpx.MockTransport(rec),
    )
    assert result.outcome == WITHHELD_NO_DATA
    assert result.emitted is False
    assert rec.calls == 0


async def test_ping_transport_failure_is_not_emitted(session_factory) -> None:  # type: ignore[no-untyped-def]
    await _seed_snapshot(session_factory, NOW - timedelta(seconds=60))
    rec = _Recorder(500)  # external service returns an error
    result = await run_heartbeat(
        _settings("https://hc.example/ping/abc"),
        session_factory,
        now=NOW,
        transport=httpx.MockTransport(rec),
    )
    assert result.outcome == PING_FAILED
    assert result.emitted is False


async def test_db_error_withholds_ping(session_factory) -> None:  # type: ignore[no-untyped-def]
    # Dispose the engine behind the factory so the query raises -- simulates
    # the DB being down, which must withhold the ping (external monitor alerts).
    await session_factory.kw["bind"].dispose()
    rec = _Recorder(200)
    result = await run_heartbeat(
        _settings("https://hc.example/ping/abc"),
        session_factory,
        now=NOW,
        transport=httpx.MockTransport(rec),
    )
    assert result.outcome == WITHHELD_DB_ERROR
    assert result.emitted is False
    assert rec.calls == 0
