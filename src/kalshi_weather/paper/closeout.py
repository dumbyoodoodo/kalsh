"""Post-settlement paper-accounting closeout (pure, read-only).

Summarizes and *verifies* the one live paper-validation episode after it has
settled authoritatively: original fill accounting, the authoritative terminal
result, exactly-once payout, final realized P&L, ledger reconciliation,
idempotency, and correction consistency. It NEVER settles a position, moves
money, submits an exchange order, or touches an experiment -- it only checks
already-recorded append-only paper rows against independent expectations.

Fail-closed: without an authoritative terminal result (settled status +
yes/no result + settlement timestamp + exactly one settlement decision + one
payout movement) the closeout is NOT_READY and nothing is emitted. All logic
is pure over injected records + an injected ``as_of`` (no DB, no clock).

OPERATIONAL VALIDATION ONLY -- a single correct fill proves the pipeline and
accounting machinery behave correctly; it says nothing about strategy
profitability, signal support, or trading readiness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

#: Frozen expectations for the validated one-contract episode (integer cents).
INITIAL_BANKROLL_CENTS = 10_000
EXPECTED_QUANTITY = 1
EXPECTED_SIDE = "yes"
EXPECTED_FILL_PRICE_CENTS = 7
EXPECTED_FEE_CENTS = 1
EXPECTED_CASH_AFTER_FILL_CENTS = 9_992  # 10000 - 7 - 1
EXPECTED_POSITION_COST_CENTS = 7
EXPECTED_EQUITY_AFTER_FILL_CENTS = 9_999  # cash 9992 + cost 7

_TERMINAL_STATUSES = frozenset({"finalized", "determined", "settled"})


class CloseoutState(StrEnum):
    NOT_READY = "NOT_READY"
    READY = "READY"
    COMPLETE = "COMPLETE"
    ACCOUNTING_MISMATCH = "ACCOUNTING_MISMATCH"
    LINEAGE_FAILURE = "LINEAGE_FAILURE"
    CORRECTION_REVIEW_REQUIRED = "CORRECTION_REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class SignalLite:
    provenance_hash: str
    version: str
    side: str | None
    source_type: str
    accepted: bool


@dataclass(frozen=True, slots=True)
class IntentLite:
    order_id: str
    side: str
    quantity: int
    limit_price_cents: int
    final_state: str
    execution_confidence: str
    signal_provenance: str


@dataclass(frozen=True, slots=True)
class FillLite:
    order_id: str
    side: str
    quantity: int
    price_cents: int
    fee_cents: int
    liquidity: str
    book_source_ref: str


@dataclass(frozen=True, slots=True)
class EvidenceLite:
    snapshot_source_id: int | None
    book_source_id: int | None
    poll_evidence_at: datetime | None
    market_status: str | None


@dataclass(frozen=True, slots=True)
class PositionLite:
    ticker: str
    yes_qty: int
    no_qty: int
    yes_cost_cents: int
    no_cost_cents: int
    realized_pnl_cents: int
    fees_cents: int


@dataclass(frozen=True, slots=True)
class SettlementLite:
    status: str
    result: str | None
    settlement_ts: datetime | None
    gross_payout_cents: int
    position_cost_cents: int
    realized_pnl_delta_cents: int
    fee_cents: int
    supersedes_id: int | None


@dataclass(frozen=True, slots=True)
class AccountLite:
    """A replayed-or-stored account state (integer cents)."""

    cash_cents: int
    reserved_cents: int
    position_cost_cents: int
    equity_at_cost_cents: int
    realized_pnl_cents: int
    fees_cents: int


@dataclass(frozen=True, slots=True)
class TerminalEvidence:
    """Authoritative Kalshi terminal metadata for the market (production)."""

    status: str | None
    result: str
    settlement_ts: datetime | None
    #: distinct (status,result) across later snapshots for correction checks
    later_results: tuple[str, ...] = ()
    later_settlement_ts: tuple[str, ...] = ()
    snapshot_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class LedgerEntryLite:
    seq: int
    kind: str
    cash_delta_cents: int
    reserved_delta_cents: int


@dataclass(frozen=True, slots=True)
class CloseoutInputs:
    ticker: str
    signals: tuple[SignalLite, ...]
    intents: tuple[IntentLite, ...]
    fills: tuple[FillLite, ...]
    evidence: tuple[EvidenceLite, ...]
    position: PositionLite | None
    settlements: tuple[SettlementLite, ...]
    replayed_account: AccountLite
    stored_account: AccountLite | None
    terminal: TerminalEvidence
    ledger: tuple[LedgerEntryLite, ...]
    reconcile_ok: bool
    as_of: datetime


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass
class CloseoutReport:
    state: CloseoutState
    ticker: str
    as_of: datetime
    checks: list[CheckResult] = field(default_factory=list)
    lineage: dict[str, Any] = field(default_factory=dict)
    fill_accounting: dict[str, Any] = field(default_factory=dict)
    settlement_accounting: dict[str, Any] = field(default_factory=dict)
    idempotency: dict[str, Any] = field(default_factory=dict)
    correction: dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "banner": "OPERATIONAL VALIDATION ONLY — NOT A TRADING RESULT",
            "state": self.state.value,
            "ticker": self.ticker,
            "as_of": self.as_of.isoformat(),
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "lineage": self.lineage,
            "fill_accounting": self.fill_accounting,
            "settlement_accounting": self.settlement_accounting,
            "idempotency": self.idempotency,
            "correction": self.correction,
            "blockers": self.blockers,
            "operational_conclusion": _conclusion(self.state),
        }


def _conclusion(state: CloseoutState) -> str:
    if state is CloseoutState.COMPLETE:
        return (
            "Live paper pipeline validated end-to-end; accounting and settlement "
            "machinery behaved correctly; exactly-once and reconciliation proved. "
            "This proves system correctness, NOT strategy profitability, and does "
            "not authorize recurring paper, live trading, or exchange orders."
        )
    if state is CloseoutState.NOT_READY:
        return "Settlement not yet authoritative; nothing verified, nothing emitted."
    return "Closeout blocked; investigate the reported condition read-only. No mutation performed."


def _is_terminal(status: str | None) -> bool:
    return status is not None and status.lower() in _TERMINAL_STATUSES


def _settled_rows(inp: CloseoutInputs) -> list[SettlementLite]:
    return [s for s in inp.settlements if s.status == "settled"]


def _validate_lineage(inp: CloseoutInputs, report: CloseoutReport) -> bool:
    accepted_signals = [s for s in inp.signals if s.accepted]
    accepted_intents = [i for i in inp.intents if i.final_state in ("filled", "accepted")]
    ok = True
    problems: list[str] = []
    if len(inp.fills) != 1:
        ok = False
        problems.append(f"expected exactly 1 fill, found {len(inp.fills)}")
    if not accepted_signals:
        ok = False
        problems.append("no accepted signal")
    if len(accepted_intents) != 1:
        ok = False
        problems.append(f"expected exactly 1 accepted intent, found {len(accepted_intents)}")
    if inp.position is None:
        ok = False
        problems.append("no open-position record")
    if not inp.evidence or all(
        e.snapshot_source_id is None and e.book_source_id is None for e in inp.evidence
    ):
        ok = False
        problems.append("no order-book / market-snapshot evidence linkage")
    # one-to-one order_id across intent and fill
    if inp.fills and accepted_intents:
        fo = {f.order_id for f in inp.fills}
        io = {i.order_id for i in accepted_intents}
        if fo != io:
            ok = False
            problems.append(f"intent/fill order_id mismatch: {io} vs {fo}")
    fill = inp.fills[0] if inp.fills else None
    report.lineage = {
        "signals": len(inp.signals),
        "accepted_signals": len(accepted_signals),
        "intents": len(inp.intents),
        "accepted_intents": len(accepted_intents),
        "fills": len(inp.fills),
        "order_id": fill.order_id if fill else None,
        "execution_confidence": accepted_intents[0].execution_confidence
        if accepted_intents
        else None,
        "signal_provenance": accepted_signals[0].provenance_hash if accepted_signals else None,
        "book_source_ref": fill.book_source_ref if fill else None,
        "problems": problems,
    }
    report.checks.append(CheckResult("lineage_one_to_one", ok, "; ".join(problems) or "exact"))
    return ok


def _verify_fill_accounting(inp: CloseoutInputs, report: CloseoutReport) -> bool:
    fill = inp.fills[0]
    checks: list[tuple[str, bool, str]] = []
    checks.append(
        ("fill_quantity", fill.quantity == EXPECTED_QUANTITY, f"{fill.quantity}")
    )
    checks.append(("fill_side", fill.side.lower() == EXPECTED_SIDE, fill.side))
    checks.append(
        ("fill_price_cents", fill.price_cents == EXPECTED_FILL_PRICE_CENTS, f"{fill.price_cents}")
    )
    checks.append(("fee_cents", fill.fee_cents == EXPECTED_FEE_CENTS, f"{fill.fee_cents}"))
    # exactly one buy ledger movement, cash after fill from replay
    buy_moves = [e for e in inp.ledger if e.kind in ("buy", "fill", "trade")]
    checks.append(("single_fill_ledger_move", len(buy_moves) == 1, f"{len(buy_moves)} buy entries"))
    report.fill_accounting = {
        "quantity": fill.quantity,
        "side": fill.side,
        "fill_price_cents": fill.price_cents,
        "fee_cents": fill.fee_cents,
        "liquidity": fill.liquidity,
        "expected_cash_after_fill_cents": EXPECTED_CASH_AFTER_FILL_CENTS,
        "expected_position_cost_cents": EXPECTED_POSITION_COST_CENTS,
        "expected_equity_after_fill_cents": EXPECTED_EQUITY_AFTER_FILL_CENTS,
    }
    ok = all(c[1] for c in checks)
    for name, passed, detail in checks:
        report.checks.append(CheckResult(name, passed, detail))
    return ok


def _readiness(inp: CloseoutInputs, report: CloseoutReport) -> tuple[bool, list[str]]:
    """The hard gate: authoritative terminal settlement present + exactly-once."""
    settled = _settled_rows(inp)
    payout_moves = [e for e in inp.ledger if e.kind in ("settlement", "payout", "settle")]
    missing: list[str] = []
    if not _is_terminal(inp.terminal.status):
        missing.append(f"market status not terminal ({inp.terminal.status})")
    if inp.terminal.result not in ("yes", "no"):
        missing.append(f"no authoritative yes/no result ({inp.terminal.result!r})")
    if inp.terminal.settlement_ts is None:
        missing.append("no settlement timestamp")
    if len(settled) != 1:
        missing.append(f"expected exactly 1 settlement decision, found {len(settled)}")
    if len(payout_moves) != 1:
        missing.append(f"expected exactly 1 payout ledger movement, found {len(payout_moves)}")
    if not inp.reconcile_ok:
        missing.append("paper reconcile failed")
    report.idempotency = {
        "settled_decisions": len(settled),
        "payout_ledger_movements": len(payout_moves),
        "correction_rows": sum(1 for s in inp.settlements if s.status == "correction_detected"),
    }
    return (not missing), missing


def _verify_settlement_accounting(
    inp: CloseoutInputs, result: str, report: CloseoutReport
) -> bool:
    if result == "yes":
        exp_payout, exp_realized, exp_cash = 100, 92, 10_092
    else:
        exp_payout, exp_realized, exp_cash = 0, -8, 9_992
    exp_equity = exp_cash  # position closed at settlement
    settle = _settled_rows(inp)[0]
    ra = inp.replayed_account
    sa = inp.stored_account
    checks: list[tuple[str, bool, str]] = []
    checks.append(
        ("settlement_result_matches_terminal", settle.result == result, f"{settle.result}")
    )
    checks.append(
        ("gross_payout", settle.gross_payout_cents == exp_payout, f"{settle.gross_payout_cents}")
    )
    checks.append(("no_settlement_fee", settle.fee_cents == 0, f"{settle.fee_cents}"))
    checks.append(("replayed_cash", ra.cash_cents == exp_cash, f"{ra.cash_cents}"))
    checks.append(
        ("replayed_equity", ra.equity_at_cost_cents == exp_equity, f"{ra.equity_at_cost_cents}")
    )
    checks.append(
        ("replayed_realized_pnl", ra.realized_pnl_cents == exp_realized, f"{ra.realized_pnl_cents}")
    )
    checks.append(
        ("replayed_position_closed", ra.position_cost_cents == 0, f"{ra.position_cost_cents}")
    )
    checks.append(("fees_total_1c", ra.fees_cents == EXPECTED_FEE_CENTS, f"{ra.fees_cents}"))
    if sa is not None:
        checks.append(
            ("stored_matches_replay_cash", sa.cash_cents == ra.cash_cents, f"{sa.cash_cents}")
        )
        checks.append(
            ("stored_matches_replay_equity", sa.equity_at_cost_cents == ra.equity_at_cost_cents,
             f"{sa.equity_at_cost_cents}")
        )
    report.settlement_accounting = {
        "authoritative_result": result,
        "expected_gross_payout_cents": exp_payout,
        "expected_realized_pnl_cents": exp_realized,
        "expected_final_cash_cents": exp_cash,
        "expected_final_equity_cents": exp_equity,
        "settlement_row": {
            "result": settle.result,
            "gross_payout_cents": settle.gross_payout_cents,
            "realized_pnl_delta_cents": settle.realized_pnl_delta_cents,
            "fee_cents": settle.fee_cents,
        },
        "replayed_account": {
            "cash_cents": ra.cash_cents,
            "equity_at_cost_cents": ra.equity_at_cost_cents,
            "realized_pnl_cents": ra.realized_pnl_cents,
            "position_cost_cents": ra.position_cost_cents,
            "fees_cents": ra.fees_cents,
        },
    }
    ok = all(c[1] for c in checks)
    for name, passed, detail in checks:
        report.checks.append(CheckResult(name, passed, detail))
    return ok


def _correction_consistency(inp: CloseoutInputs, result: str, report: CloseoutReport) -> bool:
    conflicts: list[str] = []
    for r in inp.terminal.later_results:
        if r and r != result:
            conflicts.append(f"later snapshot result {r!r} != {result!r}")
    if any(s.status == "correction_detected" for s in inp.settlements):
        conflicts.append("a correction_detected settlement row exists")
    report.correction = {
        "authoritative_result": result,
        "later_results": list(inp.terminal.later_results),
        "later_settlement_ts": list(inp.terminal.later_settlement_ts),
        "conflicts": conflicts,
        "finality": "CONSISTENT" if not conflicts else "CONFLICT",
    }
    ok = not conflicts
    report.checks.append(
        CheckResult("correction_consistency", ok, "; ".join(conflicts) or "CONSISTENT")
    )
    return ok


def evaluate_closeout(inp: CloseoutInputs) -> CloseoutReport:
    """Pure closeout evaluation. Never mutates anything; returns the state and
    all findings. Order: lineage -> fill accounting -> readiness gate ->
    settlement accounting -> correction consistency."""
    report = CloseoutReport(state=CloseoutState.NOT_READY, ticker=inp.ticker, as_of=inp.as_of)

    if not _validate_lineage(inp, report):
        report.state = CloseoutState.LINEAGE_FAILURE
        report.blockers = report.lineage.get("problems", [])
        return report

    fill_ok = _verify_fill_accounting(inp, report)

    ready, missing = _readiness(inp, report)
    if not ready:
        report.state = CloseoutState.NOT_READY
        report.blockers = missing
        return report

    result = inp.terminal.result
    # Correction review takes precedence over declaring accounting complete.
    consistent = _correction_consistency(inp, result, report)
    settle_ok = _verify_settlement_accounting(inp, result, report)

    if not consistent:
        report.state = CloseoutState.CORRECTION_REVIEW_REQUIRED
        report.blockers = report.correction["conflicts"]
        return report
    if not (fill_ok and settle_ok):
        report.state = CloseoutState.ACCOUNTING_MISMATCH
        report.blockers = [c.name for c in report.checks if not c.ok]
        return report

    report.state = CloseoutState.COMPLETE
    return report
