"""Paper-session runner: load state, gather read-only evidence, run the pure
engine, persist append-only records to the dedicated paper database.

The production research database is opened for SELECTs only; every write in
this module targets the paper database. One session = one ``paper_runs`` row
plus its child records, committed atomically.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kalshi_weather.execution.fees import KalshiEventContractFeeModel
from kalshi_weather.execution.policy import ExecutionPolicy, FillMode, RiskConfig
from kalshi_weather.paper.engine import PaperRiskPolicy, PaperSessionResult, run_paper_session
from kalshi_weather.paper.evidence import load_market_evidence
from kalshi_weather.paper.signals import PaperSignal
from kalshi_weather.paper.store import (
    PaperCashLedgerRow,
    PaperFillRow,
    PaperMarketSnapshotRow,
    PaperOrderIntentRow,
    PaperPnlSnapshotRow,
    PaperPositionRow,
    PaperRiskDecisionRow,
    PaperRun,
    PaperSignalRow,
    kill_switch_active,
    open_paper_db,
    paper_engine,
    peak_equity_cents,
    seen_order_ids,
    stored_ledger_entries,
)
from kalshi_weather.research.translation import DraftDecisionThresholds

STRATEGY_ID = "paper-skeleton"
STRATEGY_VERSION = "skeleton-v1"


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False
        )
        return out.stdout.strip() or "unknown"
    except OSError:  # pragma: no cover - environment-specific
        return "unknown"


def default_exec_policy() -> ExecutionPolicy:
    """Marketable-only, authoritative Kalshi fees, no invented depth."""
    return ExecutionPolicy(
        version="exec-policy-v1",
        fill_mode=FillMode.MARKETABLE,
        fee_model=KalshiEventContractFeeModel(),
    )


def default_sim_risk(initial_cash_cents: int) -> RiskConfig:
    """Simulator-level risk sized to the tiny paper bankroll."""
    return RiskConfig(
        version="risk-v1-paper",
        max_order_quantity=10,
        max_position_per_market=20,
        max_gross_exposure_cents=initial_cash_cents,
        max_total_possible_loss_cents=initial_cash_cents,
        max_loss_per_event_cents=initial_cash_cents,
        max_open_orders=20,
    )


def config_hash(*parts: dict[str, Any]) -> str:
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def execute_paper_run(
    *,
    research_database_url: str,
    paper_database_url: str,
    signals: list[PaperSignal],
    signal_source: str,
    initial_cash_cents: int,
    now: datetime | None = None,
    paper_risk: PaperRiskPolicy | None = None,
    thresholds: DraftDecisionThresholds | None = None,
) -> tuple[str, PaperSessionResult]:
    """Run one paper session end-to-end. Returns (run_id, engine result)."""
    now = now or datetime.now(UTC)
    paper_risk = paper_risk or PaperRiskPolicy()
    thresholds = thresholds or DraftDecisionThresholds(min_net_edge_centicents=0)
    exec_policy = default_exec_policy()
    sim_risk = default_sim_risk(initial_cash_cents)

    p_engine = paper_engine(paper_database_url)
    await open_paper_db(p_engine)
    p_factory = async_sessionmaker(p_engine, expire_on_commit=False)

    # --- read paper state + kill switch -------------------------------------
    async with p_factory() as ps:
        kill, kill_reason = await kill_switch_active(ps)
        seen = await seen_order_ids(ps)
        prior_entries = await stored_ledger_entries(ps)
        peak = await peak_equity_cents(ps)

    # --- read-only production evidence --------------------------------------
    tickers = sorted({s.ticker for s in signals})
    evidence = {}
    if tickers and not kill:
        r_engine = create_async_engine(research_database_url)
        try:
            r_factory = async_sessionmaker(r_engine, expire_on_commit=False)
            async with r_factory() as rs:
                evidence = await load_market_evidence(rs, tickers, now=now)
        finally:
            await r_engine.dispose()

    result = run_paper_session(
        now=now,
        signals=signals,
        evidence=evidence,
        seen_order_ids=seen,
        prior_entries=prior_entries,
        initial_cash_cents=initial_cash_cents,
        exec_policy=exec_policy,
        sim_risk=sim_risk,
        paper_risk=paper_risk,
        thresholds=thresholds,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        kill_active=kill,
        kill_reason=kill_reason,
        peak_equity_cents=peak,
    )

    # --- persist append-only ------------------------------------------------
    run_id = "run-" + uuid.uuid4().hex[:16]
    cfg_hash = config_hash(
        {"exec": exec_policy.version, "fees": exec_policy.fee_model.version},
        sim_risk.to_manifest(),
        paper_risk.to_manifest(),
        {"thresholds": thresholds.version, "min_edge_cc": thresholds.min_net_edge_centicents},
    )
    async with p_factory() as ps:
        ps.add(
            PaperRun(
                id=run_id,
                status=result.status,
                strategy_id=STRATEGY_ID,
                strategy_version=STRATEGY_VERSION,
                signal_source=signal_source,
                code_commit=_git_commit(),
                config_hash=cfg_hash,
                fee_model_version=exec_policy.fee_model.version,
                fill_policy_version=exec_policy.version,
                sim_risk_version=sim_risk.version,
                paper_risk_version=paper_risk.version,
                initial_cash_cents=initial_cash_cents,
                summary_json=result.summary,
            )
        )
        for sig, accepted, reason in result.signal_outcomes:
            ps.add(
                PaperSignalRow(
                    run_id=run_id,
                    ticker=sig.ticker,
                    source_type=sig.source_type.value,
                    version=sig.version,
                    generated_at=sig.generated_at,
                    expires_at=sig.expires_at,
                    probability=None if sig.probability is None else str(sig.probability),
                    side=sig.side,
                    confidence=str(sig.confidence),
                    provenance_hash=sig.provenance_hash,
                    accepted=accepted,
                    reject_reason=reason,
                )
            )
        for t, ev in evidence.items():
            ps.add(
                PaperMarketSnapshotRow(
                    run_id=run_id,
                    ticker=t,
                    snapshot_source_id=ev.snapshot_source_id,
                    snapshot_observed_at=ev.snapshot_observed_at,
                    market_status=ev.market_status,
                    yes_bid_cents=ev.yes_bid_cents,
                    yes_ask_cents=ev.yes_ask_cents,
                    book_source_id=ev.book_source_id,
                    book_captured_at=ev.book.captured_at if ev.book else None,
                    book_levels_json=(
                        {
                            "yes_asks": list(ev.book.yes_asks),
                            "yes_bids": list(ev.book.yes_bids),
                        }
                        if ev.book
                        else {}
                    ),
                    poll_evidence_at=ev.poll_evidence_at,
                    ages_json={
                        "snapshot_age_s": ev.snapshot_age_seconds(now),
                        "book_age_s": ev.book_age_seconds(now),
                        "poll_age_s": ev.poll_evidence_age_seconds(now),
                    },
                )
            )
        for d in result.decisions:
            ps.add(
                PaperRiskDecisionRow(
                    run_id=run_id,
                    ticker=d.ticker,
                    order_id=d.order_id,
                    stage=d.stage,
                    approved=d.approved,
                    reason=d.reason,
                )
            )
        for i in result.intents:
            ps.add(
                PaperOrderIntentRow(
                    run_id=run_id,
                    order_id=i.order_id,
                    ticker=i.ticker,
                    side=i.side,
                    action=i.action,
                    order_type=i.order_type,
                    limit_price_cents=i.limit_price_cents,
                    quantity=i.quantity,
                    submitted_at=i.submitted_at,
                    signal_provenance=i.signal_provenance,
                    final_state=i.final_state,
                    reject_reason=i.reject_reason,
                    execution_confidence=i.execution_confidence,
                )
            )
        for f in result.fills:
            ps.add(
                PaperFillRow(
                    run_id=run_id,
                    order_id=f.order_id,
                    ticker=f.ticker,
                    side=f.side,
                    action=f.action,
                    price_cents=f.price_cents,
                    quantity=f.quantity,
                    fee_cents=f.fee_cents,
                    liquidity=f.liquidity,
                    filled_at=f.filled_at,
                    book_source_ref=f.book_source_ref,
                )
            )
        for e in result.new_ledger_entries:
            ps.add(
                PaperCashLedgerRow(
                    run_id=run_id,
                    global_seq=e.global_seq,
                    at=e.at,
                    kind=e.kind,
                    cash_delta_cents=e.cash_delta_cents,
                    reserved_delta_cents=e.reserved_delta_cents,
                    payload_json=json.dumps(e.payload, sort_keys=True, default=str),
                )
            )
        for pos in result.positions:
            ps.add(PaperPositionRow(run_id=run_id, **pos))
        if result.pnl:
            ps.add(PaperPnlSnapshotRow(run_id=run_id, at=now, **result.pnl))
        await ps.commit()
    await p_engine.dispose()
    return run_id, result
