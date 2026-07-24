"""Write-path provenance stamping (ADR 0013).

Verifies that each workflow stamps the row's ``environment`` from the *client
that produced it* -- demo collector writes stamp ``demo``, production
price/revision writes stamp ``production`` -- and that the stamp survives the
savepoint-based settlement path after a recoverable failure.
"""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.config import Environment
from kalshi_weather.ingestion.metadata_revision import run_metadata_revision
from kalshi_weather.ingestion.settlement_sync import (
    SettlementOutcome,
    capture_settled_transitions,
)
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.storage.database import session_scope
from kalshi_weather.storage.models import (
    Base,
    MarketSnapshot,
    SettlementAttempt,
)
from kalshi_weather.storage.repositories import (
    save_market_candlestick,
    save_market_snapshot,
    save_orderbook_snapshot,
    save_trade,
)

DEMO = "https://demo-api.kalshi.co/trade-api/v2"
PROD = "https://api.elections.kalshi.com/trade-api/v2"
NOW = datetime(2026, 7, 24, 22, 0, tzinfo=UTC)
CLOSE = datetime(2026, 7, 24, 14, 0, tzinfo=UTC)
PAST_FINALITY = datetime(2026, 7, 24, 21, 0, tzinfo=UTC)


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


# --- Repository layer stamps the column ------------------------------------


