"""Fee-aware probability->decision translation: exact integer/Decimal
arithmetic, fee integration, draft-threshold gating, and symmetry."""

from decimal import Decimal

import pytest

from kalshi_weather.execution.fees import ZERO_FEE_MODEL, KalshiEventContractFeeModel
from kalshi_weather.research.translation import (
    DecisionAction,
    DraftDecisionThresholds,
    TranslationError,
    translate_probability,
)

T0 = DraftDecisionThresholds(min_net_edge_centicents=0)


def test_zero_fee_edges_are_exact_centicents() -> None:
    # p=0.60 vs YES at 55c: gross YES edge = 60.00c - 55.00c = 500 centicents.
    d = translate_probability(
        model_probability=Decimal("0.60"),
        yes_price_cents=55,
        ticker="T",
        fee_model=ZERO_FEE_MODEL,
        thresholds=T0,
    )
    assert d.action is DecisionAction.BUY_YES
    assert d.gross_edge_centicents == 500
    assert d.fee_centicents == 0
    assert d.net_edge_centicents == 500


def test_buy_no_side_symmetric() -> None:
    # p=0.40 vs YES at 45c: NO fair = 60c, NO cost = 55c -> +500cc on NO.
    d = translate_probability(
        model_probability=Decimal("0.40"),
        yes_price_cents=45,
        ticker="T",
        fee_model=ZERO_FEE_MODEL,
        thresholds=T0,
    )
    assert d.action is DecisionAction.BUY_NO
    assert d.gross_edge_centicents == 500


def test_no_positive_side_yields_no_trade() -> None:
    d = translate_probability(
        model_probability=Decimal("0.50"),
        yes_price_cents=50,
        ticker="T",
        fee_model=ZERO_FEE_MODEL,
        thresholds=T0,
    )
    assert d.action is DecisionAction.NO_TRADE
    assert d.reason == "no side has positive gross edge"


def test_real_fee_model_reduces_edge_and_can_gate_out_trade() -> None:
    fees = KalshiEventContractFeeModel()
    d = translate_probability(
        model_probability=Decimal("0.60"),
        yes_price_cents=55,
        ticker="KXHIGHCHI-TEST",
        fee_model=fees,
        thresholds=T0,
    )
    # taker fee on 1 @ 55c (general schedule): ceil(0.07*0.55*0.45*100)c-level
    assert d.fee_centicents > 0
    assert d.net_edge_centicents == d.gross_edge_centicents - d.fee_centicents
    assert d.fee_model_version == fees.version
    # a draft threshold above the achievable net edge forces NO_TRADE
    gated = translate_probability(
        model_probability=Decimal("0.60"),
        yes_price_cents=55,
        ticker="KXHIGHCHI-TEST",
        fee_model=fees,
        thresholds=DraftDecisionThresholds(min_net_edge_centicents=10_000),
    )
    assert gated.action is DecisionAction.NO_TRADE
    assert "below draft threshold" in gated.reason


def test_decision_records_draft_version_always() -> None:
    d = translate_probability(
        model_probability=Decimal("0.9"),
        yes_price_cents=50,
        ticker="T",
        fee_model=ZERO_FEE_MODEL,
        thresholds=T0,
    )
    assert "draft" in d.thresholds_version


def test_thresholds_must_declare_draft_and_be_nonnegative() -> None:
    with pytest.raises(TranslationError, match="draft"):
        DraftDecisionThresholds(min_net_edge_centicents=0, version="v1-final")
    with pytest.raises(TranslationError, match=">= 0"):
        DraftDecisionThresholds(min_net_edge_centicents=-1)


def test_input_validation() -> None:
    with pytest.raises(TranslationError, match=r"outside \[1, 99\]"):
        translate_probability(
            model_probability=Decimal("0.5"),
            yes_price_cents=0,
            ticker="T",
            fee_model=ZERO_FEE_MODEL,
            thresholds=T0,
        )
    with pytest.raises(TranslationError, match=r"outside \[0, 1\]"):
        translate_probability(
            model_probability=Decimal("1.5"),
            yes_price_cents=50,
            ticker="T",
            fee_model=ZERO_FEE_MODEL,
            thresholds=T0,
        )
    with pytest.raises(TranslationError, match="quantity"):
        translate_probability(
            model_probability=Decimal("0.5"),
            yes_price_cents=40,
            ticker="T",
            fee_model=ZERO_FEE_MODEL,
            thresholds=T0,
            quantity=0,
        )
