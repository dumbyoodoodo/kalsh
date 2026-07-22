"""EXP H0013 -- Independent-station replication of H0011 at Chicago
Midway (CHI): (1) CHI tmin_f settlement labels revise more often than
tmax_f labels; (2) a confident majority of revised CHI tmin variable-days
reach their final value only in a post-(America/Chicago)-midnight
issuance -- with H0011 AMENDMENT F1's label-formation-timing scope
statement in force from the freeze. Plus a pre-registered, decisionally
subordinate replication assessment against H0011's frozen published NYC
values.

Implements exactly what is frozen in:

  - docs/research/preregistrations/PREREG-20260722-H0013-chi-replication.md
  - docs/research/preregistrations/TEMPLATE-H0013-manifest.json
  - (inherited) docs/research/preregistrations/AMENDMENT-20260721-H0011-audit-resolution.md

Every constant, formula, and ordering decision below cites the section it
implements; nothing here may be changed without amending those documents
first. Fully deterministic -- no randomness, no bootstrap, no float value
ever reaches a decision comparison.

Usage:
    python scripts/exp_h0013_chi_replication.py \
        --dataset-dir "<KALSHI_DATA_DIR>/datasets/exp-20260722-h0013-replication" \
        --out docs/research/experiments/EXP-20260722-H0013-chi-replication-results.json \
        --out-md docs/research/experiments/EXP-20260722-H0013-chi-replication.md
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal, TypedDict
from zoneinfo import ZoneInfo

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kalshi_weather.dataset.manifest import frame_content_hash

# --- Frozen protocol constants (PREREG Sec 2, 6, 8, 10) ----------------------

STATION = "CHI"  # PREREG Sec 2
VARIABLES: list[str] = ["tmax_f", "tmin_f"]  # PREREG Sec 2
TIMEZONE_NAME = "America/Chicago"  # PREREG Sec 2
LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)

Z_95 = Decimal("1.959963984540054")  # PREREG Sec 6, frozen literal
DECIMAL_CONTEXT_PRECISION = 50  # PREREG Sec 6
COMPARISON_QUANTIZATION = Decimal("1E-12")  # PREREG Sec 6
VALUE_QUANTIZATION = Decimal("0.01")  # PREREG Sec 2 (source column numeric(6,2))
ROUNDTRIP_TOLERANCE = 1e-6  # H0011 AMENDMENT F3, inherited (PREREG Sec 2)
SERIALIZATION_QUANTIZATION = Decimal("1E-12")  # H0011 AMENDMENT F4, inherited
ZERO = Decimal(0)
HALF = Decimal("0.5")

G3_MIN_N = 1000  # PREREG Sec 8, G3
G4_MIN_R_TMIN = 100  # PREREG Sec 8, G4
G5_MAX_UNRESOLVED_RATE = Decimal("0.05")  # PREREG Sec 8, G5 (strict <)

FROZEN_CONTENT_HASH = (
    "5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775"  # PREREG Sec 10, G1
)
EXPECTED_ROW_COUNT = 26310  # full frame, PREREG Sec 10 (recorded, not gated)
EXPECTED_CHI_ROW_COUNT = 5152  # CHI subset, PREREG Sec 10 (recorded, not gated)
REQUIRED_TZDATA_PACKAGE = "tzdata"  # PREREG Sec 2/10

PART_2_SCOPE_STATEMENT = (
    "This result is evidence about settlement-label formation timing only -- "
    "that revised daily-minimum labels typically reach their final value in a "
    "post-midnight issuance. It does not identify the timing of the "
    "underlying physical temperature event (when the true daily minimum "
    "occurred), because this dataset carries issuance timestamps only, not "
    "occurrence timestamps. A study identifying the physical mechanism would "
    "require new occurrence-time data (e.g. parsed from raw CLI product "
    "text) and would constitute a separate, future hypothesis, not a "
    "re-interpretation of this one."
)  # H0011 AMENDMENT Finding 1, inherited verbatim (PREREG Sec 1)

#: H0011's frozen published NYC record -- PREREG Sec 7.3's replication
#: references, re-typed from EXP-20260721-H0011-midnight-boundary-results.json.
NYC_REFERENCE: dict[str, Any] = {
    "n_per_variable": 1282,
    "r_tmin": 311,
    "r_tmax": 170,
    "d_nyc": "0.109984399376",
    "ci95_newcombe": ["0.079970196795", "0.139841273431"],
    "p_pm_nyc": "1.000000000000",
    "ci95_wilson": ["0.987798751679", "1.000000000000"],
    "criterion_2_state": "pass",
    "source": "docs/research/experiments/EXP-20260721-H0011-midnight-boundary-results.json",
}

#: Independently re-typed frozen values (PREREG Sec 2/6/8/10) -- deliberately
#: NOT referencing the module constants above, so verify_config_matches_prereg
#: is a genuine drift check (H0005/H0011 precedent).
_FROZEN_PREREG_REFERENCE: dict[str, Any] = {
    "station": "CHI",
    "variables": ["tmax_f", "tmin_f"],
    "timezone": "America/Chicago",
    "z_constant": "1.959963984540054",
    "decimal_context_precision": 50,
    "comparison_quantization": "1E-12",
    "serialization_quantization": "1E-12",
    "value_quantization": "0.01",
    "roundtrip_tolerance": 1e-6,
    "g3_min_n": 1000,
    "g4_min_r_tmin": 100,
    "g5_max_unresolved_rate": "0.05",
    "frozen_content_hash": "5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775",
    "dataset_version": "exp-20260722-h0013-replication",
    "expected_row_count": 26310,
    "expected_chi_row_count": 5152,
    "nyc_d": "0.109984399376",
    "nyc_ci": ["0.079970196795", "0.139841273431"],
}


def verify_config_matches_prereg() -> bool:
    """TEMPLATE `reproducibility_verification.config_matches_prereg`: every
    decision-relevant frozen constant compared against the independently
    re-typed literals; reads module globals at call time so drift is
    detected rather than a constant compared to itself."""
    ref = _FROZEN_PREREG_REFERENCE
    checks = [
        ref["station"] == STATION,
        ref["variables"] == VARIABLES,
        ref["timezone"] == TIMEZONE_NAME,
        str(Z_95) == ref["z_constant"],
        ref["decimal_context_precision"] == DECIMAL_CONTEXT_PRECISION,
        str(COMPARISON_QUANTIZATION) == ref["comparison_quantization"],
        str(SERIALIZATION_QUANTIZATION) == ref["serialization_quantization"],
        str(VALUE_QUANTIZATION) == ref["value_quantization"],
        ref["roundtrip_tolerance"] == ROUNDTRIP_TOLERANCE,
        ref["g3_min_n"] == G3_MIN_N,
        ref["g4_min_r_tmin"] == G4_MIN_R_TMIN,
        str(G5_MAX_UNRESOLVED_RATE) == ref["g5_max_unresolved_rate"],
        ref["frozen_content_hash"] == FROZEN_CONTENT_HASH,
        ref["dataset_version"] == DATASET_VERSION,
        ref["expected_row_count"] == EXPECTED_ROW_COUNT,
        ref["expected_chi_row_count"] == EXPECTED_CHI_ROW_COUNT,
        ref["nyc_d"] == NYC_REFERENCE["d_nyc"],
        ref["nyc_ci"] == NYC_REFERENCE["ci95_newcombe"],
    ]
    return all(checks)


class TzdataNotPinnedError(Exception):
    """PREREG Sec 2/10: execution is blocked if the timezone resolves only
    against an unpinned system tzdata database."""


def get_pinned_tzdata_version() -> str:
    try:
        return version(REQUIRED_TZDATA_PACKAGE)
    except PackageNotFoundError as exc:
        raise TzdataNotPinnedError(
            f"pip package {REQUIRED_TZDATA_PACKAGE!r} is not installed -- "
            "PREREG Sec 2 requires a pinned tzdata package; the system "
            "timezone database is not an acceptable substitute"
        ) from exc


# --- Decimal construction / quantization (PREREG Sec 2/6, F3 inherited) ------


def quantize_value(raw: float) -> Decimal:
    """Construction via Decimal(str(v)) only, never Decimal(v); quantized to
    0.01 via ROUND_HALF_EVEN -- H0011 AMENDMENT F3, inherited."""
    return Decimal(str(raw)).quantize(VALUE_QUANTIZATION, rounding=ROUND_HALF_EVEN)


def value_roundtrips(raw: float) -> bool:
    """F3's round-trip invariant, enumerated under gate G2."""
    return abs(float(quantize_value(raw)) - raw) < ROUNDTRIP_TOLERANCE


