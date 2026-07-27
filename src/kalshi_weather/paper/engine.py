"""The forward paper loop: signal -> gates -> intent -> risk -> simulated fill.

Pure orchestration around the audited execution simulator: this module takes
already-loaded evidence and signals, and returns a bundle of append-only
records for the store. It performs no I/O, so every decision path is unit
testable with synthetic fixtures and every session is reproducible from its
recorded inputs.

Cross-run state is rebuilt by replaying the stored cash ledger through the
same ``execution/ledger.py`` arithmetic that produced it -- the replay
re-validates every historical delta, so silent corruption of paper history
fails loudly instead of propagating.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_weather.execution.engine import Simulator
from kalshi_weather.execution.ledger import EntryKind
from kalshi_weather.execution.models import (
    Action,
    OrderIntent,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
)
from kalshi_weather.execution.policy import ExecutionPolicy, RiskConfig
from kalshi_weather.paper.evidence import MarketEvidence
from kalshi_weather.paper.signals import PaperSignal, SignalSourceType, validate_signal
from kalshi_weather.research.translation import (
    DecisionAction,
    DraftDecisionThresholds,
    translate_probability,
)

#: Paper risk policy version -- explicitly draft, never validated for live use.
PAPER_RISK_VERSION = "paper-risk-draft-1 (DRAFT — NOT VALIDATED FOR LIVE TRADING)"


@dataclass(frozen=True)
class PaperRiskPolicy:
    """Conservative draft limits for the paper loop. All configurable; the
    version string travels on every run record."""

    version: str = PAPER_RISK_VERSION
    max_contracts_per_intent: int = 5
    max_gross_exposure_cents: int = 5_000  # $50 on the tiny default bankroll
    max_net_exposure_cents: int = 5_000
    max_exposure_per_market_cents: int = 1_000
    max_exposure_per_station_cents: int = 2_000
    max_daily_loss_cents: int = 1_000
    max_snapshot_age_seconds: float = 900.0
    max_book_age_seconds: float = 300.0
    require_poll_evidence: bool = True
    max_poll_evidence_age_seconds: float = 900.0
    min_signal_confidence: Decimal = Decimal("0.5")
    slippage_allowance_cents: int = 0  # extra limit room beyond best price
    default_quantity: int = 1

    def to_manifest(self) -> dict[str, Any]:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in self.__dict__.items()}


def station_of(ticker: str) -> str:
    """Best-effort station grouping for exposure caps (KXHIGHT/KXLOWT tickers)."""
    head = ticker.split("-", 1)[0]
    for prefix in ("KXHIGHT", "KXLOWT"):
        if head.startswith(prefix) and len(head) > len(prefix):
            return head[len(prefix) :]
    return "UNKNOWN"


def intent_order_id(strategy_id: str, strategy_version: str, signal: PaperSignal) -> str:
    """Deterministic idempotency key: same strategy + same signal content =
    same order id, so a rerun can never create a second fill."""
    raw = f"{strategy_id}|{strategy_version}|{signal.ticker}|{signal.provenance_hash}"
    return "pi-" + hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class DecisionRecord:
    ticker: str
    stage: str  # signal | evidence | translation | paper_risk | sim
    approved: bool
    reason: str | None
    order_id: str | None = None


@dataclass(frozen=True)
class IntentRecord:
    order_id: str
    ticker: str
    side: str
    action: str
    order_type: str
    limit_price_cents: int
    quantity: int
    submitted_at: datetime
    signal_provenance: str
    final_state: str
    reject_reason: str | None
    execution_confidence: str


@dataclass(frozen=True)
class FillRecord:
    order_id: str
    ticker: str
    side: str
    action: str
    price_cents: int
    quantity: int
    fee_cents: int
    liquidity: str
    filled_at: datetime
    book_source_ref: str


@dataclass(frozen=True)
class LedgerEntryRecord:
    global_seq: int
    at: datetime
    kind: str
    cash_delta_cents: int
    reserved_delta_cents: int
    payload: dict[str, Any]


@dataclass
class PaperSessionResult:
    status: str  # completed | refused_kill_switch
    decisions: list[DecisionRecord] = field(default_factory=list)
    intents: list[IntentRecord] = field(default_factory=list)
    fills: list[FillRecord] = field(default_factory=list)
    new_ledger_entries: list[LedgerEntryRecord] = field(default_factory=list)
    signal_outcomes: list[tuple[PaperSignal, bool, str | None]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    pnl: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


def _rebuild_simulator(
    *,
    policy: ExecutionPolicy,
    risk: RiskConfig,
    prior_entries: list[tuple[int, datetime, str, int, int, dict[str, Any]]],
    initial_cash_cents: int,
    now: datetime,
) -> tuple[Simulator, int]:
    """Simulator continuing from stored paper history (deterministic replay).

    Returns (simulator, next_global_seq). A fresh history gets one DEPOSIT.
    """
    sim = Simulator(policy=policy, risk=risk, initial_cash_cents=0)
    if prior_entries:
        for _seq, at, kind, cash_d, res_d, payload in prior_entries:
            sim.ledger.record(at, EntryKind(kind), cash_d, res_d, payload)
        next_seq = prior_entries[-1][0] + 1
    else:
        sim.ledger.record(
            now,
            EntryKind.DEPOSIT,
            initial_cash_cents,
            0,
            {"amount_cents": initial_cash_cents, "reason": "initial_cash"},
        )
        next_seq = 0  # the deposit becomes global_seq 0 below
    return sim, next_seq


def _cost_exposures(sim: Simulator) -> tuple[int, int, dict[str, int], dict[str, int]]:
    """(gross, net, per-market, per-station) exposure at cost, integer cents."""
    per_market: dict[str, int] = {}
    per_station: dict[str, int] = {}
    yes_total = no_total = 0
    for t, pos in sim.portfolio.positions.items():
        cost = pos.yes_cost_cents + pos.no_cost_cents
        if cost == 0:
            continue
        per_market[t] = per_market.get(t, 0) + cost
        st = station_of(t)
        per_station[st] = per_station.get(st, 0) + cost
        yes_total += pos.yes_cost_cents
        no_total += pos.no_cost_cents
    return yes_total + no_total, abs(yes_total - no_total), per_market, per_station


@dataclass(frozen=True)
class TranslationOutcome:
    side: Side | None
    price_cents: int
    reason: str | None


def _translate(
    signal: PaperSignal,
    ev: MarketEvidence,
    thresholds: DraftDecisionThresholds,
    policy: ExecutionPolicy,
) -> TranslationOutcome:
    """Fee-aware side selection, evaluated at the executable price for each
    side (asks for YES buys, bids for NO buys) -- never at an optimistic mid."""
    assert signal.probability is not None and ev.book is not None
    best_ask = ev.book.best_yes_ask()
    best_bid = ev.book.best_yes_bid()
    candidates: list[tuple[int, Side, int]] = []
    if best_ask is not None and 1 <= best_ask <= 99:
        d = translate_probability(
            model_probability=signal.probability,
            yes_price_cents=best_ask,
            ticker=signal.ticker,
            fee_model=policy.fee_model,
            thresholds=thresholds,
        )
        if d.action is DecisionAction.BUY_YES:
            candidates.append((d.net_edge_centicents, Side.YES, best_ask))
    if best_bid is not None and 1 <= best_bid <= 99:
        d = translate_probability(
            model_probability=signal.probability,
            yes_price_cents=best_bid,
            ticker=signal.ticker,
            fee_model=policy.fee_model,
            thresholds=thresholds,
        )
        if d.action is DecisionAction.BUY_NO:
            candidates.append((d.net_edge_centicents, Side.NO, 100 - best_bid))
    if not candidates:
        return TranslationOutcome(None, 0, "no_positive_edge_after_fees")
    candidates.sort(key=lambda c: (-c[0], c[1].value))
    _, side, price = candidates[0]
    return TranslationOutcome(side, price, None)


def run_paper_session(
    *,
    now: datetime,
    signals: list[PaperSignal],
    evidence: dict[str, MarketEvidence],
    seen_order_ids: set[str],
    prior_entries: list[tuple[int, datetime, str, int, int, dict[str, Any]]],
    initial_cash_cents: int,
    exec_policy: ExecutionPolicy,
    sim_risk: RiskConfig,
    paper_risk: PaperRiskPolicy,
    thresholds: DraftDecisionThresholds,
    strategy_id: str,
    strategy_version: str,
    kill_active: bool,
    kill_reason: str = "",
    peak_equity_cents: int | None = None,
    day_start_equity_cents: int | None = None,
) -> PaperSessionResult:
    """Run one paper session over the supplied signals. Pure: no I/O."""
    result = PaperSessionResult(status="completed")

    if kill_active:
        result.status = "refused_kill_switch"
        result.decisions.append(
            DecisionRecord("*", "kill_switch", False, f"kill_switch_active:{kill_reason}"[:80])
        )
        result.summary = {"refused": True, "reason": "kill_switch_active"}
        return result

    sim, next_seq = _rebuild_simulator(
        policy=exec_policy,
        risk=sim_risk,
        prior_entries=prior_entries,
        initial_cash_cents=initial_cash_cents,
        now=now,
    )
    replayed = len(prior_entries)

    def reject(signal: PaperSignal, stage: str, reason: str, order_id: str | None = None) -> None:
        result.decisions.append(DecisionRecord(signal.ticker, stage, False, reason, order_id))
        result.signal_outcomes.append((signal, False, reason))

    for signal in signals:
        # 1. signal validity (versioned, unexpired, hash-intact)
        why = validate_signal(signal, now=now)
        if why is not None:
            reject(signal, "signal", why)
            continue
        if signal.confidence < paper_risk.min_signal_confidence:
            reject(signal, "signal", "low_signal_confidence")
            continue

        # 2. evidence gates (fail closed)
        ev = evidence.get(signal.ticker)
        if ev is None or ev.snapshot_source_id is None:
            reject(signal, "evidence", "no_market_snapshot")
            continue
        if (ev.market_status or "").lower() != "active":
            reject(signal, "evidence", "market_not_active")
            continue
        snap_age = ev.snapshot_age_seconds(now)
        if snap_age is None or snap_age > paper_risk.max_snapshot_age_seconds:
            reject(signal, "evidence", "stale_market_snapshot")
            continue
        if ev.book is None:
            reject(signal, "evidence", "no_book_evidence")
            continue
        book_age = ev.book_age_seconds(now)
        if book_age is None or book_age > paper_risk.max_book_age_seconds:
            reject(signal, "evidence", "stale_book_evidence")
            continue
        poll_age = ev.poll_evidence_age_seconds(now)
        if paper_risk.require_poll_evidence and (
            poll_age is None or poll_age > paper_risk.max_poll_evidence_age_seconds
        ):
            reject(signal, "evidence", "missing_poll_evidence")
            continue
        confidence_class = (
            "direct_fresh" if poll_age is not None else "book_only"
        )  # poll-evidenced fresh book vs book without direct poll proof

        # 3. signal -> proposed side/price
        if signal.source_type is SignalSourceType.INTENT_FILE:
            assert signal.side is not None
            side = Side(signal.side)
            assert signal.limit_price_cents is not None and signal.quantity is not None
            price, qty = signal.limit_price_cents, signal.quantity
        else:
            outcome = _translate(signal, ev, thresholds, exec_policy)
            if outcome.side is None:
                result.decisions.append(
                    DecisionRecord(signal.ticker, "translation", False, outcome.reason)
                )
                result.signal_outcomes.append((signal, True, outcome.reason))
                continue
            side, price = outcome.side, outcome.price_cents
            qty = paper_risk.default_quantity

        order_id = intent_order_id(strategy_id, strategy_version, signal)
        limit = max(1, min(99, price + paper_risk.slippage_allowance_cents))
        worst_cost = qty * limit

        # 4. paper-level risk gates (draft; simulator risk still runs after)
        if order_id in seen_order_ids:
            reject(signal, "paper_risk", "duplicate_intent", order_id)
            continue
        if qty > paper_risk.max_contracts_per_intent:
            reject(signal, "paper_risk", "max_contracts_per_intent_exceeded", order_id)
            continue
        gross, net, per_market, per_station = _cost_exposures(sim)
        if gross + worst_cost > paper_risk.max_gross_exposure_cents:
            reject(signal, "paper_risk", "max_gross_exposure_exceeded", order_id)
            continue
        if net + worst_cost > paper_risk.max_net_exposure_cents:
            reject(signal, "paper_risk", "max_net_exposure_exceeded", order_id)
            continue
        if per_market.get(signal.ticker, 0) + worst_cost > paper_risk.max_exposure_per_market_cents:
            reject(signal, "paper_risk", "max_exposure_per_market_exceeded", order_id)
            continue
        st = station_of(signal.ticker)
        if per_station.get(st, 0) + worst_cost > paper_risk.max_exposure_per_station_cents:
            reject(signal, "paper_risk", "max_exposure_per_station_exceeded", order_id)
            continue
        if day_start_equity_cents is not None:
            p = sim.portfolio
            equity_now = p.cash_cents + p.reserved_cents + sum(
                pos.yes_cost_cents + pos.no_cost_cents for pos in p.positions.values()
            )
            if day_start_equity_cents - equity_now > paper_risk.max_daily_loss_cents:
                reject(signal, "paper_risk", "max_daily_loss_exceeded", order_id)
                continue

        # 5. build the intent and submit to the audited simulator
        intent = OrderIntent(
            order_id=order_id,
            strategy_id=strategy_id,
            ticker=signal.ticker,
            side=side,
            action=Action.BUY,
            order_type=OrderType.MARKETABLE_LIMIT,
            limit_price_cents=limit,
            quantity=qty,
            submitted_at=now,
            time_in_force=TimeInForce.IOC,
            signal_at=signal.generated_at,
            model_version=f"{signal.source_type.value}:{signal.version}",
            metadata={"signal_provenance": signal.provenance_hash},
        )
        fills_before = len(sim.fills)
        order = sim.submit(intent, book=ev.book, now=now)
        seen_order_ids.add(order_id)

        new_fills = sim.fills[fills_before:]
        for f in new_fills:
            result.fills.append(
                FillRecord(
                    order_id=order_id,
                    ticker=signal.ticker,
                    side=f.side.value,
                    action=f.action.value,
                    price_cents=f.price_cents,
                    quantity=f.quantity,
                    fee_cents=f.fee_cents,
                    liquidity=f.liquidity.value,
                    filled_at=now,
                    book_source_ref=ev.book.source_ref,
                )
            )
        reason = order.reject_reason if order.state is OrderState.REJECTED else None
        result.decisions.append(
            DecisionRecord(signal.ticker, "sim", reason is None, reason, order_id)
        )
        unfilled_reason = reason
        if reason is None and not new_fills:
            unfilled_reason = "insufficient_book_depth_at_limit"
        result.intents.append(
            IntentRecord(
                order_id=order_id,
                ticker=signal.ticker,
                side=side.value,
                action=Action.BUY.value,
                order_type=OrderType.MARKETABLE_LIMIT.value,
                limit_price_cents=limit,
                quantity=qty,
                submitted_at=now,
                signal_provenance=signal.provenance_hash,
                final_state=order.state.value,
                reject_reason=unfilled_reason,
                execution_confidence=confidence_class,
            )
        )
        result.signal_outcomes.append((signal, True, None))

    # 6. persistable new ledger entries (verbatim, globally sequenced)
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

    # 7. end-of-session accounting snapshot (exact integer cents)
    p = sim.portfolio
    position_cost = 0
    mark_value: int | None = 0
    for t, pos in sorted(p.positions.items()):
        cost = pos.yes_cost_cents + pos.no_cost_cents
        position_cost += cost
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
        ev = evidence.get(t)
        if mark_value is not None and ev is not None and ev.book is not None:
            bid, ask = ev.book.best_yes_bid(), ev.book.best_yes_ask()
            if bid is not None and ask is not None:
                mark_value += pos.yes_qty * bid + pos.no_qty * (100 - ask)  # liquidation side
            else:
                mark_value = None
        elif pos.yes_qty or pos.no_qty:
            mark_value = None  # no evidence -> no invented mark

    gross, net, _pm, _ps = _cost_exposures(sim)
    equity_at_cost = p.cash_cents + p.reserved_cents + position_cost
    realized = sum(pos.realized_pnl_cents for pos in p.positions.values())
    peak = max(peak_equity_cents or 0, equity_at_cost)
    result.pnl = {
        "cash_cents": p.cash_cents,
        "reserved_cents": p.reserved_cents,
        "position_cost_cents": position_cost,
        "equity_at_cost_cents": equity_at_cost,
        "mark_value_cents": mark_value,
        "unrealized_pnl_cents": (mark_value - position_cost) if mark_value is not None else None,
        "realized_pnl_cents": realized,
        "fees_cents": p.fees_cents,
        "gross_exposure_cents": gross,
        "net_exposure_cents": net,
        "drawdown_cents": max(0, peak - equity_at_cost),
    }
    rejected = [d for d in result.decisions if not d.approved]
    result.summary = {
        "note": "SYNTHETIC/MANUAL-SIGNAL OPERATIONAL TESTING — NOT STRATEGY PERFORMANCE",
        "signals": len(signals),
        "intents_submitted": len(result.intents),
        "fills": len(result.fills),
        "filled_contracts": sum(f.quantity for f in result.fills),
        "rejections": len(rejected),
        "rejection_reasons": sorted({str(d.reason) for d in rejected}),
        "fees_cents": p.fees_cents,
        "equity_at_cost_cents": equity_at_cost,
    }
    return result
