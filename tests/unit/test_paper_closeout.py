"""Paper-validation closeout: readiness gate, lineage, fill + settlement
accounting (YES/NO), exactly-once, correction consistency, determinism, and
isolation. Pure synthetic inputs; no DB, no network, no settlement mutation."""

import re
from datetime import UTC, datetime
from pathlib import Path

from kalshi_weather.paper.closeout import (
    AccountLite,
    CloseoutInputs,
    CloseoutState,
    EvidenceLite,
    FillLite,
    IntentLite,
    LedgerEntryLite,
    PositionLite,
    SettlementLite,
    SignalLite,
    TerminalEvidence,
    evaluate_closeout,
)

TICKER = "KXLOWTPHX-26JUL28-B85.5"
NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
OID = "pi-abc"


def sig(accepted: bool = True) -> SignalLite:
    return SignalLite("prov123", "v1", "yes", "manual", accepted)


def intent(order_id: str = OID, final_state: str = "filled") -> IntentLite:
    return IntentLite(order_id, "yes", 1, 7, final_state, "direct_fresh", "prov123")


def fill(order_id: str = OID, qty: int = 1, price: int = 7, fee: int = 1) -> FillLite:
    return FillLite(order_id, "yes", qty, price, fee, "taker", "orderbook_snapshots:75792")


def evid() -> EvidenceLite:
    return EvidenceLite(75792, 75792, NOW, "active")


def pos() -> PositionLite:
    return PositionLite(TICKER, 0, 0, 0, 0, 0, 1)


def account(cash: int, cost: int = 0, realized: int = 0, fees: int = 1) -> AccountLite:
    return AccountLite(cash, 0, cost, cash + cost, realized, fees)


def settled(result: str) -> SettlementLite:
    payout = 100 if result == "yes" else 0
    realized = 92 if result == "yes" else -8
    return SettlementLite("settled", result, NOW, payout, 7, realized, 0, None)


def base_inputs(**over: object) -> CloseoutInputs:
    """A COMPLETE YES episode by default; override to exercise other paths."""
    result = over.get("result", "yes")
    cash = 10_092 if result == "yes" else 9_992
    realized = 92 if result == "yes" else -8
    d: dict[str, object] = {
        "ticker": TICKER,
        "signals": (sig(),),
        "intents": (intent(),),
        "fills": (fill(),),
        "evidence": (evid(),),
        "position": pos(),
        "settlements": (settled(result),),  # type: ignore[arg-type]
        "replayed_account": account(cash, 0, realized),
        "stored_account": account(cash, 0, realized),
        "terminal": TerminalEvidence("finalized", result, NOW, (result,), (NOW.isoformat(),), (1,)),
        "ledger": (
            LedgerEntryLite(0, "deposit", 10_000, 0),
            LedgerEntryLite(1, "fill", -8, 0),
            LedgerEntryLite(2, "settlement", 100 if result == "yes" else 0, 0),
        ),
        "reconcile_ok": True,
        "as_of": NOW,
    }
    for k in ("signals", "intents", "fills", "evidence", "settlements", "ledger"):
        if k in over:
            d[k] = over[k]
    for k in ("position", "replayed_account", "stored_account", "terminal", "reconcile_ok"):
        if k in over:
            d[k] = over[k]
    return CloseoutInputs(**d)  # type: ignore[arg-type]


# --- readiness ---------------------------------------------------------------

def test_open_position_not_ready() -> None:
    # active market, no settlement rows, no payout movement
    inp = base_inputs(
        settlements=(),
        terminal=TerminalEvidence("active", "", None, (), (), (1,)),
        ledger=(LedgerEntryLite(0, "deposit", 10_000, 0), LedgerEntryLite(1, "fill", -8, 0)),
    )
    r = evaluate_closeout(inp)
    assert r.state is CloseoutState.NOT_READY
    assert any("status not terminal" in b for b in r.blockers)


def test_missing_terminal_result_not_ready() -> None:
    inp = base_inputs(terminal=TerminalEvidence("finalized", "", NOW, (), (), (1,)))
    r = evaluate_closeout(inp)
    assert r.state is CloseoutState.NOT_READY
    assert any("yes/no result" in b for b in r.blockers)


def test_missing_settlement_ts_not_ready() -> None:
    inp = base_inputs(terminal=TerminalEvidence("finalized", "yes", None, ("yes",), (), (1,)))
    r = evaluate_closeout(inp)
    assert r.state is CloseoutState.NOT_READY
    assert any("settlement timestamp" in b for b in r.blockers)


# --- accounting --------------------------------------------------------------

def test_settled_yes_accounting_complete() -> None:
    r = evaluate_closeout(base_inputs(result="yes"))
    assert r.state is CloseoutState.COMPLETE
    sa = r.settlement_accounting
    assert sa["expected_gross_payout_cents"] == 100
    assert sa["expected_realized_pnl_cents"] == 92
    assert sa["expected_final_cash_cents"] == 10_092


def test_settled_no_accounting_complete() -> None:
    r = evaluate_closeout(base_inputs(result="no"))
    assert r.state is CloseoutState.COMPLETE
    sa = r.settlement_accounting
    assert sa["expected_gross_payout_cents"] == 0
    assert sa["expected_realized_pnl_cents"] == -8
    assert sa["expected_final_cash_cents"] == 9_992


