"""Money as integer cents. Never use binary floats for prices or balances."""

from decimal import ROUND_HALF_EVEN, Decimal

CENTS_PER_DOLLAR = 100


class InvalidCentsError(ValueError):
    """Raised when a value cannot be represented as whole cents."""


def cents_to_decimal(cents: int) -> Decimal:
    """Convert integer cents to a Decimal dollar amount."""
    return Decimal(cents) / CENTS_PER_DOLLAR


def decimal_to_cents(amount: Decimal) -> int:
    """Convert a Decimal dollar amount to integer cents, rejecting sub-cent precision."""
    scaled = amount * CENTS_PER_DOLLAR
    quantized = scaled.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    if quantized != scaled:
        raise InvalidCentsError(f"{amount} is not representable in whole cents")
    return int(quantized)


def cents_to_probability(cents: int) -> Decimal:
    """Convert a Kalshi price in cents (1-99 range typical) to a [0, 1] probability."""
    return cents_to_decimal(cents)
