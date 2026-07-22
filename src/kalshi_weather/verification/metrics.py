"""Generic forecast-error arithmetic: signed error, absolute error, bias,
MAE, RMSE.

These are pure point-estimate utility functions -- no confidence
intervals, no significance tests, no thresholds, no interpretation. A
hypothesis's own pre-registration (H0003, M-01, H0006) decides what to do
with these numbers, including how to construct any interval on them; this
module only computes the numbers themselves, deterministically.

All arithmetic is Decimal (never binary float), matching the program-wide
reproducibility convention: a value that started as a float must be
constructed via `Decimal(str(v))`, never `Decimal(v)` directly, to avoid
importing the float's binary-exact expansion.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

DECIMAL_CONTEXT_PRECISION = 50

ErrorPair = tuple[Decimal, Decimal]  # (forecast, observed)


def quantize_value(raw: float) -> Decimal:
    """`Decimal(str(v))` construction -- never `Decimal(v)` directly."""
    return Decimal(str(raw))


def signed_error(forecast: Decimal, observed: Decimal) -> Decimal:
    """forecast - observed. Positive means the forecast ran high."""
    return forecast - observed


def absolute_error(forecast: Decimal, observed: Decimal) -> Decimal:
    return abs(forecast - observed)


def _require_pairs(pairs: Sequence[ErrorPair], fn_name: str) -> None:
    if not pairs:
        raise ValueError(f"{fn_name} requires at least one (forecast, observed) pair")


def bias(pairs: Sequence[ErrorPair]) -> Decimal:
    """Mean signed error -- the point estimate only, no CI."""
    _require_pairs(pairs, "bias")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        total = sum((f - o for f, o in pairs), Decimal(0))
        return total / Decimal(len(pairs))


def mae(pairs: Sequence[ErrorPair]) -> Decimal:
    """Mean absolute error -- the point estimate only, no CI."""
    _require_pairs(pairs, "mae")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        total = sum((abs(f - o) for f, o in pairs), Decimal(0))
        return total / Decimal(len(pairs))


def rmse(pairs: Sequence[ErrorPair]) -> Decimal:
    """Root mean squared error."""
    _require_pairs(pairs, "rmse")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        total = sum(((f - o) ** 2 for f, o in pairs), Decimal(0))
        return (total / Decimal(len(pairs))).sqrt()


def error_sd(pairs: Sequence[ErrorPair]) -> Decimal:
    """Sample standard deviation of signed error (dispersion), ddof=1.
    Requires at least 2 pairs; a single pair has no defined dispersion."""
    if len(pairs) < 2:
        raise ValueError("error_sd requires at least 2 pairs")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        n = Decimal(len(pairs))
        mean = sum((f - o for f, o in pairs), Decimal(0)) / n
        var = sum(((f - o - mean) ** 2 for f, o in pairs), Decimal(0)) / (n - 1)
        return var.sqrt()
