"""Tests for scripts/h0007_fees.py. `_VERIFIED_TEST_CONFIG` below shares its
multiplier (0.07) with the now-verified `KALSHI_WEATHER_TAKER_FEE_CONFIG`
(AMENDMENT-20260721-H0007-fee-verified.md) -- these first tests exercise the
function's general arithmetic/rounding behavior in isolation;
`test_matches_official_general_trading_fees_table` below is the actual
verification, checked directly against every row of Kalshi's own published
worked-example table."""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from h0007_fees import KALSHI_WEATHER_TAKER_FEE_CONFIG, FeeConfig, contract_fee_cents

_TEST_MULTIPLIER = Decimal("0.07")
_VERIFIED_TEST_CONFIG = FeeConfig(
    multiplier=_TEST_MULTIPLIER,
    source="test fixture matching KALSHI_WEATHER_TAKER_FEE_CONFIG's multiplier",
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


def test_kalshi_weather_taker_fee_config_is_marked_verified() -> None:
    assert KALSHI_WEATHER_TAKER_FEE_CONFIG.verified is True
    assert KALSHI_WEATHER_TAKER_FEE_CONFIG.multiplier == Decimal("0.07")


# Every (price, fee) pair below is transcribed verbatim from Kalshi's own
# "General Trading Fees Table" (kalshi-fee-schedule-2026-07-07.pdf, pages
# 4-5, "Last updated and effective: July 7, 2026") -- the ground truth this
# task exists to verify against, not a derived or assumed value.
_OFFICIAL_TABLE_1_CONTRACT = [
    (1, 1), (5, 1), (10, 1), (15, 1), (20, 2), (25, 2), (30, 2), (35, 2),
    (40, 2), (45, 2), (50, 2), (55, 2), (60, 2), (65, 2), (70, 2), (75, 2),
    (80, 2), (85, 1), (90, 1), (95, 1), (99, 1),
]
_OFFICIAL_TABLE_100_CONTRACTS = [
    (1, 7), (5, 34), (10, 63), (15, 90), (20, 112), (25, 132), (30, 147),
    (35, 160), (40, 168), (45, 174), (50, 175), (55, 174), (60, 168),
    (65, 160), (70, 147), (75, 132), (80, 112), (85, 90), (90, 63),
    (95, 34), (99, 7),
]


def test_matches_official_general_trading_fees_table_1_contract() -> None:
    for price_cents, expected_fee_cents in _OFFICIAL_TABLE_1_CONTRACT:
        got = contract_fee_cents(price_cents, contracts=1, config=KALSHI_WEATHER_TAKER_FEE_CONFIG)
        msg = f"price={price_cents}c: expected {expected_fee_cents}, got {got}"
        assert got == expected_fee_cents, msg


def test_matches_official_general_trading_fees_table_100_contracts() -> None:
    for price_cents, expected_fee_cents in _OFFICIAL_TABLE_100_CONTRACTS:
        got = contract_fee_cents(price_cents, contracts=100, config=KALSHI_WEATHER_TAKER_FEE_CONFIG)
        msg = f"price={price_cents}c: expected {expected_fee_cents}, got {got}"
        assert got == expected_fee_cents, msg
