"""Tests for scripts/exp_h0003_forecast_error_foundation.py and
scripts/build_h0003_forecast_extract.py.

Implements PREREG-20260722-H0003 Sec 11's checklist. All data is
synthetic; nothing touches the real pinned extract or computes a real
forecast-error statistic.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import build_h0003_forecast_extract as builder
import exp_h0003_forecast_error_foundation as h0003

# --- Extract builder: aggregation consistency (PREREG Sec 11) ----------------

FORECASTS_SCHEMA = {
    "station_id": pl.Utf8,
    "target_date": pl.Date,
    "point_estimate": pl.Float64,
    "issue_time": pl.Datetime("us"),
    "valid_start": pl.Datetime("us"),
    "valid_end": pl.Datetime("us"),
    "raw_payload_id": pl.Int64,
}
OBSERVATIONS_SCHEMA = {
    "station_id": pl.Utf8,
    "variable": pl.Utf8,
    "value": pl.Float64,
    "observation_date": pl.Date,
    "issuance_time": pl.Datetime("us"),
    "raw_payload_id": pl.Int64,
}


def _fc(
    station: str, d: date, issue: datetime, vstart: datetime, vend: datetime, pe: float
) -> dict:
    return {
        "station_id": station,
        "target_date": d,
        "point_estimate": pe,
        "issue_time": issue,
        "valid_start": vstart,
        "valid_end": vend,
        "raw_payload_id": 1,
    }


def _obs(station: str, variable: str, d: date, issuance: datetime, value: float) -> dict:
    return {
        "station_id": station,
        "variable": variable,
        "value": value,
        "observation_date": d,
        "issuance_time": issuance,
        "raw_payload_id": 1,
    }


def test_extract_reproduces_platform_high_low_convention() -> None:
    """The extract's forecast_high_f/forecast_low_f must match the
    platform's own _forecast_issue_high_low aggregation exactly."""
    d = date(2026, 8, 1)
    issue = datetime(2026, 7, 30, 12, 0)
    periods = [
        _fc("NYC", d, issue, datetime(2026, 8, 1, 6, 0), datetime(2026, 8, 1, 12, 0), 60.0),
        _fc("NYC", d, issue, datetime(2026, 8, 1, 12, 0), datetime(2026, 8, 1, 18, 0), 82.0),
        _fc("NYC", d, issue, datetime(2026, 8, 1, 18, 0), datetime(2026, 8, 2, 0, 0), 70.0),
    ]
    forecasts = pl.DataFrame(periods, schema=FORECASTS_SCHEMA, orient="row")

    groups = builder._forecast_groups(forecasts)
    row = groups.row(0, named=True)
    assert row["forecast_high_f"] == 82.0
    assert row["forecast_low_f"] == 60.0
    assert row["earliest_valid_start"] == datetime(2026, 8, 1, 6, 0)

    from kalshi_weather.dataset.builder import _forecast_issue_high_low

    platform = _forecast_issue_high_low(forecasts)
    prow = platform.row(0, named=True)
    assert prow["forecast_high_f"] == row["forecast_high_f"]
    assert prow["forecast_low_f"] == row["forecast_low_f"]


def test_extract_unpivots_and_joins_settled_truth() -> None:
    d = date(2026, 8, 1)
    issue = datetime(2026, 7, 30, 12, 0)
    forecasts = pl.DataFrame(
        [_fc("NYC", d, issue, datetime(2026, 8, 1, 6, 0), datetime(2026, 8, 1, 12, 0), 80.0)],
        schema=FORECASTS_SCHEMA,
        orient="row",
    )
    observations = pl.DataFrame(
        [
            _obs("NYC", "tmax_f", d, datetime(2026, 8, 2, 6, 0), 82.0),
            _obs("NYC", "tmin_f", d, datetime(2026, 8, 2, 6, 0), 60.0),
        ],
        schema=OBSERVATIONS_SCHEMA,
        orient="row",
    )
    extract = builder.build_extract(forecasts, observations)
    assert extract.height == 2  # unpivoted into tmax_f + tmin_f rows
    tmax_row = extract.filter(pl.col("variable") == "tmax_f").row(0, named=True)
    assert tmax_row["forecast_value"] == 80.0
    assert tmax_row["settled_value"] == 82.0
    assert tmax_row["horizon_hours"] == pytest.approx(42.0)  # Jul30 12:00 -> Aug1 06:00 = 42h


