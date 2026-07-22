"""Tests for scripts/exp_h0013_chi_replication.py.

Implements the PREREG-20260722-H0013-chi-replication.md Sec 11
implementation-integrity checklist. All data here is hand-constructed/
synthetic; nothing in this file touches the real pinned
exp-20260722-h0013-replication dataset or inspects a real CHI revision/
attribution statistic. run()-level tests use a fabricated dataset_dir
fixture (synthetic issuance rows), exercising the gate/manifest code
paths, not H0013 itself. The real tzdata pip-package check is
monkeypatched in run()-level tests purely for environment independence.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any
from unittest.mock import patch

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import exp_h0013_chi_replication as h0013

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
    station_id: str = "CHI",
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
) -> h0013.VariableDay:
    return h0013.VariableDay(
        variable=variable,
        observation_date=observation_date,
        issuances=[(t, h0013.quantize_value(v)) for t, v in issuances],
    )


# --- Decimal policy (PREREG Sec 2/6; F3/F4 inherited) ------------------------


def test_quantize_value_uses_str_construction_not_direct_float() -> None:
    via_str = Decimal(str(0.1))
    via_direct = Decimal(0.1)  # noqa: RUF032 -- the point of this test is this exact call
    assert via_str != via_direct
    assert h0013.quantize_value(0.1) == Decimal("0.10")


def test_value_roundtrips_true_for_clean_two_decimal_values() -> None:
    assert h0013.value_roundtrips(55.0) is True
    assert h0013.value_roundtrips(-12.34) is True


def test_value_roundtrips_false_for_corrupted_precision() -> None:
    assert h0013.value_roundtrips(55.0049) is False


def test_strictly_greater_and_less_are_quantized() -> None:
    a = Decimal(1) / Decimal(200)
    b = Decimal("0.005")
    assert h0013.strictly_greater(a, b) is False  # exact tie, not strictly greater
    assert h0013.strictly_less(a, b) is False
    assert h0013.strictly_greater(Decimal("0.006"), b) is True
    assert h0013.strictly_less(Decimal("0.004"), b) is True


def test_serialize_decimal_fixed_point_12_digits() -> None:
    assert h0013.serialize_decimal(Decimal("0.108")) == "0.108000000000"
    assert h0013.serialize_decimal(Decimal("-0.005")) == "-0.005000000000"
    text = h0013.serialize_decimal(Decimal("0.0000000000001"))
    assert "E" not in text and "e" not in text
    assert h0013.serialize_decimal(Decimal("-0.0000000000001")) == "0.000000000000"


# --- Statistical methods (PREREG Sec 6) -- hand-derived cross-checks ---------


def test_wilson_interval_hand_calculated_x60_n100() -> None:
    """PREREG Sec 11 branch example: lower bound just above 1/2. Expected
    bounds re-derived here from the frozen formula at the same 50-digit
    precision -- an independent arrangement, not a call into the module's
    interval function."""
    lower, upper = h0013.wilson_interval(60, 100)
    with h0013.localcontext() as ctx:
        ctx.prec = h0013.DECIMAL_CONTEXT_PRECISION
        z = h0013.Z_95
        p = Decimal(60) / Decimal(100)
        z2 = z * z
        n = Decimal(100)
        denom = 1 + z2 / n
        expected_lower = ((p + z2 / (2 * n)) - z * (p * (1 - p) / n + z2 / (4 * n * n)).sqrt()) / (
            denom
        )
        expected_upper = ((p + z2 / (2 * n)) + z * (p * (1 - p) / n + z2 / (4 * n * n)).sqrt()) / (
            denom
        )
    assert abs(lower - expected_lower) < Decimal("1E-30")
    assert abs(upper - expected_upper) < Decimal("1E-30")
    assert h0013.strictly_greater(lower, h0013.HALF) is True
    # Second, genuinely independent derivation: Wilson bounds are the roots
    # of (p - c)^2 = z^2 c(1-c)/n -- the quadratic-root arrangement, not the
    # implementation's center +/- halfwidth arrangement.
    with h0013.localcontext() as ctx:
        ctx.prec = 60
        z = h0013.Z_95
        n = Decimal(100)
        p = Decimal(60) / n
        a = 1 + z * z / n
        b = -(2 * p + z * z / n)
        c = p * p
        disc = (b * b - 4 * a * c).sqrt()
        root_lower = (-b - disc) / (2 * a)
        root_upper = (-b + disc) / (2 * a)
    assert abs(lower - root_lower) < Decimal("1E-30")
    assert abs(upper - root_upper) < Decimal("1E-30")
    # Fixed 10-digit spot values from that independent derivation:
    assert abs(lower - Decimal("0.5020025868")) < Decimal("1E-9")
    assert abs(upper - Decimal("0.6905987136")) < Decimal("1E-9")


def test_wilson_interval_x55_n100_straddles_half() -> None:
    lower, upper = h0013.wilson_interval(55, 100)
    assert h0013.strictly_greater(lower, h0013.HALF) is False
    assert h0013.strictly_less(upper, h0013.HALF) is False


def test_wilson_interval_x30_n100_entirely_below_half() -> None:
    _lower, upper = h0013.wilson_interval(30, 100)
    assert h0013.strictly_less(upper, h0013.HALF) is True


def test_wilson_interval_zero_and_full_numerators_well_defined() -> None:
    lower, _ = h0013.wilson_interval(0, 100)
    assert lower == Decimal(0)
    _, upper = h0013.wilson_interval(100, 100)
    assert upper == Decimal(1)
    with pytest.raises(ValueError, match="n > 0"):
        h0013.wilson_interval(0, 0)


def test_newcombe_interval_equal_counts_gives_nonpositive_lower_bound() -> None:
    lower, upper = h0013.newcombe_interval(x_tmin=50, n_tmin=100, x_tmax=50, n_tmax=100)
    assert lower < 0
    assert upper > 0
    assert h0013.strictly_greater(lower, h0013.ZERO) is False


def test_newcombe_interval_reproduces_h0011_published_nyc_interval() -> None:
    """The strongest available hand-check: at NYC's published counts
    (311/1282 vs 170/1282), the implementation must reproduce H0011's
    published CI strings exactly under the frozen serialization."""
    lower, upper = h0013.newcombe_interval(x_tmin=311, n_tmin=1282, x_tmax=170, n_tmax=1282)
    assert h0013.serialize_decimal(lower) == "0.079970196795"
    assert h0013.serialize_decimal(upper) == "0.139841273431"


# --- Population / revision definitions (PREREG Sec 2/3) ----------------------


def test_is_revised_true_when_first_and_final_differ() -> None:
    vd = _vd(
        "tmin_f",
        date(2026, 1, 1),
        [(datetime(2026, 1, 1, 22), 20.0), (datetime(2026, 1, 2, 7), 19.0)],
    )
    assert h0013.is_revised(vd) is True


def test_single_issuance_day_is_unrevised() -> None:
    vd = _vd("tmax_f", date(2026, 1, 1), [(datetime(2026, 1, 1, 22), 30.0)])
    assert h0013.is_revised(vd) is False


def test_flip_flop_day_is_unrevised_but_counted() -> None:
    vd = _vd(
        "tmax_f",
        date(2026, 1, 1),
        [
            (datetime(2026, 1, 1, 22), 30.0),
            (datetime(2026, 1, 1, 23), 31.0),
            (datetime(2026, 1, 2, 7), 30.0),
        ],
    )
    assert h0013.is_revised(vd) is False
    assert h0013.is_flip_flop(vd) is True


# --- Post-midnight attribution in America/Chicago (PREREG Sec 4/11) ----------


def test_local_midnight_boundary_is_start_of_next_day_chicago() -> None:
    boundary = h0013.local_midnight_boundary(date(2026, 1, 15))
    assert boundary == datetime(2026, 1, 16, 0, 0, 0, tzinfo=h0013.LOCAL_TZ)
    assert boundary.utcoffset() == timedelta(hours=-6)  # CST


def test_attribution_exactly_at_boundary_classifies_post_midnight() -> None:
    obs_date = date(2026, 1, 15)
    boundary_local = datetime(2026, 1, 16, 0, 0, 0, tzinfo=h0013.LOCAL_TZ)
    boundary_utc_naive = boundary_local.astimezone(UTC).replace(tzinfo=None)
    vd = _vd("tmin_f", obs_date, [(datetime(2026, 1, 15, 22), 20.0), (boundary_utc_naive, 19.0)])
    assert h0013.attribute_revision(vd) is True
    assert h0013.is_exact_boundary(vd) is True


def test_attribution_one_second_before_and_after_boundary() -> None:
    obs_date = date(2026, 1, 15)
    boundary_local = datetime(2026, 1, 16, 0, 0, 0, tzinfo=h0013.LOCAL_TZ)
    before = (boundary_local - timedelta(seconds=1)).astimezone(UTC).replace(tzinfo=None)
    after = (boundary_local + timedelta(seconds=1)).astimezone(UTC).replace(tzinfo=None)
    vd_before = _vd("tmin_f", obs_date, [(datetime(2026, 1, 15, 22), 20.0), (before, 19.0)])
    vd_after = _vd("tmin_f", obs_date, [(datetime(2026, 1, 15, 22), 20.0), (after, 19.0)])
    assert h0013.attribute_revision(vd_before) is False
    assert h0013.attribute_revision(vd_after) is True


def test_chicago_offset_regime_winter_0530_utc_is_pre_midnight() -> None:
    """PREREG Sec 11's CHI-specific conversion check: a naive-UTC issuance
    at 05:30 on T+1 is 23:30 CST on T (UTC-6) in winter -- PRE-midnight.
    Under NYC's offset (UTC-5) the same instant would be post-midnight;
    this is the replication's genuinely new conversion regime."""
    obs_date = date(2026, 1, 15)
    issuance_utc_naive = datetime(2026, 1, 16, 5, 30)
    local = h0013.to_local(issuance_utc_naive)
    assert local == datetime(2026, 1, 15, 23, 30, tzinfo=h0013.LOCAL_TZ)
    vd = _vd("tmin_f", obs_date, [(datetime(2026, 1, 15, 22), 20.0), (issuance_utc_naive, 19.0)])
    assert h0013.attribute_revision(vd) is False


