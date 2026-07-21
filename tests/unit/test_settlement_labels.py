"""Settlement-label reconstruction tests (E-A): strict as-of behavior,
no-future-leakage, bounded reconstruction, corrections, strike semantics,
and status handling."""

from datetime import date, datetime, timedelta
from decimal import Decimal

from kalshi_weather.settlement.labels import (
    SETTLEMENT_WINDOW_HOURS,
    LabelStatus,
    implied_result,
    reconstruct_label,
)


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi)


# Timeline for target date 2026-07-14 (matching the live-verified example):
# preliminary 4:37pm ET on the 14th (20:37Z), final 2:15am ET on the 15th
# (06:15Z), a late correction on the 17th. Close 04:59Z on the 15th;
# settlement 12:01Z on the 15th.
PRELIM = (_dt(2026, 7, 14, 20, 37), Decimal(89))
FINAL = (_dt(2026, 7, 15, 6, 15), Decimal(90))
CORRECTION = (_dt(2026, 7, 17, 15, 0), Decimal(91))
CLOSE = _dt(2026, 7, 15, 4, 59)
SETTLE = _dt(2026, 7, 15, 12, 1)


def _label(**overrides):  # type: ignore[no-untyped-def]
    kwargs = dict(
        market_ticker="KXHIGHNY-26JUL14-B94.5",
        station_id="NYC",
        variable="tmax_f",
        target_date=date(2026, 7, 14),
        close_time=CLOSE,
        settlement_ts=SETTLE,
        kalshi_result="no",
        expiration_value=Decimal(90),
        floor_strike=Decimal(94),
        cap_strike=Decimal(95),
        strike_type="between",
        issuances=[PRELIM, FINAL, CORRECTION],
    )
    kwargs.update(overrides)
    return reconstruct_label(**kwargs)  # type: ignore[arg-type]


def test_stage_values_are_distinct_and_correct() -> None:
    """The three stages select three different issuances: close gets the
    preliminary (final not yet issued), settlement gets the final, and the
    latest value includes the post-settlement correction."""
    label = _label()
    assert label.status is LabelStatus.RESOLVED
    assert label.settlement_time_is_exact
    assert label.value_at_close == Decimal(89)  # preliminary only at 04:59Z
    assert label.issuance_at_close == PRELIM[0]
    assert label.value_at_settlement == Decimal(90)  # morning final
    assert label.issuance_at_settlement == FINAL[0]
    assert label.latest_final_value == Decimal(91)  # includes later correction
    # the correction must NOT leak into the settlement-time value
    assert label.value_at_settlement != label.latest_final_value


def test_no_future_leakage_strict_as_of() -> None:
    """An issuance one second after the boundary is invisible to that stage."""
    boundary = _dt(2026, 7, 15, 6, 15)
    label = _label(
        settlement_ts=boundary - timedelta(seconds=1),
        issuances=[PRELIM, (boundary, Decimal(90))],
    )
    assert label.value_at_settlement == Decimal(89)  # 90 is 1s in the future
    at_boundary = _label(settlement_ts=boundary, issuances=[PRELIM, (boundary, Decimal(90))])
    assert at_boundary.value_at_settlement == Decimal(90)  # <= is inclusive


def test_bounded_reconstruction_when_settlement_ts_missing() -> None:
    label = _label(settlement_ts=None)
    assert label.status is LabelStatus.BOUNDED
    assert not label.settlement_time_is_exact
    assert label.settlement_time == CLOSE + timedelta(hours=SETTLEMENT_WINDOW_HOURS)
    # 48h bound (07-17 04:59) excludes the 07-17 15:00 correction
    assert label.value_at_settlement == Decimal(90)
    assert any("bounded" in n for n in label.notes)


def test_payout_agreement_fields() -> None:
    label = _label()
    # value 90 vs strikes between 94-95 -> implied no; Kalshi said no; value matches
    assert label.implied_result_at_settlement == "no"
    assert label.payout_result_agrees is True
    assert label.payout_value_agrees is True  # 90 == expiration_value 90

    disagreeing = _label(expiration_value=Decimal(89))
    assert disagreeing.payout_value_agrees is False


def test_statuses_for_missing_inputs() -> None:
    assert _label(station_id=None, variable=None, target_date=None).status is (
        LabelStatus.UNSUPPORTED
    )
    assert _label(issuances=[]).status is LabelStatus.MISSING_SOURCE_DATA
    assert _label(close_time=None).status is LabelStatus.AMBIGUOUS
    # issuances exist but all after the settlement bound
    late = _label(issuances=[(SETTLE + timedelta(days=3), Decimal(90))])
    assert late.status is LabelStatus.MISSING_SOURCE_DATA
    assert late.latest_final_value == Decimal(90)  # still recorded


def test_usable_property_gates_downstream() -> None:
    assert _label().usable
    assert _label(settlement_ts=None).usable  # bounded is usable
    assert not _label(issuances=[]).usable
    assert not _label(station_id=None, variable=None, target_date=None).usable


def test_implied_result_strike_semantics_boundaries() -> None:
    d = Decimal
    # between: inclusive both ends (verified empirically in EXP E-A)
    assert implied_result(d(94), strike_type="between", floor=d(94), cap=d(95)) == "yes"
    assert implied_result(d(95), strike_type="between", floor=d(94), cap=d(95)) == "yes"
    assert implied_result(d(93), strike_type="between", floor=d(94), cap=d(95)) == "no"
    assert implied_result(d(96), strike_type="between", floor=d(94), cap=d(95)) == "no"
    # greater: strict
    assert implied_result(d(96), strike_type="greater", floor=d(95), cap=None) == "yes"
    assert implied_result(d(95), strike_type="greater", floor=d(95), cap=None) == "no"
    # less: strict
    assert implied_result(d(78), strike_type="less", floor=None, cap=d(79)) == "yes"
    assert implied_result(d(79), strike_type="less", floor=None, cap=d(79)) == "no"
    # unknown type -> None, never a guess
    assert implied_result(d(80), strike_type="mystery", floor=d(1), cap=d(2)) is None


def test_unknown_strike_type_noted_not_guessed() -> None:
    label = _label(strike_type="mystery")
    assert label.status is LabelStatus.RESOLVED  # values still reconstructed
    assert label.implied_result_at_settlement is None
    assert label.payout_result_agrees is None
    assert any("unknown strike_type" in n for n in label.notes)