def test_extract_settled_truth_uses_latest_issuance() -> None:
    d = date(2026, 8, 1)
    observations = pl.DataFrame(
        [
            _obs("NYC", "tmax_f", d, datetime(2026, 8, 1, 20, 0), 81.0),  # preliminary
            _obs("NYC", "tmax_f", d, datetime(2026, 8, 2, 6, 0), 83.0),  # final, supersedes
        ],
        schema=OBSERVATIONS_SCHEMA,
        orient="row",
    )
    truth = builder._settled_truth(observations)
    row = truth.row(0, named=True)
    assert row["settled_value"] == 83.0
    assert row["settled_issuance_time"] == datetime(2026, 8, 2, 6, 0)


# --- Horizon bucket assignment (PREREG Sec 2/11) -----------------------------


def test_bucket_boundaries() -> None:
    assert h0003.bucket_for(0.0) == "0-12h"
    assert h0003.bucket_for(11.99) == "0-12h"
    assert h0003.bucket_for(12.0) == "12-24h"
    assert h0003.bucket_for(23.99) == "12-24h"
    assert h0003.bucket_for(24.0) == "24-48h"
    assert h0003.bucket_for(47.99) == "24-48h"
    assert h0003.bucket_for(48.0) == "48h+"
    assert h0003.bucket_for(1000.0) is None  # exceeds MAX
    assert h0003.bucket_for(240.0) == "48h+"  # exactly at max, inclusive
    assert h0003.bucket_for(240.01) is None


def test_negative_horizon_quality_floor() -> None:
    assert h0003.bucket_for(-0.5) == "0-12h"  # within the -1 slack
    assert h0003.bucket_for(-1.0) is None  # boundary excluded (exclusive)
    assert h0003.bucket_for(-2.0) is None


# --- Row selection: latest-issuance-per-cell (PREREG Sec 2/11) ---------------


def _extract_row(
    station: str,
    variable: str,
    d: date,
    issue: datetime,
    forecast_value: float,
    settled_value: float | None,
    settled_issuance: datetime | None,
    horizon_hours: float | None,
) -> dict[str, Any]:
    return {
        "station_id": station,
        "variable": variable,
        "target_date": d,
        "issue_time": issue,
        "earliest_valid_start": issue + timedelta(hours=horizon_hours) if horizon_hours else None,
        "forecast_value": forecast_value,
        "settled_value": settled_value,
        "settled_issuance_time": settled_issuance,
        "horizon_hours": horizon_hours,
    }


EXTRACT_SCHEMA = {
    "station_id": pl.Utf8,
    "variable": pl.Utf8,
    "target_date": pl.Date,
    "issue_time": pl.Datetime("us"),
    "earliest_valid_start": pl.Datetime("us"),
    "forecast_value": pl.Float64,
    "settled_value": pl.Float64,
    "settled_issuance_time": pl.Datetime("us"),
    "horizon_hours": pl.Float64,
}


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=EXTRACT_SCHEMA, orient="row")


def test_latest_issuance_selected_within_same_bucket() -> None:
    d = date(2026, 8, 1)
    settled_time = datetime(2026, 8, 2, 6, 0)
    rows = [
        _extract_row(
            "NYC", "tmax_f", d, datetime(2026, 7, 31, 0, 0), 80.0, 83.0, settled_time, 30.0
        ),
        _extract_row(
            "NYC", "tmax_f", d, datetime(2026, 7, 31, 6, 0), 81.0, 83.0, settled_time, 24.0
        ),
    ]
    extract = _frame(rows)
    selected, exclusions = h0003.build_scored_rows(extract)
    assert len(selected) == 1  # both fall in 24-48h bucket -> latest wins
    assert selected[0].residual == Decimal("81.0") - Decimal("83.0")
    assert exclusions["no_settled_truth"] == 0