def test_wrong_payout_is_accounting_mismatch() -> None:
    bad = SettlementLite("settled", "yes", NOW, 50, 7, 92, 0, None)  # payout should be 100
    r = evaluate_closeout(base_inputs(settlements=(bad,)))
    assert r.state is CloseoutState.ACCOUNTING_MISMATCH
    assert "gross_payout" in r.blockers


def test_stored_vs_replay_divergence_is_mismatch() -> None:
    r = evaluate_closeout(base_inputs(stored_account=account(10_091, 0, 91)))  # off by 1c
    assert r.state is CloseoutState.ACCOUNTING_MISMATCH


# --- exactly-once ------------------------------------------------------------

def test_duplicate_settlement_decision_not_ready() -> None:
    r = evaluate_closeout(base_inputs(settlements=(settled("yes"), settled("yes"))))
    assert r.state is CloseoutState.NOT_READY
    assert any("exactly 1 settlement decision" in b for b in r.blockers)


def test_duplicate_payout_movement_not_ready() -> None:
    r = evaluate_closeout(base_inputs(ledger=(
        LedgerEntryLite(0, "deposit", 10_000, 0),
        LedgerEntryLite(1, "fill", -8, 0),
        LedgerEntryLite(2, "settlement", 100, 0),
        LedgerEntryLite(3, "settlement", 100, 0),
    )))
    assert r.state is CloseoutState.NOT_READY
    assert any("exactly 1 payout" in b for b in r.blockers)


# --- lineage -----------------------------------------------------------------

def test_lineage_missing_signal_fails() -> None:
    r = evaluate_closeout(base_inputs(signals=()))
    assert r.state is CloseoutState.LINEAGE_FAILURE
    assert any("accepted signal" in b for b in r.blockers)


def test_lineage_missing_evidence_fails() -> None:
    r = evaluate_closeout(base_inputs(evidence=(EvidenceLite(None, None, None, None),)))
    assert r.state is CloseoutState.LINEAGE_FAILURE


def test_lineage_duplicate_fill_fails() -> None:
    r = evaluate_closeout(base_inputs(fills=(fill(), fill(order_id="pi-x"))))
    assert r.state is CloseoutState.LINEAGE_FAILURE


# --- reconcile / correction --------------------------------------------------

def test_reconcile_mismatch_not_ready() -> None:
    r = evaluate_closeout(base_inputs(reconcile_ok=False))
    assert r.state is CloseoutState.NOT_READY
    assert any("reconcile" in b for b in r.blockers)


def test_later_agreeing_snapshots_consistent() -> None:
    r = evaluate_closeout(base_inputs(
        terminal=TerminalEvidence("finalized", "yes", NOW, ("yes", "yes", "yes"), (), (1, 2, 3))
    ))
    assert r.state is CloseoutState.COMPLETE
    assert r.correction["finality"] == "CONSISTENT"


def test_later_conflicting_result_requires_review() -> None:
    r = evaluate_closeout(base_inputs(
        terminal=TerminalEvidence("finalized", "yes", NOW, ("yes", "no"), (), (1, 2))
    ))
    assert r.state is CloseoutState.CORRECTION_REVIEW_REQUIRED
    assert r.correction["finality"] == "CONFLICT"


def test_correction_detected_row_requires_review() -> None:
    correction = SettlementLite("correction_detected", "no", NOW, 0, 7, 0, 0, 1)
    r = evaluate_closeout(base_inputs(settlements=(settled("yes"), correction)))
    # two settlement rows but only one 'settled' -> passes exactly-once, then
    # the correction_detected row forces review
    assert r.state is CloseoutState.CORRECTION_REVIEW_REQUIRED


# --- determinism / isolation -------------------------------------------------

def test_repeated_closeout_deterministic() -> None:
    a = evaluate_closeout(base_inputs()).to_dict()
    b = evaluate_closeout(base_inputs()).to_dict()
    assert a == b


def test_banner_and_conclusion_present() -> None:
    d = evaluate_closeout(base_inputs()).to_dict()
    assert d["banner"] == "OPERATIONAL VALIDATION ONLY — NOT A TRADING RESULT"
    concl = d["operational_conclusion"].lower()
    assert "not strategy profitability" in concl
    for forbidden in ("profitable", "edge validated", "live trading ready"):
        # the COMPLETE conclusion must not assert any of these
        assert forbidden not in concl or "not" in concl


def test_module_isolation_no_experiment_or_exchange() -> None:
    src = (
        Path(__file__).resolve().parents[2] / "src/kalshi_weather/paper/closeout.py"
    ).read_text()
    banned = re.compile(
        r"kalshi_weather\.experiments|kalshi_weather\.research|brier|calibration"
        r"|submit_order|place_order|httpx|datetime\.now|utcnow",
        re.IGNORECASE,
    )
    m = banned.search(src)
    assert m is None, f"closeout references banned symbol: {m and m.group(0)!r}"
    # the pure module must not settle / write
    assert "save_" not in src and "session.add" not in src