def quantize_for_comparison(value: Decimal) -> Decimal:
    """PREREG Sec 6: strict decision comparisons decided only after 1e-12
    ROUND_HALF_EVEN quantization of both operands."""
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        return value.quantize(COMPARISON_QUANTIZATION, rounding=ROUND_HALF_EVEN)


def strictly_greater(a: Decimal, b: Decimal) -> bool:
    return quantize_for_comparison(a) > quantize_for_comparison(b)


def strictly_less(a: Decimal, b: Decimal) -> bool:
    return quantize_for_comparison(a) < quantize_for_comparison(b)


# --- Serialization (F4 inherited) --------------------------------------------


def serialize_decimal(value: Decimal) -> str:
    """Fixed-point Decimal string, quantized to 1e-12, exactly 12 digits
    after the decimal point, no scientific notation."""
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        quantized = value.quantize(SERIALIZATION_QUANTIZATION, rounding=ROUND_HALF_EVEN)
    if quantized == 0:
        quantized = abs(quantized)  # avoid a "-0.000000000000" rendering
    return format(quantized, "f")


def serialize_ci(lower: Decimal, upper: Decimal) -> list[str]:
    return [serialize_decimal(lower), serialize_decimal(upper)]


# --- Statistical methods (PREREG Sec 6) --------------------------------------


def wilson_interval(x: int, n: int) -> tuple[Decimal, Decimal]:
    """Wilson score interval, no continuity correction, two-sided 95%."""
    if n <= 0:
        raise ValueError(f"wilson_interval requires n > 0, got {n}")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        n_dec = Decimal(n)
        p_hat = Decimal(x) / n_dec
        z2 = Z_95 * Z_95
        denom = 1 + z2 / n_dec
        center = (p_hat + z2 / (2 * n_dec)) / denom
        inside = p_hat * (1 - p_hat) / n_dec + z2 / (4 * n_dec * n_dec)
        halfwidth = Z_95 * inside.sqrt() / denom
        return (center - halfwidth, center + halfwidth)


def newcombe_interval(
    *, x_tmin: int, n_tmin: int, x_tmax: int, n_tmax: int
) -> tuple[Decimal, Decimal]:
    """Newcombe (1998) hybrid score interval, no continuity correction,
    two-sided 95%; sample 1 = tmin, sample 2 = tmax (PREREG Sec 5/6)."""
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        l1, u1 = wilson_interval(x_tmin, n_tmin)
        l2, u2 = wilson_interval(x_tmax, n_tmax)
        p1 = Decimal(x_tmin) / Decimal(n_tmin)
        p2 = Decimal(x_tmax) / Decimal(n_tmax)
        diff = p1 - p2
        lower = diff - ((p1 - l1) ** 2 + (u2 - p2) ** 2).sqrt()
        upper = diff + ((u1 - p1) ** 2 + (p2 - l2) ** 2).sqrt()
        return (lower, upper)


# --- Population / unit of analysis (PREREG Sec 2, 3) -------------------------


@dataclass(slots=True)
class VariableDay:
    variable: str
    observation_date: date
    issuances: list[tuple[datetime, Decimal]]  # ascending by issuance_time; naive-UTC


def build_variable_days(frame: pl.DataFrame) -> dict[tuple[str, date], VariableDay]:
    """One VariableDay per (variable, observation_date), from the frame
    sorted by (variable, observation_date, issuance_time) ascending -- the
    frozen sorting order (PREREG Sec 10)."""
    sorted_frame = frame.sort(["variable", "observation_date", "issuance_time"])
    days: dict[tuple[str, date], VariableDay] = {}
    for row in sorted_frame.iter_rows(named=True):
        key = (row["variable"], row["observation_date"])
        vd = days.get(key)
        if vd is None:
            vd = VariableDay(
                variable=row["variable"], observation_date=row["observation_date"], issuances=[]
            )
            days[key] = vd
        vd.issuances.append((row["issuance_time"], quantize_value(row["value"])))
    return days


def is_revised(vd: VariableDay) -> bool:
    """PREREG Sec 3: revised iff first-stage value != final-stage value."""
    return vd.issuances[0][1] != vd.issuances[-1][1]


def is_flip_flop(vd: VariableDay) -> bool:
    """PREREG Sec 9: unrevised day whose intermediate values deviated."""
    if is_revised(vd) or len(vd.issuances) < 3:
        return False
    first_value = vd.issuances[0][1]
    return any(value != first_value for _, value in vd.issuances[1:-1])


# --- Post-midnight attribution (PREREG Sec 4, deterministic) -----------------

#: True = post-midnight-attributed, False = pre-midnight, None = unresolved
#: (F9 inherited: remains in the denominator, counts as NOT attributed).
AttributionOutcome = bool | None


def local_midnight_boundary(observation_date: date) -> datetime:
    """PREREG Sec 4: 00:00:00 local (America/Chicago) on observation_date
    + 1 day."""
    next_day = observation_date + timedelta(days=1)
    return datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=LOCAL_TZ)


def to_local(naive_utc: datetime) -> datetime:
    """PREREG Sec 2: issuance_time is naive and interpreted as UTC."""
    return naive_utc.replace(tzinfo=UTC).astimezone(LOCAL_TZ)


def first_appearance_of_final_value(vd: VariableDay) -> datetime:
    """PREREG Sec 4: earliest issuance whose quantized value equals the
    final-stage value -- the frozen non-monotone tie-break."""
    final_value = vd.issuances[-1][1]
    for issuance_time, value in vd.issuances:
        if value == final_value:
            return issuance_time
    raise AssertionError("unreachable: the final issuance always matches its own value")


