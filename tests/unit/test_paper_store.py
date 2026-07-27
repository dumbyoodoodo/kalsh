"""Paper store + runner integration on DISPOSABLE databases only.

The "research" database here is an in-memory SQLite with the production
schema created fresh (the established test pattern); the paper database is
a temporary SQLite file. No real database is touched.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kalshi_weather.paper import runner
from kalshi_weather.paper.signals import constant_signals
from kalshi_weather.paper.store import (
    PaperCashLedgerRow,
    PaperControl,
    PaperFillRow,
    PaperOrderIntentRow,
    PaperPnlSnapshotRow,
    PaperRun,
    kill_switch_active,
    open_paper_db,
    paper_engine,
    seen_order_ids,
    stored_ledger_entries,
)
from kalshi_weather.storage.models import (
    Base,
    CollectorRun,
    MarketPollAttempt,
    MarketSnapshot,
    OrderbookSnapshot,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
TICKER = "KXHIGHTCHI-X-B90"


async def make_research_db(url: str) -> None:
    """Disposable research DB with one active market + fresh book + poll row."""
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = CollectorRun(
            collector="kalshi",
            started_at=NOW - timedelta(minutes=5),
            finished_at=NOW - timedelta(minutes=1),
            duration_seconds=240.0,
            success=True,
            requests_attempted=1,
            retries=0,
        )
        s.add(run)
        await s.flush()
        s.add(
            MarketSnapshot(
                market_ticker=TICKER,
                event_ticker="KXHIGHTCHI-X",
                status="active",
                yes_bid_cents=52,
                yes_ask_cents=55,
                observed_at=NOW - timedelta(seconds=60),
                raw_payload_id=None,
                environment="production",
            )
        )
        s.add(
            OrderbookSnapshot(
                market_ticker=TICKER,
                captured_at=NOW - timedelta(seconds=60),
                yes_levels_json=[[52, 10]],
                no_levels_json=[[45, 10]],  # -> yes ask 55
                best_yes_bid_cents=52,
                best_yes_ask_cents=55,
                environment="production",
            )
        )
        s.add(
            MarketPollAttempt(
                collector_run_id=run.id,
                ticker=TICKER,
                endpoint_type="orderbook",
                environment="production",
                eligibility_state="eligible",
                attempt_state="attempted",
                outcome="succeeded_new_data",
                requested_at=NOW - timedelta(seconds=90),
                completed_at=NOW - timedelta(seconds=85),
                created_at=NOW - timedelta(seconds=85),
            )
        )
        await s.commit()
    await engine.dispose()


@pytest.fixture
def paper_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path}/paper.db"


RESEARCH_URL = "sqlite+aiosqlite:///{path}/research.db"


async def test_end_to_end_run_persists_and_is_idempotent(tmp_path: Path, paper_url: str) -> None:
    research_url = RESEARCH_URL.format(path=tmp_path)
    await make_research_db(research_url)
    signals = constant_signals(tickers=[TICKER], probability=Decimal("0.7"), generated_at=NOW)

    run_id, result = await runner.execute_paper_run(
        research_database_url=research_url,
        paper_database_url=paper_url,
        signals=signals,
        signal_source="constant",
        initial_cash_cents=10_000,
        now=NOW,
    )
    assert result.status == "completed" and len(result.fills) == 1

    engine = paper_engine(paper_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        runs = (await s.scalars(select(PaperRun))).all()
        fills = (await s.scalars(select(PaperFillRow))).all()
        intents = (await s.scalars(select(PaperOrderIntentRow))).all()
        entries = await stored_ledger_entries(s)
        pnl = (await s.scalars(select(PaperPnlSnapshotRow))).all()
    assert len(runs) == 1 and runs[0].id == run_id
    assert runs[0].code_commit and runs[0].config_hash  # provenance recorded
    assert "DRAFT" in runs[0].paper_risk_version
    assert len(fills) == 1 and fills[0].book_source_ref.startswith("orderbook_snapshots:")
    assert len(intents) == 1
    assert entries[0][2] == "deposit"  # kind of first entry
    # equity at cost = bankroll minus the (real Kalshi) taker fee, exactly
    assert len(pnl) == 1
    assert pnl[0].equity_at_cost_cents == 10_000 - fills[0].fee_cents
    assert fills[0].fee_cents > 0

    # SAME signals again (same provenance) -> duplicate intent, no new fill
    run_id2, result2 = await runner.execute_paper_run(
        research_database_url=research_url,
        paper_database_url=paper_url,
        signals=signals,
        signal_source="constant",
        initial_cash_cents=10_000,
        now=NOW + timedelta(minutes=5),
    )
    assert run_id2 != run_id
    assert not result2.fills
    async with factory() as s:
        fills2 = (await s.scalars(select(PaperFillRow))).all()
        runs2 = (await s.scalars(select(PaperRun))).all()
    assert len(fills2) == 1  # unchanged -- idempotent
    assert len(runs2) == 2  # but both runs are recorded (append-only)
    await engine.dispose()


async def test_kill_switch_blocks_and_resume_restores(tmp_path: Path, paper_url: str) -> None:
    research_url = RESEARCH_URL.format(path=tmp_path)
    await make_research_db(research_url)
    engine = paper_engine(paper_url)
    await open_paper_db(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(PaperControl(kind="kill", reason="ops drill"))
        await s.commit()
        active, reason = await kill_switch_active(s)
    assert active and reason == "ops drill"

    signals = constant_signals(tickers=[TICKER], probability=Decimal("0.7"), generated_at=NOW)
    _, result = await runner.execute_paper_run(
        research_database_url=research_url,
        paper_database_url=paper_url,
        signals=signals,
        signal_source="constant",
        initial_cash_cents=10_000,
        now=NOW,
    )
    assert result.status == "refused_kill_switch"
    assert not result.fills and not result.new_ledger_entries

    # resume is an explicit appended record; runs work again afterwards
    async with factory() as s:
        s.add(PaperControl(kind="resume", reason="drill over"))
        await s.commit()
        active2, _ = await kill_switch_active(s)
    assert not active2
    _, result2 = await runner.execute_paper_run(
        research_database_url=research_url,
        paper_database_url=paper_url,
        signals=signals,
        signal_source="constant",
        initial_cash_cents=10_000,
        now=NOW,
    )
    assert result2.status == "completed"
    await engine.dispose()


async def test_store_is_append_only_and_ledger_replays(tmp_path: Path, paper_url: str) -> None:
    research_url = RESEARCH_URL.format(path=tmp_path)
    await make_research_db(research_url)
    signals = constant_signals(tickers=[TICKER], probability=Decimal("0.7"), generated_at=NOW)
    await runner.execute_paper_run(
        research_database_url=research_url,
        paper_database_url=paper_url,
        signals=signals,
        signal_source="constant",
        initial_cash_cents=10_000,
        now=NOW,
    )
    engine = paper_engine(paper_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        entries = await stored_ledger_entries(s)
        seen = await seen_order_ids(s)
        ledger_rows = (await s.scalars(select(PaperCashLedgerRow))).all()
    await engine.dispose()
    assert seen  # idempotency keys persisted
    # global sequence is strictly increasing from 0 with no gaps
    seqs = [e[0] for e in entries]
    assert seqs == list(range(len(seqs)))
    # replay through the audited portfolio arithmetic reproduces exact cash
    from kalshi_weather.execution.ledger import EntryKind, LedgerEntry, replay

    portfolio = replay(
        0,
        [
            LedgerEntry(
                seq=i,
                at=at,
                kind=EntryKind(kind),
                cash_delta_cents=c,
                reserved_delta_cents=r,
                payload=p,
            )
            for i, (_s, at, kind, c, r, p) in enumerate(entries)
        ],
    )
    cost = sum(x.yes_cost_cents + x.no_cost_cents for x in portfolio.positions.values())
    # cash conservation: bankroll = cash + reserved + position cost + fees paid
    assert portfolio.cash_cents + portfolio.reserved_cents + cost + portfolio.fees_cents == 10_000
    assert all(row.payload_json for row in ledger_rows)  # payloads stored verbatim