def test_chicago_offset_regime_summer_0530_utc_is_post_midnight() -> None:
    """Same wall-clock UTC input in summer: 05:30 UTC on T+1 is 00:30 CDT
    (UTC-5) on T+1 -- POST-midnight."""
    obs_date = date(2026, 7, 15)
    issuance_utc_naive = datetime(2026, 7, 16, 5, 30)
    local = h0013.to_local(issuance_utc_naive)
    assert local == datetime(2026, 7, 16, 0, 30, tzinfo=h0013.LOCAL_TZ)
    vd = _vd("tmin_f", obs_date, [(datetime(2026, 7, 15, 22), 70.0), (issuance_utc_naive, 69.0)])
    assert h0013.attribute_revision(vd) is True


def test_earliest_appearance_tie_break_nonmonotone_sequence() -> None:
    vd = _vd(
        "tmin_f",
        date(2026, 1, 1),
        [
            (datetime(2026, 1, 1, 21), 20.0),
            (datetime(2026, 1, 1, 22), 19.0),  # matches final -- earliest appearance
            (datetime(2026, 1, 1, 23), 18.0),  # deviates
            (datetime(2026, 1, 2, 7), 19.0),  # final, reappears
        ],
    )
    assert h0013.first_appearance_of_final_value(vd) == datetime(2026, 1, 1, 22)
    assert h0013.is_nonmonotone_final_appearance(vd) is True


