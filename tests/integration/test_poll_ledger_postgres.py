"""PostgreSQL-backed per-ticker polling-evidence tests (ADR 0020):

- migration 0010 -> 0011 upgrade/downgrade is additive; the new table and its
  indexes exist after upgrade and are removed on downgrade, and an existing
  collector_runs row is untouched (no historical mutation, no backfill);
- the availability builder consumes direct per-ticker evidence: an unchanged
  successful poll is OBSERVED and a rate-limited poll breaks continuity.
"""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kalshi_weather.execution.availability import IntervalState, build_availability_timeline
from kalshi_weather.storage.models import CollectorRun, MarketPollAttempt

pytestmark = pytest.mark.integration

_TEST_DB = "kalshi_poll_ledger_it"
TK = "KXTEST-26JUL26-T50"


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
    url = _with_db(admin, _TEST_DB)
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield url
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
        with eng.connect() as c:
            c.execute(
                text(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where datname = :d and pid <> pg_backend_pid()"
                ),
                {"d": _TEST_DB},
            )
            c.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DB}"))
        eng.dispose()


def _alembic(url: str):  # type: ignore[no-untyped-def]
    from alembic.config import Config

    return Config("alembic.ini")


def T(h: int, m: int = 0) -> datetime:
    return datetime(2026, 7, 26, h, m, tzinfo=UTC)


def test_migration_0011_is_additive_upgrade_and_downgrade(throwaway_db: str) -> None:
    from alembic import command

    cfg = _alembic(throwaway_db)
    command.upgrade(cfg, "0010")
    sync = create_engine(throwaway_db)
    with sync.begin() as c:
        # a pre-existing collector_runs row (historical data) must survive both ways
        c.execute(
            text(
                "insert into collector_runs (collector, started_at, finished_at, "
                "duration_seconds, success, requests_attempted, retries, stats_json, "
                "schema_version) values ('kalshi', now(), now(), 1, true, 1, 0, '{}', '1')"
            )
        )
    command.upgrade(cfg, "0011")
    with sync.connect() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar() == "0011"
        assert inspect(c).has_table("market_poll_attempts")
        idx = {i["name"] for i in inspect(c).get_indexes("market_poll_attempts")}
        assert "ix_market_poll_attempts_ticker_time" in idx
        assert "ix_market_poll_attempts_run" in idx
        runs_after = c.execute(text("select count(*) from collector_runs")).scalar()
    assert runs_after == 1  # historical row untouched; no backfill

    command.downgrade(cfg, "0010")
    with sync.connect() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar() == "0010"
        assert not inspect(c).has_table("market_poll_attempts")  # additive object removed
        assert c.execute(text("select count(*) from collector_runs")).scalar() == 1
    sync.dispose()


@pytest.mark.asyncio
async def test_availability_consumes_direct_evidence(throwaway_db: str) -> None:
    from kalshi_weather.storage.models import Base

    eng = create_async_engine(throwaway_db)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as s:
            run = CollectorRun(
                collector="kalshi",
                started_at=T(10, 0),
                finished_at=T(10, 5),
                duration_seconds=300,
                success=True,
                requests_attempted=2,
                retries=0,
                stats_json={},
            )
            s.add(run)
            await s.flush()
            now = T(10, 6)
            s.add_all(
                [
                    # unchanged successful poll -> still OBSERVED
                    MarketPollAttempt(
                        collector_run_id=run.id,
                        ticker=TK,
                        endpoint_type="orderbook",
                        environment="production",
                        eligibility_state="eligible",
                        attempt_state="attempted",
                        outcome="succeeded_unchanged",
                        requested_at=T(10, 2),
                        completed_at=T(10, 2),
                        persisted_row_count=0,
                        deduplicated=True,
                        schema_version="1",
                        created_at=now,
                    ),
                    # a rate-limited poll later -> breaks continuity
                    MarketPollAttempt(
                        collector_run_id=run.id,
                        ticker=TK,
                        endpoint_type="orderbook",
                        environment="production",
                        eligibility_state="eligible",
                        attempt_state="attempted",
                        outcome="rate_limited",
                        requested_at=T(10, 30),
                        completed_at=T(10, 30),
                        http_status=429,
                        rate_limited=True,
                        schema_version="1",
                        created_at=now,
                    ),
                ]
            )
            await s.commit()

        async with Session() as s:
            tl = await build_availability_timeline(s, tickers=(TK,), start=T(10, 0), end=T(11, 0))
    finally:
        await eng.dispose()

    ta = tl.tickers[TK]
    # unchanged successful poll is direct-evidence OBSERVED
    assert ta.state_at(T(10, 2)) is IntervalState.OBSERVED
    # the rate-limited poll breaks continuity (UNKNOWN there)
    assert ta.state_at(T(10, 30)) is IntervalState.UNKNOWN
    summ = tl.summary(TK)
    assert summ["evidence_source"] == "direct_per_ticker"
    assert summ["evidence_schema_era"] == "poll-ledger-0011"
    assert summ["rate_limited_poll_pct"] > 0
    assert summ["unchanged_success_pct"] > 0
