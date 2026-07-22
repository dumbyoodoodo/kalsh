"""Tests for kalshi_weather.verification.metrics."""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from kalshi_weather.verification.metrics import (
    absolute_error,
    bias,
    error_sd,
    mae,
    quantize_value,
    rmse,
    signed_error,
)


def test_quantize_value_uses_str_construction() -> None:
    via_str = Decimal(str(0.1))
    via_direct = Decimal(0.1)  # noqa: RUF032 -- the point of this test is this exact call
    assert via_str != via_direct
    assert quantize_value(0.1) == via_str


def test_signed_error_sign_convention() -> None:
    assert signed_error(Decimal("82"), Decimal("80")) == Decimal("2")  # forecast ran high
    assert signed_error(Decimal("78"), Decimal("80")) == Decimal("-2")


def test_absolute_error_always_nonnegative() -> None:
    assert absolute_error(Decimal("82"), Decimal("80")) == Decimal("2")
    assert absolute_error(Decimal("78"), Decimal("80")) == Decimal("2")


def test_bias_is_mean_signed_error() -> None:
    pairs = [(Decimal("82"), Decimal("80")), (Decimal("78"), Decimal("80"))]
    assert bias(pairs) == Decimal("0")


def test_bias_nonzero_when_consistently_high() -> None:
    pairs = [(Decimal("82"), Decimal("80")), (Decimal("83"), Decimal("80"))]
    assert bias(pairs) == Decimal("2.5")


def test_bias_empty_raises() -> None:
    with pytest.raises(ValueError, match="bias"):
        bias([])


def test_mae_is_mean_absolute_error() -> None:
    pairs = [(Decimal("82"), Decimal("80")), (Decimal("78"), Decimal("80"))]
    assert mae(pairs) == Decimal("2")  # both errors are magnitude 2, signs cancel in bias not mae


def test_mae_empty_raises() -> None:
    with pytest.raises(ValueError, match="mae"):
        mae([])


def test_rmse_hand_calculated() -> None:
    pairs = [(Decimal("83"), Decimal("80")), (Decimal("77"), Decimal("80"))]
    # errors: +3, -3 -> squared: 9, 9 -> mean 9 -> sqrt 3
    assert rmse(pairs) == Decimal("3")


def test_rmse_empty_raises() -> None:
    with pytest.raises(ValueError, match="rmse"):
        rmse([])


def test_rmse_penalizes_large_errors_more_than_mae() -> None:
    pairs = [(Decimal("80"), Decimal("80")), (Decimal("90"), Decimal("80"))]  # errors 0, 10
    assert rmse(pairs) > mae(pairs)


def test_error_sd_requires_at_least_two_pairs() -> None:
    with pytest.raises(ValueError, match="error_sd"):
        error_sd([(Decimal("80"), Decimal("80"))])


def test_error_sd_hand_calculated() -> None:
    pairs = [
        (Decimal("82"), Decimal("80")),
        (Decimal("78"), Decimal("80")),
        (Decimal("82"), Decimal("80")),
        (Decimal("78"), Decimal("80")),
    ]
    # errors: 2,-2,2,-2 -> mean 0 -> variance = (4+4+4+4)/3 = 16/3 -> sd = sqrt(16/3)
    # computed at the same 50-digit precision the library itself uses, so
    # this is an apples-to-apples independent re-derivation, not merely a
    # lower-precision approximation.
    with localcontext() as ctx:
        ctx.prec = 50
        expected = (Decimal(16) / Decimal(3)).sqrt()
    assert abs(error_sd(pairs) - expected) < Decimal("1E-30")


def test_zero_error_everywhere() -> None:
    pairs = [(Decimal("80"), Decimal("80"))] * 3
    assert bias(pairs) == Decimal("0")
    assert mae(pairs) == Decimal("0")
    assert rmse(pairs) == Decimal("0")