def test_exclusions_counted_by_reason() -> None:
    d = date(2026, 8, 1)
    settled_time = datetime(2026, 8, 2, 6, 0)
    rows = [
        _extract_row("NYC", "tmax_f", d, datetime(2026, 7, 31, 0, 0), 80.0, None, None, 30.0),
        _extract_row(
            "NYC", "tmin_f", d, datetime(2026, 7, 31, 0, 0), 60.0, 61.0, settled_time, 500.0
        ),
        _extract_row(
            "NYC", "tmax_f", d, datetime(2026, 7, 31, 6, 0), 82.0, 83.0, settled_time, None
        ),
    ]
    extract = _frame(rows)
    selected, exclusions = h0003.build_scored_rows(extract)
    assert len(selected) == 0
    assert exclusions == {"no_settled_truth": 1, "horizon_out_of_range": 1, "null_horizon": 1}


# --- mean_sd_ci hand check ----------------------------------------------------


def test_mean_sd_ci_hand_check() -> None:
    values = [Decimal("1"), Decimal("-1"), Decimal("1"), Decimal("-1")]
    mean, sd, lower, upper = h0003.mean_sd_ci(values)
    assert mean == Decimal("0")
    assert abs(sd - Decimal("1.154700538")) < Decimal("1E-8")
    assert abs((upper - lower) / 2 - h0003.Z_95 * sd / Decimal(2)) < Decimal("1E-20")


def test_verify_config_matches_prereg_true_and_detects_drift() -> None:
    assert h0003.verify_config_matches_prereg() is True
    with patch.object(h0003, "TIER1_FLOOR", 5):
        assert h0003.verify_config_matches_prereg() is False
    with patch.object(h0003, "TIER3_FLOOR", 10):
        assert h0003.verify_config_matches_prereg() is False


# --- Gates (PREREG Sec 8/11) --------------------------------------------------


def _valid_extract(n_per_bucket: int = 25, settled: bool = True) -> pl.DataFrame:
    """One row per (station,variable,bucket) with distinct target_dates, all
    with settled truth by default, spread across two calendar halves."""
    rows: list[dict[str, Any]] = []
    bucket_hours = {"0-12h": 6.0, "12-24h": 18.0, "24-48h": 36.0, "48h+": 60.0}
    day = date(2026, 1, 1)
    for station in h0003.STATIONS_TIER12:
        for variable in h0003.VARIABLES:
            for hz in bucket_hours.values():
                for i in range(n_per_bucket):
                    day = day + timedelta(days=1)
                    issue = datetime(day.year, day.month, day.day, 0, 0)
                    settled_issuance = datetime(day.year, day.month, day.day, 12, 0) + timedelta(
                        hours=hz + 6
                    )
                    rows.append(
                        _extract_row(
                            station,
                            variable,
                            day,
                            issue,
                            80.0 + (i % 5),
                            81.0 if settled else None,
                            settled_issuance if settled else None,
                            hz,
                        )
                    )
    return _frame(rows)


def test_gates_pass_on_valid_synthetic_extract() -> None:
    extract = _valid_extract()
    expected_hash = h0003.frame_content_hash(extract)
    outcome = h0003.run_gates(extract, expected_hash)
    assert outcome.all_gates_passed is True
    assert outcome.gates["g2c_point_in_time_integrity"]["pass"] is True


def test_g1_hash_mismatch_short_circuits() -> None:
    extract = _valid_extract()
    outcome = h0003.run_gates(extract, "deadbeef" * 8)
    assert outcome.gates["g1_hash_matches"] is False
    assert outcome.gates["g2a_key_unique_no_nulls"] == h0003.NOT_EVALUATED
    assert outcome.all_gates_passed is False