def test_multiple_post_midnight_candidates_uses_earliest() -> None:
    obs_date = date(2026, 1, 1)
    boundary_local = datetime(2026, 1, 2, 0, 0, 0, tzinfo=h0013.LOCAL_TZ)

    def utc_naive(offset_minutes: int) -> datetime:
        return (
            (boundary_local + timedelta(minutes=offset_minutes))
            .astimezone(UTC)
            .replace(tzinfo=None)
        )

    vd = _vd(
        "tmin_f",
        obs_date,
        [
            (utc_naive(-180), 20.0),
            (utc_naive(30), 19.0),  # earliest post-midnight match
            (utc_naive(60), 19.0),
            (utc_naive(385), 19.0),
        ],
    )
    assert h0013.first_appearance_of_final_value(vd) == utc_naive(30)
    assert h0013.attribute_revision(vd) is True


def test_attribution_unresolved_on_extreme_date_returns_none() -> None:
    vd = _vd(
        "tmin_f", date.max, [(datetime(2026, 1, 1, 22), 20.0), (datetime(2026, 1, 2, 7), 19.0)]
    )
    assert h0013.attribute_revision(vd) is None
    assert h0013.is_exact_boundary(vd) is False


def test_unresolved_stays_in_denominator_not_numerator() -> None:
    revised_days = [
        _vd(
            "tmin_f", date.max, [(datetime(2026, 1, 1, 22), 20.0), (datetime(2026, 1, 2, 7), 19.0)]
        ),
        _vd(
            "tmin_f",
            date(2026, 1, 1),
            [(datetime(2026, 1, 1, 22), 20.0), (datetime(2026, 1, 2, 7), 19.0)],
        ),
    ]
    attributions = [h0013.attribute_revision(vd) for vd in revised_days]
    assert attributions[0] is None
    assert attributions[1] is True
    part2 = h0013.compute_part2(revised_days, attributions)
    assert part2["r_tmin"] == 2
    assert part2["x_pm"] == 1
    assert part2["n_unresolved"] == 1


# --- DST transitions in America/Chicago (PREREG Sec 2/11) --------------------


def test_dst_spring_forward_2026_conversion_uses_cdt_offset() -> None:
    """2026-03-08 02:00 local does not exist (jump to 03:00 CDT). An
    issuance after the transition converts at UTC-5 (CDT), not UTC-6."""
    naive_utc = datetime(2026, 3, 8, 8, 30)  # = 03:30 CDT
    local = h0013.to_local(naive_utc)
    assert local == datetime(2026, 3, 8, 3, 30, tzinfo=h0013.LOCAL_TZ)
    assert local.utcoffset() == timedelta(hours=-5)