def is_nonmonotone_final_appearance(vd: VariableDay) -> bool:
    """PREREG Sec 9: final value appeared, deviated, reappeared."""
    final_value = vd.issuances[-1][1]
    first_appearance_time = first_appearance_of_final_value(vd)
    seen_deviation = False
    for issuance_time, value in vd.issuances:
        if issuance_time <= first_appearance_time:
            continue
        if value != final_value:
            seen_deviation = True
    return seen_deviation


def attribute_revision(vd: VariableDay) -> AttributionOutcome:
    """PREREG Sec 4: post-midnight (True), pre-midnight (False), or
    unresolved (None) -- F9's closed trigger list, caught narrowly."""
    try:
        first_appearance_time = first_appearance_of_final_value(vd)
        boundary = local_midnight_boundary(vd.observation_date)
        local_dt = to_local(first_appearance_time)
        return local_dt >= boundary  # PREREG Sec 4: at-or-after
    except (ValueError, OverflowError, TypeError):
        return None


def is_exact_boundary(vd: VariableDay) -> bool:
    """PREREG Sec 9: first appearance lands exactly on the boundary."""
    try:
        first_appearance_time = first_appearance_of_final_value(vd)
        boundary = local_midnight_boundary(vd.observation_date)
        return to_local(first_appearance_time) == boundary
    except (ValueError, OverflowError, TypeError):
        return False


# --- Eligibility gates (PREREG Sec 8; F6 sentinel inherited) -----------------

NOT_EVALUATED: Literal["not_evaluated"] = "not_evaluated"


class G2Invariants(TypedDict):
    natural_key_unique: bool
    no_nulls: bool
    station_set_exact: bool
    variable_set_exact: bool
    value_roundtrip_verified: bool


def compute_g2_invariants(frame: pl.DataFrame) -> G2Invariants:
    """PREREG Sec 2/8: all G2 invariants on the CHI-filtered frame,
    including the F3 round-trip check."""
    total = frame.height
    natural_key_unique = (
        frame.select(["station_id", "variable", "issuance_time"]).unique().height == total
    )
    no_nulls = bool(
        frame.select(
            (
                pl.col("value").is_null()
                | pl.col("issuance_time").is_null()
                | pl.col("observation_date").is_null()
                | pl.col("station_id").is_null()
                | pl.col("variable").is_null()
            ).sum()
        ).item()
        == 0
    )
    station_set_exact = set(frame["station_id"].unique().to_list()) == {STATION}
    variable_set_exact = set(frame["variable"].unique().to_list()) == set(VARIABLES)
    value_roundtrip_verified = all(
        value_roundtrips(v) for v in frame["value"].to_list() if v is not None
    )
    return {
        "natural_key_unique": natural_key_unique,
        "no_nulls": no_nulls,
        "station_set_exact": station_set_exact,
        "variable_set_exact": variable_set_exact,
        "value_roundtrip_verified": value_roundtrip_verified,
    }


@dataclass(slots=True)
class GateOutcome:
    gates: dict[str, Any]
    all_gates_passed: bool
    variable_days: dict[tuple[str, date], VariableDay] | None
    revised_tmin_days: list[VariableDay] | None
    tmin_attributions: list[AttributionOutcome] | None


def run_gates(full_frame: pl.DataFrame) -> GateOutcome:
    """PREREG Sec 8/10 evaluation order: G1 (full-frame hash) -> frozen CHI
    filter -> G2 -> G3 -> G4 -> G5. On the first failure, later gate fields
    are the NOT_EVALUATED sentinel and no interval is computed."""
    gates: dict[str, Any] = {
        "g1_frame_hash_matches": NOT_EVALUATED,
        "g2_invariants": {
            "natural_key_unique": NOT_EVALUATED,
            "no_nulls": NOT_EVALUATED,
            "station_set_exact": NOT_EVALUATED,
            "variable_set_exact": NOT_EVALUATED,
            "value_roundtrip_verified": NOT_EVALUATED,
        },
        "g3_n_tmax": NOT_EVALUATED,
        "g3_n_tmin": NOT_EVALUATED,
        "g4_r_tmin": NOT_EVALUATED,
        "g5_unresolved_attribution_rate": NOT_EVALUATED,
        "all_gates_passed": False,
    }

    # G1 verifies the LOADED FULL FRAME (all stations), before the filter --
    # the hash is recomputed with the platform's canonical frame_content_hash
    # and compared to the frozen pin (PREREG Sec 8 G1).
    recomputed_hash = frame_content_hash(full_frame)
    g1_pass = recomputed_hash == FROZEN_CONTENT_HASH
    gates["g1_frame_hash_matches"] = g1_pass
    if not g1_pass:
        return GateOutcome(gates, False, None, None, None)

    # Frozen filter step (PREREG Sec 2/8): CHI rows only, after G1.
    frame = full_frame.filter(pl.col("station_id") == STATION)

    g2 = compute_g2_invariants(frame)
    gates["g2_invariants"] = dict(g2)
    g2_pass = all(g2.values())
    if not g2_pass:
        return GateOutcome(gates, False, None, None, None)

    variable_days = build_variable_days(frame)
    n_tmax = sum(1 for k in variable_days if k[0] == "tmax_f")
    n_tmin = sum(1 for k in variable_days if k[0] == "tmin_f")
    gates["g3_n_tmax"] = n_tmax
    gates["g3_n_tmin"] = n_tmin
    g3_pass = n_tmax >= G3_MIN_N and n_tmin >= G3_MIN_N
    if not g3_pass:
        return GateOutcome(gates, False, variable_days, None, None)

    tmin_days = [vd for k, vd in variable_days.items() if k[0] == "tmin_f"]
    revised_tmin_days = [vd for vd in tmin_days if is_revised(vd)]
    r_tmin = len(revised_tmin_days)
    gates["g4_r_tmin"] = r_tmin
    g4_pass = r_tmin >= G4_MIN_R_TMIN
    if not g4_pass:
        return GateOutcome(gates, False, variable_days, revised_tmin_days, None)

    tmin_attributions = [attribute_revision(vd) for vd in revised_tmin_days]
    n_unresolved = sum(1 for a in tmin_attributions if a is None)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        unresolved_rate = Decimal(n_unresolved) / Decimal(r_tmin)
    gates["g5_unresolved_attribution_rate"] = serialize_decimal(unresolved_rate)
    g5_pass = strictly_less(unresolved_rate, G5_MAX_UNRESOLVED_RATE)
    gates["all_gates_passed"] = g5_pass
    return GateOutcome(gates, g5_pass, variable_days, revised_tmin_days, tmin_attributions)


# --- Primary estimands (PREREG Sec 5, 6) -------------------------------------


class Part1Result(TypedDict):
    n_tmax: int
    r_tmax: int
    p_tmax: str
    n_tmin: int
    r_tmin: int
    p_tmin: str
    d_hat: str
    ci95_newcombe: list[str]
    criterion_1_pass: bool


