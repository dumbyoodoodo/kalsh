"""Deterministic settlement of open paper positions from authoritative
Kalshi settlement records.

Evidence source (read-only): ``market_snapshots`` rows whose ``result`` is
``yes``/``no`` -- the outcome Kalshi actually paid on, with
``settlement_ts`` and a terminal status. Snapshots are append-only, so the
full result history per ticker is visible: agreement across every recorded
result is required, a conflict fails closed as ambiguous, and a conflict
with an already-settled paper position is recorded as a correction event
for human review -- money is never silently re-moved.

Payout semantics match the audited ``Position.settle``: each YES contract
pays 100 cents when result is YES, each NO contract pays 100 cents when
result is NO; the loser pays nothing. No settlement fee is charged
(Kalshi's fee schedule assesses trading fees, not settlement).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.execution.models import Side
from kalshi_weather.execution.policy import ExecutionPolicy, RiskConfig
from kalshi_weather.paper.engine import LedgerEntryRecord, _rebuild_simulator
from kalshi_weather.storage.models import MarketSnapshot

#: Snapshot statuses that may carry an authoritative result.
_TERMINAL_STATUSES = ("finalized", "determined", "settled")
#: The only outcomes this skeleton supports paying out.
SUPPORTED_RESULTS = ("yes", "no")

#: Deterministic skip/settle reason codes (recorded, never silent).
REASON_UNRESOLVED = "no_authoritative_settlement_yet"
REASON_AMBIGUOUS = "conflicting_settlement_results"
REASON_UNSUPPORTED = "unsupported_settlement_outcome"
REASON_ALREADY_SETTLED = "already_settled"
REASON_CORRECTION = "correction_detected_vs_prior_settlement"


@dataclass(frozen=True)
class SettlementEvidence:
    """The authoritative outcome for one ticker, or why there isn't one."""

    ticker: str
    result: str | None  # 'yes' | 'no' when usable
    settlement_ts: datetime | None
    snapshot_source_id: int | None
    observed_at: datetime | None
    reason: str | None  # None when usable; else a REASON_* code
    distinct_results: tuple[str, ...] = ()


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def load_settlement_evidence(
    session: AsyncSession, tickers: list[str]
) -> dict[str, SettlementEvidence]:
    """Read-only: the full recorded result history per ticker, collapsed to
    one usable outcome or a deterministic failure reason."""
    out: dict[str, SettlementEvidence] = {}
    for ticker in tickers:
        rows = (
            await session.scalars(
                select(MarketSnapshot)
                .where(
                    MarketSnapshot.market_ticker == ticker,
                    MarketSnapshot.result.is_not(None),
                    MarketSnapshot.result != "",
                )
                .order_by(MarketSnapshot.observed_at.asc())
            )
        ).all()
        results = tuple(sorted({r.result for r in rows if r.result}))
        if not rows:
            out[ticker] = SettlementEvidence(ticker, None, None, None, None, REASON_UNRESOLVED)
            continue
        if len(results) > 1:
            out[ticker] = SettlementEvidence(
                ticker, None, None, None, None, REASON_AMBIGUOUS, results
            )
            continue
        latest = rows[-1]
        if latest.result not in SUPPORTED_RESULTS or (
            latest.status not in _TERMINAL_STATUSES
        ):
            out[ticker] = SettlementEvidence(
                ticker,
                None,
                _utc(latest.settlement_ts),
                latest.id,
                _utc(latest.observed_at),
                REASON_UNSUPPORTED,
                results,
            )
            continue
        out[ticker] = SettlementEvidence(
            ticker=ticker,
            result=latest.result,
            settlement_ts=_utc(latest.settlement_ts),
            snapshot_source_id=latest.id,
            observed_at=_utc(latest.observed_at),
            reason=None,
            distinct_results=results,
        )
    return out


# --- pure settlement session -------------------------------------------------


@dataclass(frozen=True)
class SettlementDecision:
    """One per candidate ticker: a paid settlement, a deterministic skip, or
    a detected correction (append-only successor, no money movement)."""

    ticker: str
    status: str  # settled | skipped_<reason> | correction_detected
    result: str | None
    settlement_ts: datetime | None
    snapshot_source_id: int | None
    gross_payout_cents: int = 0
    position_cost_cents: int = 0
    realized_pnl_delta_cents: int = 0
    fee_cents: int = 0  # no settlement fee on Kalshi
    distinct_results: str = ""
    supersedes_row_id: int | None = None


@dataclass
class SettlementSessionResult:
    decisions: list[SettlementDecision] = field(default_factory=list)
    new_ledger_entries: list[LedgerEntryRecord] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    pnl: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