def test_dst_spring_forward_boundary_classification_unaffected() -> None:
    obs_date = date(2026, 3, 7)  # boundary = 2026-03-08 00:00 CST (UTC-6)
    boundary_local = datetime(2026, 3, 8, 0, 0, 0, tzinfo=h0013.LOCAL_TZ)
    assert boundary_local.utcoffset() == timedelta(hours=-6)
    just_after = (boundary_local + timedelta(minutes=1)).astimezone(UTC).replace(tzinfo=None)
    vd = _vd("tmin_f", obs_date, [(datetime(2026, 3, 7, 22), 30.0), (just_after, 29.0)])
    assert h0013.attribute_revision(vd) is True


def test_dst_fall_back_2025_conversion_handles_fold() -> None:
    """2025-11-02: 01:30 CDT and 01:30 CST both exist. 06:30 UTC = 01:30
    CDT (before fold, UTC-5); 07:30 UTC = 01:30 CST (after fold, UTC-6)."""
    before_fold = h0013.to_local(datetime(2025, 11, 2, 6, 30))
    after_fold = h0013.to_local(datetime(2025, 11, 2, 7, 30))
    assert before_fold.utcoffset() == timedelta(hours=-5)
    assert after_fold.utcoffset() == timedelta(hours=-6)
    assert before_fold.hour == after_fold.hour == 1
    assert before_fold.timestamp() < after_fold.timestamp()


def test_dst_fall_back_boundary_classification_unaffected() -> None:
    obs_date = date(2025, 11, 1)  # boundary = 2025-11-02 00:00 CDT (UTC-5)
    boundary_local = datetime(2025, 11, 2, 0, 0, 0, tzinfo=h0013.LOCAL_TZ)
    assert boundary_local.utcoffset() == timedelta(hours=-5)
    just_before = (boundary_local - timedelta(minutes=1)).astimezone(UTC).replace(tzinfo=None)
    vd = _vd("tmin_f", obs_date, [(datetime(2025, 11, 1, 22), 40.0), (just_before, 39.0)])
    assert h0013.attribute_revision(vd) is False


# --- tzdata pinning ----------------------------------------------------------


def test_get_pinned_tzdata_version_raises_when_not_installed() -> None:
    with (
        patch("exp_h0013_chi_replication.version", side_effect=PackageNotFoundError),
        pytest.raises(h0013.TzdataNotPinnedError),
    ):
        h0013.get_pinned_tzdata_version()


def test_tzdata_is_pinned_in_the_execution_environment() -> None:
    assert h0013.get_pinned_tzdata_version()


# --- Config-drift verification -----------------------------------------------


def test_verify_config_matches_prereg_true_for_unmodified_module() -> None:
    assert h0013.verify_config_matches_prereg() is True


def test_verify_config_matches_prereg_detects_drift() -> None:
    with patch.object(h0013, "G4_MIN_R_TMIN", 99):
        assert h0013.verify_config_matches_prereg() is False
    with patch.object(h0013, "Z_95", Decimal("1.96")):
        assert h0013.verify_config_matches_prereg() is False
    with patch.object(h0013, "FROZEN_CONTENT_HASH", "0" * 64):
        assert h0013.verify_config_matches_prereg() is False
    with patch.object(h0013, "TIMEZONE_NAME", "America/New_York"):
        assert h0013.verify_config_matches_prereg() is False
    with patch.object(h0013, "NYC_REFERENCE", {**h0013.NYC_REFERENCE, "d_nyc": "0.200000000000"}):
        assert h0013.verify_config_matches_prereg() is False


# --- Synthetic dataset builder for gate/run-level tests ----------------------


def _build_synthetic_frame(
    *,
    n_tmax_days: int = 1200,
    n_tmin_days: int = 1200,
    tmin_revised_fraction_denominator: int = 3,
    start: date = date(2023, 1, 1),
    include_other_stations: bool = True,
) -> pl.DataFrame:
    """A multi-station frame whose CHI subset satisfies G2-G5 by
    construction: most tmax days unrevised, ~1/3 of tmin days revised with
    post-midnight-attributed finals (06:25 UTC next day = 00:25 CST /
    01:25 CDT -- post-midnight in both Chicago offset regimes). Includes
    NYC (and one DEN) rows so the frozen CHI filter and the NYC
    consistency check are exercised against a genuinely multi-station
    artifact."""
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
            if i % 10 == 0:
                payload_id += 1
                rows.append(_row("tmax_f", 61.0, d, final_time, payload_id=payload_id))
        if i < n_tmin_days:
            payload_id += 1
            rows.append(_row("tmin_f", 40.0, d, first_time, payload_id=payload_id))
            if i % tmin_revised_fraction_denominator == 0:
                payload_id += 1
                rows.append(_row("tmin_f", 39.0, d, final_time, payload_id=payload_id))
        if include_other_stations and i < 400:
            # NYC subset: revised tmin every 4th day, tmax every 20th.
            payload_id += 1
            rows.append(
                _row("tmax_f", 62.0, d, first_time, station_id="NYC", payload_id=payload_id)
            )
            if i % 20 == 0:
                payload_id += 1
                rows.append(
                    _row("tmax_f", 63.0, d, final_time, station_id="NYC", payload_id=payload_id)
                )
            payload_id += 1
            rows.append(
                _row("tmin_f", 42.0, d, first_time, station_id="NYC", payload_id=payload_id)
            )
            if i % 4 == 0:
                payload_id += 1
                rows.append(
                    _row("tmin_f", 41.0, d, final_time, station_id="NYC", payload_id=payload_id)
                )
    if include_other_stations:
        payload_id += 1
        rows.append(
            _row(
                "tmax_f",
                80.0,
                start,
                datetime.combine(start, datetime.min.time()),
                station_id="DEN",
                payload_id=payload_id,
            )
        )
    return _frame(rows)