def compute_part1(variable_days: dict[tuple[str, date], VariableDay]) -> Part1Result:
    """PREREG Sec 5 E1: D_chi = p_tmin - p_tmax, unpaired Newcombe 95% CI."""
    tmax_days = [vd for k, vd in variable_days.items() if k[0] == "tmax_f"]
    tmin_days = [vd for k, vd in variable_days.items() if k[0] == "tmin_f"]
    n_tmax, n_tmin = len(tmax_days), len(tmin_days)
    r_tmax = sum(1 for vd in tmax_days if is_revised(vd))
    r_tmin = sum(1 for vd in tmin_days if is_revised(vd))
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        p_tmax = Decimal(r_tmax) / Decimal(n_tmax)
        p_tmin = Decimal(r_tmin) / Decimal(n_tmin)
        d_hat = p_tmin - p_tmax
    lower, upper = newcombe_interval(x_tmin=r_tmin, n_tmin=n_tmin, x_tmax=r_tmax, n_tmax=n_tmax)
    return {
        "n_tmax": n_tmax,
        "r_tmax": r_tmax,
        "p_tmax": serialize_decimal(p_tmax),
        "n_tmin": n_tmin,
        "r_tmin": r_tmin,
        "p_tmin": serialize_decimal(p_tmin),
        "d_hat": serialize_decimal(d_hat),
        "ci95_newcombe": serialize_ci(lower, upper),
        "criterion_1_pass": strictly_greater(lower, ZERO),
    }


class Part2Result(TypedDict):
    r_tmin: int
    x_pm: int
    n_unresolved: int
    p_pm: str
    ci95_wilson: list[str]
    criterion_2_state: str
    required_adjacent_fields: list[str]


REQUIRED_ADJACENT_FIELDS = [
    "descriptive_results.tmax_attribution_rate",
    "descriptive_results.stratified_gap_decomposition",
]  # H0011 AMENDMENT Finding 1, inherited


def compute_part2(
    revised_tmin_days: list[VariableDay], attributions: list[AttributionOutcome]
) -> Part2Result:
    """PREREG Sec 5 E2: p_pm_chi = X_pm / R_tmin, Wilson 95% CI; unresolved
    in denominator, excluded from numerator."""
    r_tmin = len(revised_tmin_days)
    x_pm = sum(1 for a in attributions if a is True)
    n_unresolved = sum(1 for a in attributions if a is None)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        p_pm = Decimal(x_pm) / Decimal(r_tmin)
    lower, upper = wilson_interval(x_pm, r_tmin)
    if strictly_greater(lower, HALF):
        state = "pass"
    elif strictly_less(upper, HALF):
        state = "below"
    else:
        state = "straddle"
    return {
        "r_tmin": r_tmin,
        "x_pm": x_pm,
        "n_unresolved": n_unresolved,
        "p_pm": serialize_decimal(p_pm),
        "ci95_wilson": serialize_ci(lower, upper),
        "criterion_2_state": state,
        "required_adjacent_fields": list(REQUIRED_ADJACENT_FIELDS),
    }


# --- Decision rule (PREREG Sec 7.1) ------------------------------------------


@dataclass(slots=True)
class Verdict:
    outcome: Literal["confirmed", "rejected", "inconclusive", "blocked"]
    evaluation_step: str
    reason: str


def evaluate(
    gate_outcome: GateOutcome, part1: Part1Result | None, part2: Part2Result | None
) -> Verdict:
    """PREREG Sec 7.1's exact decision table, top to bottom, each row
    terminal."""
    if not gate_outcome.all_gates_passed:
        return Verdict(
            "blocked",
            "step_0",
            "an eligibility gate failed; see eligibility_gates in the manifest "
            "-- not a scientific outcome, no interval was computed",
        )
    assert part1 is not None and part2 is not None  # guaranteed once gates pass

    d_lower = Decimal(part1["ci95_newcombe"][0])
    if not strictly_greater(d_lower, ZERO):
        return Verdict(
            "rejected",
            "step_1",
            "L(D_chi) <= 0 (quantized): no confident positive tmin-vs-tmax rate difference at CHI",
        )

    pm_lower = Decimal(part2["ci95_wilson"][0])
    pm_upper = Decimal(part2["ci95_wilson"][1])
    if strictly_greater(pm_lower, HALF):
        return Verdict("confirmed", "step_2", "L(D_chi) > 0 and L(p_pm_chi) > 1/2")
    if strictly_less(pm_upper, HALF):
        return Verdict(
            "rejected",
            "step_3",
            "L(D_chi) > 0 and U(p_pm_chi) < 1/2: rate difference exists but the "
            "formation-timing conjunct is refuted",
        )
    return Verdict(
        "inconclusive",
        "step_4",
        "L(D_chi) > 0 and the p_pm_chi interval straddles 1/2",
    )


# --- Replication assessment (PREREG Sec 7.3; subordinate to Sec 7.1) --------


class ReplicationAssessment(TypedDict):
    nyc_reference: dict[str, Any]
    direction_replicated: bool
    magnitude_consistent: bool
    d_gap_vs_nyc: str
    part2_consistent: bool


def compute_replication_assessment(part1: Part1Result, part2: Part2Result) -> ReplicationAssessment:
    """PREREG Sec 7.3: mechanical derivations against the frozen NYC
    published values. No field here can change the Sec 7.1 outcome."""
    chi_lower = Decimal(part1["ci95_newcombe"][0])
    chi_upper = Decimal(part1["ci95_newcombe"][1])
    nyc_lower = Decimal(NYC_REFERENCE["ci95_newcombe"][0])
    nyc_upper = Decimal(NYC_REFERENCE["ci95_newcombe"][1])
    # Closed-interval overlap, quantized comparisons; touching counts as
    # overlap (PREREG Sec 7.3 / Sec 11's touching-case requirement).
    disjoint = strictly_greater(chi_lower, nyc_upper) or strictly_greater(nyc_lower, chi_upper)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        d_gap = Decimal(part1["d_hat"]) - Decimal(NYC_REFERENCE["d_nyc"])
    return {
        "nyc_reference": dict(NYC_REFERENCE),
        "direction_replicated": part1["criterion_1_pass"],
        "magnitude_consistent": not disjoint,
        "d_gap_vs_nyc": serialize_decimal(d_gap),
        "part2_consistent": part2["criterion_2_state"] == NYC_REFERENCE["criterion_2_state"],
    }


# --- Descriptive analyses (PREREG Sec 9; non-decisional throughout) ----------


def _rate_or_null(numerator: int, denominator: int) -> str | None:
    """F7 inherited: a zero-denominator descriptive ratio is null, never 0."""
    if denominator == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        return serialize_decimal(Decimal(numerator) / Decimal(denominator))


def build_tmax_attribution_rate(
    variable_days: dict[tuple[str, date], VariableDay],
) -> dict[str, Any]:
    """Mandatory adjacent disclosure (F1 inherited)."""
    tmax_days = [vd for k, vd in variable_days.items() if k[0] == "tmax_f"]
    revised_tmax = [vd for vd in tmax_days if is_revised(vd)]
    attributions = [attribute_revision(vd) for vd in revised_tmax]
    x_pm_tmax = sum(1 for a in attributions if a is True)
    r_tmax = len(revised_tmax)
    return {"r_tmax": r_tmax, "x_pm_tmax": x_pm_tmax, "rate": _rate_or_null(x_pm_tmax, r_tmax)}


