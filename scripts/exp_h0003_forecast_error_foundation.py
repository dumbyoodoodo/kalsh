"""EXP H0003 -- Forecast-error foundation: descriptive/inferential
characterization of NWS point-forecast error by station, variable,
horizon, and time (Track A), plus the frozen original NYC/tmax
PIT-vs-climatology claim (Track B).

Implements exactly what is frozen in:

  - docs/research/preregistrations/PREREG-20260722-H0003-forecast-error-foundation.md
  - docs/research/preregistrations/TEMPLATE-H0003-manifest.json

This is a descriptive and inferential study, not a trading study or a
predictive model. Fully deterministic; no randomness; no float reaches
a decision comparison.

Usage:
    python scripts/exp_h0003_forecast_error_foundation.py \
        --extract-dir "<KALSHI_DATA_DIR>/datasets/exp-20260722-h0003-forecast-extract" \
        --out docs/research/experiments/EXP-20260722-H0003-forecast-error-foundation-results.json \
        --out-md docs/research/experiments/EXP-20260722-H0003-forecast-error-foundation.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Any, Literal

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kalshi_weather.dataset.manifest import frame_content_hash

# --- Frozen protocol constants (PREREG Sec 2, 4, 5, 7, 8) --------------------

STATIONS_TIER12: list[str] = ["CHI", "DEN", "LAX", "NYC"]
STATION_TIER3 = "NYC"
VARIABLES: list[str] = ["tmax_f", "tmin_f"]
PROVIDER = "nws"  # not a column of the extract; documented scope, not filtered here

BUCKET_BOUNDS: list[tuple[str, float, float]] = [
    ("0-12h", 0.0, 12.0),
    ("12-24h", 12.0, 24.0),
    ("24-48h", 24.0, 48.0),
    ("48h+", 48.0, float("inf")),
]
BUCKET_ORDER = [b[0] for b in BUCKET_BOUNDS]

HORIZON_MIN = -1.0  # PREREG Sec 2/7: quality floor, exclusive
HORIZON_MAX = 240.0  # inclusive

Z_95 = Decimal("1.959963984540054")
DECIMAL_CONTEXT_PRECISION = 50
COMPARISON_QUANTIZATION = Decimal("1E-12")
SERIALIZATION_QUANTIZATION = Decimal("1E-12")
ZERO = Decimal(0)

TIER1_FLOOR = 20  # PREREG Sec 8
TIER2_FLOOR = 30
TIER3_FLOOR = 90
TIER3_MIN_WINDOWS = 3

_FROZEN_PREREG_REFERENCE: dict[str, Any] = {
    "stations_tier12": ["CHI", "DEN", "LAX", "NYC"],
    "station_tier3": "NYC",
    "variables": ["tmax_f", "tmin_f"],
    "bucket_order": ["0-12h", "12-24h", "24-48h", "48h+"],
    "horizon_min": -1.0,
    "horizon_max": 240.0,
    "z": "1.959963984540054",
    "tier1_floor": 20,
    "tier2_floor": 30,
    "tier3_floor": 90,
    "tier3_min_windows": 3,
}


def verify_config_matches_prereg() -> bool:
    ref = _FROZEN_PREREG_REFERENCE
    checks = [
        ref["stations_tier12"] == STATIONS_TIER12,
        ref["station_tier3"] == STATION_TIER3,
        ref["variables"] == VARIABLES,
        ref["bucket_order"] == BUCKET_ORDER,
        ref["horizon_min"] == HORIZON_MIN,
        ref["horizon_max"] == HORIZON_MAX,
        str(Z_95) == ref["z"],
        ref["tier1_floor"] == TIER1_FLOOR,
        ref["tier2_floor"] == TIER2_FLOOR,
        ref["tier3_floor"] == TIER3_FLOOR,
        ref["tier3_min_windows"] == TIER3_MIN_WINDOWS,
    ]
    return all(checks)


# --- Decimal policy (program-wide, inherited) --------------------------------


def quantize_for_comparison(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        return value.quantize(COMPARISON_QUANTIZATION, rounding=ROUND_HALF_EVEN)


def strictly_less(a: Decimal, b: Decimal) -> bool:
    return quantize_for_comparison(a) < quantize_for_comparison(b)


def at_least(a: Decimal, threshold: Decimal) -> bool:
    return not strictly_less(a, threshold)


def serialize_decimal(value: Decimal) -> str:
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        quantized = value.quantize(SERIALIZATION_QUANTIZATION, rounding=ROUND_HALF_EVEN)
    if quantized == 0:
        quantized = abs(quantized)
    return format(quantized, "f")


def serialize_ci(lower: Decimal, upper: Decimal) -> list[str]:
    return [serialize_decimal(lower), serialize_decimal(upper)]


def mean_sd_ci(values: list[Decimal]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """(mean, sd, lower, upper) -- deterministic normal-approximation 95% CI,
    the same construction used in H0017 Sec 5."""
    n = len(values)
    if n < 2:
        raise ValueError("mean_sd_ci requires n >= 2")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        n_dec = Decimal(n)
        mean = sum(values, ZERO) / n_dec
        var = sum(((v - mean) ** 2 for v in values), ZERO) / (n_dec - 1)
        sd = var.sqrt()
        half = Z_95 * sd / n_dec.sqrt()
        return (mean, sd, mean - half, mean + half)


def bucket_for(horizon_hours: float) -> str | None:
    """None means excluded (outside the quality floor). Values within the
    (-1, 0) collection-latency slack are classified as the nearest bucket
    (0-12h) -- the reported horizon_hours value itself is never clamped,
    only its bucket assignment (PREREG Sec 2/7)."""
    if not (HORIZON_MIN < horizon_hours <= HORIZON_MAX):
        return None
    effective = max(horizon_hours, 0.0)
    for name, lo, hi in BUCKET_BOUNDS:
        if lo <= effective < hi:
            return name
    return None


# --- Row model and selection (PREREG Sec 2) ----------------------------------


@dataclass(slots=True)
class ScoredRow:
    station: str
    variable: str
    target_date: date
    issue_time: datetime
    horizon_hours: float
    bucket: str
    residual: Decimal  # forecast - settled


def build_scored_rows(extract: pl.DataFrame) -> tuple[list[ScoredRow], dict[str, int]]:
    """Apply quality exclusions, join point-in-time integrity is assumed
    verified by G2c before this is called, and select the latest issuance
    per (station, variable, target_date, bucket) -- PREREG Sec 2."""
    exclusions = {
        "no_settled_truth": 0,
        "horizon_out_of_range": 0,
        "null_horizon": 0,
    }
    candidates: dict[tuple[str, str, date, str], list[ScoredRow]] = defaultdict(list)
    for r in extract.iter_rows(named=True):
        if r["station_id"] not in STATIONS_TIER12 or r["variable"] not in VARIABLES:
            continue
        if r["settled_value"] is None:
            exclusions["no_settled_truth"] += 1
            continue
        if r["horizon_hours"] is None:
            exclusions["null_horizon"] += 1
            continue
        bucket = bucket_for(r["horizon_hours"])
        if bucket is None:
            exclusions["horizon_out_of_range"] += 1
            continue
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            residual = Decimal(str(r["forecast_value"])) - Decimal(str(r["settled_value"]))
        row = ScoredRow(
            station=r["station_id"],
            variable=r["variable"],
            target_date=r["target_date"],
            issue_time=r["issue_time"],
            horizon_hours=r["horizon_hours"],
            bucket=bucket,
            residual=residual,
        )
        key = (row.station, row.variable, row.target_date, row.bucket)
        candidates[key].append(row)

    selected: list[ScoredRow] = []
    for rows in candidates.values():
        rows.sort(key=lambda r: r.issue_time)
        selected.append(rows[-1])  # latest issuance in this cell
    selected.sort(key=lambda r: (r.station, r.variable, r.target_date, r.issue_time))
    return selected, exclusions


# --- Gates (PREREG Sec 8) ----------------------------------------------------

NOT_EVALUATED: Literal["not_evaluated"] = "not_evaluated"

EXTRACT_HASH_DEFAULT: str | None = None  # set at runtime from the manifest actually built


@dataclass(slots=True)
class GateOutcome:
    gates: dict[str, Any]
    all_gates_passed: bool
    rows: list[ScoredRow] | None
    exclusions: dict[str, int] | None


def run_gates(extract: pl.DataFrame, expected_hash: str) -> GateOutcome:
    gates: dict[str, Any] = {
        "g1_hash_matches": NOT_EVALUATED,
        "g2a_key_unique_no_nulls": NOT_EVALUATED,
        "g2b_station_variable_sets_exact": NOT_EVALUATED,
        "g2c_point_in_time_integrity": NOT_EVALUATED,
        "all_integrity_gates_passed": False,
    }

    g1 = frame_content_hash(extract) == expected_hash
    gates["g1_hash_matches"] = g1
    if not g1:
        return GateOutcome(gates, False, None, None)

    g2a = {
        "key_unique": (
            extract.select(["station_id", "variable", "target_date", "issue_time"]).unique().height
            == extract.height
        ),
        "no_null_core_fields": bool(
            extract.select(
                (
                    pl.col("station_id").is_null()
                    | pl.col("variable").is_null()
                    | pl.col("target_date").is_null()
                    | pl.col("issue_time").is_null()
                    | pl.col("forecast_value").is_null()
                ).sum()
            ).item()
            == 0
        ),
    }
    gates["g2a_key_unique_no_nulls"] = g2a
    if not all(g2a.values()):
        return GateOutcome(gates, False, None, None)

    g2b = {
        "station_set_subset": set(extract["station_id"].unique().to_list()) <= set(STATIONS_TIER12),
        "variable_set_exact": set(extract["variable"].unique().to_list()) == set(VARIABLES),
    }
    gates["g2b_station_variable_sets_exact"] = g2b
    if not all(g2b.values()):
        return GateOutcome(gates, False, None, None)

    settled = extract.filter(pl.col("settled_value").is_not_null())
    g2c_ok = bool(
        settled.select((pl.col("settled_issuance_time") <= pl.col("issue_time")).sum()).item() == 0
    )
    gates["g2c_point_in_time_integrity"] = {
        "violations": 0 if g2c_ok else "present",
        "pass": g2c_ok,
    }
    if not g2c_ok:
        return GateOutcome(gates, False, None, None)

    gates["all_integrity_gates_passed"] = True
    rows, exclusions = build_scored_rows(extract)
    return GateOutcome(gates, True, rows, exclusions)


# --- Track A: Tier 1 (descriptive) -------------------------------------------


def cell_counts(rows: list[ScoredRow]) -> dict[str, dict[str, dict[str, int]]]:
    counts: dict[str, dict[str, dict[str, int]]] = {
        s: {v: dict.fromkeys(BUCKET_ORDER, 0) for v in VARIABLES} for s in STATIONS_TIER12
    }
    for r in rows:
        counts[r.station][r.variable][r.bucket] += 1
    return counts


def tier1_cell(residuals: list[Decimal]) -> dict[str, Any]:
    n = len(residuals)
    if n < 2:
        return {"n": n, "note": "insufficient for CI (n<2)"}
    mean, sd, lo, hi = mean_sd_ci(residuals)
    abs_res = [abs(r) for r in residuals]
    mae, _, mae_lo, mae_hi = mean_sd_ci(abs_res)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        rmse = (sum((r * r for r in residuals), ZERO) / Decimal(n)).sqrt()
    sorted_res = sorted(residuals)
    quantiles = {
        str(q): serialize_decimal(sorted_res[min(int(q * n), n - 1)])
        for q in (
            Decimal("0.10"),
            Decimal("0.25"),
            Decimal("0.50"),
            Decimal("0.75"),
            Decimal("0.90"),
        )
    }
    return {
        "n": n,
        "bias": serialize_decimal(mean),
        "bias_ci95": serialize_ci(lo, hi),
        "mae": serialize_decimal(mae),
        "mae_ci95": serialize_ci(mae_lo, mae_hi),
        "rmse": serialize_decimal(rmse),
        "sd": serialize_decimal(sd),
        "quantiles": quantiles,
    }


def build_tier1_tables(rows: list[ScoredRow]) -> dict[str, Any]:
    by_cell: dict[tuple[str, str, str], list[Decimal]] = defaultdict(list)
    by_month: dict[tuple[str, str, str], list[Decimal]] = defaultdict(list)
    for r in rows:
        by_cell[(r.station, r.variable, r.bucket)].append(r.residual)
        by_month[(r.station, r.variable, f"{r.target_date.year}-{r.target_date.month:02d}")].append(
            r.residual
        )
    cells = {f"{s}|{v}|{b}": tier1_cell(vals) for (s, v, b), vals in sorted(by_cell.items())}
    months = {f"{s}|{v}|{m}": tier1_cell(vals) for (s, v, m), vals in sorted(by_month.items())}
    return {"by_station_variable_bucket": cells, "by_station_variable_month": months}


# --- Track A: Tier 2 ----------------------------------------------------------


def tier2a_horizon_curve(rows: list[ScoredRow]) -> dict[str, Any]:
    by_bucket: dict[str, list[Decimal]] = defaultdict(list)
    for r in rows:
        by_bucket[r.bucket].append(abs(r.residual))
    curve = {}
    maes: list[Decimal] = []
    for bucket in BUCKET_ORDER:
        vals = by_bucket.get(bucket, [])
        if len(vals) < 2:
            curve[bucket] = {"n": len(vals), "note": "insufficient for CI"}
            continue
        mae, _, lo, hi = mean_sd_ci(vals)
        curve[bucket] = {
            "n": len(vals),
            "mae": serialize_decimal(mae),
            "ci95": serialize_ci(lo, hi),
        }
        maes.append(mae)
    monotonic = None
    if len(maes) == len(BUCKET_ORDER):
        monotonic = all(not strictly_less(maes[i + 1], maes[i]) for i in range(len(maes) - 1))
    return {"curve": curve, "monotonic_nondecreasing": monotonic}


def tier2b_stability(rows: list[ScoredRow]) -> dict[str, Any]:
    dates = sorted({r.target_date for r in rows})
    if len(dates) < 2:
        return {"note": "insufficient distinct dates for a split"}
    mid = dates[len(dates) // 2]
    first_half = [abs(r.residual) for r in rows if r.target_date < mid]
    second_half = [abs(r.residual) for r in rows if r.target_date >= mid]
    if len(first_half) < 2 or len(second_half) < 2:
        return {
            "note": "insufficient rows in one half",
            "first_half_n": len(first_half),
            "second_half_n": len(second_half),
        }
    m1, s1, _l1, _u1 = mean_sd_ci(first_half)
    m2, s2, _l2, _u2 = mean_sd_ci(second_half)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        diff = m2 - m1
        se = (s1 * s1 / Decimal(len(first_half)) + s2 * s2 / Decimal(len(second_half))).sqrt()
        half = Z_95 * se
    return {
        "split_date": mid.isoformat(),
        "first_half": {"n": len(first_half), "mae": serialize_decimal(m1)},
        "second_half": {"n": len(second_half), "mae": serialize_decimal(m2)},
        "diff_second_minus_first": serialize_decimal(diff),
        "ci95": serialize_ci(diff - half, diff + half),
    }


def tier2c_station_comparison(rows: list[ScoredRow]) -> dict[str, Any]:
    by_station: dict[str, list[Decimal]] = defaultdict(list)
    for r in rows:
        by_station[r.station].append(abs(r.residual))
    out = {}
    for station in STATIONS_TIER12:
        vals = by_station.get(station, [])
        if len(vals) < 2:
            out[station] = {"n": len(vals), "note": "insufficient for CI"}
            continue
        mae, _, lo, hi = mean_sd_ci(vals)
        out[station] = {"n": len(vals), "mae": serialize_decimal(mae), "ci95": serialize_ci(lo, hi)}
    return out


# --- Track A decision --------------------------------------------------------


@dataclass(slots=True)
class TrackAVerdict:
    outcome: Literal["BLOCKED", "INCONCLUSIVE-ACCRUAL", "PARTIAL-DESCRIPTIVE", "FULL-FOUNDATION"]
    evaluation_step: str
    reason: str
    shortfall: dict[str, Any]


def evaluate_track_a(
    gate_outcome: GateOutcome, counts: dict[str, dict[str, dict[str, int]]] | None
) -> TrackAVerdict:
    if not gate_outcome.all_gates_passed:
        return TrackAVerdict(
            "BLOCKED", "step_0", "an integrity gate failed; no interval computed", {}
        )
    assert counts is not None

    tier1_short = {
        f"{s}|{v}|{b}": counts[s][v][b]
        for s in STATIONS_TIER12
        for v in VARIABLES
        for b in BUCKET_ORDER
        if counts[s][v][b] < TIER1_FLOOR
    }
    if tier1_short:
        return TrackAVerdict(
            "INCONCLUSIVE-ACCRUAL",
            "step_1",
            f"{len(tier1_short)} of {len(STATIONS_TIER12) * len(VARIABLES) * len(BUCKET_ORDER)} "
            f"cells below TIER1_FLOOR={TIER1_FLOOR}",
            {"tier1_floor": TIER1_FLOOR, "short_cells": tier1_short},
        )

    pooled_bucket_n = {
        b: sum(counts[s][v][b] for s in STATIONS_TIER12 for v in VARIABLES) for b in BUCKET_ORDER
    }
    tier2_short = {b: n for b, n in pooled_bucket_n.items() if n < TIER2_FLOOR}
    if tier2_short:
        return TrackAVerdict(
            "PARTIAL-DESCRIPTIVE",
            "step_2",
            f"Tier 1 floor met everywhere; pooled bucket(s) below TIER2_FLOOR={TIER2_FLOOR}",
            {"tier2_floor": TIER2_FLOOR, "short_buckets": tier2_short},
        )

    return TrackAVerdict(
        "FULL-FOUNDATION", "step_3", "Tier 1 and Tier 2 floors met in every cell/bucket", {}
    )


# --- Track B (frozen original; NYC tmax PIT-vs-climatology) -----------------


@dataclass(slots=True)
class TrackBVerdict:
    outcome: Literal["BLOCKED", "INCONCLUSIVE-ACCRUAL", "CONFIRMED", "REJECTED"]
    evaluation_step: str
    reason: str


def evaluate_track_b(
    gate_outcome: GateOutcome, rows: list[ScoredRow] | None
) -> tuple[TrackBVerdict, dict[str, int]]:
    if not gate_outcome.all_gates_passed:
        return (
            TrackBVerdict("BLOCKED", "step_0", "an integrity gate failed; no interval computed"),
            {},
        )
    assert rows is not None
    nyc_tmax = [r for r in rows if r.station == STATION_TIER3 and r.variable == "tmax_f"]
    by_bucket: dict[str, set[date]] = defaultdict(set)
    for r in nyc_tmax:
        by_bucket[r.bucket].add(r.target_date)
    counts = {b: len(by_bucket.get(b, set())) for b in BUCKET_ORDER}
    short = {b: n for b, n in counts.items() if n < TIER3_FLOOR}
    distinct_dates = len({r.target_date for r in nyc_tmax})
    enough_windows = distinct_dates >= TIER3_MIN_WINDOWS  # a coarse, honest proxy; see PREREG
    if short or not enough_windows:
        return (
            TrackBVerdict(
                "INCONCLUSIVE-ACCRUAL",
                "step_1",
                f"NYC tmax settled days per bucket below TIER3_FLOOR={TIER3_FLOOR} "
                f"and/or fewer than {TIER3_MIN_WINDOWS} distinct dates for walk-forward windows",
            ),
            counts,
        )
    # PIT/coverage/log-score-vs-climatology machinery intentionally not
    # implemented until this branch is reachable (PREREG Sec 1.1/8): building
    # it against zero real data would be untested and premature per
    # CLAUDE.md's "do not build ahead of a demonstrated need."
    return (
        TrackBVerdict(
            "INCONCLUSIVE-ACCRUAL",
            "step_1",
            "gate floor met but PIT/coverage/log-score machinery is not yet implemented "
            "(deferred until this branch is first reachable)",
        ),
        counts,
    )


# --- Manifest validation -----------------------------------------------------


class ManifestValidationError(Exception):
    pass


_REQUIRED_MANIFEST_PATHS: dict[str, set[str]] = {
    "": {
        "experiment",
        "hypothesis",
        "preregistration",
        "dataset",
        "config",
        "eligibility_gates",
        "track_a_results",
        "track_b_results",
        "descriptive_results",
        "reproducibility_verification",
        "date_run",
    },
    "preregistration": {
        "document",
        "frozen_date",
        "provenance_note",
        "original_hypotheses_entry",
        "elaboration_flags_for_audit",
    },
    "dataset": {"version", "frame", "content_hash", "row_count_expected", "analysis_git_commit"},
    "eligibility_gates": {
        "g1_hash_matches",
        "g2a_key_unique_no_nulls",
        "g2b_station_variable_sets_exact",
        "g2c_point_in_time_integrity",
        "all_integrity_gates_passed",
    },
    "track_a_results": {
        "cell_counts",
        "decision",
        "tier1_tables",
        "tier2a_horizon_curve",
        "tier2b_stability",
        "tier2c_station_comparison",
    },
    "track_a_results.decision": {"outcome", "evaluation_step", "reason", "shortfall"},
    "track_b_results": {"settled_day_counts_by_bucket", "decision", "pit_and_coverage"},
    "track_b_results.decision": {"outcome", "evaluation_step", "reason"},
    "descriptive_results": {"exclusions", "sensitivity"},
    "reproducibility_verification": {
        "rerun_byte_identical",
        "config_matches_prereg",
        "serialization_policy",
    },
}


def _get_path(results: dict[str, Any], path: str) -> Any:
    node: Any = results
    if path == "":
        return node
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ManifestValidationError(f"missing manifest key path: {path!r}")
        node = node[part]
    return node


def validate_manifest(results: dict[str, Any]) -> None:
    for path, required_keys in _REQUIRED_MANIFEST_PATHS.items():
        node = _get_path(results, path)
        if not isinstance(node, dict):
            raise ManifestValidationError(f"expected an object at {path!r}, got {type(node)}")
        missing = required_keys - node.keys()
        if missing:
            raise ManifestValidationError(f"manifest missing key(s) {sorted(missing)} at {path!r}")
    a_outcome = results["track_a_results"]["decision"]["outcome"]
    if a_outcome not in (
        "BLOCKED",
        "INCONCLUSIVE-ACCRUAL",
        "PARTIAL-DESCRIPTIVE",
        "FULL-FOUNDATION",
    ):
        raise ManifestValidationError(f"unrecognized Track A outcome: {a_outcome!r}")
    b_outcome = results["track_b_results"]["decision"]["outcome"]
    if b_outcome not in ("BLOCKED", "INCONCLUSIVE-ACCRUAL", "CONFIRMED", "REJECTED"):
        raise ManifestValidationError(f"unrecognized Track B outcome: {b_outcome!r}")


# --- Orchestration -----------------------------------------------------------

PREREGISTRATION_METADATA: dict[str, Any] = {
    "document": "docs/research/preregistrations/PREREG-20260722-H0003-forecast-error-foundation.md",
    "frozen_date": "2026-07-22",
    "provenance_note": (
        "PREREG Sec 0: structural facts only (row counts, date ranges, precision) seen "
        "pre-freeze; no forecast-error statistic computed for any cell"
    ),
    "original_hypotheses_entry": (
        "HYPOTHESES.md H0003, opened 2026-07-21; frozen hypothesis text unaltered "
        "(Sec 1.1); this document adds an additive descriptive/inferential layer (Sec 1.2)"
    ),
    "elaboration_flags_for_audit": [
        "two_independent_decision_tracks_A_and_B (Sec 6)",
        "horizon_definition_uses_valid_start_not_calendar_midnight (Sec 2/12 R2)",
        "tier2c_station_comparison_non_decisional (Sec 4/5)",
        "no_multiplicity_correction_two_declared_tests (Sec 5)",
        "settled_truth_not_value_at_settlement (Sec 2)",
    ],
}


def run(extract_dir: Path, extract_hash: str, row_count_expected: int) -> dict[str, Any]:
    extract = pl.read_parquet(extract_dir / "forecast_horizon_extract.parquet")
    gate_outcome = run_gates(extract, extract_hash)

    counts: dict[str, dict[str, dict[str, int]]] | None = None
    tier1: dict[str, Any] | None = None
    tier2a: dict[str, Any] | None = None
    tier2b: dict[str, Any] | None = None
    tier2c: dict[str, Any] | None = None
    if gate_outcome.all_gates_passed:
        assert gate_outcome.rows is not None
        counts = cell_counts(gate_outcome.rows)

    verdict_a = evaluate_track_a(gate_outcome, counts)
    verdict_b, track_b_counts = evaluate_track_b(gate_outcome, gate_outcome.rows)

    if gate_outcome.all_gates_passed and verdict_a.outcome in (
        "PARTIAL-DESCRIPTIVE",
        "FULL-FOUNDATION",
    ):
        assert gate_outcome.rows is not None
        tier1 = build_tier1_tables(gate_outcome.rows)
        tier2c = tier2c_station_comparison(gate_outcome.rows)
    if gate_outcome.all_gates_passed and verdict_a.outcome == "FULL-FOUNDATION":
        assert gate_outcome.rows is not None
        tier2a = tier2a_horizon_curve(gate_outcome.rows)
        tier2b = tier2b_stability(gate_outcome.rows)

    results: dict[str, Any] = {
        "experiment": "EXP-20260722-H0003-forecast-error-foundation",
        "hypothesis": "H0003",
        "preregistration": PREREGISTRATION_METADATA,
        "dataset": {
            "version": "exp-20260722-h0003-forecast-extract",
            "frame": "forecast_horizon_extract.parquet",
            "content_hash": extract_hash,
            "row_count_expected": row_count_expected,
            "analysis_git_commit": None,
        },
        "config": {
            "stations_tier1_2": list(STATIONS_TIER12),
            "station_tier3": STATION_TIER3,
            "variables": list(VARIABLES),
            "provider": PROVIDER,
            "truth_convention": (
                "settled = latest-issuance CLI value per (station,variable,"
                "observation_date) -- _settled_observations convention"
            ),
            "horizon_definition": (
                "min(valid_start) over the (station,target_date,issue_time) period "
                "group used for the high/low aggregation, minus issue_time, hours"
            ),
            "horizon_buckets": ["[0,12)", "[12,24)", "[24,48)", "[48,inf)"],
            "quality_floor": "horizon_hours in (-1, 240]",
            "selection_within_cell": "latest issuance per (station,variable,target_date,bucket)",
            "tier1_floor": TIER1_FLOOR,
            "tier2_floor": TIER2_FLOOR,
            "tier3_floor": TIER3_FLOOR,
            "ci_method": (
                "z = 1.959963984540054, deterministic normal-approximation CI; Decimal "
                "precision-50; 1e-12 quantized decisions"
            ),
            "multiplicity": (
                "two declared Tier-2 tests (2a, 2b); Tier 2c non-decisional; no correction applied"
            ),
            "random_seed": "not applicable -- fully deterministic",
            "software_versions": None,
        },
        "eligibility_gates": gate_outcome.gates,
        "track_a_results": {
            "cell_counts": counts or {},
            "decision": {
                "outcome": verdict_a.outcome,
                "evaluation_step": verdict_a.evaluation_step,
                "reason": verdict_a.reason,
                "shortfall": verdict_a.shortfall,
            },
            "tier1_tables": tier1 or {},
            "tier2a_horizon_curve": tier2a or {},
            "tier2b_stability": tier2b or {},
            "tier2c_station_comparison": tier2c or {},
        },
        "track_b_results": {
            "settled_day_counts_by_bucket": track_b_counts,
            "decision": {
                "outcome": verdict_b.outcome,
                "evaluation_step": verdict_b.evaluation_step,
                "reason": verdict_b.reason,
            },
            "pit_and_coverage": {},
        },
        "descriptive_results": {
            "exclusions": gate_outcome.exclusions or {},
            "sensitivity": {},
        },
        "reproducibility_verification": {
            "rerun_byte_identical": None,
            "config_matches_prereg": verify_config_matches_prereg(),
            "serialization_policy": (
                "Every non-integer decision-relevant quantity is a JSON string: Decimal "
                "quantized to 1e-12 via ROUND_HALF_EVEN, fixed-point, 12 digits. Integer "
                "counts remain plain JSON integers."
            ),
        },
        "date_run": None,
    }
    validate_manifest(results)
    return results


def render_markdown(results: dict[str, Any]) -> str:
    a = results["track_a_results"]["decision"]
    b = results["track_b_results"]["decision"]
    excl = results["descriptive_results"]["exclusions"]
    return f"""# EXP-20260722-H0003-forecast-error-foundation

