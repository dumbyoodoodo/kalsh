"""Tests for scripts/h0007_fees.py (AMENDMENT-20260721-H0007-pre-execution
Finding 5). These test the FEE FUNCTION's mathematical correctness and its
safety gate against an unverified config -- they do NOT assert anything
about Kalshi's actual current fee constant, which remains unverified/blocked
(see the amendment). The `_TEST_MULTIPLIER` below is an illustrative value
for exercising the formula's arithmetic only."""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from h0007_fees import FeeConfig, contract_fee_cents

# Illustrative only -- NOT a verified Kalshi constant. Used solely to
# exercise contract_fee_cents' rounding/edge-case behavior deterministically.
_TEST_MULTIPLIER = Decimal("0.07")
_VERIFIED_TEST_CONFIG = FeeConfig(
    multiplier=_TEST_MULTIPLIER,
    source="test fixture, not Kalshi's official schedule",
    verified=True,
)
_UNVERIFIED_CONFIG = FeeConfig(
    multiplier=_TEST_MULTIPLIER, source="unverified placeholder", verified=False
)


def test_unverified_config_refuses_to_compute() -> None:
    """The safety gate: an unverified config must raise, never silently
    compute -- this is the mechanism keeping H0007 blocked on fees."""
    with pytest.raises(ValueError, match="not verified"):
        contract_fee_cents(50, contracts=1, config=_UNVERIFIED_CONFIG)


def test_fee_at_50_cents_hand_calculated() -> None:
    # 0.07 * 0.5 * 0.5 * 100 = 1.75 -> ceil -> 2
    assert contract_fee_cents(50, contracts=1, config=_VERIFIED_TEST_CONFIG) == 2


def test_fee_at_10_cents_hand_calculated() -> None:
    # 0.07 * 0.1 * 0.9 * 100 = 0.63 -> ceil -> 1
    assert contract_fee_cents(10, contracts=1, config=_VERIFIED_TEST_CONFIG) == 1


def test_fee_at_1_cent_hand_calculated() -> None:
    # 0.07 * 0.01 * 0.99 * 100 = 0.0693 -> ceil -> 1 (minimum, never zero)
    assert contract_fee_cents(1, contracts=1, config=_VERIFIED_TEST_CONFIG) == 1


def test_fee_at_99_cents_hand_calculated() -> None:
    # 0.07 * 0.99 * 0.01 * 100 = 0.0693 -> ceil -> 1
    assert contract_fee_cents(99, contracts=1, config=_VERIFIED_TEST_CONFIG) == 1


def test_fee_is_symmetric_around_50_cents() -> None:
    """p*(1-p) is symmetric -- fee(P) == fee(100-P) for any P, a property of
    the formula's shape, independent of the (unverified) constant."""
    for price in (5, 20, 35, 49):
        low = contract_fee_cents(price, contracts=1, config=_VERIFIED_TEST_CONFIG)
        high = contract_fee_cents(100 - price, contracts=1, config=_VERIFIED_TEST_CONFIG)
        assert low == high


def test_fee_scales_with_contract_count() -> None:
    # 0.07 * 0.5 * 0.5 * 100 * 5 = 8.75 -> ceil -> 9
    assert contract_fee_cents(50, contracts=5, config=_VERIFIED_TEST_CONFIG) == 9


def test_fee_never_rounds_down() -> None:
    """Every hand-calculated case above lands on a non-integer raw cents
    value; the result must always be the ceiling, never truncated/rounded
    to nearest."""
    raw = _TEST_MULTIPLIER * Decimal("0.5") * Decimal("0.5") * Decimal(100)
    assert raw == Decimal("1.75")
    assert contract_fee_cents(50, contracts=1, config=_VERIFIED_TEST_CONFIG) == 2  # not 1 or 1.75


def test_price_out_of_tradeable_range_rejected() -> None:
    with pytest.raises(ValueError, match="1-99"):
        contract_fee_cents(0, contracts=1, config=_VERIFIED_TEST_CONFIG)
    with pytest.raises(ValueError, match="1-99"):
        contract_fee_cents(100, contracts=1, config=_VERIFIED_TEST_CONFIG)


def test_zero_or_negative_contracts_rejected() -> None:
    with pytest.raises(ValueError, match="contracts"):
        contract_fee_cents(50, contracts=0, config=_VERIFIED_TEST_CONFIG)