def _pin_frozen_hash_to(frame: pl.DataFrame) -> Any:
    """G1 recomputes the loaded FULL frame's canonical content hash; a
    synthetic frame passes only by pinning the frozen constant to its own
    true hash -- the gate logic runs unmodified."""
    return patch.object(h0013, "FROZEN_CONTENT_HASH", h0013.frame_content_hash(frame))


def _write_dataset_dir(frame: pl.DataFrame, tmp_path: Path) -> Path:
    frame.write_parquet(tmp_path / "observation_issuances.parquet")
    return tmp_path


def _run_patched(dataset_dir: Path, frame: pl.DataFrame) -> dict[str, Any]:
    with (
        patch.object(h0013, "get_pinned_tzdata_version", return_value="2025.2"),
        _pin_frozen_hash_to(frame),
    ):
        return h0013.run(dataset_dir)


# --- Eligibility gates (PREREG Sec 8) ----------------------------------------


def test_all_gates_pass_with_valid_multi_station_synthetic_data() -> None:
    frame = _build_synthetic_frame()
    with _pin_frozen_hash_to(frame):
        outcome = h0013.run_gates(frame)
    assert outcome.all_gates_passed is True
    assert outcome.gates["g1_frame_hash_matches"] is True
    assert all(outcome.gates["g2_invariants"].values())  # on the CHI-filtered frame
    assert outcome.gates["g3_n_tmax"] == 1200  # NYC/DEN rows excluded by the filter
    assert outcome.gates["g3_n_tmin"] == 1200
    assert outcome.gates["g4_r_tmin"] == 400
    assert outcome.gates["g5_unresolved_attribution_rate"] == "0.000000000000"


def test_g1_fails_on_content_mismatch_and_short_circuits_everything() -> None:
    frame = _build_synthetic_frame()  # recomputed hash != the real frozen pin
    outcome = h0013.run_gates(frame)
    assert outcome.gates["g1_frame_hash_matches"] is False
    assert outcome.all_gates_passed is False
    assert outcome.gates["g2_invariants"]["natural_key_unique"] == h0013.NOT_EVALUATED
    assert outcome.gates["g3_n_tmax"] == h0013.NOT_EVALUATED
    assert outcome.variable_days is None


def test_g1_detects_single_value_tampering() -> None:
    frame = _build_synthetic_frame()
    tampered = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.lit(61.0))
        .otherwise(pl.col("value"))
        .alias("value")
    )
    with _pin_frozen_hash_to(frame):
        outcome = h0013.run_gates(tampered)
    assert outcome.gates["g1_frame_hash_matches"] is False


def test_chi_filter_scopes_g2_station_set_to_chi_only() -> None:
    """The frozen filter step: G2's station_set_exact is evaluated on the
    filtered frame and passes even though the full frame has 3 stations."""
    frame = _build_synthetic_frame(include_other_stations=True)
    with _pin_frozen_hash_to(frame):
        outcome = h0013.run_gates(frame)
    assert outcome.gates["g2_invariants"]["station_set_exact"] is True


def test_empty_chi_subset_blocks_at_g2_never_silently_passes() -> None:
    """PREREG Sec 11: a frame whose CHI subset is empty must block (the
    empty station set != {CHI}), not pass vacuously."""
    frame = _build_synthetic_frame(include_other_stations=True).filter(
        pl.col("station_id") != "CHI"
    )
    with _pin_frozen_hash_to(frame):
        outcome = h0013.run_gates(frame)
    assert outcome.gates["g2_invariants"]["station_set_exact"] is False
    assert outcome.all_gates_passed is False


