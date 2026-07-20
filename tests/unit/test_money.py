from decimal import Decimal

import pytest

from kalshi_weather.domain.money import (
    InvalidCentsError,
    cents_to_decimal,
    cents_to_probability,
    decimal_to_cents,
)


def test_cents_to_decimal() -> None:
    assert cents_to_decimal(150) == Decimal("1.50")
    assert cents_to_decimal(0) == Decimal("0")
    assert cents_to_decimal(-25) == Decimal("-0.25")


def test_decimal_to_cents_round_trip() -> None:
    assert decimal_to_cents(Decimal("1.50")) == 150
    assert decimal_to_cents(Decimal("0.01")) == 1
    assert decimal_to_cents(Decimal("0.99")) == 99


def test_decimal_to_cents_rejects_sub_cent_precision() -> None:
    with pytest.raises(InvalidCentsError):
        decimal_to_cents(Decimal("1.005"))


def test_cents_to_probability_boundaries() -> None:
    assert cents_to_probability(1) == Decimal("0.01")
    assert cents_to_probability(99) == Decimal("0.99")