def build_stratified_gap_decomposition(
    variable_days: dict[tuple[str, date], VariableDay],
    tmax_attribution_rate: dict[str, Any],
    x_pm_tmin: int,
) -> dict[str, Any]:
    """Mandatory adjacent disclosure (F1 inherited): post- vs pre-midnight
    stratum share of the raw gap, per valid variable-day."""
    n_tmax = sum(1 for k in variable_days if k[0] == "tmax_f")
    n_tmin = sum(1 for k in variable_days if k[0] == "tmin_f")
    tmax_days = [vd for k, vd in variable_days.items() if k[0] == "tmax_f"]
    tmin_days = [vd for k, vd in variable_days.items() if k[0] == "tmin_f"]
    r_tmax = sum(1 for vd in tmax_days if is_revised(vd))
    r_tmin = sum(1 for vd in tmin_days if is_revised(vd))
    x_pm_tmax = tmax_attribution_rate["x_pm_tmax"]

    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        r_pm_tmin = Decimal(x_pm_tmin) / Decimal(n_tmin)
        r_pm_tmax = Decimal(x_pm_tmax) / Decimal(n_tmax)
        r_pre_tmin = Decimal(r_tmin - x_pm_tmin) / Decimal(n_tmin)
        r_pre_tmax = Decimal(r_tmax - x_pm_tmax) / Decimal(n_tmax)
        post_gap = r_pm_tmin - r_pm_tmax
        pre_gap = r_pre_tmin - r_pre_tmax
    return {
        "post_midnight_stratum_gap": serialize_decimal(post_gap),
        "pre_midnight_stratum_gap": serialize_decimal(pre_gap),
    }


def build_first_appearance_local_hour_histogram(
    variable_days: dict[tuple[str, date], VariableDay],
) -> dict[str, list[int]]:
    """F5 inherited: revised days only, 24 bins (local hour), per variable."""
    histograms: dict[str, list[int]] = {v: [0] * 24 for v in VARIABLES}
    for (variable, _obs_date), vd in variable_days.items():
        if not is_revised(vd):
            continue
        try:
            local_dt = to_local(first_appearance_of_final_value(vd))
        except (ValueError, OverflowError, TypeError):
            continue  # unresolved; excluded from this descriptive histogram
        histograms[variable][local_dt.hour] += 1
    return histograms


def build_issuance_count_distribution(
    variable_days: dict[tuple[str, date], VariableDay],
) -> dict[str, dict[str, int]]:
    distributions: dict[str, dict[str, int]] = {v: {} for v in VARIABLES}
    for (variable, _obs_date), vd in variable_days.items():
        key = str(len(vd.issuances))
        distributions[variable][key] = distributions[variable].get(key, 0) + 1
    return distributions


def build_single_issuance_days(
    variable_days: dict[tuple[str, date], VariableDay],
) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(VARIABLES, 0)
    for (variable, _obs_date), vd in variable_days.items():
        if len(vd.issuances) == 1:
            counts[variable] += 1
    return counts


def build_flip_flop_counts(
    variable_days: dict[tuple[str, date], VariableDay],
) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {
        v: {"unrevised_with_intermediate_deviation": 0, "revised_nonmonotone_final_appearance": 0}
        for v in VARIABLES
    }
    for (variable, _obs_date), vd in variable_days.items():
        if is_flip_flop(vd):
            counts[variable]["unrevised_with_intermediate_deviation"] += 1
        elif is_revised(vd) and is_nonmonotone_final_appearance(vd):
            counts[variable]["revised_nonmonotone_final_appearance"] += 1
    return counts


def build_exact_midnight_boundary_issuances(
    variable_days: dict[tuple[str, date], VariableDay],
) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(VARIABLES, 0)
    for (variable, _obs_date), vd in variable_days.items():
        if is_revised(vd) and is_exact_boundary(vd):
            counts[variable] += 1
    return counts


def build_concordance_2x2(variable_days: dict[tuple[str, date], VariableDay]) -> dict[str, int]:
    tmax_by_date = {d: vd for (v, d), vd in variable_days.items() if v == "tmax_f"}
    tmin_by_date = {d: vd for (v, d), vd in variable_days.items() if v == "tmin_f"}
    both_dates = set(tmax_by_date) & set(tmin_by_date)
    both = tmin_only = tmax_only = neither = 0
    for d in both_dates:
        tmin_revised = is_revised(tmin_by_date[d])
        tmax_revised = is_revised(tmax_by_date[d])
        if tmin_revised and tmax_revised:
            both += 1
        elif tmin_revised:
            tmin_only += 1
        elif tmax_revised:
            tmax_only += 1
        else:
            neither += 1
    return {"both": both, "tmin_only": tmin_only, "tmax_only": tmax_only, "neither": neither}


def build_unresolved_attribution_days(
    revised_tmin_days: list[VariableDay], attributions: list[AttributionOutcome]
) -> list[str]:
    return [
        vd.observation_date.isoformat()
        for vd, outcome in zip(revised_tmin_days, attributions, strict=True)
        if outcome is None
    ]


def build_per_year_revision_rates(
    variable_days: dict[tuple[str, date], VariableDay],
) -> dict[str, dict[str, dict[str, Any]]]:
    """PREREG Sec 9 (new in H0013): per calendar year, per variable:
    {n, r, rate} -- point estimates only, the stability disclosure."""
    per_year: dict[str, dict[str, dict[str, Any]]] = {v: {} for v in VARIABLES}
    for (variable, obs_date), vd in sorted(variable_days.items(), key=lambda kv: kv[0]):
        year = str(obs_date.year)
        cell = per_year[variable].setdefault(year, {"n": 0, "r": 0})
        cell["n"] += 1
        if is_revised(vd):
            cell["r"] += 1
    for variable in VARIABLES:
        for cell in per_year[variable].values():
            cell["rate"] = _rate_or_null(cell["r"], cell["n"])
    return per_year


def build_feature_summary(
    variable_days: dict[tuple[str, date], VariableDay],
) -> dict[str, dict[str, Any]]:
    """PREREG Sec 9 (new in H0013): the dataset-characterization block."""
    summary: dict[str, dict[str, Any]] = {}
    for variable in VARIABLES:
        days = [vd for k, vd in variable_days.items() if k[0] == variable]
        n_days = len(days)
        issuance_rows = sum(len(vd.issuances) for vd in days)
        single = sum(1 for vd in days if len(vd.issuances) == 1)
        dates = [vd.observation_date for vd in days]
        summary[variable] = {
            "n_days": n_days,
            "issuance_rows": issuance_rows,
            "mean_issuances_per_day": _rate_or_null(issuance_rows, n_days),
            "single_issuance_days": single,
            "first_date": min(dates).isoformat() if dates else None,
            "last_date": max(dates).isoformat() if dates else None,
        }
    return summary


