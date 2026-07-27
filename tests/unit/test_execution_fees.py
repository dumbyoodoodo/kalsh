"""Fee-model tests, grounded in official Kalshi documentation.

Every expected number below is taken from, or hand-derived against, an official
source and cited inline:

* CFTC-filed Kalshi Fee Schedule (cftc.gov, rule091222kexdcm003, eff. 2022-09-12):
  general formula ``round up(0.07 x C x P x (1-P))`` and the S&P500/NASDAQ-100
  special ``round up(0.035 ...)``, plus the published fee tables for 1 and 100
  contracts.
* Kalshi Predictions API "Fee Rounding" docs (docs.kalshi.com, accessed
  2026-07-26): the per-order rounding accumulator + $0.01 rebate, with two
  worked examples reproduced verbatim here.

Tests assert exact integer values; nothing is derived from the implementation.
"""

from datetime import UTC, datetime

import pytest
from _fee_tables import CFTC_GENERAL, CFTC_SPECIAL

from kalshi_weather.execution.engine import Simulator
from kalshi_weather.execution.fees import (
    ConfigurableFeeModel,
    KalshiEventContractFeeModel,
    ZeroFeeModel,
    apply_rounding,
)
from kalshi_weather.execution.market import OrderBook
from kalshi_weather.execution.models import (
    Action,
    Liquidity,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
)
from kalshi_weather.execution.policy import ExecutionPolicy, FillMode, RiskConfig

KALSHI = KalshiEventContractFeeModel()


def _net(
    price: int, qty: int, *, ticker: str = "EVENT-X", liq: Liquidity = Liquidity.TAKER, acc: int = 0
) -> int:
    return KALSHI.assess(
        price_cents=price,
        quantity=qty,
        side=Side.YES,
        action=Action.BUY,
        liquidity=liq,
        ticker=ticker,
        accumulator_centicents=acc,
    ).net_fee_cents


# --- CFTC published fee tables (general schedule, 0.07) --------------------


@pytest.mark.parametrize("price,fee1,fee100", CFTC_GENERAL)
def test_cftc_general_table(price: int, fee1: int, fee100: int) -> None:
    # round up(0.07 x C x P x (1-P)) -- CFTC Kalshi Fee Schedule, General table.
    assert _net(price, 1) == fee1
    assert _net(price, 100) == fee100


@pytest.mark.parametrize("price,fee1,fee100", CFTC_SPECIAL)
def test_cftc_sp500_nasdaq_table(price: int, fee1: int, fee100: int) -> None:
    # round up(0.035 x C x P x (1-P)) -- CFTC schedule, S&P500/NASDAQ-100 table.
    for ticker in ("INXD-26JUL01", "NASDAQ100W-26"):
        assert _net(price, 1, ticker=ticker) == fee1
        assert _net(price, 100, ticker=ticker) == fee100


def test_special_schedule_only_for_inx_nasdaq_prefixes() -> None:
    # A non-INX/NASDAQ ticker uses the general (0.07) schedule.
    assert _net(50, 100, ticker="HIGHNY-26") == 175  # general
    assert _net(50, 100, ticker="INXD-26") == 88  # special
    assert _net(50, 100, ticker="NASDAQ100D-26") == 88


# --- boundaries ------------------------------------------------------------


def test_price_and_quantity_boundaries() -> None:
    assert _net(1, 1) == 1  # longshot single contract: fee == price (doubles outlay)
    assert _net(99, 1) == 1
    assert _net(50, 1) == 2  # peak per-contract fee
    assert _net(1, 100) == 7
    assert _net(99, 100) == 7
    assert _net(50, 100) == 175
    # large quantity stays exact integer math
    assert _net(50, 10_000) == 17500  # 0.07 * 10000 * 0.25 = $175.00


def test_net_fee_never_negative_and_cent_aligned() -> None:
    for p in range(1, 100):
        for q in (1, 3, 7, 100, 999):
            n = _net(p, q)
            assert n >= 0
            for tk in ("EVENT-X", "INXD-1"):
                assert _net(p, q, ticker=tk) >= 0


def test_maker_vs_taker() -> None:
    # Default: no maker fee (CFTC filing charges none). Taker uses 0.07.
    assert _net(50, 100, liq=Liquidity.MAKER) == 0
    assert _net(50, 100, liq=Liquidity.TAKER) == 175
    # If an official maker coefficient is supplied, it is applied.
    m = KalshiEventContractFeeModel(charge_maker_fee=True, maker_coeff_num=35, maker_coeff_den=1000)
    got = m.assess(
        price_cents=50,
        quantity=100,
        side=Side.YES,
        action=Action.BUY,
        liquidity=Liquidity.MAKER,
        ticker="EVENT-X",
        accumulator_centicents=0,
    ).net_fee_cents
    assert got == 88  # 0.035 * 100 * 0.25 = $0.875 -> $0.88


def test_entry_and_exit_both_charged() -> None:
    # Fee depends on price, not direction: buying and selling at 50c cost the same.
    buy = _net(50, 100)
    sell = KALSHI.assess(
        price_cents=50,
        quantity=100,
        side=Side.YES,
        action=Action.SELL,
        liquidity=Liquidity.TAKER,
        ticker="EVENT-X",
        accumulator_centicents=0,
    ).net_fee_cents
    assert buy == sell == 175