def test_g2_fails_on_duplicate_natural_key_in_chi_subset() -> None:
    frame = _build_synthetic_frame()
    chi_row = frame.filter(pl.col("station_id") == "CHI").row(0, named=True)
    frame = pl.concat([frame, _frame([chi_row])])
    with _pin_frozen_hash_to(frame):
        outcome = h0013.run_gates(frame)
    assert outcome.gates["g2_invariants"]["natural_key_unique"] is False
    assert outcome.all_gates_passed is False
    assert outcome.gates["g3_n_tmax"] == h0013.NOT_EVALUATED


def test_g2_ignores_duplicate_rows_outside_chi() -> None:
    """A duplicate NYC row must NOT fail G2 -- the invariants are scoped to
    the CHI-filtered frame (the decisional population)."""
    frame = _build_synthetic_frame()
    nyc_row = frame.filter(pl.col("station_id") == "NYC").row(0, named=True)
    frame_with_dup = pl.concat([frame, _frame([nyc_row])])
    with _pin_frozen_hash_to(frame_with_dup):
        outcome = h0013.run_gates(frame_with_dup)
    assert outcome.gates["g2_invariants"]["natural_key_unique"] is True
    assert outcome.all_gates_passed is True


def test_g3_fails_on_insufficient_chi_days() -> None:
    frame = _build_synthetic_frame(n_tmax_days=500, n_tmin_days=500)
    with _pin_frozen_hash_to(frame):
        outcome = h0013.run_gates(frame)
    assert outcome.gates["g3_n_tmax"] == 500
    assert outcome.all_gates_passed is False
    assert outcome.gates["g4_r_tmin"] == h0013.NOT_EVALUATED


def test_g4_fails_on_insufficient_revised_tmin() -> None:
    frame = _build_synthetic_frame(tmin_revised_fraction_denominator=1000)
    with _pin_frozen_hash_to(frame):
        outcome = h0013.run_gates(frame)
    assert outcome.gates["g4_r_tmin"] < h0013.G4_MIN_R_TMIN
    assert outcome.all_gates_passed is False
    assert outcome.gates["g5_unresolved_attribution_rate"] == h0013.NOT_EVALUATED


def test_g5_fails_at_exactly_5_percent_unresolved_boundary() -> None:
    """Strict '<': exactly 0.05 must fail."""
    frame = _build_synthetic_frame()

    def fake_attribute(vd: h0013.VariableDay, _calls: list[int] = []) -> h0013.AttributionOutcome:  # noqa: B006
        _calls.append(1)
        return None if len(_calls) <= 20 else True  # 20/400 = exactly 0.05

    with (
        patch("exp_h0013_chi_replication.attribute_revision", side_effect=fake_attribute),
        _pin_frozen_hash_to(frame),
    ):
        outcome = h0013.run_gates(frame)
    assert outcome.gates["g5_unresolved_attribution_rate"] == "0.050000000000"
    assert outcome.all_gates_passed is False


# --- Decision table (PREREG Sec 7.1) -----------------------------------------


def _fake_gate_outcome(passed: bool = True) -> h0013.GateOutcome:
    return h0013.GateOutcome(
        gates={},
        all_gates_passed=passed,
        variable_days=None,
        revised_tmin_days=None,
        tmin_attributions=None,
    )


def _part1_with_d_ci(lower: str, upper: str) -> h0013.Part1Result:
    return {
        "n_tmax": 1200,
        "r_tmax": 100,
        "p_tmax": "0.083333333333",
        "n_tmin": 1200,
        "r_tmin": 400,
        "p_tmin": "0.333333333333",
        "d_hat": "0.250000000000",
        "ci95_newcombe": [lower, upper],
        "criterion_1_pass": h0013.strictly_greater(Decimal(lower), h0013.ZERO),
    }


def _part2_with_pm_ci(lower: str, upper: str, state: str = "n/a") -> h0013.Part2Result:
    return {
        "r_tmin": 400,
        "x_pm": 300,
        "n_unresolved": 0,
        "p_pm": "0.750000000000",
        "ci95_wilson": [lower, upper],
        "criterion_2_state": state,
        "required_adjacent_fields": list(h0013.REQUIRED_ADJACENT_FIELDS),
    }


def test_decision_blocked_when_gates_fail() -> None:
    verdict = h0013.evaluate(_fake_gate_outcome(passed=False), None, None)
    assert verdict.outcome == "blocked"
    assert verdict.evaluation_step == "step_0"