def build_nyc_consistency_check(full_frame: pl.DataFrame) -> dict[str, Any]:
    """PREREG Sec 9 (new in H0013): Part 1 counts recomputed from the pinned
    frame's NYC subset by the same code path, next to H0011's published
    counts. Descriptive only; cannot alter the outcome."""
    nyc_frame = full_frame.filter(pl.col("station_id") == "NYC")
    nyc_days = build_variable_days(nyc_frame)
    n_tmax = sum(1 for k in nyc_days if k[0] == "tmax_f")
    n_tmin = sum(1 for k in nyc_days if k[0] == "tmin_f")
    r_tmax = sum(1 for k, vd in nyc_days.items() if k[0] == "tmax_f" and is_revised(vd))
    r_tmin = sum(1 for k, vd in nyc_days.items() if k[0] == "tmin_f" and is_revised(vd))
    return {
        "recomputed": {"n_tmax": n_tmax, "r_tmax": r_tmax, "n_tmin": n_tmin, "r_tmin": r_tmin},
        "published_h0011": {
            "n_tmax": NYC_REFERENCE["n_per_variable"],
            "r_tmax": NYC_REFERENCE["r_tmax"],
            "n_tmin": NYC_REFERENCE["n_per_variable"],
            "r_tmin": NYC_REFERENCE["r_tmin"],
        },
        "note": (
            "expected +1 day per variable vs published (archive advanced "
            "2026-07-20 -> 2026-07-21 between pins); larger divergence is a "
            "data-integrity flag -- PREREG Sec 9"
        ),
    }


class DescriptiveResults(TypedDict):
    tmax_attribution_rate: dict[str, Any]
    stratified_gap_decomposition: dict[str, Any]
    first_appearance_local_hour_histogram: dict[str, list[int]]
    issuance_count_distribution: dict[str, dict[str, int]]
    single_issuance_days: dict[str, int]
    flip_flop_counts: dict[str, dict[str, int]]
    exact_midnight_boundary_issuances: dict[str, int]
    concordance_2x2: dict[str, int]
    unresolved_attribution_days: list[str]
    per_year_revision_rates: dict[str, dict[str, dict[str, Any]]]
    feature_summary: dict[str, dict[str, Any]]
    nyc_consistency_check: dict[str, Any]


def build_descriptive_results(
    variable_days: dict[tuple[str, date], VariableDay],
    revised_tmin_days: list[VariableDay],
    tmin_attributions: list[AttributionOutcome],
    full_frame: pl.DataFrame,
) -> DescriptiveResults:
    tmax_attribution_rate = build_tmax_attribution_rate(variable_days)
    x_pm_tmin = sum(1 for a in tmin_attributions if a is True)
    return {
        "tmax_attribution_rate": tmax_attribution_rate,
        "stratified_gap_decomposition": build_stratified_gap_decomposition(
            variable_days, tmax_attribution_rate, x_pm_tmin
        ),
        "first_appearance_local_hour_histogram": build_first_appearance_local_hour_histogram(
            variable_days
        ),
        "issuance_count_distribution": build_issuance_count_distribution(variable_days),
        "single_issuance_days": build_single_issuance_days(variable_days),
        "flip_flop_counts": build_flip_flop_counts(variable_days),
        "exact_midnight_boundary_issuances": build_exact_midnight_boundary_issuances(variable_days),
        "concordance_2x2": build_concordance_2x2(variable_days),
        "unresolved_attribution_days": build_unresolved_attribution_days(
            revised_tmin_days, tmin_attributions
        ),
        "per_year_revision_rates": build_per_year_revision_rates(variable_days),
        "feature_summary": build_feature_summary(variable_days),
        "nyc_consistency_check": build_nyc_consistency_check(full_frame),
    }