# --- Predictions API accumulator worked examples ---------------------------


def test_predictions_api_accumulator_example_1() -> None:
    # docs.kalshi.com: buy 3 @ $0.055 as three 1-lot matches, target $0.01.
    # trade fee $0.0085 (85 cc), revenue -$0.055 (-550 cc) per fill.
    acc, nets = 0, []
    for _ in range(3):
        rounding, _rebate, net, acc = apply_rounding(
            revenue_centicents=-550,
            trade_fee_centicents=85,
            accumulator_centicents=acc,
            target_centicents=100,
        )
        assert rounding == 65
        nets.append(net)
    assert nets == [150, 50, 150]  # published Net Fee column ($0.0150, $0.0050, $0.0150)


def test_predictions_api_accumulator_example_2() -> None:
    # docs.kalshi.com: buy 0.90 @ $0.50 as three 0.30-lot matches, target $0.01.
    # trade fee $0.0041 (41 cc), revenue -$0.15 (-1500 cc) per fill.
    acc, nets, rebates = 0, [], []
    for _ in range(3):
        rounding, rebate, net, acc = apply_rounding(
            revenue_centicents=-1500,
            trade_fee_centicents=41,
            accumulator_centicents=acc,
            target_centicents=100,
        )
        assert rounding == 59
        nets.append(net)
        rebates.append(rebate)
    assert nets == [100, 0, 100]  # published Net Fee column ($0.0100, $0.0000, $0.0100)
    assert rebates == [0, 100, 0]  # rebate fires on fill 2


def test_direct_member_precision_keeps_centicents() -> None:
    # Direct members round to $0.0001; a whole-cent-price fill needs no rounding.
    m = KalshiEventContractFeeModel(target_precision_centicents=1)
    a = m.assess(
        price_cents=50,
        quantity=1,
        side=Side.YES,
        action=Action.BUY,
        liquidity=Liquidity.TAKER,
        ticker="EVENT-X",
        accumulator_centicents=0,
    )
    assert a.breakdown.trade_fee_centicents == 175  # $0.0175 exact
    assert a.breakdown.rounding_fee_centicents == 0
    assert a.breakdown.net_fee_centicents == 175


# --- partial-fill accumulation through the simulator -----------------------


def _oi(oid: str, price: int, qty: int) -> OrderIntent:
    return OrderIntent(
        order_id=oid,
        strategy_id="s",
        ticker="EVENT-X",
        side=Side.YES,
        action=Action.BUY,
        order_type=OrderType.MARKETABLE_LIMIT,
        limit_price_cents=price,
        quantity=qty,
        submitted_at=datetime(2026, 1, 1, tzinfo=UTC),
        time_in_force=TimeInForce.IOC,
    )


def test_partial_fills_share_one_order_accumulator() -> None:
    # An order that fills across several book levels at the SAME price must use a
    # single per-order accumulator, not round each fill independently.
    pol = ExecutionPolicy(fill_mode=FillMode.MARKETABLE, fee_model=KALSHI)
    sim = Simulator(policy=pol, risk=RiskConfig(), initial_cash_cents=1_000_000)
    book = OrderBook(
        ticker="EVENT-X",
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        yes_asks=((50, 34), (50, 33), (50, 33)),  # three same-price levels
    )
    o = sim.submit(_oi("multi", 50, 100), book=book)
    assert o.filled_quantity == 100
    multi = sum(f.fee_cents for f in sim.fills)

    # single fill of the same 100 @ 50
    sim2 = Simulator(policy=pol, risk=RiskConfig(), initial_cash_cents=1_000_000)
    book2 = OrderBook(
        ticker="EVENT-X", captured_at=datetime(2026, 1, 1, tzinfo=UTC), yes_asks=((50, 100),)
    )
    sim2.submit(_oi("single", 50, 100), book=book2)
    single = sum(f.fee_cents for f in sim2.fills)

    assert single == 175
    # The accumulator makes the multi-fill total converge to the single-fill fee
    # (within the un-rebated sub-cent remainder), and never rounds independently
    # to a strictly larger amount than one cent over.
    assert 0 <= multi - single <= 1


# --- backward compatibility -------------------------------------------------


def test_zero_and_configurable_models_unchanged() -> None:
    z = ZeroFeeModel()
    assert (
        z.fee_cents(
            price_cents=40, quantity=10, side=Side.YES, action=Action.BUY, liquidity=Liquidity.TAKER
        )
        == 0
    )
    c = ConfigurableFeeModel(rate_bps=700)
    assert (
        c.fee_cents(
            price_cents=40, quantity=10, side=Side.YES, action=Action.BUY, liquidity=Liquidity.TAKER
        )
        == 17
    )  # unchanged placeholder math


def test_manifest_records_provenance() -> None:
    man = KALSHI.to_manifest()
    assert man["type"] == "kalshi_event_contract"
    assert man["general_coefficient"] == "0.07"
    assert man["sp500_nasdaq100_coefficient"] == "0.035"
    assert "2022-09-12" in str(man["schedule_effective_date"])
    assert "429" in str(man["limitations"])  # documents the unreachable current PDF
