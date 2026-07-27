"""Coverage-gate, per-market classification, and fill-confidence tests.

All synthetic; deterministic. Accounting correctness is deliberately NOT an input
to any confidence grade here (that is asserted elsewhere).
"""

from datetime import UTC, datetime, timedelta

import pytest

from kalshi_weather.execution.coverage import (
    FillConfidence,
    GateSeverity,
    ReplayConfidence,
    ReplayCoveragePolicy,
    evaluate_coverage,
    fill_confidence,
    market_quality,
)
from kalshi_weather.execution.fills import compatible_taker_side, simulate_passive
from kalshi_weather.execution.history import (
    ExclusionCounts,
    HistoricalReplayData,
    QualityReport,
)
from kalshi_weather.execution.market import MarketData, OrderBook, Trade
from kalshi_weather.execution.models import Action, OrderIntent, OrderType, Side, TimeInForce
from kalshi_weather.execution.policy import ExecutionPolicy
from kalshi_weather.execution.replay import SettlementEvent


def T(h: int, m: int = 0) -> datetime:
    return datetime(2026, 7, 26, h, m, tzinfo=UTC)


def _data(books, trades=(), settlements=(), per_market=None):  # type: ignore[no-untyped-def]
    q = QualityReport()
    q.per_market = per_market or {}
    return HistoricalReplayData(
        market_data=MarketData(order_books=list(books), trades=list(trades)),
        settlements=list(settlements),
        market_status=[],
        counts=ExclusionCounts(),
        quality=q,
    )


def _book(tk, at):  # type: ignore[no-untyped-def]
    return OrderBook(tk, at, yes_asks=((50, 10),), yes_bids=((48, 10),), source_ref=f"b:{tk}:{at}")


# --- per-market classification ---------------------------------------------


def test_high_quality_market_is_high_confidence() -> None:
    # fresh books every 2 min, trades present, settlement present
    books = [_book("A", T(10) + timedelta(minutes=2 * i)) for i in range(6)]
    trades = [Trade("A", T(10, 1), yes_price_cents=50, quantity=1, taker_side=Side.NO)]
    d = _data(books, trades, [SettlementEvent("A", Side.YES, T(18))])
    mq = market_quality(d, "A", ReplayCoveragePolicy())
    assert mq.confidence is ReplayConfidence.HIGH_CONFIDENCE
    assert mq.marketable_eligible and mq.passive_eligible and mq.production_pct == 100.0


def test_sparse_book_market_flags_stale() -> None:
    # two books 30 min apart -> exceeds max_individual_book_age (900s)
    d = _data([_book("A", T(10)), _book("A", T(10, 30))])
    mq = market_quality(d, "A", ReplayCoveragePolicy())
    assert mq.max_book_age_seconds == 1800
    assert mq.confidence is ReplayConfidence.LIMITED_CONFIDENCE
    assert any("book gap" in r for r in mq.reasons)


def test_no_trade_market_is_passive_ineligible() -> None:
    books = [_book("A", T(10) + timedelta(minutes=i)) for i in range(3)]
    mq = market_quality(_data(books), "A", ReplayCoveragePolicy())
    assert mq.marketable_eligible is True
    assert mq.passive_eligible is False  # passive needs trade coverage


def test_no_book_market_is_insufficient() -> None:
    d = _data([], trades=[Trade("A", T(10), yes_price_cents=50, quantity=1)])
    mq = market_quality(d, "A", ReplayCoveragePolicy())
    assert mq.book_count == 0
    assert not mq.marketable_eligible and mq.confidence is ReplayConfidence.INSUFFICIENT_DATA


def test_conflicts_make_market_insufficient() -> None:
    d = _data([_book("A", T(10))], per_market={"A": {"book_conflicts": 2}})
    mq = market_quality(d, "A", ReplayCoveragePolicy())
    assert not mq.marketable_eligible and mq.confidence is ReplayConfidence.INSUFFICIENT_DATA


# --- global gates + verdict ------------------------------------------------


def test_all_pass_high_confidence() -> None:
    books = [_book("A", T(10) + timedelta(minutes=2 * i)) for i in range(4)]
    d = _data(books, [Trade("A", T(10, 1), yes_price_cents=50, quantity=1, taker_side=Side.NO)])
    cov = evaluate_coverage(d, ("A",), ReplayCoveragePolicy())
    assert cov.verdict is ReplayConfidence.HIGH_CONFIDENCE
    assert not cov.failed_gates() and not cov.warning_gates()


def test_warning_only_limited_confidence() -> None:
    # median book age > 300s (warning) but < 900s (required ok)
    books = [_book("A", T(10) + timedelta(seconds=400 * i)) for i in range(4)]
    cov = evaluate_coverage(_data(books), ("A",), ReplayCoveragePolicy())
    assert cov.verdict is ReplayConfidence.LIMITED_CONFIDENCE
    assert any(
        g.name == "median_book_age_seconds" and g.severity is GateSeverity.WARNING
        for g in cov.gates
    )


