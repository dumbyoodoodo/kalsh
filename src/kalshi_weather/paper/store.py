"""Append-only paper-trading persistence in a DEDICATED database.

Isolation by construction: these tables live on their own SQLAlchemy
metadata (``PaperBase``) in a separate database (``PAPER_DATABASE_URL``,
default a local SQLite file) -- never in the production research database,
which this package only reads. No function here mutates or removes an
existing row; corrections append successor records.

The cash ledger rows reproduce ``execution/ledger.py`` entries verbatim
(kind, deltas, payload), so any past paper state is deterministically
rebuildable by replaying them through the same audited ``Portfolio``
arithmetic used at decision time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PAPER_SCHEMA_VERSION = 1


class PaperBase(DeclarativeBase):
    """Dedicated metadata: paper tables can never collide with research ones."""


def _now() -> datetime:
    return datetime.now(UTC)


class PaperMeta(PaperBase):
    __tablename__ = "paper_meta"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PaperRun(PaperBase):
    __tablename__ = "paper_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    status: Mapped[str] = mapped_column(String(40))  # completed | refused_kill_switch
    strategy_id: Mapped[str] = mapped_column(String(80))
    strategy_version: Mapped[str] = mapped_column(String(80))
    signal_source: Mapped[str] = mapped_column(String(40))
    code_commit: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    fee_model_version: Mapped[str] = mapped_column(String(64))
    fill_policy_version: Mapped[str] = mapped_column(String(64))
    sim_risk_version: Mapped[str] = mapped_column(String(64))
    paper_risk_version: Mapped[str] = mapped_column(String(120))
    initial_cash_cents: Mapped[int] = mapped_column(Integer)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PaperControl(PaperBase):
    """Kill-switch state machine: latest row wins (kill | resume)."""

    __tablename__ = "paper_control"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    kind: Mapped[str] = mapped_column(String(10))  # kill | resume
    reason: Mapped[str] = mapped_column(Text, default="")


class PaperSignalRow(PaperBase):
    __tablename__ = "paper_signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ticker: Mapped[str] = mapped_column(String(80))
    source_type: Mapped[str] = mapped_column(String(30))
    version: Mapped[str] = mapped_column(String(80))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    probability: Mapped[str | None] = mapped_column(String(20), nullable=True)
    side: Mapped[str | None] = mapped_column(String(4), nullable=True)
    confidence: Mapped[str] = mapped_column(String(20))
    provenance_hash: Mapped[str] = mapped_column(String(64))
    accepted: Mapped[bool] = mapped_column(Boolean)
    reject_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)


class PaperMarketSnapshotRow(PaperBase):
    """The exact market evidence a decision saw (with production source ids),
    so the decision replays without re-querying live tables."""

    __tablename__ = "paper_market_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ticker: Mapped[str] = mapped_column(String(80))
    snapshot_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    market_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    yes_bid_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yes_ask_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    book_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    book_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    book_levels_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    poll_evidence_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ages_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PaperOrderIntentRow(PaperBase):
    __tablename__ = "paper_order_intents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    order_id: Mapped[str] = mapped_column(String(64), index=True)  # idempotency key
    ticker: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(4))
    action: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(20))
    limit_price_cents: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    signal_provenance: Mapped[str] = mapped_column(String(64))
    final_state: Mapped[str] = mapped_column(String(24))
    reject_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    execution_confidence: Mapped[str] = mapped_column(String(24), default="unknown")


class PaperRiskDecisionRow(PaperBase):
    __tablename__ = "paper_risk_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ticker: Mapped[str] = mapped_column(String(80))
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stage: Mapped[str] = mapped_column(String(24))  # signal|evidence|translation|paper_risk|sim
    approved: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(String(80), nullable=True)


class PaperFillRow(PaperBase):
    __tablename__ = "paper_fills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    ticker: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(4))
    action: Mapped[str] = mapped_column(String(4))
    price_cents: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    fee_cents: Mapped[int] = mapped_column(Integer)
    liquidity: Mapped[str] = mapped_column(String(12))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    book_source_ref: Mapped[str] = mapped_column(String(80))


class PaperCashLedgerRow(PaperBase):
    """Verbatim execution-ledger entries; ``global_seq`` is unique so the whole
    paper history replays deterministically in order."""

    __tablename__ = "paper_cash_ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    global_seq: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(16))
    cash_delta_cents: Mapped[int] = mapped_column(Integer)
    reserved_delta_cents: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text)


class PaperPositionRow(PaperBase):
    """End-of-run position snapshot (append-only; one set per run)."""

    __tablename__ = "paper_positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ticker: Mapped[str] = mapped_column(String(80))
    yes_qty: Mapped[int] = mapped_column(Integer)
    yes_cost_cents: Mapped[int] = mapped_column(Integer)
    no_qty: Mapped[int] = mapped_column(Integer)
    no_cost_cents: Mapped[int] = mapped_column(Integer)
    realized_pnl_cents: Mapped[int] = mapped_column(Integer)
    fees_cents: Mapped[int] = mapped_column(Integer)


class PaperPnlSnapshotRow(PaperBase):
    __tablename__ = "paper_pnl_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cash_cents: Mapped[int] = mapped_column(Integer)
    reserved_cents: Mapped[int] = mapped_column(Integer)
    position_cost_cents: Mapped[int] = mapped_column(Integer)
    equity_at_cost_cents: Mapped[int] = mapped_column(Integer)
    mark_value_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unrealized_pnl_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    realized_pnl_cents: Mapped[int] = mapped_column(Integer)
    fees_cents: Mapped[int] = mapped_column(Integer)
    gross_exposure_cents: Mapped[int] = mapped_column(Integer)
    net_exposure_cents: Mapped[int] = mapped_column(Integer)
    drawdown_cents: Mapped[int] = mapped_column(Integer)


class PaperSettlementRow(PaperBase):
    """One settlement event or deterministic skip for one paper position.

    Append-only: a correction never edits the prior row -- it appends a
    successor whose ``supersedes_id`` points at the superseded settlement,
    with status ``correction_detected`` and NO automatic money movement
    (human review required before any reversal)."""

    __tablename__ = "paper_settlements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ticker: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(48))  # settled | skipped_* | correction_detected
    result: Mapped[str | None] = mapped_column(String(8), nullable=True)
    settlement_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snapshot_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gross_payout_cents: Mapped[int] = mapped_column(Integer, default=0)
    position_cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    realized_pnl_delta_cents: Mapped[int] = mapped_column(Integer, default=0)
    fee_cents: Mapped[int] = mapped_column(Integer, default=0)  # Kalshi charges none at settlement
    distinct_results: Mapped[str] = mapped_column(String(40), default="")
    supersedes_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PaperAttributionRow(PaperBase):
    __tablename__ = "paper_attribution"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    dimension: Mapped[str] = mapped_column(String(30))
    key: Mapped[str] = mapped_column(String(80))
    metric: Mapped[str] = mapped_column(String(40))
    value: Mapped[int] = mapped_column(Integer)


# --- open / helpers ----------------------------------------------------------


def paper_engine(url: str) -> AsyncEngine:
    """Create the paper engine, creating a parent directory for SQLite files
    (code creates the directories it's configured to write into)."""
    if url.startswith("sqlite") and ":///" in url and ":memory:" not in url:
        Path(url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(url)


async def open_paper_db(engine: AsyncEngine) -> None:
    """Create tables if missing and stamp the paper schema version once."""
    async with engine.begin() as conn:
        await conn.run_sync(PaperBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        meta = (await session.scalars(select(PaperMeta).limit(1))).first()
        if meta is None:
            session.add(PaperMeta(schema_version=PAPER_SCHEMA_VERSION))
            await session.commit()
        elif meta.schema_version != PAPER_SCHEMA_VERSION:
            raise RuntimeError(
                f"paper DB schema {meta.schema_version} != expected {PAPER_SCHEMA_VERSION}"
            )


async def kill_switch_active(session: AsyncSession) -> tuple[bool, str]:
    row = (
        await session.scalars(select(PaperControl).order_by(PaperControl.id.desc()).limit(1))
    ).first()
    if row is None:
        return False, ""
    return row.kind == "kill", row.reason


async def seen_order_ids(session: AsyncSession) -> set[str]:
    return set((await session.scalars(select(PaperOrderIntentRow.order_id))).all())


async def stored_ledger_entries(
    session: AsyncSession,
) -> list[tuple[int, datetime, str, int, int, dict[str, Any]]]:
    rows = (
        await session.scalars(
            select(PaperCashLedgerRow).order_by(PaperCashLedgerRow.global_seq)
        )
    ).all()
    return [
        (
            r.global_seq,
            r.at if r.at.tzinfo else r.at.replace(tzinfo=UTC),
            r.kind,
            r.cash_delta_cents,
            r.reserved_delta_cents,
            json.loads(r.payload_json),
        )
        for r in rows
    ]


async def latest_settlements(session: AsyncSession) -> dict[str, tuple[int, str]]:
    """Latest *paid* settlement per ticker: ticker -> (row id, result)."""
    rows = (
        await session.scalars(
            select(PaperSettlementRow)
            .where(PaperSettlementRow.status == "settled")
            .order_by(PaperSettlementRow.id)
        )
    ).all()
    out: dict[str, tuple[int, str]] = {}
    for r in rows:
        out[r.ticker] = (r.id, r.result or "")
    return out


async def peak_equity_cents(session: AsyncSession) -> int | None:
    rows = (await session.scalars(select(PaperPnlSnapshotRow.equity_at_cost_cents))).all()
    return max(rows) if rows else None
