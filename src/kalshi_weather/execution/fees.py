"""Versioned fee models for the execution simulator.

Three concrete implementations behind one structural interface (``FeeModelLike``):

* ``ZeroFeeModel``           -- always $0.00 (the safe default).
* ``ConfigurableFeeModel``   -- the original labelled PLACEHOLDER (a simple
  bps-on-notional-risk form). NOT a match to any real schedule; kept so existing
  runs stay reproducible.
* ``KalshiEventContractFeeModel`` -- an AUTHORITATIVE event-contract fee model
  grounded ONLY in official Kalshi sources (see below). Coefficients and
  rounding are taken verbatim from those documents; nothing is inferred from
  memory or third-party calculators.

Authoritative sources (see docs/adr/0016-kalshi-fee-model.md for the full
citation, quoted formulas, and worked-example tables):

1. Kalshi Fee Schedule, filed with the CFTC (cftc.gov, filing rule091222kexdcm003,
   effective 2022-09-12). Gives the general trade-fee formula
   ``fees = round up(0.07 x C x P x (1 - P))`` and the S&P-500 / NASDAQ-100
   special schedule ``round up(0.035 x C x P x (1 - P))`` for tickers beginning
   ``INX`` / ``NASDAQ100``. P is price in dollars, C is number of contracts,
   "round up" is to the next cent. No settlement fee; resting orders are not
   charged a trade fee in that filing.
2. Kalshi Predictions API documentation, "Fee Rounding"
   (docs.kalshi.com/getting_started/fee_rounding, accessed 2026-07-26). Gives the
   per-fill centicent ($0.0001) trade-fee rounding, the balance-precision
   rounding fee, and the PER-ORDER fee accumulator that issues a whole-cent
   ($0.01) rebate whenever accumulated rounding overpayment exceeds $0.01 --
   spanning both taker and maker fills of an order.

DOCUMENTED LIMITATION: the *current* consolidated fee-schedule PDF
(kalshi.com/docs/kalshi-fee-schedule.pdf, "July 2026") was NOT reachable from
this environment (HTTP 429), so the general 0.07 coefficient could not be
re-confirmed as unchanged for July 2026, and current per-market MAKER-fee
coefficients could not be retrieved from an official source. The model therefore
defaults to charging no maker fee (matching the CFTC filing) and exposes the
maker coefficient as configuration for when an official value is supplied. Fee
schedules change over time; a run pins the fee-model version so historical
simulations stay reproducible.

All arithmetic is exact integer math. Intermediate fee precision is integer
CENTICENTS (1 centicent = $0.0001 = one ten-thousandth of a dollar); the
simulator's cash ledger stays in integer cents. No binary floating point is used
for any fee value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from kalshi_weather.execution.models import Action, Liquidity, Side

CENTICENTS_PER_CENT = 100
CENTICENTS_PER_DOLLAR = 10_000
#: Rebate threshold / unit: accumulated rounding overpayment strictly exceeding
#: $0.01 (100 centicents) triggers a whole-cent rebate (Predictions API docs).
REBATE_UNIT_CENTICENTS = 100

#: Balance-precision targets from the Predictions API docs.
TARGET_NON_DIRECT_CENTICENTS = 100  # non-direct member balances round to $0.01
TARGET_DIRECT_CENTICENTS = 1  # direct member balances round to $0.0001


@dataclass(frozen=True)
class FeeBreakdown:
    """The three official per-fill fee components plus the net (all in the units
    named). ``net_fee_cents`` is the cash the ledger actually charges."""

    fee_model_version: str
    market_fee_schedule: str  # "general" | "sp500_nasdaq100" | "n/a"
    liquidity: str
    trade_fee_centicents: int
    rounding_fee_centicents: int
    rebate_centicents: int
    net_fee_centicents: int
    net_fee_cents: int


@dataclass(frozen=True)
class FeeAssessment:
    """Result of assessing one fill: the net fee (cents) the ledger charges, the
    component breakdown, and the updated per-order rounding accumulator."""

    net_fee_cents: int
    breakdown: FeeBreakdown
    accumulator_centicents: int


@runtime_checkable
class FeeModelLike(Protocol):
    """Structural interface every fee model satisfies."""

    @property
    def version(self) -> str: ...

    def assess(
        self,
        *,
        price_cents: int,
        quantity: int,
        side: Side,
        action: Action,
        liquidity: Liquidity,
        ticker: str,
        accumulator_centicents: int,
    ) -> FeeAssessment: ...

    def to_manifest(self) -> dict[str, object]: ...


def _flat_assessment(
    version: str, fee_cents: int, liquidity: Liquidity, accumulator: int
) -> FeeAssessment:
    """Wrap a flat (no rounding/rebate) fee into the common assessment shape."""
    return FeeAssessment(
        net_fee_cents=fee_cents,
        breakdown=FeeBreakdown(
            fee_model_version=version,
            market_fee_schedule="n/a",
            liquidity=liquidity.value,
            trade_fee_centicents=fee_cents * CENTICENTS_PER_CENT,
            rounding_fee_centicents=0,
            rebate_centicents=0,
            net_fee_centicents=fee_cents * CENTICENTS_PER_CENT,
            net_fee_cents=fee_cents,
        ),
        accumulator_centicents=accumulator,
    )


@dataclass(frozen=True)
class ZeroFeeModel:
    """Charges nothing. The safe default -- keeps synthetic accounting exact and
    makes no claim about real fees."""

    version: str = "zero-v1"

    def fee_cents(
        self,
        *,
        price_cents: int,
        quantity: int,
        side: Side,
        action: Action,
        liquidity: Liquidity,
    ) -> int:
        return 0

    def assess(
        self,
        *,
        price_cents: int,
        quantity: int,
        side: Side,
        action: Action,
        liquidity: Liquidity,
        ticker: str,
        accumulator_centicents: int,
    ) -> FeeAssessment:
        return _flat_assessment(self.version, 0, liquidity, accumulator_centicents)

    def to_manifest(self) -> dict[str, object]:
        return {"type": "zero", "version": self.version}


@dataclass(frozen=True)
class ConfigurableFeeModel:
    """A CONFIGURABLE PLACEHOLDER fee, integer cents, rounded up. NOT a verified
    match to Kalshi's schedule -- retained so pre-existing runs reproduce and so
    a rough non-zero fee can be dialled in for sensitivity tests. Default ZERO.

    Simplified form: ``ceil(rate_bps * p * (100 - p) / 1e6 * qty)`` cents, bounded
    below by a per-contract minimum. Deterministic integer math."""

    version: str = "configurable-v1"
    rate_bps: int = 0  # basis points on price*(100-price) notional-risk; 0 => zero
    per_contract_min_cents: int = 0
    charge_on_maker: bool = True

    def fee_cents(
        self,
        *,
        price_cents: int,
        quantity: int,
        side: Side,
        action: Action,
        liquidity: Liquidity,
    ) -> int:
        if self.rate_bps <= 0 and self.per_contract_min_cents <= 0:
            return 0
        if liquidity is Liquidity.MAKER and not self.charge_on_maker:
            return 0
        notional_risk = price_cents * (100 - price_cents)  # 0..2500
        raw = self.rate_bps * notional_risk * quantity
        fee = -(-raw // (10000 * 100))  # ceil division to cents
        return max(fee, self.per_contract_min_cents * quantity)

    def assess(
        self,
        *,
        price_cents: int,
        quantity: int,
        side: Side,
        action: Action,
        liquidity: Liquidity,
        ticker: str,
        accumulator_centicents: int,
    ) -> FeeAssessment:
        fee = self.fee_cents(
            price_cents=price_cents,
            quantity=quantity,
            side=side,
            action=action,
            liquidity=liquidity,
        )
        return _flat_assessment(self.version, fee, liquidity, accumulator_centicents)

    def to_manifest(self) -> dict[str, object]:
        return {
            "type": "configurable",
            "version": self.version,
            "rate_bps": self.rate_bps,
            "per_contract_min_cents": self.per_contract_min_cents,
            "charge_on_maker": self.charge_on_maker,
            "note": "PLACEHOLDER -- not a verified Kalshi fee schedule",
        }


def _ceil_div(num: int, den: int) -> int:
    return -(-num // den)


def apply_rounding(
    *,
    revenue_centicents: int,
    trade_fee_centicents: int,
    accumulator_centicents: int,
    target_centicents: int,
) -> tuple[int, int, int, int]:
    """The Predictions API per-fill rounding + per-order accumulator, exactly.

    Given a fill's signed revenue (negative for a buyer) and the trade fee (both
    in centicents), and the running per-order accumulator, return
    ``(rounding_fee, rebate, net_fee, new_accumulator)`` all in centicents.

    Steps (docs.kalshi.com/getting_started/fee_rounding):
      balance_change = revenue - trade_fee
      floored        = floor(balance_change) to target precision (toward -inf)
      rounding_fee   = balance_change - floored
      accumulator   += rounding_fee; while accumulator > $0.01: rebate $0.01
      net_fee        = trade_fee + rounding_fee - rebate  (always >= 0)
    """
    balance_change = revenue_centicents - trade_fee_centicents
    floored = (balance_change // target_centicents) * target_centicents  # floor toward -inf
    rounding_fee = balance_change - floored
    acc = accumulator_centicents + rounding_fee
    rebate = 0
    while acc > REBATE_UNIT_CENTICENTS:
        rebate += REBATE_UNIT_CENTICENTS
        acc -= REBATE_UNIT_CENTICENTS
    net_fee = trade_fee_centicents + rounding_fee - rebate
    return rounding_fee, rebate, net_fee, acc


@dataclass(frozen=True)
class KalshiEventContractFeeModel:
    """Authoritative event-contract fee model (see module docstring for sources).

    Trade fee (per fill), centicents, ceiled to the nearest centicent:
        general schedule:            ceil(0.07  * C * p * (100 - p) / 100)
        S&P-500 / NASDAQ-100 (INX*, NASDAQ100*): ceil(0.035 * C * p * (100 - p) / 100)
    where p is the integer-cent price (1..99) and C the contract count. Then the
    Predictions-API balance-precision rounding and per-order $0.01 rebate
    accumulator are applied (``apply_rounding``).
    """

    version: str = "kalshi-event-v1"
    #: $0.01 for non-direct members (default), $0.0001 for direct members.
    target_precision_centicents: int = TARGET_NON_DIRECT_CENTICENTS
    #: The CFTC filing charges no maker fee; the current schedule adds maker fees
    #: on some markets but the coefficient was not officially retrievable here.
    #: Off by default; supply an official (num/den) coefficient to enable.
    charge_maker_fee: bool = False
    maker_coeff_num: int = 0
    maker_coeff_den: int = 1
    #: Effective-date / source provenance recorded in the manifest.
    schedule_effective_date: str = "2022-09-12"
    source: str = (
        "CFTC-filed Kalshi Fee Schedule (rule091222kexdcm003, eff. 2022-09-12) + "
        "Kalshi Predictions API 'Fee Rounding' docs (accessed 2026-07-26)"
    )

    # --- schedule selection ------------------------------------------------

    def _schedule(self, ticker: str) -> tuple[str, int, int]:
        """Return (name, coeff_num, coeff_den) for the market. Coefficients are
        expressed as an exact fraction of the $-per-contract-per-unit-P(1-P)."""
        tk = ticker.upper()
        if tk.startswith("INX") or tk.startswith("NASDAQ100"):
            return ("sp500_nasdaq100", 35, 1000)  # 0.035
        return ("general", 7, 100)  # 0.07

    def _taker_trade_fee_centicents(
        self, coeff_num: int, coeff_den: int, price_cents: int, quantity: int
    ) -> int:
        """ceil(coeff * C * p * (100 - p)) centicents. coeff = coeff_num/coeff_den.

        exact_centicents = coeff * C * p * (100 - p)  (since 1 unit of P(1-P) at
        $1 max-value = 10000 centicents, and P=p/100, 1-P=(100-p)/100 cancels the
        10000). Ceiled to the next whole centicent."""
        num = coeff_num * quantity * price_cents * (100 - price_cents)
        return _ceil_div(num, coeff_den)

    # --- assessment --------------------------------------------------------

    def assess(
        self,
        *,
        price_cents: int,
        quantity: int,
        side: Side,
        action: Action,
        liquidity: Liquidity,
        ticker: str,
        accumulator_centicents: int,
    ) -> FeeAssessment:
        sched_name, cnum, cden = self._schedule(ticker)

        if liquidity is Liquidity.MAKER:
            if not self.charge_maker_fee:
                trade_fee_cc = 0
            else:
                trade_fee_cc = self._taker_trade_fee_centicents(
                    self.maker_coeff_num, self.maker_coeff_den, price_cents, quantity
                )
        else:  # taker / simulated cross => taker schedule
            trade_fee_cc = self._taker_trade_fee_centicents(cnum, cden, price_cents, quantity)

        # Signed revenue in centicents: a buyer pays (revenue negative); a seller
        # receives (positive). Fill price is whole cents -> whole centicents.
        gross_cc = quantity * price_cents * CENTICENTS_PER_CENT
        revenue_cc = -gross_cc if action is Action.BUY else gross_cc

        rounding_cc, rebate_cc, net_cc, new_acc = apply_rounding(
            revenue_centicents=revenue_cc,
            trade_fee_centicents=trade_fee_cc,
            accumulator_centicents=accumulator_centicents,
            target_centicents=self.target_precision_centicents,
        )
        if net_cc < 0:  # invariant: net fee is never negative
            raise ValueError(f"net fee went negative: {net_cc} cc")
        if (
            self.target_precision_centicents >= CENTICENTS_PER_CENT
            and net_cc % CENTICENTS_PER_CENT != 0
        ):
            # Non-direct (cent-precision) members: the net fee charged to the
            # integer-cent ledger must be whole cents. For whole-cent prices and
            # whole-contract quantities it always is; a non-cent value here means
            # an unsupported sub-cent input reached the ledger path.
            raise ValueError(
                f"net fee {net_cc} cc is not cent-aligned; sub-cent inputs are out of domain"
            )
        # Direct members keep centicent precision; the breakdown carries the exact
        # sub-cent components. The integer-cent ledger uses non-direct mode.
        net_cents = net_cc // CENTICENTS_PER_CENT
        return FeeAssessment(
            net_fee_cents=net_cents,
            breakdown=FeeBreakdown(
                fee_model_version=self.version,
                market_fee_schedule=sched_name,
                liquidity=liquidity.value,
                trade_fee_centicents=trade_fee_cc,
                rounding_fee_centicents=rounding_cc,
                rebate_centicents=rebate_cc,
                net_fee_centicents=net_cc,
                net_fee_cents=net_cents,
            ),
            accumulator_centicents=new_acc,
        )

    def to_manifest(self) -> dict[str, object]:
        return {
            "type": "kalshi_event_contract",
            "version": self.version,
            "schedule_effective_date": self.schedule_effective_date,
            "source": self.source,
            "general_coefficient": "0.07",
            "sp500_nasdaq100_coefficient": "0.035",
            "sp500_nasdaq100_ticker_prefixes": ["INX", "NASDAQ100"],
            "target_precision_centicents": self.target_precision_centicents,
            "charge_maker_fee": self.charge_maker_fee,
            "maker_coefficient": (
                f"{self.maker_coeff_num}/{self.maker_coeff_den}"
                if self.charge_maker_fee
                else "0 (not charged)"
            ),
            "rebate_unit_centicents": REBATE_UNIT_CENTICENTS,
            "limitations": (
                "current July-2026 consolidated fee-schedule PDF was unreachable "
                "(HTTP 429) from this environment; coefficients grounded on the "
                "CFTC-filed schedule; current per-market maker coefficients not "
                "officially retrieved (maker fee off by default)"
            ),
        }


ZERO_FEE_MODEL = ZeroFeeModel()