def test_missing_book_is_fatal() -> None:
    d = _data([], trades=[Trade("A", T(10), yes_price_cents=50, quantity=1)])
    cov = evaluate_coverage(d, ("A",), ReplayCoveragePolicy())
    assert cov.verdict is ReplayConfidence.INSUFFICIENT_DATA
    assert any(
        g.name == "ticker_book_coverage_pct" and g.severity is GateSeverity.FAIL for g in cov.gates
    )


def test_missing_settlement_fatal_when_configured() -> None:
    books = [_book("A", T(10) + timedelta(minutes=2 * i)) for i in range(3)]
    pol = ReplayCoveragePolicy(
        missing_settlement_fatal=True, min_ticker_settlement_coverage_pct=100.0
    )
    cov = evaluate_coverage(_data(books), ("A",), pol)
    assert cov.verdict is ReplayConfidence.INSUFFICIENT_DATA


def test_conflict_threshold_fatal() -> None:
    books = [_book("A", T(10) + timedelta(minutes=2 * i)) for i in range(3)]
    cov = evaluate_coverage(
        _data(books, per_market={"A": {"book_conflicts": 1}}), ("A",), ReplayCoveragePolicy()
    )
    assert any(g.name == "conflicts" and g.severity is GateSeverity.FAIL for g in cov.gates)
    assert cov.verdict is ReplayConfidence.INSUFFICIENT_DATA


def test_provenance_failure_when_nonproduction_admitted() -> None:
    books = [_book("A", T(10) + timedelta(minutes=2 * i)) for i in range(3)]
    d = _data(books, per_market={"A": {"admitted_nonproduction": 1}})
    cov = evaluate_coverage(d, ("A",), ReplayCoveragePolicy())
    assert any(g.name == "production_pct" and g.severity is GateSeverity.FAIL for g in cov.gates)


def test_mixed_quality_multimarket_identifies_bad_market() -> None:
    good = [_book("GOOD", T(10) + timedelta(minutes=2 * i)) for i in range(4)]
    bad: list[OrderBook] = []  # no books for BAD
    d = _data(
        good + bad,
        trades=[Trade("GOOD", T(10, 1), yes_price_cents=50, quantity=1, taker_side=Side.NO)],
    )
    cov = evaluate_coverage(d, ("GOOD", "BAD"), ReplayCoveragePolicy())
    by = {m.ticker: m for m in cov.markets}
    assert by["GOOD"].confidence is ReplayConfidence.HIGH_CONFIDENCE
    assert by["BAD"].confidence is ReplayConfidence.INSUFFICIENT_DATA
    assert cov.verdict is ReplayConfidence.INSUFFICIENT_DATA  # one unusable market


# --- determinism -----------------------------------------------------------


def test_coverage_is_deterministic_and_order_independent() -> None:
    books = [_book("A", T(10) + timedelta(minutes=2 * i)) for i in range(4)]
    d1 = _data(books)
    d2 = _data(list(reversed(books)))  # different row order
    c1 = evaluate_coverage(d1, ("A",), ReplayCoveragePolicy())
    c2 = evaluate_coverage(d2, ("A",), ReplayCoveragePolicy())
    assert c1.to_manifest() == c2.to_manifest()


def test_policy_version_change_distinguishes_manifest() -> None:
    books = [_book("A", T(10) + timedelta(minutes=2 * i)) for i in range(4)]
    d = _data(books)
    a = evaluate_coverage(d, ("A",), ReplayCoveragePolicy(version="v1"))
    b = evaluate_coverage(d, ("A",), ReplayCoveragePolicy(version="v2"))
    assert a.to_manifest()["coverage_policy_version"] != b.to_manifest()["coverage_policy_version"]


# --- fill confidence -------------------------------------------------------


def _hi_market() -> object:
    books = [_book("A", T(10) + timedelta(minutes=2 * i)) for i in range(4)]
    return market_quality(
        _data(books, [Trade("A", T(10, 1), yes_price_cents=50, quantity=1, taker_side=Side.NO)]),
        "A",
        ReplayCoveragePolicy(),
    )


def test_fresh_marketable_fill_high() -> None:
    pol = ReplayCoveragePolicy()
    c = fill_confidence(
        fill_mode="marketable",
        is_passive=False,
        book_age_seconds=60,
        market=_hi_market(),
        policy=pol,
        unsafe_override=False,
    )  # type: ignore[arg-type]
    assert c is FillConfidence.HIGH