def run_settlement_session(
    *,
    now: datetime,
    prior_entries: list[tuple[int, datetime, str, int, int, dict[str, Any]]],
    evidence: dict[str, SettlementEvidence],
    prior_settled: dict[str, tuple[int, str]],
    exec_policy: ExecutionPolicy,
    sim_risk: RiskConfig,
    peak_equity_cents: int | None = None,
) -> SettlementSessionResult:
    """Settle every open paper position that has unambiguous authoritative
    evidence. Pure: no I/O. Each position settles exactly once; reruns skip
    as ``already_settled``; conflicting evidence fails closed."""
    result = SettlementSessionResult()
    sim, next_seq = _rebuild_simulator(
        policy=exec_policy,
        risk=sim_risk,
        prior_entries=prior_entries,
        initial_cash_cents=0,
        now=now,
    )
    replayed = len(prior_entries)
    # Idempotency: mark previously-settled tickers on the rebuilt simulator so
    # a second run cannot record even a zero-payout duplicate entry.
    sim.settled_markets.update(prior_settled)

    # 1. corrections: evidence that now CONTRADICTS an already-paid settlement
    for ticker, (row_id, paid_result) in sorted(prior_settled.items()):
        ev = evidence.get(ticker)
        if ev is not None and ev.result is not None and ev.result != paid_result:
            result.decisions.append(
                SettlementDecision(
                    ticker=ticker,
                    status="correction_detected",
                    result=ev.result,
                    settlement_ts=ev.settlement_ts,
                    snapshot_source_id=ev.snapshot_source_id,
                    distinct_results=",".join(ev.distinct_results),
                    supersedes_row_id=row_id,
                )
            )

    # 2. settle open positions with usable evidence
    open_tickers = sorted(
        t
        for t, pos in sim.portfolio.positions.items()
        if not pos.settled and (pos.yes_qty or pos.no_qty)
    )
    for ticker in open_tickers:
        if ticker in prior_settled:
            result.decisions.append(
                SettlementDecision(ticker, f"skipped_{REASON_ALREADY_SETTLED}", None, None, None)
            )
            continue
        ev = evidence.get(ticker)
        if ev is None or ev.reason == REASON_UNRESOLVED:
            result.decisions.append(
                SettlementDecision(ticker, f"skipped_{REASON_UNRESOLVED}", None, None, None)
            )
            continue
        if ev.reason is not None:  # ambiguous / unsupported -- fail closed
            result.decisions.append(
                SettlementDecision(
                    ticker,
                    f"skipped_{ev.reason}",
                    None,
                    ev.settlement_ts,
                    ev.snapshot_source_id,
                    distinct_results=",".join(ev.distinct_results),
                )
            )
            continue
        pos = sim.portfolio.position(ticker)
        cost_before = pos.yes_cost_cents + pos.no_cost_cents
        realized_before = pos.realized_pnl_cents
        assert ev.result is not None
        payout = sim.settle(ticker, Side(ev.result), now)
        result.decisions.append(
            SettlementDecision(
                ticker=ticker,
                status="settled",
                result=ev.result,
                settlement_ts=ev.settlement_ts,
                snapshot_source_id=ev.snapshot_source_id,
                gross_payout_cents=payout,
                position_cost_cents=cost_before,
                realized_pnl_delta_cents=pos.realized_pnl_cents - realized_before,
                distinct_results=",".join(ev.distinct_results),
            )
        )

    # 3. persistable ledger entries (globally sequenced continuation)
    for i, e in enumerate(sim.ledger.entries[replayed:]):
        result.new_ledger_entries.append(
            LedgerEntryRecord(
                global_seq=next_seq + i,
                at=e.at,
                kind=e.kind.value,
                cash_delta_cents=e.cash_delta_cents,
                reserved_delta_cents=e.reserved_delta_cents,
                payload=e.payload,
            )
        )

    # 4. exact post-settlement accounting snapshot (no marks -- positions that
    # remain open have no fresh book here; nothing is invented)
    p = sim.portfolio
    position_cost = 0
    for t, pos in sorted(p.positions.items()):
        position_cost += pos.yes_cost_cents + pos.no_cost_cents
        result.positions.append(
            {
                "ticker": t,
                "yes_qty": pos.yes_qty,
                "yes_cost_cents": pos.yes_cost_cents,
                "no_qty": pos.no_qty,
                "no_cost_cents": pos.no_cost_cents,
                "realized_pnl_cents": pos.realized_pnl_cents,
                "fees_cents": pos.fees_cents,
            }
        )
    equity_at_cost = p.cash_cents + p.reserved_cents + position_cost
    peak = max(peak_equity_cents or 0, equity_at_cost)
    result.pnl = {
        "cash_cents": p.cash_cents,
        "reserved_cents": p.reserved_cents,
        "position_cost_cents": position_cost,
        "equity_at_cost_cents": equity_at_cost,
        "mark_value_cents": None,
        "unrealized_pnl_cents": None,
        "realized_pnl_cents": sum(x.realized_pnl_cents for x in p.positions.values()),
        "fees_cents": p.fees_cents,
        "gross_exposure_cents": position_cost,
        "net_exposure_cents": abs(
            sum(x.yes_cost_cents for x in p.positions.values())
            - sum(x.no_cost_cents for x in p.positions.values())
        ),
        "drawdown_cents": max(0, peak - equity_at_cost),
    }
    by_status: dict[str, int] = {}
    for d in result.decisions:
        by_status[d.status] = by_status.get(d.status, 0) + 1
    result.summary = {
        "note": "SYNTHETIC/MANUAL-SIGNAL OPERATIONAL TESTING — NOT STRATEGY PERFORMANCE",
        "candidates": len(open_tickers),
        "by_status": by_status,
        "gross_payout_cents": sum(d.gross_payout_cents for d in result.decisions),
        "realized_pnl_delta_cents": sum(d.realized_pnl_delta_cents for d in result.decisions),
        "equity_at_cost_cents": equity_at_cost,
    }
    return result
