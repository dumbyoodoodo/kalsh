from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.observatory.continuity import (
    RunRecord,
    find_continuity_gaps,
    find_missed_run_cycles,
    load_run_records,
    load_stream_timestamps,
    newest_timestamp,
)
from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import record_collector_run, save_trade

NOW = datetime(2026, 7, 22, 12, 0, 0)


def _ts(*hours: float) -> list[datetime]:
    return [NOW - timedelta(hours=h) for h in hours]


def test_find_continuity_gaps_empty_input_returns_no_gaps() -> None:
    assert find_continuity_gaps([], stream="market", max_gap_hours=2.0, now=NOW) == []


def test_find_continuity_gaps_no_gap_when_all_within_threshold() -> None:
    timestamps = _ts(0.5, 1.0, 1.5)
    gaps = find_continuity_gaps(timestamps, stream="market", max_gap_hours=2.0, now=NOW)
    assert gaps == []


def test_find_continuity_gaps_detects_internal_gap() -> None:
    # 10h and 4h ago: an 6h internal gap, well within threshold of "now".
    timestamps = _ts(10.0, 4.0)
    gaps = find_continuity_gaps(timestamps, stream="market", max_gap_hours=5.0, now=NOW)
    assert len(gaps) == 1
    assert gaps[0].stream == "market"
    assert gaps[0].gap_start == NOW - timedelta(hours=10.0)
    assert gaps[0].gap_end == NOW - timedelta(hours=4.0)
    assert gaps[0].hours == pytest.approx(6.0)


def test_find_continuity_gaps_detects_trailing_gap_to_now() -> None:
    # Newest timestamp is 10h old; nothing since -- a stream that stopped.
    timestamps = _ts(10.0)
    gaps = find_continuity_gaps(timestamps, stream="candle", max_gap_hours=2.0, now=NOW)
    assert len(gaps) == 1
    assert gaps[0].gap_start == NOW - timedelta(hours=10.0)
    assert gaps[0].gap_end == NOW


def test_find_continuity_gaps_sorts_unordered_input() -> None:
    timestamps = _ts(1.0, 10.0, 4.0)  # deliberately out of order
    gaps = find_continuity_gaps(timestamps, stream="market", max_gap_hours=5.0, now=NOW)
    assert len(gaps) == 1
    assert gaps[0].gap_start == NOW - timedelta(hours=10.0)
    assert gaps[0].gap_end == NOW - timedelta(hours=4.0)


def _runs(collector: str, *hours_ago: float) -> list[RunRecord]:
    return [
        RunRecord(collector=collector, started_at=NOW - timedelta(hours=h), success=True)
        for h in hours_ago
    ]


def test_find_missed_run_cycles_no_gap_within_multiplier() -> None:
    # 5-minute interval, runs every ~5 minutes -- well within 3x multiplier.
    runs = [
        RunRecord(collector="kalshi", started_at=NOW - timedelta(minutes=m), success=True)
        for m in (0, 5, 10, 15)
    ]
    windows = find_missed_run_cycles(runs, collector="kalshi", interval_seconds=300, now=NOW)
    assert windows == []


def test_find_missed_run_cycles_detects_internal_gap() -> None:
    # 5-min interval -> 15-min threshold. Internal gap ~59min (well over);
    # trailing gap to `now` only ~1min (well under) -- exactly one window.
    runs = _runs("kalshi", 1.0, 1.0 / 60)
    windows = find_missed_run_cycles(runs, collector="kalshi", interval_seconds=300, now=NOW)
    assert len(windows) == 1
    assert windows[0].collector == "kalshi"
    assert windows[0].expected_interval_seconds == 300


def test_find_missed_run_cycles_detects_trailing_gap() -> None:
    runs = _runs("weather", 2.0)  # last run 2h ago
    windows = find_missed_run_cycles(runs, collector="weather", interval_seconds=1800, now=NOW)
    assert len(windows) == 1
    assert windows[0].gap_start == NOW - timedelta(hours=2.0)
    assert windows[0].gap_end == NOW


def test_find_missed_run_cycles_ignores_other_collectors() -> None:
    runs = _runs("kalshi", 0.1) + _runs("weather", 100.0)
    windows = find_missed_run_cycles(runs, collector="kalshi", interval_seconds=300, now=NOW)
    assert windows == []


def test_find_missed_run_cycles_empty_input_returns_no_windows() -> None:
    assert find_missed_run_cycles([], collector="kalshi", interval_seconds=300, now=NOW) == []


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


async def test_load_stream_timestamps_returns_naive_utc(session: AsyncSession) -> None:
    aware_now = datetime.now(UTC)
    await save_trade(
        session,
        trade_id="t1",
        market_ticker="KXTICKER",
        executed_at=aware_now,
        price_cents=50,
        count=1,
        taker_side="yes",
        raw_payload_id=None,
    )
    timestamps = await load_stream_timestamps(
        session, "trade", since=aware_now - timedelta(hours=1)
    )
    assert len(timestamps) == 1
    assert timestamps[0].tzinfo is None


async def test_load_stream_timestamps_unknown_stream_raises(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        await load_stream_timestamps(session, "bogus", since=datetime(2026, 1, 1))


async def test_load_run_records_returns_naive_utc_and_correct_fields(
    session: AsyncSession,
) -> None:
    started = datetime.now(UTC)
    finished = started + timedelta(seconds=5)
    await record_collector_run(
        session,
        collector="kalshi",
        started_at=started,
        finished_at=finished,
        success=True,
        requests_attempted=1,
        retries=0,
        stats={},
    )
    records = await load_run_records(session, since=started - timedelta(hours=1))
    assert len(records) == 1
    assert records[0].collector == "kalshi"
    assert records[0].success is True
    assert records[0].started_at.tzinfo is None


async def test_newest_timestamp_none_when_empty(session: AsyncSession) -> None:
    assert await newest_timestamp(session, "trade") is None


async def test_newest_timestamp_returns_naive_utc(session: AsyncSession) -> None:
    aware_now = datetime.now(UTC)
    await save_trade(
        session,
        trade_id="t1",
        market_ticker="KXTICKER",
        executed_at=aware_now,
        price_cents=50,
        count=1,
        taker_side="yes",
        raw_payload_id=None,
    )
    result = await newest_timestamp(session, "trade")
    assert result is not None
    assert result.tzinfo is None
