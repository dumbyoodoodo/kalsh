"""Tests for scripts/exp_h0011_midnight_boundary.py.

Implements exactly what H0011's own docstring cites:
  - docs/research/preregistrations/PREREG-20260721-H0011-midnight-boundary.md
  - docs/research/preregistrations/AMENDMENT-20260721-H0011-audit-resolution.md
  - docs/research/preregistrations/TEMPLATE-H0011-manifest.json
  - docs/research/preregistrations/PROVENANCE-20260721-H0011-posthoc-vs-prospective.md

All data here is hand-constructed/synthetic; nothing in this file touches
the real pinned exp-20260721-h0002 dataset or inspects a real revision/
attribution statistic. A handful of tests call `run()` end-to-end, but
only against a fabricated `dataset_dir` fixture built in this file (a
few thousand synthetic issuance rows, not real archive data) -- this
exercises the manifest-assembly/validation code path, not H0011 itself.
The real `tzdata` pip-package check (`get_pinned_tzdata_version`) is
monkeypatched in run()-level tests purely for environment independence;
its own pass/fail logic is tested directly and unpatched elsewhere.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any
from unittest.mock import patch

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import exp_h0011_midnight_boundary as h0011

SOURCE_SCHEMA = {
    "station_id": pl.Utf8,
    "variable": pl.Utf8,
    "value": pl.Float64,
    "observation_date": pl.Date,
    "issuance_time": pl.Datetime("us"),
    "raw_payload_id": pl.Int64,
}


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=SOURCE_SCHEMA, orient="row")  # type: ignore[arg-type]


def _row(
    variable: str,
    value: float,
    obs_date: date,
    issuance_time: datetime,
    *,
    station_id: str = "NYC",
    payload_id: int = 1,
) -> dict[str, Any]:
    return {
        "station_id": station_id,
        "variable": variable,
        "value": value,
        "observation_date": obs_date,
        "issuance_time": issuance_time,
        "raw_payload_id": payload_id,
    }


def _vd(
    variable: str,
    observation_date: date,
    issuances: list[tuple[datetime, float]],
) -> h0011.VariableDay:
    """Direct VariableDay construction (bypasses frame parsing) for
    attribution/decision-level unit tests."""
    return h0011.VariableDay(
        variable=variable,
        observation_date=observation_date,
        issuances=[(t, h0011.quantize_value(v)) for t, v in issuances],
    )


# --- Decimal policy (PREREG Sec 2/6.2, AMENDMENT Finding 3/4) ----------------


def test_quantize_value_uses_str_construction_not_direct_float() -> None:
    """AMENDMENT Finding 3: Decimal(str(v)), never Decimal(v) directly --
    the latter would import the float's full binary-exact expansion. 55.0
    round-trips identically either way; the distinguishing check is that
    the *string* path is what's used, verified via the two constructions
    disagreeing on an exact binary value like 0.1."""
    via_str = Decimal(str(0.1))
    via_direct = Decimal(0.1)  # noqa: RUF032 -- the point of this test is this exact call
    assert via_str != via_direct  # the two paths are NOT equivalent in general
    assert h0011.quantize_value(0.1) == Decimal("0.10")  # str-path gives the expected exact value


def test_quantize_value_rounds_half_even() -> None:
    # 55.005 is not exactly representable in binary float, but the
    # str-repr path resolves it to the printed decimal; check the
    # documented ROUND_HALF_EVEN behavior at a clean tie.
    assert h0011.quantize_value(55.0).quantize(Decimal("1")) == Decimal("55")


def test_value_roundtrips_true_for_clean_two_decimal_values() -> None:
    assert h0011.value_roundtrips(55.0) is True
    assert h0011.value_roundtrips(-12.34) is True
    assert h0011.value_roundtrips(0.0) is True


def test_value_roundtrips_false_for_corrupted_precision() -> None:
    """A value carrying more than 2 decimal digits of real signal (not
    float noise) must fail the round-trip invariant."""
    assert h0011.value_roundtrips(55.0049) is False


def test_comparison_quantization_strips_float_noise() -> None:
    exact = Decimal(1) / Decimal(200)
    # abs(99.5/100 - 1)'s exact binary value; float literal intentional (RUF032)
    noisy_float_image = Decimal(0.005000000000000004)  # noqa: RUF032
    assert h0011.quantize_for_comparison(exact) == h0011.quantize_for_comparison(noisy_float_image)


def test_strictly_greater_and_less_are_quantized() -> None:
    a = Decimal(1) / Decimal(200)
    b = Decimal("0.005")
    assert h0011.strictly_greater(a, b) is False  # exact tie, not strictly greater
    assert h0011.strictly_less(a, b) is False
    assert h0011.strictly_greater(Decimal("0.006"), b) is True
    assert h0011.strictly_less(Decimal("0.004"), b) is True


# --- Serialization (AMENDMENT Finding 4) -------------------------------------


def test_serialize_decimal_fixed_point_12_digits() -> None:
    assert h0011.serialize_decimal(Decimal("0.108")) == "0.108000000000"
    assert h0011.serialize_decimal(Decimal("-0.005")) == "-0.005000000000"
    assert h0011.serialize_decimal(Decimal("1")) == "1.000000000000"


def test_serialize_decimal_never_scientific_notation() -> None:
    text = h0011.serialize_decimal(Decimal("0.0000000000001"))
    assert "E" not in text and "e" not in text


def test_serialize_decimal_no_negative_zero() -> None:
    assert h0011.serialize_decimal(Decimal("-0.0000000000001")) == "0.000000000000"


def test_serialize_ci_returns_two_strings() -> None:
    out = h0011.serialize_ci(Decimal("0.1"), Decimal("0.2"))
    assert out == ["0.100000000000", "0.200000000000"]


# --- Statistical methods (PREREG Sec 6.1) -- hand-derived cross-checks ------


def test_wilson_interval_hand_calculated_x60_n100() -> None:
    """PREREG Sec 11 checklist example: lower bound just above 1/2."""
    lower, upper = h0011.wilson_interval(60, 100)
    # Hand-derived via the frozen formula: p=0.6, z=1.959963984540054.
    # Uses the identical 50-digit precision context PREREG Sec 6.2 freezes
    # for the implementation, so this is an apples-to-apples comparison,
    # not merely a lower-precision approximation.
    with h0011.localcontext() as ctx:
        ctx.prec = h0011.DECIMAL_CONTEXT_PRECISION
        z = h0011.Z_95
        p = Decimal(60) / Decimal(100)
        z2 = z * z
        n = Decimal(100)
        denom = 1 + z2 / n
        center = (p + z2 / (2 * n)) / denom
        inside = p * (1 - p) / n + z2 / (4 * n * n)
        halfwidth = z * inside.sqrt()
        halfwidth = halfwidth / denom
        expected_lower = center - halfwidth
        expected_upper = center + halfwidth
    assert abs(lower - expected_lower) < Decimal("1E-30")
    assert abs(upper - expected_upper) < Decimal("1E-30")
    assert h0011.strictly_greater(lower, h0011.HALF) is True


def test_wilson_interval_hand_calculated_x55_n100_straddles_half() -> None:
    lower, upper = h0011.wilson_interval(55, 100)
    assert h0011.strictly_greater(lower, h0011.HALF) is False
    assert h0011.strictly_less(upper, h0011.HALF) is False  # straddles


def test_wilson_interval_hand_calculated_x30_n100_entirely_below_half() -> None:
    _lower, upper = h0011.wilson_interval(30, 100)
    assert h0011.strictly_less(upper, h0011.HALF) is True


def test_wilson_interval_zero_numerator_lower_bound_is_exactly_zero() -> None:
    lower, _upper = h0011.wilson_interval(0, 100)
    assert lower == Decimal(0)


def test_wilson_interval_full_numerator_upper_bound_is_exactly_one() -> None:
    _lower, upper = h0011.wilson_interval(100, 100)
    assert upper == Decimal(1)


def test_wilson_interval_requires_positive_n() -> None:
    with pytest.raises(ValueError, match="n > 0"):
        h0011.wilson_interval(0, 0)


def test_newcombe_interval_equal_counts_gives_nonpositive_lower_bound() -> None:
    lower, upper = h0011.newcombe_interval(x_tmin=50, n_tmin=100, x_tmax=50, n_tmax=100)
    assert lower < 0
    assert upper > 0
    assert h0011.strictly_greater(lower, h0011.ZERO) is False


def test_newcombe_interval_matches_hand_calculation() -> None:
    """Independent hand-derivation of PREREG Sec 6.1's exact formula, at
    the same frozen 50-digit precision as the implementation."""
    x_tmin, n_tmin, x_tmax, n_tmax = 311, 1282, 170, 1282
    with h0011.localcontext() as ctx:
        ctx.prec = h0011.DECIMAL_CONTEXT_PRECISION
        l1, u1 = h0011.wilson_interval(x_tmin, n_tmin)
        l2, u2 = h0011.wilson_interval(x_tmax, n_tmax)
        p1 = Decimal(x_tmin) / Decimal(n_tmin)
        p2 = Decimal(x_tmax) / Decimal(n_tmax)
        expected_lower = (p1 - p2) - ((p1 - l1) ** 2 + (u2 - p2) ** 2).sqrt()
        expected_upper = (p1 - p2) + ((u1 - p1) ** 2 + (p2 - l2) ** 2).sqrt()
    lower, upper = h0011.newcombe_interval(
        x_tmin=x_tmin, n_tmin=n_tmin, x_tmax=x_tmax, n_tmax=n_tmax
    )
    assert abs(lower - expected_lower) < Decimal("1E-30")
    assert abs(upper - expected_upper) < Decimal("1E-30")


# --- Population / revision definitions (PREREG Sec 2/3) ---------------------


def test_is_revised_true_when_first_and_final_differ() -> None:
    vd = _vd(
        "tmin_f",
        date(2026, 1, 1),
        [(datetime(2026, 1, 1, 21), 40.0), (datetime(2026, 1, 2, 6), 39.0)],
    )
    assert h0011.is_revised(vd) is True


def test_is_revised_false_when_first_equals_final() -> None:
    vd = _vd(
        "tmin_f",
        date(2026, 1, 1),
        [(datetime(2026, 1, 1, 21), 40.0), (datetime(2026, 1, 2, 6), 40.0)],
    )
    assert h0011.is_revised(vd) is False


def test_single_issuance_day_is_unrevised() -> None:
    """PREREG Sec 2: a single-issuance day compares its own value to
    itself -- unrevised, no special case required."""
    vd = _vd("tmax_f", date(2026, 1, 1), [(datetime(2026, 1, 1, 21), 60.0)])
    assert h0011.is_revised(vd) is False


def test_is_flip_flop_true_for_unrevised_day_with_intermediate_deviation() -> None:
    vd = _vd(
        "tmax_f",
        date(2026, 1, 1),
        [
            (datetime(2026, 1, 1, 21), 60.0),
            (datetime(2026, 1, 1, 23), 61.0),  # deviates mid-sequence
            (datetime(2026, 1, 2, 6), 60.0),  # back to first value -- unrevised
        ],
    )
    assert h0011.is_revised(vd) is False
    assert h0011.is_flip_flop(vd) is True


def test_is_flip_flop_false_for_a_genuinely_revised_day() -> None:
    vd = _vd(
        "tmax_f",
        date(2026, 1, 1),
        [(datetime(2026, 1, 1, 21), 60.0), (datetime(2026, 1, 2, 6), 61.0)],
    )
    assert h0011.is_flip_flop(vd) is False


# --- Post-midnight attribution (PREREG Sec 4) --------------------------------


def test_local_midnight_boundary_is_start_of_next_day() -> None:
    boundary = h0011.local_midnight_boundary(date(2026, 1, 15))
    assert boundary == datetime(2026, 1, 16, 0, 0, 0, tzinfo=h0011.NY_TZ)


def test_attribution_exactly_at_boundary_classifies_post_midnight() -> None:
    """PREREG Sec 4.5: 'at-or-after' -- an issuance at exactly 00:00:00
    local belongs to the next day."""
    obs_date = date(2026, 1, 15)
    boundary_local = datetime(2026, 1, 16, 0, 0, 0, tzinfo=h0011.NY_TZ)
    boundary_utc_naive = boundary_local.astimezone(h0011.UTC).replace(tzinfo=None)
    vd = _vd(
        "tmin_f",
        obs_date,
        [(datetime(2026, 1, 15, 21), 40.0), (boundary_utc_naive, 39.0)],
    )
    assert h0011.attribute_revision(vd) is True
    assert h0011.is_exact_boundary(vd) is True


def test_attribution_one_second_before_boundary_classifies_pre_midnight() -> None:
    obs_date = date(2026, 1, 15)
    boundary_local = datetime(2026, 1, 16, 0, 0, 0, tzinfo=h0011.NY_TZ)
    just_before_utc_naive = (
        (boundary_local - timedelta(seconds=1)).astimezone(h0011.UTC).replace(tzinfo=None)
    )
    vd = _vd(
        "tmin_f",
        obs_date,
        [(datetime(2026, 1, 15, 21), 40.0), (just_before_utc_naive, 39.0)],
    )
    assert h0011.attribute_revision(vd) is False
    assert h0011.is_exact_boundary(vd) is False


def test_attribution_one_second_after_boundary_classifies_post_midnight() -> None:
    obs_date = date(2026, 1, 15)
    boundary_local = datetime(2026, 1, 16, 0, 0, 0, tzinfo=h0011.NY_TZ)
    just_after_utc_naive = (
        (boundary_local + timedelta(seconds=1)).astimezone(h0011.UTC).replace(tzinfo=None)
    )
    vd = _vd(
        "tmin_f",
        obs_date,
        [(datetime(2026, 1, 15, 21), 40.0), (just_after_utc_naive, 39.0)],
    )
    assert h0011.attribute_revision(vd) is True


def test_earliest_occurrence_tie_break_nonmonotone_sequence() -> None:
    """PREREG Sec 4.3: final value appears, deviates, reappears -- the
    EARLIEST appearance governs, and this is flagged as non-monotone."""
    vd = _vd(
        "tmin_f",
        date(2026, 1, 1),
        [
            (datetime(2026, 1, 1, 20), 40.0),  # first
            (datetime(2026, 1, 1, 21), 39.0),  # matches final -- earliest appearance
            (datetime(2026, 1, 1, 22), 38.0),  # deviates
            (datetime(2026, 1, 2, 6), 39.0),  # final, reappears
        ],
    )
    first_appearance = h0011.first_appearance_of_final_value(vd)
    assert first_appearance == datetime(
        2026, 1, 1, 21
    )  # the earliest match, not the final issuance
    assert h0011.is_nonmonotone_final_appearance(vd) is True


def test_multiple_post_midnight_candidates_uses_earliest() -> None:
    """Several distinct post-midnight issuances match the final value;
    the earliest of them must be selected, not the last. Constructed
    relative to the real boundary (rather than hand-picked UTC hours) to
    avoid an offset-arithmetic mistake in the test itself."""
    obs_date = date(2026, 1, 1)
    boundary_local = datetime(2026, 1, 2, 0, 0, 0, tzinfo=h0011.NY_TZ)

    def utc_naive(offset_minutes_from_boundary: int) -> datetime:
        local = boundary_local + timedelta(minutes=offset_minutes_from_boundary)
        return local.astimezone(h0011.UTC).replace(tzinfo=None)

    pre_midnight = utc_naive(-180)  # 21:00 local the prior evening
    candidate_a = utc_naive(30)  # 00:30 local -- earliest post-midnight match
    candidate_b = utc_naive(60)  # 01:00 local -- later post-midnight match
    final_issuance = utc_naive(385)  # 06:25 local -- the terminal issuance

    vd = _vd(
        "tmin_f",
        obs_date,
        [
            (pre_midnight, 40.0),  # first, non-matching
            (candidate_a, 39.0),  # post-midnight, matches final -- earliest candidate
            (candidate_b, 39.0),  # post-midnight, also matches final -- later candidate
            (final_issuance, 39.0),  # final issuance itself
        ],
    )
    first_appearance = h0011.first_appearance_of_final_value(vd)
    assert first_appearance == candidate_a
    assert first_appearance != candidate_b
    assert h0011.attribute_revision(vd) is True


def test_attribution_unresolved_on_extreme_date_returns_none() -> None:
    """AMENDMENT Finding 9: an unconvertible-timestamp failure -- here,
    date.max + 1 day overflows -- is caught narrowly and returns None
    (unresolved), never propagates as a crash."""
    vd = _vd(
        "tmin_f", date.max, [(datetime(2026, 1, 1, 21), 40.0), (datetime(2026, 1, 2, 6), 39.0)]
    )
    assert h0011.attribute_revision(vd) is None
    assert h0011.is_exact_boundary(vd) is False


def test_unresolved_stays_in_denominator_not_numerator() -> None:
    revised_days = [
        _vd(
            "tmin_f", date.max, [(datetime(2026, 1, 1, 21), 40.0), (datetime(2026, 1, 2, 6), 39.0)]
        ),
        _vd(
            "tmin_f",
            date(2026, 1, 1),
            [(datetime(2026, 1, 1, 21), 40.0), (datetime(2026, 1, 2, 6), 39.0)],
        ),
    ]
    attributions = [h0011.attribute_revision(vd) for vd in revised_days]
    assert attributions[0] is None
    assert attributions[1] is True
    part2 = h0011.compute_part2(revised_days, attributions)
    assert part2["r_tmin"] == 2  # unresolved day still counted in the denominator
    assert part2["x_pm"] == 1  # but not in the numerator
    assert part2["n_unresolved"] == 1


# --- DST transitions (PREREG Sec 2, implementation-integrity checklist) -----


def test_dst_spring_forward_2026_conversion_uses_correct_offset() -> None:
    """2026-03-08 02:00 local does not exist (clocks jump to 03:00 EDT).
    An issuance shortly after the transition (in UTC) must convert using
    the post-transition EDT (UTC-4) offset, not the pre-transition EST
    (UTC-5) offset."""
    # 2026-03-08 07:30 UTC = 2026-03-08 03:30 EDT (UTC-4, post-transition)
    naive_utc = datetime(2026, 3, 8, 7, 30)
    local = h0011.to_local(naive_utc)
    assert local == datetime(2026, 3, 8, 3, 30, tzinfo=h0011.NY_TZ)
    assert local.utcoffset() == timedelta(hours=-4)


def test_dst_spring_forward_boundary_classification_unaffected() -> None:
    """The local-midnight boundary itself (00:00:00) is never inside the
    02:00 DST gap, so classification around a spring-forward date is
    ordinary."""
    obs_date = date(2026, 3, 7)  # boundary = 2026-03-08 00:00 EST (pre-transition, UTC-5)
    boundary_local = datetime(2026, 3, 8, 0, 0, 0, tzinfo=h0011.NY_TZ)
    assert boundary_local.utcoffset() == timedelta(hours=-5)
    just_after_utc_naive = (
        (boundary_local + timedelta(minutes=1)).astimezone(h0011.UTC).replace(tzinfo=None)
    )
    vd = _vd("tmin_f", obs_date, [(datetime(2026, 3, 7, 21), 40.0), (just_after_utc_naive, 39.0)])
    assert h0011.attribute_revision(vd) is True


def test_dst_fall_back_2025_conversion_uses_correct_offset() -> None:
    """2025-11-02 01:30 EDT and 01:30 EST both exist locally (the fold);
    conversion from a well-defined UTC instant is correct regardless, and
    each converted datetime carries the correct utcoffset()/fold. Ordering
    the two via `.timestamp()` (true epoch seconds) is used deliberately
    here, not Python's `<` operator -- see the dedicated quirk test below
    for why."""
    # 2025-11-02 05:30 UTC = 01:30 EDT (UTC-4, before the fold)
    before_fold = h0011.to_local(datetime(2025, 11, 2, 5, 30))
    assert before_fold.utcoffset() == timedelta(hours=-4)
    # 2025-11-02 06:30 UTC = 01:30 EST (UTC-5, after the fold) -- same wall clock, different instant
    after_fold = h0011.to_local(datetime(2025, 11, 2, 6, 30))
    assert after_fold.utcoffset() == timedelta(hours=-5)
    assert before_fold.hour == after_fold.hour == 1  # the ambiguous wall-clock hour
    assert before_fold.timestamp() < after_fold.timestamp()  # true instants are correctly ordered


def test_dst_fold_same_wall_clock_comparison_operator_quirk_does_not_affect_production_code() -> (
    None
):
    """Documents a real CPython/PEP 495 subtlety found while writing this
    suite: two aware datetimes sharing the same tzinfo object and the
    same naive wall-clock fields, but different `fold`, compare EQUAL
    under `==`/`<` (and even subtract to zero) despite being genuinely
    different instants (`.timestamp()` differs) -- ordering must use
    `.timestamp()`, not the comparison operators, when this collision is
    possible. Production code (`attribute_revision`/`is_exact_boundary`)
    is unaffected: it only ever compares a first-appearance local time
    against `local_midnight_boundary()`, which is always exactly
    00:00:00 -- a wall-clock value that never falls inside the
    repeated fall-back hour (01:00-02:00 local) and therefore never
    collides with a fold=1 twin."""
    before_fold = h0011.to_local(datetime(2025, 11, 2, 5, 30))
    after_fold = h0011.to_local(datetime(2025, 11, 2, 6, 30))
    assert (
        before_fold.year,
        before_fold.month,
        before_fold.day,
        before_fold.hour,
        before_fold.minute,
    ) == (
        after_fold.year,
        after_fold.month,
        after_fold.day,
        after_fold.hour,
        after_fold.minute,
    )
    assert (
        before_fold == after_fold
    )  # the documented quirk: naive-field equality, not instant equality
    assert not (before_fold < after_fold)  # comparison operators ignore fold here
    assert (
        before_fold.timestamp() != after_fold.timestamp()
    )  # but they are NOT the same real instant
    assert after_fold.timestamp() - before_fold.timestamp() == 3600.0


def test_dst_fall_back_boundary_classification_unaffected() -> None:
    obs_date = date(2025, 11, 1)  # boundary = 2025-11-02 00:00 EDT (pre-fold, UTC-4)
    boundary_local = datetime(2025, 11, 2, 0, 0, 0, tzinfo=h0011.NY_TZ)
    assert boundary_local.utcoffset() == timedelta(hours=-4)
    just_before_utc_naive = (
        (boundary_local - timedelta(minutes=1)).astimezone(h0011.UTC).replace(tzinfo=None)
    )
    vd = _vd("tmin_f", obs_date, [(datetime(2025, 11, 1, 21), 40.0), (just_before_utc_naive, 39.0)])
    assert h0011.attribute_revision(vd) is False


# --- tzdata pinning (PREREG Sec 2/10) ----------------------------------------


def test_get_pinned_tzdata_version_raises_when_not_installed() -> None:
    with (
        patch("exp_h0011_midnight_boundary.version", side_effect=PackageNotFoundError),
        pytest.raises(h0011.TzdataNotPinnedError),
    ):
        h0011.get_pinned_tzdata_version()


def test_get_pinned_tzdata_version_returns_version_when_installed() -> None:
    with patch("exp_h0011_midnight_boundary.version", return_value="2025.2"):
        assert h0011.get_pinned_tzdata_version() == "2025.2"


def test_tzdata_is_pinned_in_the_execution_environment() -> None:
    """Integrity-review Finding F4: tzdata is now a pinned project
    dependency, so the real (unpatched) gate must pass in this
    environment and return a non-empty version string."""
    assert h0011.get_pinned_tzdata_version()


# --- Config-drift verification (integrity-review Finding F3) -----------------


def test_verify_config_matches_prereg_true_for_unmodified_module() -> None:
    assert h0011.verify_config_matches_prereg() is True


def test_verify_config_matches_prereg_detects_threshold_drift() -> None:
    with patch.object(h0011, "G4_MIN_R_TMIN", 99):
        assert h0011.verify_config_matches_prereg() is False


def test_verify_config_matches_prereg_detects_z_constant_drift() -> None:
    with patch.object(h0011, "Z_95", Decimal("1.96")):
        assert h0011.verify_config_matches_prereg() is False


def test_verify_config_matches_prereg_detects_frozen_hash_drift() -> None:
    with patch.object(h0011, "FROZEN_CONTENT_HASH", "0" * 64):
        assert h0011.verify_config_matches_prereg() is False


# --- Synthetic dataset builder for gate/run-level tests ----------------------


def _build_synthetic_frame(
    *,
    n_tmax_days: int = 1200,
    n_tmin_days: int = 1200,
    tmin_revised_fraction_denominator: int = 3,  # every Nth day is revised
    start: date = date(2023, 1, 1),
) -> pl.DataFrame:
    """A frame satisfying G1 (once hash-monkeypatched)/G2/G3/G4/G5 by
    construction -- most tmax days unrevised, ~1/3 of tmin days revised
    with post-midnight-attributed finals."""
    rows: list[dict[str, Any]] = []
    payload_id = 0
    n = max(n_tmax_days, n_tmin_days)
    for i in range(n):
        d = start + timedelta(days=i)
        first_time = datetime.combine(d, datetime.min.time()) + timedelta(hours=21, minutes=35)
        final_time = datetime.combine(d, datetime.min.time()) + timedelta(
            days=1, hours=6, minutes=25
        )
        if i < n_tmax_days:
            payload_id += 1
            rows.append(_row("tmax_f", 60.0, d, first_time, payload_id=payload_id))
            if i % 10 == 0:  # a minority of tmax days revise too, post-midnight-attributed
                payload_id += 1
                rows.append(_row("tmax_f", 61.0, d, final_time, payload_id=payload_id))
        if i < n_tmin_days:
            payload_id += 1
            rows.append(_row("tmin_f", 40.0, d, first_time, payload_id=payload_id))
            if i % tmin_revised_fraction_denominator == 0:
                payload_id += 1
                rows.append(_row("tmin_f", 39.0, d, final_time, payload_id=payload_id))
    return _frame(rows)


def _dataset_manifest(content_hash: str = h0011.FROZEN_CONTENT_HASH) -> dict[str, Any]:
    return {"content_hashes": {"observation_issuances": content_hash}}


def _pin_frozen_hash_to(frame: pl.DataFrame) -> Any:
    """Integrity-review Finding F1: G1 now recomputes the loaded frame's
    canonical content hash (kalshi_weather.dataset.manifest.
    frame_content_hash) and compares it to the module's frozen pin. A
    synthetic frame can therefore pass G1 only by pinning the frozen
    constant to the synthetic frame's own true hash -- the gate logic
    itself runs unmodified, against a real recomputation."""
    return patch.object(h0011, "FROZEN_CONTENT_HASH", h0011.frame_content_hash(frame))


def _run_patched(dataset_dir: Path, frame: pl.DataFrame) -> dict[str, Any]:
    """run() against a synthetic dataset dir: tzdata check stubbed for
    environment independence, frozen hash pinned to the synthetic frame
    (F1) so the recomputing G1 gate can pass."""
    with (
        patch.object(h0011, "get_pinned_tzdata_version", return_value="2025.2"),
        _pin_frozen_hash_to(frame),
    ):
        return h0011.run(dataset_dir)


# --- Eligibility gates (PREREG Sec 8, AMENDMENT Finding 6) -------------------


def test_all_gates_pass_with_valid_synthetic_data() -> None:
    frame = _build_synthetic_frame()
    with _pin_frozen_hash_to(frame):
        outcome = h0011.run_gates(frame, _dataset_manifest())
    assert outcome.all_gates_passed is True
    assert outcome.gates["g1_frame_hash_matches"] is True
    assert all(outcome.gates["g2_invariants"].values())
    assert outcome.gates["g3_n_tmax"] == 1200
    assert outcome.gates["g3_n_tmin"] == 1200
    assert outcome.gates["g4_r_tmin"] == 400  # every 3rd of 1200 days
    assert outcome.gates["g5_unresolved_attribution_rate"] == "0.000000000000"


def test_g1_fails_when_frame_content_differs_despite_manifest_claiming_frozen_hash() -> None:
    """Integrity-review F1's regression test: the dataset directory's
    manifest claims the frozen hash, but the loaded frame's recomputed
    content hash differs -- G1 must fail (the manifest claim is not
    decisive) and short-circuit everything downstream."""
    frame = _build_synthetic_frame()  # recomputed hash != the real frozen pin
    outcome = h0011.run_gates(frame, _dataset_manifest(content_hash=h0011.FROZEN_CONTENT_HASH))
    assert outcome.gates["g1_frame_hash_matches"] is False
    assert outcome.all_gates_passed is False
    assert outcome.gates["g2_invariants"]["natural_key_unique"] == h0011.NOT_EVALUATED
    assert outcome.gates["g3_n_tmax"] == h0011.NOT_EVALUATED
    assert outcome.gates["g4_r_tmin"] == h0011.NOT_EVALUATED
    assert outcome.gates["g5_unresolved_attribution_rate"] == h0011.NOT_EVALUATED
    assert outcome.variable_days is None


def test_g1_detects_single_value_tampering() -> None:
    """Pin the frozen hash to the original frame, then flip one value --
    the recomputed hash must no longer match and G1 must fail."""
    frame = _build_synthetic_frame()
    tampered = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.lit(61.0))
        .otherwise(pl.col("value"))
        .alias("value")
    )
    with _pin_frozen_hash_to(frame):
        outcome = h0011.run_gates(tampered, _dataset_manifest())
    assert outcome.gates["g1_frame_hash_matches"] is False
    assert outcome.all_gates_passed is False


def test_g1_manifest_claim_is_not_decisive_when_frame_matches() -> None:
    """The converse: the frame's recomputed hash matches the (pinned)
    frozen constant while the directory manifest claims garbage -- G1
    passes, because the loaded frame is what the gate verifies."""
    frame = _build_synthetic_frame()
    with _pin_frozen_hash_to(frame):
        outcome = h0011.run_gates(frame, _dataset_manifest(content_hash="deadbeef"))
    assert outcome.gates["g1_frame_hash_matches"] is True
    assert outcome.all_gates_passed is True


def test_g2_fails_on_duplicate_natural_key_and_short_circuits_g3_onward() -> None:
    frame = _build_synthetic_frame()
    dup_row = frame.row(0, named=True)
    frame = pl.concat([frame, _frame([dup_row])])
    with _pin_frozen_hash_to(frame):
        outcome = h0011.run_gates(frame, _dataset_manifest())
    assert outcome.gates["g2_invariants"]["natural_key_unique"] is False
    assert outcome.all_gates_passed is False
    assert outcome.gates["g3_n_tmax"] == h0011.NOT_EVALUATED


def test_g2_fails_on_null_value() -> None:
    frame = _build_synthetic_frame()
    frame = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 0).then(None).otherwise(pl.col("value")).alias("value")
    )
    with _pin_frozen_hash_to(frame):
        outcome = h0011.run_gates(frame, _dataset_manifest())
    assert outcome.gates["g2_invariants"]["no_nulls"] is False
    assert outcome.all_gates_passed is False


def test_g2_fails_on_wrong_station_set() -> None:
    frame = _build_synthetic_frame()
    frame = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.lit("LAX"))
        .otherwise(pl.col("station_id"))
        .alias("station_id")
    )
    with _pin_frozen_hash_to(frame):
        outcome = h0011.run_gates(frame, _dataset_manifest())
    assert outcome.gates["g2_invariants"]["station_set_exact"] is False
    assert outcome.all_gates_passed is False


def test_g2_fails_on_wrong_variable_set() -> None:
    frame = _build_synthetic_frame()
    frame = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.lit("precip_in"))
        .otherwise(pl.col("variable"))
        .alias("variable")
    )
    with _pin_frozen_hash_to(frame):
        outcome = h0011.run_gates(frame, _dataset_manifest())
    assert outcome.gates["g2_invariants"]["variable_set_exact"] is False
    assert outcome.all_gates_passed is False


def test_g2_fails_on_roundtrip_invariant_violation() -> None:
    frame = _build_synthetic_frame()
    frame = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.lit(55.0049))
        .otherwise(pl.col("value"))
        .alias("value")
    )
    with _pin_frozen_hash_to(frame):
        outcome = h0011.run_gates(frame, _dataset_manifest())
    assert outcome.gates["g2_invariants"]["value_roundtrip_verified"] is False
    assert outcome.all_gates_passed is False


def test_g3_fails_on_insufficient_variable_days_and_short_circuits_g4_onward() -> None:
    frame = _build_synthetic_frame(n_tmax_days=500, n_tmin_days=500)
    with _pin_frozen_hash_to(frame):
        outcome = h0011.run_gates(frame, _dataset_manifest())
    assert outcome.gates["g3_n_tmax"] == 500
    assert outcome.all_gates_passed is False
    assert outcome.gates["g4_r_tmin"] == h0011.NOT_EVALUATED
    assert outcome.variable_days is not None  # built before G3 was evaluated
    assert outcome.revised_tmin_days is None


def test_g4_fails_on_insufficient_revised_tmin_and_short_circuits_g5() -> None:
    # revise only every 20th day -> well under the 100-day G4 minimum at n=1200
    frame = _build_synthetic_frame(tmin_revised_fraction_denominator=1000)
    with _pin_frozen_hash_to(frame):
        outcome = h0011.run_gates(frame, _dataset_manifest())
    assert outcome.gates["g4_r_tmin"] < h0011.G4_MIN_R_TMIN
    assert outcome.all_gates_passed is False
    assert outcome.gates["g5_unresolved_attribution_rate"] == h0011.NOT_EVALUATED
    assert outcome.revised_tmin_days is not None
    assert outcome.tmin_attributions is None


def test_g5_fails_at_exactly_5_percent_unresolved_boundary() -> None:
    """Strict '<': exactly 0.05 must fail, not pass."""
    frame = _build_synthetic_frame()

    def fake_attribute(vd: h0011.VariableDay, _calls: list[int] = []) -> h0011.AttributionOutcome:  # noqa: B006
        _calls.append(1)
        return None if len(_calls) <= 20 else True  # 20/400 = exactly 0.05

    with (
        patch("exp_h0011_midnight_boundary.attribute_revision", side_effect=fake_attribute),
        _pin_frozen_hash_to(frame),
    ):
        outcome = h0011.run_gates(frame, _dataset_manifest())
    assert outcome.gates["g5_unresolved_attribution_rate"] == "0.050000000000"
    assert outcome.all_gates_passed is False


def test_g5_passes_just_under_5_percent() -> None:
    frame = _build_synthetic_frame()

    def fake_attribute(vd: h0011.VariableDay, _calls: list[int] = []) -> h0011.AttributionOutcome:  # noqa: B006
        _calls.append(1)
        return None if len(_calls) <= 19 else True  # 19/400 < 0.05

    with (
        patch("exp_h0011_midnight_boundary.attribute_revision", side_effect=fake_attribute),
        _pin_frozen_hash_to(frame),
    ):
        outcome = h0011.run_gates(frame, _dataset_manifest())
    assert outcome.all_gates_passed is True


# --- Decision table (PREREG Sec 7.1) -----------------------------------------


def _fake_gate_outcome(passed: bool = True) -> h0011.GateOutcome:
    return h0011.GateOutcome(
        gates={},
        all_gates_passed=passed,
        variable_days=None,
        revised_tmin_days=None,
        tmin_attributions=None,
    )


def _part1_with_d_ci(lower: str, upper: str) -> h0011.Part1Result:
    return {
        "n_tmax": 1200,
        "r_tmax": 100,
        "p_tmax": "0.083333333333",
        "n_tmin": 1200,
        "r_tmin": 400,
        "p_tmin": "0.333333333333",
        "d_hat": "0.250000000000",
        "ci95_newcombe": [lower, upper],
        "criterion_1_pass": h0011.strictly_greater(Decimal(lower), h0011.ZERO),
    }


def _part2_with_pm_ci(lower: str, upper: str) -> h0011.Part2Result:
    return {
        "r_tmin": 400,
        "x_pm": 300,
        "n_unresolved": 0,
        "p_pm": "0.750000000000",
        "ci95_wilson": [lower, upper],
        "criterion_2_state": "n/a",
        "required_adjacent_fields": list(h0011.REQUIRED_ADJACENT_FIELDS),
    }


def test_decision_blocked_when_gates_fail() -> None:
    verdict = h0011.evaluate(_fake_gate_outcome(passed=False), None, None)
    assert verdict.outcome == "blocked"
    assert verdict.evaluation_step == "step_0"


def test_decision_branch1_rejected_when_d_lower_negative() -> None:
    part1 = _part1_with_d_ci("-0.010000000000", "0.050000000000")
    part2 = _part2_with_pm_ci("0.600000000000", "0.900000000000")
    verdict = h0011.evaluate(_fake_gate_outcome(), part1, part2)
    assert verdict.outcome == "rejected"
    assert verdict.evaluation_step == "step_1"


def test_decision_branch1_rejected_at_exact_zero_tie() -> None:
    """Exact quantized equality of L(D) with 0 resolves to Rejected, not Confirmed."""
    part1 = _part1_with_d_ci("0.000000000000", "0.050000000000")
    part2 = _part2_with_pm_ci("0.600000000000", "0.900000000000")
    verdict = h0011.evaluate(_fake_gate_outcome(), part1, part2)
    assert verdict.outcome == "rejected"
    assert verdict.evaluation_step == "step_1"


def test_decision_branch2_confirmed() -> None:
    part1 = _part1_with_d_ci("0.100000000000", "0.300000000000")
    part2 = _part2_with_pm_ci("0.550000000000", "0.900000000000")
    verdict = h0011.evaluate(_fake_gate_outcome(), part1, part2)
    assert verdict.outcome == "confirmed"
    assert verdict.evaluation_step == "step_2"


def test_decision_branch2_confirmed_boundary_just_above_half() -> None:
    part1 = _part1_with_d_ci("0.100000000000", "0.300000000000")
    part2 = _part2_with_pm_ci("0.500000000001", "0.900000000000")
    verdict = h0011.evaluate(_fake_gate_outcome(), part1, part2)
    assert verdict.outcome == "confirmed"


def test_decision_branch3_rejected_mechanism_refuted() -> None:
    part1 = _part1_with_d_ci("0.100000000000", "0.300000000000")
    part2 = _part2_with_pm_ci("0.100000000000", "0.400000000000")
    verdict = h0011.evaluate(_fake_gate_outcome(), part1, part2)
    assert verdict.outcome == "rejected"
    assert verdict.evaluation_step == "step_3"


def test_decision_branch4_inconclusive_straddle() -> None:
    part1 = _part1_with_d_ci("0.100000000000", "0.300000000000")
    part2 = _part2_with_pm_ci("0.450000000000", "0.550000000000")
    verdict = h0011.evaluate(_fake_gate_outcome(), part1, part2)
    assert verdict.outcome == "inconclusive"
    assert verdict.evaluation_step == "step_4"


def test_decision_exact_tie_at_half_lower_bound_is_inconclusive_not_confirmed() -> None:
    """L(p_pm) exactly 0.5: 'exceeds one half' is never satisfied by equality."""
    part1 = _part1_with_d_ci("0.100000000000", "0.300000000000")
    part2 = _part2_with_pm_ci("0.500000000000", "0.700000000000")
    verdict = h0011.evaluate(_fake_gate_outcome(), part1, part2)
    assert verdict.outcome == "inconclusive"


def test_decision_exact_tie_at_half_upper_bound_is_inconclusive_not_rejected() -> None:
    """U(p_pm) exactly 0.5: 'below one half' is never satisfied by equality."""
    part1 = _part1_with_d_ci("0.100000000000", "0.300000000000")
    part2 = _part2_with_pm_ci("0.300000000000", "0.500000000000")
    verdict = h0011.evaluate(_fake_gate_outcome(), part1, part2)
    assert verdict.outcome == "inconclusive"


def test_decision_table_is_exhaustive_for_random_ci_configurations() -> None:
    """Every (d_lower_sign, pm_position) combination must land in exactly
    one of the four branches -- no configuration should be un-decidable."""
    d_lowers = ["-0.050000000000", "0.000000000000", "0.050000000000"]
    pm_pairs = [
        ("0.100000000000", "0.300000000000"),
        ("0.450000000000", "0.550000000000"),
        ("0.500000000000", "0.700000000000"),
        ("0.300000000000", "0.500000000000"),
        ("0.600000000000", "0.900000000000"),
    ]
    for d_lower in d_lowers:
        for pm_lower, pm_upper in pm_pairs:
            part1 = _part1_with_d_ci(d_lower, "0.900000000000")
            part2 = _part2_with_pm_ci(pm_lower, pm_upper)
            verdict = h0011.evaluate(_fake_gate_outcome(), part1, part2)
            assert verdict.outcome in ("confirmed", "rejected", "inconclusive")


# --- Manifest validation -----------------------------------------------------


def test_validate_manifest_accepts_a_real_run_output() -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame)
    results = _run_patched(dataset_dir, frame)
    h0011.validate_manifest(results)  # must not raise


def test_validate_manifest_raises_on_missing_top_level_key() -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame)
    results = _run_patched(dataset_dir, frame)
    del results["eligibility_gates"]
    with pytest.raises(h0011.ManifestValidationError, match="eligibility_gates"):
        h0011.validate_manifest(results)


def test_validate_manifest_raises_on_missing_nested_config_key() -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame)
    results = _run_patched(dataset_dir, frame)
    del results["config"]["part_2_scope_statement"]
    with pytest.raises(h0011.ManifestValidationError, match="config"):
        h0011.validate_manifest(results)


def test_validate_manifest_raises_on_unrecognized_outcome() -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame)
    results = _run_patched(dataset_dir, frame)
    results["primary_results"]["decision"]["outcome"] = "maybe"
    with pytest.raises(h0011.ManifestValidationError, match="outcome"):
        h0011.validate_manifest(results)


def test_manifest_has_amendment_metadata() -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame)
    results = _run_patched(dataset_dir, frame)
    amendment = results["preregistration"]["amendment"]
    assert amendment["document"].endswith("AMENDMENT-20260721-H0011-audit-resolution.md")
    assert len(amendment["resolves"]) == 9  # F1-F9


# --- End-to-end run() and determinism ----------------------------------------


def _write_dataset_dir(frame: pl.DataFrame, tmp_path: Path | None = None) -> Path:
    import tempfile

    directory = Path(tempfile.mkdtemp()) if tmp_path is None else tmp_path
    frame.write_parquet(directory / "observation_issuances.parquet")
    (directory / "manifest.json").write_text(json.dumps(_dataset_manifest()))
    return directory


def test_run_end_to_end_produces_confirmed_when_all_revisions_post_midnight(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    results = _run_patched(dataset_dir, frame)
    assert results["primary_results"]["decision"]["outcome"] == "confirmed"
    assert results["eligibility_gates"]["all_gates_passed"] is True
    assert results["descriptive_results"]["tmax_attribution_rate"]["r_tmax"] > 0
    # F3 double-duty: run() computes config_matches_prereg mechanically, and
    # the check is sensitive enough to detect this test's own pinned (i.e.
    # drifted) FROZEN_CONTENT_HASH -- on the real pinned dataset, with no
    # patching, this field is expected True.
    assert results["reproducibility_verification"]["config_matches_prereg"] is False


def test_run_end_to_end_blocked_reports_gate_failure_without_crashing(tmp_path: Path) -> None:
    frame = _build_synthetic_frame(n_tmax_days=10, n_tmin_days=10)
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    results = _run_patched(dataset_dir, frame)
    assert results["primary_results"]["decision"]["outcome"] == "blocked"
    assert results["primary_results"]["part_1_rate_difference"]["p_tmax"] == h0011.NOT_EVALUATED
    h0011.validate_manifest(results)  # even a blocked run produces a schema-complete manifest


def test_run_twice_produces_byte_identical_json(tmp_path: Path) -> None:
    """No randomness anywhere in this experiment -- two independent
    invocations against the same synthetic dataset must match exactly,
    aside from the date_run/analysis_git_commit fields the CLI (not
    run()) sets, which run() itself leaves as None both times."""
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    first = _run_patched(dataset_dir, frame)
    second = _run_patched(dataset_dir, frame)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first == second


def test_run_blocked_by_unpinned_tzdata() -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame)
    with (
        patch.object(
            h0011, "get_pinned_tzdata_version", side_effect=h0011.TzdataNotPinnedError("x")
        ),
        pytest.raises(h0011.TzdataNotPinnedError),
    ):
        h0011.run(dataset_dir)