def test_stale_marketable_fill_unsupported() -> None:
    pol = ReplayCoveragePolicy()
    c = fill_confidence(
        fill_mode="marketable",
        is_passive=False,
        book_age_seconds=1000,
        market=_hi_market(),
        policy=pol,
        unsafe_override=False,
    )  # type: ignore[arg-type]
    assert c is FillConfidence.UNSUPPORTED  # beyond max_individual_book_age


def test_passive_fill_is_low() -> None:
    pol = ReplayCoveragePolicy()
    c = fill_confidence(
        fill_mode="passive",
        is_passive=True,
        book_age_seconds=60,
        market=_hi_market(),
        policy=pol,
        unsafe_override=False,
    )  # type: ignore[arg-type]
    assert c is FillConfidence.LOW


def test_unsafe_override_downgrades_to_medium() -> None:
    pol = ReplayCoveragePolicy()
    c = fill_confidence(
        fill_mode="marketable",
        is_passive=False,
        book_age_seconds=60,
        market=_hi_market(),
        policy=pol,
        unsafe_override=True,
    )  # type: ignore[arg-type]
    assert c is FillConfidence.MEDIUM


# --- trade-direction-aware passive fills -----------------------------------


def _passive(side: Side, action: Action, price: int) -> OrderIntent:
    return OrderIntent(
        order_id="p",
        strategy_id="s",
        ticker="M",
        side=side,
        action=action,
        order_type=OrderType.PASSIVE_LIMIT,
        limit_price_cents=price,
        quantity=5,
        submitted_at=T(9),
        time_in_force=TimeInForce.GTC,
    )


def test_compatible_taker_side_mapping() -> None:
    assert compatible_taker_side(Side.YES, Action.BUY) is Side.NO
    assert compatible_taker_side(Side.YES, Action.SELL) is Side.YES
    assert compatible_taker_side(Side.NO, Action.BUY) is Side.YES
    assert compatible_taker_side(Side.NO, Action.SELL) is Side.NO


def test_passive_fills_on_compatible_direction() -> None:
    # resting YES bid @ 40 filled by taker aggressively SELLING yes (taker_side NO)
    trades = [Trade("M", T(10), yes_price_cents=39, quantity=8, taker_side=Side.NO)]
    fills = simulate_passive(_passive(Side.YES, Action.BUY, 40), trades, ExecutionPolicy())
    assert fills and fills[0].quantity == 5


def test_passive_excludes_incompatible_direction() -> None:
    # taker BUYING yes (taker_side YES) does not fill a resting YES bid
    trades = [Trade("M", T(10), yes_price_cents=39, quantity=8, taker_side=Side.YES)]
    fills = simulate_passive(_passive(Side.YES, Action.BUY, 40), trades, ExecutionPolicy())
    assert fills == []


def test_passive_excludes_unknown_direction_by_default() -> None:
    trades = [Trade("M", T(10), yes_price_cents=39, quantity=8, taker_side=None)]
    fills = simulate_passive(_passive(Side.YES, Action.BUY, 40), trades, ExecutionPolicy())
    assert fills == []
    # unless explicitly allowed
    fills2 = simulate_passive(
        _passive(Side.YES, Action.BUY, 40),
        trades,
        ExecutionPolicy(passive_allow_unknown_taker=True),
    )
    assert fills2 and fills2[0].quantity == 5


def test_enforcement_refuses_insufficient_unless_override() -> None:
    from kalshi_weather.execution.history_replay import (
        InsufficientCoverageError,
        enforce_coverage,
    )

    d = _data([], trades=[Trade("A", T(10), yes_price_cents=50, quantity=1)])
    cov = evaluate_coverage(d, ("A",), ReplayCoveragePolicy())
    assert cov.verdict is ReplayConfidence.INSUFFICIENT_DATA
    with pytest.raises(InsufficientCoverageError):
        enforce_coverage(cov, allow_insufficient=False)
    enforce_coverage(cov, allow_insufficient=True)  # override: does not raise

    # HIGH/LIMITED never blocks
    good = [_book("A", T(10) + timedelta(minutes=2 * i)) for i in range(4)]
    hi = evaluate_coverage(_data(good), ("A",), ReplayCoveragePolicy())
    enforce_coverage(hi, allow_insufficient=False)


def test_passive_truncates_at_trade_gap() -> None:
    # first trade at T(10), then a >15min gap, then more volume -> only first counts
    trades = [
        Trade("M", T(10), yes_price_cents=39, quantity=2, taker_side=Side.NO),
        Trade("M", T(10, 20), yes_price_cents=39, quantity=8, taker_side=Side.NO),  # 20min later
    ]
    pol = ExecutionPolicy(max_passive_trade_gap_seconds=900)
    fills = simulate_passive(_passive(Side.YES, Action.BUY, 40), trades, pol)
    assert fills and fills[0].quantity == 2  # volume beyond the gap is excluded