@pytest.mark.parametrize(
    ("d_lower", "pm_lower", "pm_upper", "expected_outcome", "expected_step"),
    [
        ("-0.010000000000", "0.600000000000", "0.900000000000", "rejected", "step_1"),
        ("0.000000000000", "0.600000000000", "0.900000000000", "rejected", "step_1"),
        ("0.100000000000", "0.550000000000", "0.900000000000", "confirmed", "step_2"),
        ("0.100000000000", "0.500000000001", "0.900000000000", "confirmed", "step_2"),
        ("0.100000000000", "0.100000000000", "0.400000000000", "rejected", "step_3"),
        ("0.100000000000", "0.450000000000", "0.550000000000", "inconclusive", "step_4"),
        ("0.100000000000", "0.500000000000", "0.700000000000", "inconclusive", "step_4"),
        ("0.100000000000", "0.300000000000", "0.500000000000", "inconclusive", "step_4"),
    ],
)
def test_decision_table_branches(
    d_lower: str, pm_lower: str, pm_upper: str, expected_outcome: str, expected_step: str
) -> None:
    part1 = _part1_with_d_ci(d_lower, "0.900000000000")
    part2 = _part2_with_pm_ci(pm_lower, pm_upper)
    verdict = h0013.evaluate(_fake_gate_outcome(), part1, part2)
    assert verdict.outcome == expected_outcome
    assert verdict.evaluation_step == expected_step


# --- Replication assessment (PREREG Sec 7.3/11) ------------------------------


def test_replication_overlap_true_when_cis_overlap() -> None:
    part1 = _part1_with_d_ci("0.100000000000", "0.150000000000")  # overlaps NYC [0.0800, 0.1398]
    part2 = _part2_with_pm_ci("0.980000000000", "1.000000000000", state="pass")
    rep = h0013.compute_replication_assessment(part1, part2)
    assert rep["direction_replicated"] is True
    assert rep["magnitude_consistent"] is True
    assert rep["part2_consistent"] is True


def test_replication_overlap_false_when_cis_disjoint() -> None:
    part1 = _part1_with_d_ci("0.200000000000", "0.300000000000")  # entirely above NYC's upper
    part2 = _part2_with_pm_ci("0.100000000000", "0.400000000000", state="below")
    rep = h0013.compute_replication_assessment(part1, part2)
    assert rep["direction_replicated"] is True
    assert rep["magnitude_consistent"] is False
    assert rep["part2_consistent"] is False


def test_replication_overlap_true_when_cis_exactly_touch() -> None:
    """PREREG Sec 11 touching case: CHI's lower equals NYC's upper exactly
    -- closed intervals, so this counts as overlap."""
    part1 = _part1_with_d_ci("0.139841273431", "0.200000000000")
    part2 = _part2_with_pm_ci("0.980000000000", "1.000000000000", state="pass")
    rep = h0013.compute_replication_assessment(part1, part2)
    assert rep["magnitude_consistent"] is True


def test_replication_overlap_true_when_chi_below_but_touching_from_other_side() -> None:
    part1 = _part1_with_d_ci("0.010000000000", "0.079970196795")  # upper touches NYC's lower
    part2 = _part2_with_pm_ci("0.980000000000", "1.000000000000", state="pass")
    rep = h0013.compute_replication_assessment(part1, part2)
    assert rep["magnitude_consistent"] is True


def test_replication_d_gap_is_signed_difference() -> None:
    part1 = _part1_with_d_ci("0.100000000000", "0.150000000000")  # d_hat = 0.25
    part2 = _part2_with_pm_ci("0.980000000000", "1.000000000000", state="pass")
    rep = h0013.compute_replication_assessment(part1, part2)
    expected = Decimal("0.250000000000") - Decimal("0.109984399376")
    assert rep["d_gap_vs_nyc"] == h0013.serialize_decimal(expected)


def test_replication_direction_false_when_criterion_1_fails() -> None:
    part1 = _part1_with_d_ci("-0.010000000000", "0.050000000000")
    part2 = _part2_with_pm_ci("0.980000000000", "1.000000000000", state="pass")
    rep = h0013.compute_replication_assessment(part1, part2)
    assert rep["direction_replicated"] is False


def test_replication_nyc_reference_is_frozen_published_record() -> None:
    part1 = _part1_with_d_ci("0.100000000000", "0.150000000000")
    part2 = _part2_with_pm_ci("0.980000000000", "1.000000000000", state="pass")
    rep = h0013.compute_replication_assessment(part1, part2)
    assert rep["nyc_reference"]["d_nyc"] == "0.109984399376"
    assert rep["nyc_reference"]["ci95_newcombe"] == ["0.079970196795", "0.139841273431"]
    assert rep["nyc_reference"]["n_per_variable"] == 1282


# --- Descriptives new in H0013 (PREREG Sec 9) --------------------------------


def _small_variable_days() -> dict[tuple[str, date], h0013.VariableDay]:
    frame = _build_synthetic_frame(
        n_tmax_days=30, n_tmin_days=30, include_other_stations=False, start=date(2023, 12, 25)
    )
    return h0013.build_variable_days(frame)


def test_per_year_revision_rates_split_on_calendar_year() -> None:
    rates = h0013.build_per_year_revision_rates(_small_variable_days())
    assert set(rates["tmin_f"].keys()) == {"2023", "2024"}
    total_n = sum(cell["n"] for cell in rates["tmin_f"].values())
    total_r = sum(cell["r"] for cell in rates["tmin_f"].values())
    assert total_n == 30
    assert total_r == 10  # every 3rd day revised
    for cell in rates["tmin_f"].values():
        assert cell["rate"] is not None