# --- Manifest validation (TEMPLATE-H0013-manifest.json parity) ---------------


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
        "primary_results",
        "descriptive_results",
        "reproducibility_verification",
        "date_run",
    },
    "preregistration": {
        "document",
        "frozen_date",
        "provenance_note",
        "original_hypotheses_entry",
        "inherited_amendments",
        "elaboration_flags_for_audit",
    },
    "preregistration.inherited_amendments": {"document", "applies_as", "blocked_on"},
    "dataset": {
        "version",
        "frame",
        "content_hashes",
        "row_count_expected",
        "chi_row_count_expected",
        "dataset_build_git_commit",
        "source_db_revision",
        "discovery_sample_reference",
        "analysis_git_commit",
    },
    "config": {
        "station",
        "variables",
        "unit_of_analysis",
        "valid_variable_day",
        "revision_definition",
        "attribution_rule",
        "decimal_policy",
        "timezone",
        "station_filter",
        "estimand_1",
        "estimand_2",
        "ci_method_estimand_1",
        "ci_method_estimand_2",
        "z_constant",
        "exact_arithmetic",
        "pairing_treatment",
        "decision_rule",
        "random_seed",
        "tzdata_version",
        "software_versions",
        "part_2_scope_statement",
    },
    "eligibility_gates": {
        "g1_frame_hash_matches",
        "g2_invariants",
        "g3_n_tmax",
        "g3_n_tmin",
        "g4_r_tmin",
        "g5_unresolved_attribution_rate",
        "all_gates_passed",
    },
    "eligibility_gates.g2_invariants": {
        "natural_key_unique",
        "no_nulls",
        "station_set_exact",
        "variable_set_exact",
        "value_roundtrip_verified",
    },
    "primary_results": {
        "part_1_rate_difference",
        "part_2_post_midnight_attribution",
        "decision",
        "replication_assessment",
    },
    "primary_results.part_1_rate_difference": {
        "n_tmax",
        "r_tmax",
        "p_tmax",
        "n_tmin",
        "r_tmin",
        "p_tmin",
        "d_hat",
        "ci95_newcombe",
        "criterion_1_pass",
    },
    "primary_results.part_2_post_midnight_attribution": {
        "r_tmin",
        "x_pm",
        "n_unresolved",
        "p_pm",
        "ci95_wilson",
        "criterion_2_state",
        "required_adjacent_fields",
    },
    "primary_results.decision": {"outcome", "evaluation_step", "reason"},
    "primary_results.replication_assessment": {
        "nyc_reference",
        "direction_replicated",
        "magnitude_consistent",
        "d_gap_vs_nyc",
        "part2_consistent",
    },
    "descriptive_results": {
        "tmax_attribution_rate",
        "stratified_gap_decomposition",
        "first_appearance_local_hour_histogram",
        "issuance_count_distribution",
        "single_issuance_days",
        "flip_flop_counts",
        "exact_midnight_boundary_issuances",
        "concordance_2x2",
        "unresolved_attribution_days",
        "per_year_revision_rates",
        "feature_summary",
        "nyc_consistency_check",
    },
    "reproducibility_verification": {
        "rerun_byte_identical",
        "config_matches_prereg",
        "issuance_time_utc_verified",
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
    """Every key TEMPLATE-H0013-manifest.json defines must exist in the
    produced results dict; aborts loudly, never a partial manifest."""
    for path, required_keys in _REQUIRED_MANIFEST_PATHS.items():
        node = _get_path(results, path)
        if not isinstance(node, dict):
            raise ManifestValidationError(f"expected an object at {path!r}, got {type(node)}")
        missing = required_keys - node.keys()
        if missing:
            raise ManifestValidationError(f"manifest missing key(s) {sorted(missing)} at {path!r}")

    outcome = results["primary_results"]["decision"]["outcome"]
    if outcome not in ("confirmed", "rejected", "inconclusive", "blocked"):
        raise ManifestValidationError(f"unrecognized decision outcome: {outcome!r}")


# --- Top-level orchestration -------------------------------------------------

CONFIG_BLOCK: dict[str, Any] = {
    "station": STATION,
    "variables": list(VARIABLES),
    "unit_of_analysis": "variable-day (station_id, variable, observation_date)",
    "valid_variable_day": (
        ">=1 stored issuance; single-issuance days counted as unrevised in Part 1 "
        "denominators (H0002 convention, mirrored)"
    ),
    "revision_definition": (
        "first-issued value != final-issued value, Decimal-quantized to 0.01 "
        "(construction/rounding frozen -- see decimal_policy); any-revision "
        "indicator per variable-day; first = min(issuance_time), final = max(issuance_time)"
    ),
    "attribution_rule": (
        "revised tmin day is post-midnight-attributed iff the earliest issuance whose "
        "quantized value equals the final value has local(issuance_time) >= 00:00:00 "
        "America/Chicago on observation_date + 1 day; issuance_time interpreted as "
        "naive-UTC; unresolved cases remain in denominator as non-attributed -- PREREG Sec 4"
    ),
    "decimal_policy": (
        "float->Decimal construction via Decimal(str(v)) only, never Decimal(v); value "
        "quantization to 0.01 via ROUND_HALF_EVEN; round-trip invariant "
        "(|float(quantized) - raw_float| < 1e-6) checked under gate G2, violation "
        "aborts -- H0011 AMENDMENT Finding 3, inherited"
    ),
    "timezone": f"{TIMEZONE_NAME} (IANA, via pinned pip tzdata)",
    "station_filter": (
        "frozen filter step: station_id == 'CHI' applied after G1 (full-frame hash) "
        "and before G2 -- PREREG Sec 2/8"
    ),
    "estimand_1": (
        "D_chi = p_tmin - p_tmax (proportion of revised CHI variable-days); "
        "positive = tmin revises more"
    ),
    "estimand_2": (
        "p_pm_chi = X_pm / R_tmin (proportion of revised CHI tmin variable-days "
        "post-midnight-attributed)"
    ),
    "ci_method_estimand_1": (
        "Newcombe (1998) hybrid score interval for a difference of independent "
        "proportions, no continuity correction, two-sided 95% -- PREREG Sec 6"
    ),
    "ci_method_estimand_2": (
        "Wilson score interval, no continuity correction, two-sided 95% -- PREREG Sec 6"
    ),
    "z_constant": f"{Z_95} (frozen Decimal literal)",
    "exact_arithmetic": (
        "integer counts; Decimal precision-50 interval bounds; all strict decision "
        "comparisons after 1e-12 ROUND_HALF_EVEN quantization of both operands; "
        "quantized equality never satisfies a strict inequality -- PREREG Sec 6"
    ),
    "pairing_treatment": (
        "decisional interval unpaired (H0011 audit-approved convention, inherited); "
        "no paired robustness check exists -- H0011 AMENDMENT Finding 2"
    ),
    "decision_rule": (
        "1) any gate fails -> EXECUTION BLOCKED; 2) L(D_chi) <= 0 -> Rejected; "
        "3) L(D_chi) > 0 and L(p_pm_chi) > 1/2 -> Confirmed; 4) L(D_chi) > 0 and "
        "U(p_pm_chi) < 1/2 -> Rejected (formation-timing conjunct refuted); "
        "5) L(D_chi) > 0 and interval straddles 1/2 -> Inconclusive -- PREREG Sec 7.1"
    ),
    "random_seed": "not applicable -- fully deterministic, no resampling",
    "part_2_scope_statement": PART_2_SCOPE_STATEMENT,
}

PREREGISTRATION_METADATA: dict[str, Any] = {
    "document": "docs/research/preregistrations/PREREG-20260722-H0013-chi-replication.md",
    "frozen_date": "2026-07-22",
    "provenance_note": (
        "PREREG Sec 0 (inline): availability facts inspected pre-freeze; no CHI "
        "revision/attribution statistic computed"
    ),
    "original_hypotheses_entry": "HYPOTHESES.md H0013, opened 2026-07-22",
    "inherited_amendments": {
        "document": "docs/research/preregistrations/AMENDMENT-20260721-H0011-audit-resolution.md",
        "applies_as": (
            "folded into PREREG-20260722-H0013 Secs 1/2/4/6/9 from the start (F1 scope "
            "statement, F2 no paired check, F3 decimal policy, F4 serialization, F5 "
            "histogram, F6 gate sentinel, F7 null ratios, F9 unresolved trigger list)"
        ),
        "blocked_on": [],
    },
    "elaboration_flags_for_audit": [
        "replication_assessment_subordinate_to_primary_decision (PREREG Sec 7.3)",
        "part2_cadence_expectation_disclosed_pre_freeze (PREREG Sec 1/12 R1)",
        "chi_selected_for_cadence_match_pre_freeze (PREREG Sec 0/12 R4)",
        "nyc_consistency_check_descriptive_only (PREREG Sec 9)",
    ],
}

DATASET_VERSION = "exp-20260722-h0013-replication"
DATASET_FRAME = "observation_issuances.parquet"
DATASET_BUILD_GIT_COMMIT = "9285a0145db39d5829b93b648a1e19e5a32c7237"
SOURCE_DB_REVISION = "0007"
DISCOVERY_SAMPLE_REFERENCE = (
    "exp-20260721-h0002 (NYC), used only via H0011's frozen published results "
    "(ec38d1a66d7d93eeccdd6ca6df34d14d6e5214477a50c903e976a24f5f327ca9)"
)


def run(dataset_dir: Path) -> dict[str, Any]:
    """Loads the pinned frame, runs the frozen protocol end to end, and
    returns the full results dict matching TEMPLATE-H0013-manifest.json."""
    tzdata_version = get_pinned_tzdata_version()  # blocks if unpinned

    full_frame = pl.read_parquet(dataset_dir / DATASET_FRAME)

    gate_outcome = run_gates(full_frame)

    part1: Part1Result | None = None
    part2: Part2Result | None = None
    if gate_outcome.all_gates_passed:
        assert gate_outcome.variable_days is not None
        assert gate_outcome.revised_tmin_days is not None
        assert gate_outcome.tmin_attributions is not None
        part1 = compute_part1(gate_outcome.variable_days)
        part2 = compute_part2(gate_outcome.revised_tmin_days, gate_outcome.tmin_attributions)

    # PREREG Sec 10 evaluation order: the Sec 7.1 decision is finalized
    # BEFORE the Sec 7.3 replication fields and Sec 9 descriptives.
    verdict = evaluate(gate_outcome, part1, part2)

    replication: ReplicationAssessment | None = None
    if part1 is not None and part2 is not None:
        replication = compute_replication_assessment(part1, part2)

    descriptive: DescriptiveResults | None = None
    if gate_outcome.all_gates_passed:
        assert gate_outcome.variable_days is not None
        assert gate_outcome.revised_tmin_days is not None
        assert gate_outcome.tmin_attributions is not None
        descriptive = build_descriptive_results(
            gate_outcome.variable_days,
            gate_outcome.revised_tmin_days,
            gate_outcome.tmin_attributions,
            full_frame,
        )

    empty_part1: Part1Result = {
        "n_tmax": 0,
        "r_tmax": 0,
        "p_tmax": NOT_EVALUATED,
        "n_tmin": 0,
        "r_tmin": 0,
        "p_tmin": NOT_EVALUATED,
        "d_hat": NOT_EVALUATED,
        "ci95_newcombe": [NOT_EVALUATED, NOT_EVALUATED],
        "criterion_1_pass": False,
    }
    empty_part2: Part2Result = {
        "r_tmin": 0,
        "x_pm": 0,
        "n_unresolved": 0,
        "p_pm": NOT_EVALUATED,
        "ci95_wilson": [NOT_EVALUATED, NOT_EVALUATED],
        "criterion_2_state": NOT_EVALUATED,
        "required_adjacent_fields": list(REQUIRED_ADJACENT_FIELDS),
    }
    empty_replication: ReplicationAssessment = {
        "nyc_reference": dict(NYC_REFERENCE),
        "direction_replicated": False,
        "magnitude_consistent": False,
        "d_gap_vs_nyc": NOT_EVALUATED,
        "part2_consistent": False,
    }
    empty_descriptive: DescriptiveResults = {
        "tmax_attribution_rate": {},
        "stratified_gap_decomposition": {},
        "first_appearance_local_hour_histogram": {},
        "issuance_count_distribution": {},
        "single_issuance_days": {},
        "flip_flop_counts": {},
        "exact_midnight_boundary_issuances": {},
        "concordance_2x2": {},
        "unresolved_attribution_days": [],
        "per_year_revision_rates": {},
        "feature_summary": {},
        "nyc_consistency_check": {},
    }

    results: dict[str, Any] = {
        "experiment": "EXP-20260722-H0013-chi-replication",
        "hypothesis": "H0013",
        "preregistration": PREREGISTRATION_METADATA,
        "dataset": {
            "version": DATASET_VERSION,
            "frame": DATASET_FRAME,
            "content_hashes": {"observation_issuances": FROZEN_CONTENT_HASH},
            "row_count_expected": EXPECTED_ROW_COUNT,
            "chi_row_count_expected": EXPECTED_CHI_ROW_COUNT,
            "dataset_build_git_commit": DATASET_BUILD_GIT_COMMIT,
            "source_db_revision": SOURCE_DB_REVISION,
            "discovery_sample_reference": DISCOVERY_SAMPLE_REFERENCE,
            "analysis_git_commit": None,  # filled in by the CLI wrapper
        },
        "config": {**CONFIG_BLOCK, "tzdata_version": tzdata_version, "software_versions": None},
        "eligibility_gates": gate_outcome.gates,
        "primary_results": {
            "part_1_rate_difference": part1 or empty_part1,
            "part_2_post_midnight_attribution": part2 or empty_part2,
            "decision": {
                "outcome": verdict.outcome,
                "evaluation_step": verdict.evaluation_step,
                "reason": verdict.reason,
            },
            "replication_assessment": replication or empty_replication,
        },
        "descriptive_results": descriptive or empty_descriptive,
        "reproducibility_verification": {
            "rerun_byte_identical": None,  # populated after the second invocation
            "config_matches_prereg": verify_config_matches_prereg(),
            # PREREG Sec 11's naive-UTC cross-check requires a live database
            # connection; performed as a pre-execution checklist step and
            # recorded post-run (H0005/H0011 convention).
            "issuance_time_utc_verified": None,
            "serialization_policy": (
                "Every non-integer decision-relevant quantity in primary_results is a "
                "JSON string: the Decimal value quantized to 1e-12 via ROUND_HALF_EVEN, "
                "fixed-point notation, exactly 12 digits after the decimal point, no "
                "scientific notation, leading '-' only if negative -- H0011 AMENDMENT "
                "Finding 4, inherited. Integer counts remain plain JSON integers."
            ),
        },
        "date_run": None,
    }
    validate_manifest(results)
    return results


def render_markdown(results: dict[str, Any]) -> str:
    decision = results["primary_results"]["decision"]
    part1 = results["primary_results"]["part_1_rate_difference"]
    part2 = results["primary_results"]["part_2_post_midnight_attribution"]
    replication = results["primary_results"]["replication_assessment"]
    descriptive = results["descriptive_results"]
    nyc = replication["nyc_reference"]
    return f"""# EXP-20260722-H0013-chi-replication

Executed exactly as pre-registered in
`PREREG-20260722-H0013-chi-replication.md` (H0011's frozen protocol
re-instantiated for CHI, with the H0011 amendment's F1-F9 resolutions
inherited from the freeze).

## Decision

**{decision["outcome"].upper()}** ({decision["reason"]}).

## Part 1 (rate difference, CHI)

- tmax: {part1["r_tmax"]}/{part1["n_tmax"]} revised (p = {part1["p_tmax"]})
- tmin: {part1["r_tmin"]}/{part1["n_tmin"]} revised (p = {part1["p_tmin"]})
- D_chi = {part1["d_hat"]}, Newcombe 95% CI {part1["ci95_newcombe"]}
- criterion 1 (L > 0): {"pass" if part1["criterion_1_pass"] else "fail"}

## Replication assessment vs H0011 (NYC, frozen published values)

- NYC: D = {nyc["d_nyc"]}, CI {nyc["ci95_newcombe"]} (N = {nyc["n_per_variable"]}/variable)
- direction_replicated: {replication["direction_replicated"]}
- magnitude_consistent (CI overlap, descriptive): {replication["magnitude_consistent"]}
- D_chi - D_nyc (descriptive): {replication["d_gap_vs_nyc"]}
- part2_consistent (descriptive): {replication["part2_consistent"]}

## Part 2 (adjacent required reporting -- inherited AMENDMENT Finding 1)

{results["config"]["part_2_scope_statement"]}

- tmax attribution rate: {descriptive.get("tmax_attribution_rate")}
- Stratified gap decomposition: {descriptive.get("stratified_gap_decomposition")}
- Part 2 result: {part2}

## NYC same-frame consistency cross-check (descriptive)

{descriptive.get("nyc_consistency_check")}

Full cell-level results: see the accompanying `-results.json`. Figures,
diagnostics, comparison narrative, limitations, and the generalization
recommendation: `docs/research/postmortems/2026-07-22-h0013-closeout.md`.
"""


if __name__ == "__main__":
    import contextlib
    import subprocess

    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Leave date_run/analysis_git_commit null (for byte-identical rerun comparison).",
    )
    args = parser.parse_args()

    output = run(args.dataset_dir)
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