async def test_save_functions_accept_and_store_environment(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        snap = await save_market_snapshot(
            s,
            market_ticker="T",
            event_ticker="E",
            market_type="binary",
            title="t",
            subtitle=None,
            status="open",
            yes_bid_cents=0,
            yes_ask_cents=1,
            last_price_cents=0,
            volume=0,
            open_interest=0,
            close_time=CLOSE,
            rules_primary="r",
            rules_secondary=None,
            raw_payload_id=None,
            environment="demo",
        )
        ob = await save_orderbook_snapshot(
            s,
            market_ticker="T",
            yes_levels=[[1, 2]],
            no_levels=[[3, 4]],
            raw_payload_id=None,
            environment="demo",
        )
        tr = await save_trade(
            s,
            trade_id="tid1",
            market_ticker="T",
            executed_at=NOW,
            price_cents=50,
            count=1,
            taker_side="yes",
            raw_payload_id=None,
            environment="demo",
        )
        cd = await save_market_candlestick(
            s,
            market_ticker="T",
            series_ticker="S",
            period_interval_seconds=60,
            period_start=CLOSE,
            period_end=CLOSE,
            price_open_cents=1,
            price_high_cents=2,
            price_low_cents=1,
            price_close_cents=2,
            price_mean_cents=1,
            price_close_is_carried_forward=False,
            yes_bid_open_cents=1,
            yes_bid_high_cents=1,
            yes_bid_low_cents=1,
            yes_bid_close_cents=1,
            yes_ask_open_cents=1,
            yes_ask_high_cents=1,
            yes_ask_low_cents=1,
            yes_ask_close_cents=1,
            volume=0,
            open_interest=0,
            raw_payload_id=None,
            environment="production",
        )
    assert snap.record.environment == "demo"
    assert ob.record.environment == "demo"
    assert tr.record.environment == "demo"
    assert cd.record.environment == "production"


async def test_environment_is_not_in_the_content_hash(session_factory) -> None:  # type: ignore[no-untyped-def]
    """A differing environment must NOT force a spurious new snapshot -- it is
    provenance, not market state."""
    kw: dict[str, Any] = dict(
        market_ticker="T",
        event_ticker="E",
        market_type="binary",
        title="t",
        subtitle=None,
        status="open",
        yes_bid_cents=0,
        yes_ask_cents=1,
        last_price_cents=0,
        volume=0,
        open_interest=0,
        close_time=CLOSE,
        rules_primary="r",
        rules_secondary=None,
        raw_payload_id=None,
    )
    async with session_scope(session_factory) as s:
        first = await save_market_snapshot(s, environment="demo", **kw)
        # identical market state, different environment -> still a duplicate
        second = await save_market_snapshot(s, environment="production", **kw)
    assert first.record.content_hash == second.record.content_hash
    assert second.was_duplicate is True


# --- End-to-end: the stamp comes from the producing client -----------------


def _client(base_url: str, handler) -> KalshiClient:  # type: ignore[no-untyped-def]
    return KalshiClient(
        base_url=base_url,
        environment=Environment.DEVELOPMENT,
        transport=httpx.MockTransport(handler),
        min_request_interval_seconds=0,
        max_retries=1,
    )


def _market_payload(ticker: str, *, result: str, vol: int = 0) -> dict:
    return {
        "market": {
            "ticker": ticker,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "market_type": "binary",
            "title": "t",
            "status": "finalized",
            "yes_bid_dollars": "0.0000",
            "yes_ask_dollars": "0.0100",
            "last_price_dollars": "0.0000",
            "volume_fp": f"{vol}.00",
            "open_interest_fp": f"{vol}.00",
            "close_time": CLOSE.isoformat().replace("+00:00", "Z"),
            "expiration_time": PAST_FINALITY.isoformat().replace("+00:00", "Z"),
            "rules_primary": "r",
            "result": result,
        }
    }


async def _seed_settled(session: AsyncSession, ticker: str, environment: str | None) -> None:
    await save_market_snapshot(
        session,
        market_ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        market_type="binary",
        title="t",
        subtitle=None,
        status="finalized",
        yes_bid_cents=0,
        yes_ask_cents=1,
        last_price_cents=0,
        volume=0,
        open_interest=0,
        close_time=CLOSE,
        rules_primary="r",
        rules_secondary=None,
        raw_payload_id=None,
        result="no",
        expiration_value=None,
        expiration_time=PAST_FINALITY,
        environment=environment,
    )


async def test_demo_settlement_stamps_demo(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await save_market_snapshot(
            s,
            market_ticker="KXT-1",
            event_ticker="KXT",
            market_type="binary",
            title="t",
            subtitle=None,
            status="closed",
            yes_bid_cents=0,
            yes_ask_cents=1,
            last_price_cents=0,
            volume=0,
            open_interest=0,
            close_time=CLOSE,
            rules_primary="r",
            rules_secondary=None,
            raw_payload_id=None,
            result=None,
            expiration_time=PAST_FINALITY,
            environment=None,
        )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_market_payload("KXT-1", result="yes"))

    async with _client(DEMO, handler) as client, session_scope(session_factory) as session:
        await capture_settled_transitions(
            client,
            session,
            open_tickers=set(),
            limit=5,
            recent_days=7,
            session_factory=session_factory,
            now=NOW,
        )
    async with session_scope(session_factory) as s:
        attempt = (await s.scalars(select(SettlementAttempt))).one()
        latest = (
            await s.scalars(select(MarketSnapshot).where(MarketSnapshot.result == "yes"))
        ).one()
    assert attempt.outcome == SettlementOutcome.SETTLED_AND_SAVED.value
    assert attempt.environment == "demo"
    assert latest.environment == "demo", "the new settled snapshot is stamped from the demo client"


async def test_production_metadata_revision_stamps_production(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed_settled(s, "KXT-2", environment="demo")  # collected from demo, vol 0

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_market_payload("KXT-2", result="no", vol=646))

    async with _client(PROD, handler) as client:
        report = await run_metadata_revision(
            session_factory=session_factory, client=client, now=NOW
        )
    assert report.totals()["changed"] == 1  # volume moved 0 -> 646
    async with session_scope(session_factory) as s:
        rows = (
            await s.scalars(
                select(MarketSnapshot)
                .where(MarketSnapshot.market_ticker == "KXT-2")
                .order_by(MarketSnapshot.id)
            )
        ).all()
    assert rows[0].environment == "demo", "original demo row untouched"
    assert rows[-1].environment == "production", "revision snapshot stamped production"


async def test_savepoint_persistence_failure_still_records_environment(  # type: ignore[no-untyped-def]
    session_factory, monkeypatch
) -> None:
    """A recoverable persistence failure records a retryable attempt in the
    savepoint-recovery path; that attempt row must still carry demo provenance.

    Forcing the failure by monkeypatch (not a bad FK) because this suite runs
    on SQLite, which does not enforce foreign keys -- the very reason the
    original production FK bug escaped unit testing.
    """
    from kalshi_weather.ingestion import settlement_sync as ss

    async with session_scope(session_factory) as s:
        await save_market_snapshot(
            s,
            market_ticker="KXT-3",
            event_ticker="KXT",
            market_type="binary",
            title="t",
            subtitle=None,
            status="closed",
            yes_bid_cents=0,
            yes_ask_cents=1,
            last_price_cents=0,
            volume=0,
            open_interest=0,
            close_time=CLOSE,
            rules_primary="r",
            rules_secondary=None,
            raw_payload_id=None,
            result=None,
            expiration_time=PAST_FINALITY,
            environment=None,
        )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_market_payload("KXT-3", result="yes"))

    async def _boom(*a: Any, **k: Any):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(ss, "persist_market_snapshot", _boom)

    async with _client(DEMO, handler) as client, session_scope(session_factory) as session:
        stats = await capture_settled_transitions(
            client,
            session,
            open_tickers=set(),
            limit=5,
            recent_days=7,
            session_factory=session_factory,
            now=NOW,
        )
    async with session_scope(session_factory) as s:
        attempt = (await s.scalars(select(SettlementAttempt))).one()
    assert attempt.outcome == SettlementOutcome.PERSISTENCE_FAILURE.value
    assert attempt.retryable is True
    assert attempt.environment == "demo", "provenance retained through the recovery path"
    assert stats.outcomes[SettlementOutcome.PERSISTENCE_FAILURE.value] == 1