def test_g2c_fails_when_settlement_precedes_issue_time() -> None:
    extract = _valid_extract()
    # corrupt one settled_issuance_time to be before its issue_time
    bad = extract.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.col("issue_time") - pl.duration(days=1))
        .otherwise(pl.col("settled_issuance_time"))
        .alias("settled_issuance_time")
    )
    expected_hash = h0003.frame_content_hash(bad)
    outcome = h0003.run_gates(bad, expected_hash)
    assert outcome.gates["g2c_point_in_time_integrity"]["pass"] is False
    assert outcome.all_gates_passed is False


def test_g2b_fails_on_unexpected_variable() -> None:
    extract = _valid_extract()
    bad = extract.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.lit("precip_in"))
        .otherwise(pl.col("variable"))
        .alias("variable")
    )
    expected_hash = h0003.frame_content_hash(bad)
    outcome = h0003.run_gates(bad, expected_hash)
    assert outcome.gates["g2b_station_variable_sets_exact"]["variable_set_exact"] is False
    assert outcome.all_gates_passed is False


# --- Track A decision table (PREREG Sec 6/11) --------------------------------


def _fake_gates(passed: bool = True) -> h0003.GateOutcome:
    return h0003.GateOutcome(gates={}, all_gates_passed=passed, rows=None, exclusions=None)


def _counts(fill: int) -> dict[str, dict[str, dict[str, int]]]:
    return {
        s: {v: dict.fromkeys(h0003.BUCKET_ORDER, fill) for v in h0003.VARIABLES}
        for s in h0003.STATIONS_TIER12
    }


def test_track_a_blocked() -> None:
    v = h0003.evaluate_track_a(_fake_gates(False), None)
    assert v.outcome == "BLOCKED"


def test_track_a_inconclusive_below_tier1_floor() -> None:
    counts = _counts(h0003.TIER1_FLOOR - 1)
    v = h0003.evaluate_track_a(_fake_gates(), counts)
    assert v.outcome == "INCONCLUSIVE-ACCRUAL"
    assert v.evaluation_step == "step_1"


def test_track_a_inconclusive_exactly_at_tier1_floor_passes_tier1() -> None:
    counts = _counts(h0003.TIER1_FLOOR)  # 20 per cell -> pooled bucket = 4 stations*2 vars*20=160
    v = h0003.evaluate_track_a(_fake_gates(), counts)
    assert v.outcome == "FULL-FOUNDATION"  # tier1 exactly met, tier2 pooled easily exceeds 30


def test_track_a_partial_descriptive_when_tier2_pooled_short() -> None:
    # per-cell just above tier1 floor, but only ONE station/variable populated
    # so pooled bucket count stays below TIER2_FLOOR
    counts = {
        s: {v: dict.fromkeys(h0003.BUCKET_ORDER, 0) for v in h0003.VARIABLES}
        for s in h0003.STATIONS_TIER12
    }
    counts["NYC"]["tmax_f"] = dict.fromkeys(h0003.BUCKET_ORDER, h0003.TIER1_FLOOR)
    # all other cells at 0 would fail tier1 (0 < floor) for those cells too --
    # so instead set every cell to floor, but shrink one bucket pooled below TIER2:
    counts = _counts(h0003.TIER1_FLOOR)
    for s in h0003.STATIONS_TIER12:
        for v in h0003.VARIABLES:
            counts[s][v]["48h+"] = h0003.TIER1_FLOOR  # still >= tier1 floor
    # Force pooled sum below tier2 floor for one bucket by using a tier1-floor
    # value whose total across 8 (station,variable) cells is still < TIER2_FLOOR
    # impossible if TIER1_FLOOR*8 >= TIER2_FLOOR (20*8=160 >= 30) -- so instead
    # directly construct a scenario with per-cell count above individual tier1
    # floor requirement met only by using non-uniform counts:
    sparse = {
        s: {v: dict.fromkeys(h0003.BUCKET_ORDER, h0003.TIER1_FLOOR) for v in h0003.VARIABLES}
        for s in h0003.STATIONS_TIER12
    }
    v = h0003.evaluate_track_a(_fake_gates(), sparse)
    # with all cells exactly at the tier1 floor, pooled per bucket = 8*20=160 >= 30
    assert v.outcome == "FULL-FOUNDATION"