def test_feature_summary_reports_cadence_and_coverage() -> None:
    summary = h0013.build_feature_summary(_small_variable_days())
    assert summary["tmin_f"]["n_days"] == 30
    assert summary["tmin_f"]["issuance_rows"] == 40  # 30 first + 10 revised finals
    assert summary["tmin_f"]["single_issuance_days"] == 20
    assert summary["tmin_f"]["first_date"] == "2023-12-25"
    assert summary["tmin_f"]["mean_issuances_per_day"] == "1.333333333333"


def test_nyc_consistency_check_uses_same_code_path_on_nyc_subset() -> None:
    frame = _build_synthetic_frame()
    check = h0013.build_nyc_consistency_check(frame)
    assert check["recomputed"]["n_tmax"] == 400
    assert check["recomputed"]["n_tmin"] == 400
    assert check["recomputed"]["r_tmin"] == 100  # every 4th of 400 NYC days
    assert check["recomputed"]["r_tmax"] == 20  # every 20th of 400 NYC days
    assert check["published_h0011"]["r_tmin"] == 311


def test_rate_or_null_zero_denominator_is_null() -> None:
    assert h0013._rate_or_null(0, 0) is None
    assert h0013._rate_or_null(1, 4) == "0.250000000000"


# --- Manifest validation -----------------------------------------------------


def test_validate_manifest_accepts_a_real_run_output(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    results = _run_patched(dataset_dir, frame)
    h0013.validate_manifest(results)  # must not raise


def test_validate_manifest_raises_on_missing_replication_key(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    results = _run_patched(dataset_dir, frame)
    del results["primary_results"]["replication_assessment"]["direction_replicated"]
    with pytest.raises(h0013.ManifestValidationError, match="replication_assessment"):
        h0013.validate_manifest(results)


def test_validate_manifest_raises_on_missing_descriptive_key(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    results = _run_patched(dataset_dir, frame)
    del results["descriptive_results"]["nyc_consistency_check"]
    with pytest.raises(h0013.ManifestValidationError, match="descriptive_results"):
        h0013.validate_manifest(results)


def test_manifest_has_inherited_amendment_metadata(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    results = _run_patched(dataset_dir, frame)
    inherited = results["preregistration"]["inherited_amendments"]
    assert inherited["document"].endswith("AMENDMENT-20260721-H0011-audit-resolution.md")
    assert "F1" in inherited["applies_as"]


# --- End-to-end run() and determinism ----------------------------------------


def test_run_end_to_end_confirmed_with_replication_fields(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    results = _run_patched(dataset_dir, frame)
    assert results["primary_results"]["decision"]["outcome"] == "confirmed"
    assert results["eligibility_gates"]["all_gates_passed"] is True
    rep = results["primary_results"]["replication_assessment"]
    assert rep["direction_replicated"] is True
    # synthetic D ~= 0.233, disjoint from NYC's [0.0800, 0.1398]:
    assert rep["magnitude_consistent"] is False
    assert results["descriptive_results"]["nyc_consistency_check"]["recomputed"]["n_tmin"] == 400
    # config_matches_prereg detects this test's own pinned (drifted) hash:
    assert results["reproducibility_verification"]["config_matches_prereg"] is False


def test_run_end_to_end_blocked_produces_schema_complete_manifest(tmp_path: Path) -> None:
    frame = _build_synthetic_frame(n_tmax_days=10, n_tmin_days=10)
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    results = _run_patched(dataset_dir, frame)
    assert results["primary_results"]["decision"]["outcome"] == "blocked"
    assert results["primary_results"]["part_1_rate_difference"]["p_tmax"] == h0013.NOT_EVALUATED
    assert (
        results["primary_results"]["replication_assessment"]["d_gap_vs_nyc"] == h0013.NOT_EVALUATED
    )
    h0013.validate_manifest(results)


def test_run_twice_produces_byte_identical_json(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    first = _run_patched(dataset_dir, frame)
    second = _run_patched(dataset_dir, frame)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first == second


def test_run_blocked_by_unpinned_tzdata(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    with (
        patch.object(
            h0013, "get_pinned_tzdata_version", side_effect=h0013.TzdataNotPinnedError("x")
        ),
        pytest.raises(h0013.TzdataNotPinnedError),
    ):
        h0013.run(dataset_dir)


def test_render_markdown_contains_decision_scope_and_replication(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    results = _run_patched(dataset_dir, frame)
    md = h0013.render_markdown(results)
    assert "CONFIRMED" in md
    assert "settlement-label formation timing only" in md  # F1 scope statement
    assert "direction_replicated: True" in md
    assert "tmax attribution rate" in md