Executed exactly as pre-registered in
`PREREG-20260722-H0003-forecast-error-foundation.md`.

## Track A (descriptive/inferential foundation, Tiers 1-2)

**{a["outcome"]}** ({a["evaluation_step"]}: {a["reason"]})

Shortfall detail: {json.dumps(a["shortfall"], default=str)}

## Track B (frozen original: NYC tmax PIT-vs-climatology, Tier 3)

**{b["outcome"]}** ({b["evaluation_step"]}: {b["reason"]})

Settled-day counts by bucket (NYC tmax): \
{results["track_b_results"]["settled_day_counts_by_bucket"]}

## Exclusions

{json.dumps(excl, default=str)}

Full cell counts, tables (where reached), and gate detail: see the
accompanying `-results.json`. Narrative, interpretation, limitations,
and recommendations for M-01:
`docs/research/postmortems/2026-07-22-h0003-closeout.md`.
"""


if __name__ == "__main__":
    import contextlib
    import subprocess

    parser = argparse.ArgumentParser()
    parser.add_argument("--extract-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()

    manifest = json.loads((args.extract_dir / "manifest.json").read_text())
    extract_hash = manifest["content_hashes"]["forecast_horizon_extract"]
    row_count = manifest["row_counts"]["forecast_horizon_extract"]

    output = run(args.extract_dir, extract_hash, row_count)
    if not args.raw:
        output["date_run"] = datetime.now(UTC).date().isoformat()
        with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
            output["dataset"]["analysis_git_commit"] = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip()

    args.out.write_text(json.dumps(output, indent=2, default=str))
    args.out_md.write_text(render_markdown(output))
    print(f"wrote {args.out}")
    print(f"wrote {args.out_md}")