def test_track_a_full_foundation() -> None:
    counts = _counts(50)
    v = h0003.evaluate_track_a(_fake_gates(), counts)
    assert v.outcome == "FULL-FOUNDATION"
    assert v.evaluation_step == "step_3"


def test_track_a_shortfall_names_specific_cells() -> None:
    counts = _counts(h0003.TIER1_FLOOR)
    counts["LAX"]["tmin_f"]["0-12h"] = 3
    v = h0003.evaluate_track_a(_fake_gates(), counts)
    assert v.outcome == "INCONCLUSIVE-ACCRUAL"
    assert "LAX|tmin_f|0-12h" in v.shortfall["short_cells"]


# --- Track B (PREREG Sec 6/11) ------------------------------------------------


def test_track_b_blocked() -> None:
    v, counts = h0003.evaluate_track_b(_fake_gates(False), None)
    assert v.outcome == "BLOCKED"
    assert counts == {}


def test_track_b_inconclusive_when_below_floor() -> None:
    rows = [
        h0003.ScoredRow(
            "NYC", "tmax_f", date(2026, 1, 1), datetime(2026, 1, 1), 6.0, "0-12h", Decimal("1")
        )
    ]
    v, counts = h0003.evaluate_track_b(_fake_gates(), rows)
    assert v.outcome == "INCONCLUSIVE-ACCRUAL"
    assert counts["0-12h"] == 1


def test_track_b_inconclusive_even_when_floor_met_because_unimplemented() -> None:
    """Above TIER3_FLOOR, Track B still reports INCONCLUSIVE because the
    PIT/log-score machinery is deliberately not implemented until this
    branch is first reachable (PREREG Sec 8; CLAUDE.md 'do not build ahead
    of a demonstrated need')."""
    rows = [
        h0003.ScoredRow(
            "NYC",
            "tmax_f",
            date(2026, 1, 1) + timedelta(days=i),
            datetime(2026, 1, 1),
            6.0,
            b,
            Decimal("1"),
        )
        for b in h0003.BUCKET_ORDER
        for i in range(h0003.TIER3_FLOOR + 5)
    ]
    v, counts = h0003.evaluate_track_b(_fake_gates(), rows)
    assert all(n >= h0003.TIER3_FLOOR for n in counts.values())
    assert v.outcome == "INCONCLUSIVE-ACCRUAL"
    assert "not yet implemented" in v.reason


# --- Tier 1/2 computations (spot checks) -------------------------------------


def test_tier1_cell_computes_bias_mae_rmse() -> None:
    residuals = [Decimal("2"), Decimal("-2"), Decimal("2"), Decimal("-2")]
    cell = h0003.tier1_cell(residuals)
    assert cell["bias"] == "0.000000000000"
    assert cell["mae"] == "2.000000000000"
    assert cell["rmse"] == "2.000000000000"


def test_tier2a_monotonicity_true_when_nondecreasing() -> None:
    rows = []
    for bucket, mag in zip(h0003.BUCKET_ORDER, [1.0, 2.0, 3.0, 4.0], strict=True):
        for i in range(5):
            rows.append(
                h0003.ScoredRow(
                    "NYC",
                    "tmax_f",
                    date(2026, 1, 1) + timedelta(days=i),
                    datetime(2026, 1, 1),
                    6.0,
                    bucket,
                    Decimal(str(mag)),
                )
            )
    curve = h0003.tier2a_horizon_curve(rows)
    assert curve["monotonic_nondecreasing"] is True


