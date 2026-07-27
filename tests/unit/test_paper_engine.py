"""Forward paper engine: gates, fills, idempotency, kill switch, accounting.

Synthetic fixtures only -- no database, no network, no experiment data.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kalshi_weather.execution.fees import ZERO_FEE_MODEL, KalshiEventContractFeeModel
from kalshi_weather.execution.market import OrderBook
from kalshi_weather.execution.policy import ExecutionPolicy, FillMode, RiskConfig
from kalshi_weather.paper.engine import (
    PaperRiskPolicy,
    intent_order_id,
    run_paper_session,
    station_of,
)
from kalshi_weather.paper.evidence import MarketEvidence
from kalshi_weather.paper.signals import PaperSignal, SignalSourceType
from kalshi_weather.research.translation import DraftDecisionThresholds

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
T0 = DraftDecisionThresholds(min_net_edge_centicents=0)


def signal(ticker: str = "KXHIGHTCHI-X-B90", p: str = "0.70", **kw: object) -> PaperSignal:
    base: dict[str, object] = {
        "source_type": SignalSourceType.CONSTANT,
        "version": "v1",
        "generated_at": NOW,
        "ticker": ticker,
        "confidence": Decimal("1"),
        "expires_at": NOW + timedelta(hours=1),
        "probability": Decimal(p),
    }
    base.update(kw)
    return PaperSignal(**base).with_hash()  # type: ignore[arg-type]


def evidence(
    ticker: str = "KXHIGHTCHI-X-B90",
    *,
    ask: tuple[int, int] | None = (55, 10),
    bid: tuple[int, int] | None = (52, 10),
    status: str = "active",
    snap_age_s: float = 60,
    book_age_s: float = 60,
    poll_age_s: float | None = 120,
) -> MarketEvidence:
    book = OrderBook(
        ticker=ticker,
        captured_at=NOW - timedelta(seconds=book_age_s),
        yes_asks=(ask,) if ask else (),
        yes_bids=(bid,) if bid else (),
        source_ref="orderbook_snapshots:1",
    )
    return MarketEvidence(
        ticker=ticker,
        snapshot_source_id=1,
        snapshot_observed_at=NOW - timedelta(seconds=snap_age_s),
        market_status=status,
        yes_bid_cents=bid[0] if bid else None,
        yes_ask_cents=ask[0] if ask else None,
        book=book,
        book_source_id=1,
        poll_evidence_at=None if poll_age_s is None else NOW - timedelta(seconds=poll_age_s),
    )


def run(signals: list[PaperSignal], ev: dict[str, MarketEvidence], **kw: object):  # type: ignore[no-untyped-def]
    defaults: dict[str, object] = {
        "now": NOW,
        "signals": signals,
        "evidence": ev,
        "seen_order_ids": set(),
        "prior_entries": [],
        "initial_cash_cents": 10_000,
        "exec_policy": ExecutionPolicy(fill_mode=FillMode.MARKETABLE, fee_model=ZERO_FEE_MODEL),
        "sim_risk": RiskConfig(max_order_quantity=10),
        "paper_risk": PaperRiskPolicy(),
        "thresholds": T0,
        "strategy_id": "paper-skeleton",
        "strategy_version": "skeleton-v1",
        "kill_active": False,
    }
    defaults.update(kw)
    return run_paper_session(**defaults)  # type: ignore[arg-type]


TICKER = "KXHIGHTCHI-X-B90"


def test_happy_path_buy_yes_fill_and_exact_accounting() -> None:
    res = run([signal()], {TICKER: evidence()})
    assert res.status == "completed"
    assert len(res.fills) == 1
    f = res.fills[0]
    assert (f.side, f.action, f.price_cents, f.quantity) == ("yes", "buy", 55, 1)
    # zero-fee: cash = 10000 - 55; equity at cost unchanged
    assert res.pnl["cash_cents"] == 10_000 - 55
    assert res.pnl["position_cost_cents"] == 55
    assert res.pnl["equity_at_cost_cents"] == 10_000
    assert res.pnl["fees_cents"] == 0
    # mark at liquidation side (bid 52) -> unrealized -3
    assert res.pnl["mark_value_cents"] == 52
    assert res.pnl["unrealized_pnl_cents"] == -3
    assert res.intents[0].execution_confidence == "direct_fresh"


def test_buy_no_side_when_probability_below_market() -> None:
    res = run([signal(p="0.20")], {TICKER: evidence()})
    assert len(res.fills) == 1
    assert res.fills[0].side == "no"
    assert res.fills[0].price_cents == 100 - 52  # crosses the YES bid


def test_no_trade_when_no_edge() -> None:
    res = run([signal(p="0.54")], {TICKER: evidence(ask=(55, 10), bid=(53, 10))})
    assert not res.intents and not res.fills
    assert any(
        d.stage == "translation" and d.reason == "no_positive_edge_after_fees"
        for d in res.decisions
    )


def test_evidence_gates_fail_closed() -> None:
    cases = {
        "no_market_snapshot": {},  # missing evidence entirely
        "market_not_active": {TICKER: evidence(status="settled")},
        "stale_market_snapshot": {TICKER: evidence(snap_age_s=10_000)},
        "stale_book_evidence": {TICKER: evidence(book_age_s=10_000)},
        "missing_poll_evidence": {TICKER: evidence(poll_age_s=None)},
    }
    for expected, ev in cases.items():
        res = run([signal()], ev)  # type: ignore[arg-type]
        assert not res.fills, expected
        assert any(d.reason == expected for d in res.decisions), expected


def test_expired_and_unversioned_signals_rejected() -> None:
    expired = signal(expires_at=NOW - timedelta(minutes=1))
    unversioned = signal(version="")
    res = run([expired, unversioned], {TICKER: evidence()})
    reasons = {d.reason for d in res.decisions}
    assert {"expired_signal", "unversioned_signal"} <= reasons
    assert not res.intents


def test_low_confidence_rejected() -> None:
    res = run([signal(confidence=Decimal("0.1"))], {TICKER: evidence()})
    assert any(d.reason == "low_signal_confidence" for d in res.decisions)


def test_idempotent_rerun_creates_no_duplicate_fill() -> None:
    s = signal()
    first = run([s], {TICKER: evidence()})
    assert len(first.fills) == 1
    seen = {i.order_id for i in first.intents}
    prior = [
        (e.global_seq, e.at, e.kind, e.cash_delta_cents, e.reserved_delta_cents, e.payload)
        for e in first.new_ledger_entries
    ]
    second = run([s], {TICKER: evidence()}, seen_order_ids=seen, prior_entries=prior)
    assert not second.fills
    assert any(d.reason == "duplicate_intent" for d in second.decisions)
    # deterministic key: same signal content -> same order id
    assert intent_order_id("paper-skeleton", "skeleton-v1", s) in seen


def test_partial_fill_when_depth_insufficient() -> None:
    manual = signal(
        source_type=SignalSourceType.INTENT_FILE,
        probability=None,
        side="yes",
        quantity=5,
        limit_price_cents=55,
    )
    res = run([manual], {TICKER: evidence(ask=(55, 3))})
    assert len(res.fills) == 1 and res.fills[0].quantity == 3  # partial: only 3 at the ask
    assert res.intents[0].final_state in ("partially_filled", "canceled", "expired")


def test_no_fill_without_depth_records_reason() -> None:
    manual = signal(
        source_type=SignalSourceType.INTENT_FILE,
        probability=None,
        side="yes",
        quantity=1,
        limit_price_cents=40,  # below best ask -> no marketable level
    )
    res = run([manual], {TICKER: evidence(ask=(55, 10))})
    assert not res.fills
    assert res.intents[0].reject_reason == "insufficient_book_depth_at_limit"


def test_paper_risk_limits_reject_with_reasons() -> None:
    manual_big = signal(
        source_type=SignalSourceType.INTENT_FILE,
        probability=None,
        side="yes",
        quantity=50,
        limit_price_cents=55,
    )
    res = run([manual_big], {TICKER: evidence()})
    assert any(d.reason == "max_contracts_per_intent_exceeded" for d in res.decisions)
    # per-market cap: policy tiny cap forces rejection of a normal intent
    res2 = run(
        [signal()],
        {TICKER: evidence()},
        paper_risk=PaperRiskPolicy(max_exposure_per_market_cents=10),
    )
    assert any(d.reason == "max_exposure_per_market_exceeded" for d in res2.decisions)
    res3 = run(
        [signal()],
        {TICKER: evidence()},
        paper_risk=PaperRiskPolicy(max_exposure_per_station_cents=10),
    )
    assert any(d.reason == "max_exposure_per_station_exceeded" for d in res3.decisions)


def test_daily_loss_limit_blocks_new_intents() -> None:
    res = run(
        [signal()],
        {TICKER: evidence()},
        day_start_equity_cents=25_000,  # implies a 15_000c loss vs current 10_000 equity
    )
    assert any(d.reason == "max_daily_loss_exceeded" for d in res.decisions)
    assert not res.fills


def test_kill_switch_refuses_run() -> None:
    res = run([signal()], {TICKER: evidence()}, kill_active=True, kill_reason="ops")
    assert res.status == "refused_kill_switch"
    assert not res.intents and not res.fills and not res.new_ledger_entries
    assert res.decisions[0].stage == "kill_switch"


def test_deterministic_reruns() -> None:
    a = run([signal()], {TICKER: evidence()})
    b = run([signal()], {TICKER: evidence()})
    assert a.fills == b.fills and a.pnl == b.pnl and a.summary == b.summary


def test_cash_conservation_across_ledger() -> None:
    res = run([signal()], {TICKER: evidence()})
    total_cash_delta = sum(e.cash_delta_cents for e in res.new_ledger_entries)
    assert total_cash_delta == res.pnl["cash_cents"]  # started from empty ledger
    total_reserved = sum(e.reserved_delta_cents for e in res.new_ledger_entries)
    assert total_reserved == res.pnl["reserved_cents"] == 0


def test_real_fee_model_charges_fees_exactly() -> None:
    res = run(
        [signal()],
        {TICKER: evidence()},
        exec_policy=ExecutionPolicy(
            fill_mode=FillMode.MARKETABLE, fee_model=KalshiEventContractFeeModel()
        ),
    )
    assert len(res.fills) == 1
    fee = res.fills[0].fee_cents
    assert fee > 0
    assert res.pnl["fees_cents"] == fee
    assert res.pnl["cash_cents"] == 10_000 - 55 - fee


def test_summary_is_labelled_operational_not_performance() -> None:
    res = run([signal()], {TICKER: evidence()})
    assert "NOT STRATEGY PERFORMANCE" in res.summary["note"]


def test_station_parsing() -> None:
    assert station_of("KXHIGHTCHI-26JUL28-B90") == "CHI"
    assert station_of("KXLOWTNY-26JUL28-B70") == "NY"
    assert station_of("SOMETHING-ELSE") == "UNKNOWN"
