"""Versioned, explicit execution assumptions, fee model, and risk config.

Nothing here has a hidden default that changes accounting silently: the policy
version is recorded on every fill and in every run manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from kalshi_weather.execution.fees import (
    ZERO_FEE_MODEL,
    ConfigurableFeeModel,
    FeeModelLike,
    KalshiEventContractFeeModel,
    ZeroFeeModel,
)

#: Backward-compatible alias: ``FeeModel`` historically named the configurable
#: placeholder. Keep it pointing at ``ConfigurableFeeModel`` so existing configs,
#: tests, and imports keep working. New code should name a concrete model.
FeeModel = ConfigurableFeeModel

__all__ = [
    "ZERO_FEE_MODEL",
    "ConfigurableFeeModel",
    "ExecutionPolicy",
    "FeeModel",
    "FeeModelLike",
    "FillMode",
    "KalshiEventContractFeeModel",
    "RiskConfig",
    "ZeroFeeModel",
]


class FillMode(StrEnum):
    SYNTHETIC = "synthetic"  # Mode C: exact fixtures (default for tests)
    MARKETABLE = "marketable"  # Mode A: cross the visible book
    PASSIVE = "passive"  # Mode B: conservative resting-fill via later volume


# --- execution policy -------------------------------------------------------


@dataclass(frozen=True)
class ExecutionPolicy:
    version: str = "exec-policy-v1"
    fill_mode: FillMode = FillMode.SYNTHETIC
    fee_model: FeeModelLike = field(default_factory=lambda: ZERO_FEE_MODEL)
    #: Mode A/B slippage is implicit in consuming real book levels; no extra
    #: adverse adjustment is added (documented as conservative-neutral).
    slippage_extra_cents: int = 0
    #: Mode B queue model: assume this many contracts sit ahead of ours at our
    #: price; a passive order needs MORE subsequent volume-through than this.
    queue_ahead_contracts: int = 0
    #: Reject a fill attempt if the referenced book is older than this (seconds).
    max_book_age_seconds: float = 300.0
    #: Reject if the book's spread exceeds this.
    max_spread_cents: int = 100
    #: What to do with an unfilled remainder on an IOC/immediate order.
    cancel_remainder_on_ioc: bool = True
    #: Missing book/data => no fill (never invent a price).
    fill_on_missing_data: bool = False
    price_min_cents: int = 1
    price_max_cents: int = 99
    random_seed: int = 0  # no randomness is used; recorded for reproducibility

    def to_manifest(self) -> dict[str, object]:
        return {
            "version": self.version,
            "fill_mode": self.fill_mode.value,
            "fee_model_version": self.fee_model.version,
            "fee_model": self.fee_model.to_manifest(),
            "slippage_extra_cents": self.slippage_extra_cents,
            "queue_ahead_contracts": self.queue_ahead_contracts,
            "max_book_age_seconds": self.max_book_age_seconds,
            "max_spread_cents": self.max_spread_cents,
            "cancel_remainder_on_ioc": self.cancel_remainder_on_ioc,
            "fill_on_missing_data": self.fill_on_missing_data,
            "price_bounds_cents": [self.price_min_cents, self.price_max_cents],
            "random_seed": self.random_seed,
        }


# --- risk configuration -----------------------------------------------------


@dataclass(frozen=True)
class RiskConfig:
    version: str = "risk-v1"
    max_order_quantity: int = 1000
    max_position_per_market: int = 5000
    max_gross_exposure_cents: int = 100_000_00
    max_total_possible_loss_cents: int = 100_000_00
    max_loss_per_event_cents: int = 50_000_00
    max_open_orders: int = 200
    max_markets_with_exposure: int = 100
    min_available_cash_cents: int = 0
    max_book_age_seconds: float = 300.0
    max_spread_cents: int = 100
    price_min_cents: int = 1
    price_max_cents: int = 99

    def to_manifest(self) -> dict[str, object]:
        return {k: v for k, v in self.__dict__.items()}