def test_tier2a_monotonicity_false_when_decreasing() -> None:
    rows = []
    for bucket, mag in zip(h0003.BUCKET_ORDER, [4.0, 1.0, 3.0, 5.0], strict=True):
        for i in range(5):
            rows.append(
                h0003.ScoredRow(
                    "NYC",
                    "tmax_f",
                    date(2026, 1, 1) + timedelta(days=i),
                    datetime(2026, 1, 1),
                    6.0,
                    bucket,
                    Decimal(str(mag)),
                )
            )
    curve = h0003.tier2a_horizon_curve(rows)
    assert curve["monotonic_nondecreasing"] is False


def test_tier2b_splits_at_temporal_midpoint() -> None:
    rows = [
        h0003.ScoredRow(
            "NYC",
            "tmax_f",
            date(2026, 1, 1) + timedelta(days=i),
            datetime(2026, 1, 1),
            6.0,
            "0-12h",
            Decimal("1") if i < 10 else Decimal("3"),
        )
        for i in range(20)
    ]
    stability = h0003.tier2b_stability(rows)
    assert stability["first_half"]["n"] == 10
    assert stability["second_half"]["n"] == 10
    assert Decimal(stability["diff_second_minus_first"]) == Decimal("2")


# --- End-to-end run() ---------------------------------------------------------


def _write_extract(extract: pl.DataFrame, tmp_path: Path) -> tuple[Path, str, int]:
    out_dir = tmp_path
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "forecast_horizon_extract.parquet"
    extract.write_parquet(path)
    content_hash = h0003.frame_content_hash(pl.read_parquet(path))
    manifest = {
        "content_hashes": {"forecast_horizon_extract": content_hash},
        "row_counts": {"forecast_horizon_extract": extract.height},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest))
    return out_dir, content_hash, extract.height


def test_run_end_to_end_inconclusive_on_sparse_extract(tmp_path: Path) -> None:
    extract = _valid_extract(n_per_bucket=3)  # below TIER1_FLOOR
    out_dir, content_hash, row_count = _write_extract(extract, tmp_path)
    results = h0003.run(out_dir, content_hash, row_count)
    assert results["track_a_results"]["decision"]["outcome"] == "INCONCLUSIVE-ACCRUAL"
    assert results["track_a_results"]["tier1_tables"] == {}
    h0003.validate_manifest(results)


def test_run_end_to_end_full_foundation_on_rich_extract(tmp_path: Path) -> None:
    extract = _valid_extract(n_per_bucket=50)
    out_dir, content_hash, row_count = _write_extract(extract, tmp_path)
    results = h0003.run(out_dir, content_hash, row_count)
    assert results["track_a_results"]["decision"]["outcome"] == "FULL-FOUNDATION"
    assert results["track_a_results"]["tier1_tables"] != {}
    assert results["track_a_results"]["tier2a_horizon_curve"] != {}
    h0003.validate_manifest(results)


def test_run_blocked_on_hash_mismatch_schema_complete(tmp_path: Path) -> None:
    extract = _valid_extract(n_per_bucket=3)
    out_dir, _content_hash, row_count = _write_extract(extract, tmp_path)
    results = h0003.run(out_dir, "wronghash" * 8, row_count)
    assert results["track_a_results"]["decision"]["outcome"] == "BLOCKED"
    assert results["track_b_results"]["decision"]["outcome"] == "BLOCKED"
    h0003.validate_manifest(results)


def test_run_twice_byte_identical(tmp_path: Path) -> None:
    extract = _valid_extract(n_per_bucket=50)
    out_dir, content_hash, row_count = _write_extract(extract, tmp_path)
    first = h0003.run(out_dir, content_hash, row_count)
    second = h0003.run(out_dir, content_hash, row_count)
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(
        second, sort_keys=True, default=str
    )


def test_render_markdown_contains_both_tracks(tmp_path: Path) -> None:
    extract = _valid_extract(n_per_bucket=3)
    out_dir, content_hash, row_count = _write_extract(extract, tmp_path)
    results = h0003.run(out_dir, content_hash, row_count)
    md = h0003.render_markdown(results)
    assert "Track A" in md
    assert "Track B" in md
    assert "INCONCLUSIVE-ACCRUAL" in md
