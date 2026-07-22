"""EXP H0011 -- Two-part claim: (1) NYC tmin_f settlement labels revise
more often than tmax_f labels; (2) the tmin excess is driven by the
local-midnight boundary (a confident majority of revised tmin
variable-days settle to their final value only in a post-midnight
issuance).

Implements exactly what is frozen in:

  - docs/research/preregistrations/PREREG-20260721-H0011-midnight-boundary.md
  - docs/research/preregistrations/AMENDMENT-20260721-H0011-audit-resolution.md
  - docs/research/preregistrations/TEMPLATE-H0011-manifest.json
  - docs/research/preregistrations/PROVENANCE-20260721-H0011-posthoc-vs-prospective.md

Every constant, formula, and ordering decision below cites the exact
section/finding it implements; nothing here may be changed without
amending those documents first. Fully deterministic -- no randomness, no
bootstrap, no float value ever reaches a decision comparison (PREREG
Sec 6.2, AMENDMENT Finding 3/4).

Usage:
    python scripts/exp_h0011_midnight_boundary.py \
        --dataset-dir data/datasets/exp-20260721-h0002 \
        --out docs/research/experiments/EXP-<date>-H0011-midnight-boundary-results.json \
        --out-md docs/research/experiments/EXP-<date>-H0011-midnight-boundary.md
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

# --- Frozen protocol constants (PREREG Sec 2, 6, 8, 10; AMENDMENT F1-F4) ----

STATION = "NYC"  # PREREG Sec 2
VARIABLES: list[str] = ["tmax_f", "tmin_f"]  # PREREG Sec 2
TIMEZONE_NAME = "America/New_York"  # PREREG Sec 2
NY_TZ = ZoneInfo(TIMEZONE_NAME)

Z_95 = Decimal("1.959963984540054")  # PREREG Sec 6.1, frozen literal
DECIMAL_CONTEXT_PRECISION = 50  # PREREG Sec 6.2
COMPARISON_QUANTIZATION = Decimal("1E-12")  # PREREG Sec 6.2
VALUE_QUANTIZATION = Decimal("0.01")  # PREREG Sec 2 (source column numeric(6,2))
ROUNDTRIP_TOLERANCE = 1e-6  # AMENDMENT Finding 3
SERIALIZATION_QUANTIZATION = Decimal("1E-12")  # AMENDMENT Finding 4
ZERO = Decimal(0)
HALF = Decimal("0.5")

G3_MIN_N = 1000  # PREREG Sec 8, G3
G4_MIN_R_TMIN = 100  # PREREG Sec 8, G4
G5_MAX_UNRESOLVED_RATE = Decimal("0.05")  # PREREG Sec 8, G5 (strict <)

FROZEN_CONTENT_HASH = (
    "ec38d1a66d7d93eeccdd6ca6df34d14d6e5214477a50c903e976a24f5f327ca9"  # PREREG Sec 10, G1
)
EXPECTED_ROW_COUNT = 5148  # dataset manifest, PREREG Sec 10 (recorded, not gated)
REQUIRED_TZDATA_PACKAGE = "tzdata"  # PREREG Sec 2/10: pinned pip package required

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
)  # AMENDMENT Finding 1, verbatim

#: Independently re-typed frozen values (PREREG Sec 2/6/8/10) -- deliberately
#: NOT referencing the module constants above, so verify_config_matches_prereg
#: below is a genuine drift check rather than a constant compared to itself
#: (the H0005 verify_config_matches_prereg precedent, restored per the
#: pre-execution integrity review's Finding F3).
_FROZEN_PREREG_REFERENCE: dict[str, Any] = {
    "station": "NYC",
    "variables": ["tmax_f", "tmin_f"],
    "timezone": "America/New_York",
    "z_constant": "1.959963984540054",
    "decimal_context_precision": 50,
    "comparison_quantization": "1E-12",
    "serialization_quantization": "1E-12",
    "value_quantization": "0.01",
    "roundtrip_tolerance": 1e-6,
    "g3_min_n": 1000,
    "g4_min_r_tmin": 100,
    "g5_max_unresolved_rate": "0.05",
    "frozen_content_hash": "ec38d1a66d7d93eeccdd6ca6df34d14d6e5214477a50c903e976a24f5f327ca9",
    "dataset_version": "exp-20260721-h0002",
    "expected_row_count": 5148,
}


def verify_config_matches_prereg() -> bool:
    """PREREG Sec 10 / TEMPLATE `reproducibility_verification.config_matches_
    prereg`, computed mechanically per the established H0005 pattern: every
    decision-relevant frozen constant in this module is compared against the
    independently re-typed literals in _FROZEN_PREREG_REFERENCE. Reads module
    globals at call time, so a drifted constant is detected rather than a
    constant being compared against itself."""
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
    ]
    return all(checks)


class TzdataNotPinnedError(Exception):
    """PREREG Sec 2/10: execution is blocked if the timezone resolves only
    against an unpinned system tzdata database rather than a pinned pip
    package whose version can be recorded in the manifest."""


def get_pinned_tzdata_version() -> str:
    """Returns the pinned pip `tzdata` package version, or raises
    TzdataNotPinnedError. Never falls back to the system database silently
    -- PREREG Sec 2's explicit blocking requirement."""
    try:
        return version(REQUIRED_TZDATA_PACKAGE)
    except PackageNotFoundError as exc:
        raise TzdataNotPinnedError(
            f"pip package {REQUIRED_TZDATA_PACKAGE!r} is not installed -- "
            "PREREG Sec 2 requires a pinned tzdata package; the system "
            "timezone database is not an acceptable substitute"
        ) from exc


# --- Decimal construction / quantization (PREREG Sec 2, AMENDMENT F3) -------


def quantize_value(raw: float) -> Decimal:
    """AMENDMENT Finding 3: construction via Decimal(str(v)) only, never
    Decimal(v) directly on the float object; quantized to 0.01 (the source
    column's numeric(6,2) precision) via ROUND_HALF_EVEN."""
    return Decimal(str(raw)).quantize(VALUE_QUANTIZATION, rounding=ROUND_HALF_EVEN)


def value_roundtrips(raw: float) -> bool:
    """AMENDMENT Finding 3's round-trip invariant, enumerated under gate
    G2: the constructed-and-quantized Decimal, converted back to float,
    must lie within 1e-6 of the original raw value."""
    return abs(float(quantize_value(raw)) - raw) < ROUNDTRIP_TOLERANCE


def quantize_for_comparison(value: Decimal) -> Decimal:
    """PREREG Sec 6.2: every strict decision comparison is decided only
    after quantizing both operands to 1e-12 (ROUND_HALF_EVEN); quantized
    equality means the strict inequality is not satisfied."""
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        return value.quantize(COMPARISON_QUANTIZATION, rounding=ROUND_HALF_EVEN)


def strictly_greater(a: Decimal, b: Decimal) -> bool:
    return quantize_for_comparison(a) > quantize_for_comparison(b)


def strictly_less(a: Decimal, b: Decimal) -> bool:
    return quantize_for_comparison(a) < quantize_for_comparison(b)


# --- Serialization (AMENDMENT Finding 4) -------------------------------------


def serialize_decimal(value: Decimal) -> str:
    """AMENDMENT Finding 4: fixed-point Decimal string, quantized to
    1e-12 via ROUND_HALF_EVEN, exactly 12 digits after the decimal point,
    no scientific notation, leading '-' only if negative."""
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        quantized = value.quantize(SERIALIZATION_QUANTIZATION, rounding=ROUND_HALF_EVEN)
    if quantized == 0:
        quantized = abs(quantized)  # avoid a "-0.000000000000" rendering
    return format(quantized, "f")


def serialize_ci(lower: Decimal, upper: Decimal) -> list[str]:
    return [serialize_decimal(lower), serialize_decimal(upper)]


# --- Statistical methods (PREREG Sec 6.1) ------------------------------------


def wilson_interval(x: int, n: int) -> tuple[Decimal, Decimal]:
    """Wilson score interval, no continuity correction, two-sided 95% --
    PREREG Sec 6.1. Requires n > 0 (callers are gated: G3/G4 guarantee
    this before any interval is computed)."""
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
    """Newcombe (1998) hybrid score interval for a difference of two
    independent proportions, no continuity correction, two-sided 95% --
    PREREG Sec 6.1. Sample 1 is always tmin, sample 2 always tmax, so the
    result is a CI on D = p_tmin - p_tmax (PREREG Sec 5's sign
    convention)."""
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
    """PREREG Sec 2/10: one VariableDay per (variable, observation_date),
    built from the frame sorted by (variable, observation_date,
    issuance_time) ascending -- the frozen sorting order."""
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
    """PREREG Sec 3: revised iff first-stage value != final-stage value.
    A single-issuance day compares its own value to itself (False) --
    the single-issuance-day special case in Sec 2 falls out of this
    naturally, no branch required."""
    return vd.issuances[0][1] != vd.issuances[-1][1]


def is_flip_flop(vd: VariableDay) -> bool:
    """PREREG Sec 3/9: an unrevised day (first == final) whose
    intermediate values nonetheless deviated -- descriptive only."""
    if is_revised(vd) or len(vd.issuances) < 3:
        return False
    first_value = vd.issuances[0][1]
    return any(value != first_value for _, value in vd.issuances[1:-1])


# --- Post-midnight attribution (PREREG Sec 4, deterministic, no discretion) --

#: True = post-midnight-attributed, False = pre-midnight, None = unresolved
#: (AMENDMENT Finding 9: remains in the denominator, counts as NOT
#: post-midnight-attributed).
AttributionOutcome = bool | None


def local_midnight_boundary(observation_date: date) -> datetime:
    """PREREG Sec 4.4: 00:00:00 local (America/New_York) on observation_date
    + 1 day."""
    next_day = observation_date + timedelta(days=1)
    return datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=NY_TZ)


def to_local(naive_utc: datetime) -> datetime:
    """PREREG Sec 2: issuance_time is naive and interpreted as UTC."""
    return naive_utc.replace(tzinfo=UTC).astimezone(NY_TZ)


def first_appearance_of_final_value(vd: VariableDay) -> datetime:
    """PREREG Sec 4.3: the earliest issuance (ascending order, already
    guaranteed by build_variable_days) whose quantized value equals the
    final-stage value -- the frozen tie-break for non-monotone sequences.
    The final issuance itself always matches by construction, so this
    never raises for a well-formed VariableDay."""
    final_value = vd.issuances[-1][1]
    for issuance_time, value in vd.issuances:
        if value == final_value:
            return issuance_time
    raise AssertionError("unreachable: the final issuance always matches its own value")


def is_nonmonotone_final_appearance(vd: VariableDay) -> bool:
    """PREREG Sec 4.3/9: the final value appeared, then deviated, then
    reappeared before the terminal run -- descriptive only."""
    final_value = vd.issuances[-1][1]
    first_appearance_time = first_appearance_of_final_value(vd)
    seen_deviation_after_first_appearance = False
    for issuance_time, value in vd.issuances:
        if issuance_time <= first_appearance_time:
            continue
        if value != final_value:
            seen_deviation_after_first_appearance = True
    return seen_deviation_after_first_appearance


def attribute_revision(vd: VariableDay) -> AttributionOutcome:
    """PREREG Sec 4: classifies a revised variable-day as post-midnight
    (True), pre-midnight (False), or unresolved (None). AMENDMENT
    Finding 9's exhaustive unresolved trigger list: an unconvertible
    timestamp, or a null value/timestamp surviving G2/F3's assertions --
    caught narrowly, never a broad except."""
    try:
        first_appearance_time = first_appearance_of_final_value(vd)
        boundary = local_midnight_boundary(vd.observation_date)
        local_dt = to_local(first_appearance_time)
        return local_dt >= boundary  # PREREG Sec 4.5: at-or-after
    except (ValueError, OverflowError, TypeError):
        return None


def is_exact_boundary(vd: VariableDay) -> bool:
    """PREREG Sec 4.5/9: the first-appearance issuance lands exactly on
    the local-midnight boundary."""
    try:
        first_appearance_time = first_appearance_of_final_value(vd)
        boundary = local_midnight_boundary(vd.observation_date)
        return to_local(first_appearance_time) == boundary
    except (ValueError, OverflowError, TypeError):
        return False


# --- Eligibility gates (PREREG Sec 8; AMENDMENT Finding 3 (G2 invariant), --
# --- Finding 6 (not_evaluated sentinel)) -------------------------------------

NOT_EVALUATED: Literal["not_evaluated"] = "not_evaluated"


class G2Invariants(TypedDict):
    natural_key_unique: bool
    no_nulls: bool
    station_set_exact: bool
    variable_set_exact: bool
    value_roundtrip_verified: bool


def compute_g2_invariants(frame: pl.DataFrame) -> G2Invariants:
    """PREREG Sec 2/8, AMENDMENT Finding 3: all G2 invariants, including
    the round-trip check newly enumerated under this same gate."""
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


def run_gates(frame: pl.DataFrame, dataset_manifest: dict[str, Any]) -> GateOutcome:
    """PREREG Sec 8, AMENDMENT Finding 6: evaluated strictly in order
    G1->G2->G3->G4->G5; on the first failure, later gate fields are
    recorded as NOT_EVALUATED and no confidence interval is computed.

    `dataset_manifest` (the dataset directory's own manifest.json) is
    retained in the signature as recorded metadata context only -- since
    integrity-review Finding F1, G1 recomputes the loaded frame's canonical
    content hash and no longer consults the manifest's claimed hash."""
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

    # Integrity-review Finding F1: G1 verifies the LOADED FRAME, not the
    # dataset directory's manifest claim -- the hash is recomputed with the
    # platform's canonical frame_content_hash (kalshi_weather.dataset.
    # manifest, reused verbatim, the same algorithm that produced the frozen
    # pin) and compared to FROZEN_CONTENT_HASH directly. The directory
    # manifest's own claimed hash remains recorded metadata (in the dataset
    # directory's manifest.json and, as the frozen pin, in this run's
    # dataset.content_hashes output field); it is no longer decisive.
    recomputed_hash = frame_content_hash(frame)
    g1_pass = recomputed_hash == FROZEN_CONTENT_HASH
    gates["g1_frame_hash_matches"] = g1_pass
    if not g1_pass:
        return GateOutcome(gates, False, None, None, None)

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


# --- Primary estimands (PREREG Sec 5, 6.1) -----------------------------------


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
    """PREREG Sec 5 E1, Sec 6.1: D = p_tmin - p_tmax, unpaired Newcombe
    95% CI."""
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
]  # AMENDMENT Finding 1


def compute_part2(
    revised_tmin_days: list[VariableDay], attributions: list[AttributionOutcome]
) -> Part2Result:
    """PREREG Sec 5 E2, Sec 6.1: p_pm = X_pm / R_tmin, Wilson 95% CI.
    Unresolved days remain in the denominator, excluded from the
    numerator -- AMENDMENT Finding 9."""
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


# --- Decision rule (PREREG Sec 7.1, AMENDMENT Finding 1) ---------------------


@dataclass(slots=True)
class Verdict:
    outcome: Literal["confirmed", "rejected", "inconclusive", "blocked"]
    evaluation_step: str
    reason: str


def evaluate(
    gate_outcome: GateOutcome, part1: Part1Result | None, part2: Part2Result | None
) -> Verdict:
    """PREREG Sec 7.1's exact four-step (plus step-0 blocking) decision
    table, evaluated top to bottom, each row terminal."""
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
            "L(D) <= 0 (quantized): no confident positive tmin-vs-tmax rate difference",
        )

    pm_lower = Decimal(part2["ci95_wilson"][0])
    pm_upper = Decimal(part2["ci95_wilson"][1])
    if strictly_greater(pm_lower, HALF):
        return Verdict("confirmed", "step_2", "L(D) > 0 and L(p_pm) > 1/2")
    if strictly_less(pm_upper, HALF):
        return Verdict(
            "rejected",
            "step_3",
            "L(D) > 0 and U(p_pm) < 1/2: rate difference exists but the attribution "
            "fraction is confidently below one half (mechanism refuted)",
        )
    return Verdict(
        "inconclusive",
        "step_4",
        "L(D) > 0 and the p_pm interval straddles 1/2 (mechanism unresolved)",
    )


# --- Descriptive analyses (PREREG Sec 9; AMENDMENT Findings 1, 2, 5, 7, 9) --
# --- Non-decisional throughout: computed after the verdict is already final.


def _rate_or_null(numerator: int, denominator: int) -> str | None:
    """AMENDMENT Finding 7: a zero-denominator descriptive ratio is null,
    never fabricated as 0."""
    if denominator == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        return serialize_decimal(Decimal(numerator) / Decimal(denominator))


def build_tmax_attribution_rate(
    variable_days: dict[tuple[str, date], VariableDay],
) -> dict[str, Any]:
    """PREREG Sec 9: the same Sec 4 procedure applied to revised tmax
    days -- the discriminating-power disclosure for Sec 12 R1. Point
    estimate only, no CI."""
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
    """PREREG Sec 9: how much of the raw tmin-tmax rate gap sits in the
    post-midnight stratum vs the pre-midnight stratum, per valid
    variable-day (not per revised day) -- the identifiable rendering of
    the entry's 'excess' language (AMENDMENT Finding 1 / PREREG Sec 12 R2)."""
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
        post_midnight_stratum_gap = r_pm_tmin - r_pm_tmax
        pre_midnight_stratum_gap = r_pre_tmin - r_pre_tmax
    return {
        "post_midnight_stratum_gap": serialize_decimal(post_midnight_stratum_gap),
        "pre_midnight_stratum_gap": serialize_decimal(pre_midnight_stratum_gap),
    }


def build_first_appearance_local_hour_histogram(
    variable_days: dict[tuple[str, date], VariableDay],
) -> dict[str, list[int]]:
    """AMENDMENT Finding 5: revised variable-days only, 24 bins (local
    wall-clock hour 0-23), per variable."""
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
    """PREREG Sec 4.5/9: among revised variable-days, how many first-
    appearance issuances land exactly on the boundary (expected ~0)."""
    counts: dict[str, int] = dict.fromkeys(VARIABLES, 0)
    for (variable, _obs_date), vd in variable_days.items():
        if is_revised(vd) and is_exact_boundary(vd):
            counts[variable] += 1
    return counts


def build_concordance_2x2(variable_days: dict[tuple[str, date], VariableDay]) -> dict[str, int]:
    """PREREG Sec 6.3/9: within-date tmin/tmax revision concordance, over
    dates where both variables have a valid variable-day."""
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
    """AMENDMENT Finding 9: individually enumerated; expected count 0."""
    return [
        vd.observation_date.isoformat()
        for vd, outcome in zip(revised_tmin_days, attributions, strict=True)
        if outcome is None
    ]


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


def build_descriptive_results(
    variable_days: dict[tuple[str, date], VariableDay],
    revised_tmin_days: list[VariableDay],
    tmin_attributions: list[AttributionOutcome],
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
    }


# --- Manifest validation (TEMPLATE-H0011-manifest.json parity) --------------


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
        "amendment",
        "elaboration_flags_for_audit",
    },
    "preregistration.amendment": {"document", "frozen_date", "resolves", "blocked_on"},
    "dataset": {
        "version",
        "frame",
        "content_hashes",
        "row_count_expected",
        "dataset_build_git_commit",
        "source_db_revision",
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
    "primary_results": {"part_1_rate_difference", "part_2_post_midnight_attribution", "decision"},
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
    """Every key TEMPLATE-H0011-manifest.json defines must exist in the
    produced results dict. Aborts loudly rather than silently returning a
    partial manifest."""
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


# --- Top-level orchestration (NOT invoked against real data this session) --

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
        "America/New_York on observation_date + 1 day; issuance_time interpreted as "
        "naive-UTC; unresolved cases remain in denominator as non-attributed -- PREREG Sec 4"
    ),
    "decimal_policy": (
        "float->Decimal construction via Decimal(str(v)) only, never Decimal(v); value "
        "quantization to 0.01 via ROUND_HALF_EVEN; round-trip invariant "
        "(|float(quantized) - raw_float| < 1e-6) checked under gate G2, violation "
        "aborts -- AMENDMENT Finding 3"
    ),
    "timezone": f"{TIMEZONE_NAME} (IANA, via pinned pip tzdata)",
    "estimand_1": (
        "D = p_tmin - p_tmax (proportion of revised variable-days); positive = tmin revises more"
    ),
    "estimand_2": (
        "p_pm = X_pm / R_tmin (proportion of revised tmin variable-days post-midnight-attributed)"
    ),
    "ci_method_estimand_1": (
        "Newcombe (1998) hybrid score interval for a difference of independent "
        "proportions, no continuity correction, two-sided 95% -- PREREG Sec 6.1"
    ),
    "ci_method_estimand_2": (
        "Wilson score interval, no continuity correction, two-sided 95% -- PREREG Sec 6.1"
    ),
    "z_constant": f"{Z_95} (frozen Decimal literal)",
    "exact_arithmetic": (
        "integer counts; Fraction/Decimal proportions; Decimal precision-50 interval "
        "bounds; all strict decision comparisons after 1e-12 ROUND_HALF_EVEN "
        "quantization of both operands; quantized equality never satisfies a strict "
        "inequality -- PREREG Sec 6.2"
    ),
    "pairing_treatment": (
        "decisional interval unpaired per the frozen H0011 entry (audit-approved, "
        "PREREG Sec 12 R6); the non-decisional paired-Newcombe robustness check is "
        "removed -- AMENDMENT Finding 2"
    ),
    "decision_rule": (
        "1) any gate fails -> EXECUTION BLOCKED; 2) L(D) <= 0 -> Rejected; "
        "3) L(D) > 0 and L(p_pm) > 1/2 -> Confirmed; 4) L(D) > 0 and U(p_pm) < 1/2 -> "
        "Rejected (mechanism refuted); 5) L(D) > 0 and interval straddles 1/2 -> "
        "Inconclusive -- PREREG Sec 7.1"
    ),
    "random_seed": "not applicable -- fully deterministic, no resampling",
    "part_2_scope_statement": PART_2_SCOPE_STATEMENT,
}

PREREGISTRATION_METADATA: dict[str, Any] = {
    "document": "docs/research/preregistrations/PREREG-20260721-H0011-midnight-boundary.md",
    "frozen_date": "2026-07-21",
    "provenance_note": (
        "docs/research/preregistrations/PROVENANCE-20260721-H0011-posthoc-vs-prospective.md"
    ),
    "original_hypotheses_entry": "HYPOTHESES.md H0011, opened 2026-07-21",
    "amendment": {
        "document": "docs/research/preregistrations/AMENDMENT-20260721-H0011-audit-resolution.md",
        "frozen_date": "2026-07-21",
        "resolves": [
            "part2_scope_and_adjacent_reporting (F1, required)",
            "paired_robustness_removed (F2, required)",
            "decimal_construction_and_roundtrip_invariant (F3, required)",
            "manifest_serialization_format (F4, required)",
            "histogram_scope_and_binning (F5, recommended)",
            "gate_failure_sentinel (F6, recommended)",
            "null_convention_for_zero_denominator_ratios (F7, recommended)",
            "entry_wording_discrepancy_documented (F8, documentation)",
            "unresolved_attribution_trigger_list_closed (F9, documentation)",
        ],
        "blocked_on": [],
    },
    "elaboration_flags_for_audit": [
        "excess_wording_resolution (PREREG Sec 6.3/12 R2)",
        "decision_branch_completed_step3 (PREREG Sec 7.1)",
        "unpaired_interval_kept_decisional (PREREG Sec 6.3/12 R6, audit-approved)",
        "part2_discriminating_power (PREREG Sec 12 R1, resolved by AMENDMENT Finding 1)",
    ],
}

DATASET_VERSION = "exp-20260721-h0002"
DATASET_FRAME = "observation_issuances.parquet"
DATASET_BUILD_GIT_COMMIT = "2e76d32065b46b3e470bcf7bf088d393c5d6fc1f"
SOURCE_DB_REVISION = "0005"


def run(dataset_dir: Path) -> dict[str, Any]:
    """Loads the pinned observation_issuances export, runs the frozen
    protocol end to end, and returns the full results dict matching
    TEMPLATE-H0011-manifest.json's schema. `dataset_dir` must contain the
    pinned exp-20260721-h0002 frame + its own manifest.json (G1's
    hash-pin source)."""
    tzdata_version = get_pinned_tzdata_version()  # PREREG Sec 2/10: blocks if unpinned

    dataset_manifest = json.loads((dataset_dir / "manifest.json").read_text())
    frame = pl.read_parquet(dataset_dir / DATASET_FRAME)

    gate_outcome = run_gates(frame, dataset_manifest)

    part1: Part1Result | None = None
    part2: Part2Result | None = None
    if gate_outcome.all_gates_passed:
        assert gate_outcome.variable_days is not None
        assert gate_outcome.revised_tmin_days is not None
        assert gate_outcome.tmin_attributions is not None
        part1 = compute_part1(gate_outcome.variable_days)
        part2 = compute_part2(gate_outcome.revised_tmin_days, gate_outcome.tmin_attributions)

    # PREREG Sec 9/10 evaluation order (integrity-review Finding F2): the
    # Sec 7.1 decision is finalized BEFORE any descriptive output is
    # computed -- descriptives are non-decisional and never precede the
    # verdict.
    verdict = evaluate(gate_outcome, part1, part2)

    descriptive: DescriptiveResults | None = None
    if gate_outcome.all_gates_passed:
        assert gate_outcome.variable_days is not None
        assert gate_outcome.revised_tmin_days is not None
        assert gate_outcome.tmin_attributions is not None
        descriptive = build_descriptive_results(
            gate_outcome.variable_days,
            gate_outcome.revised_tmin_days,
            gate_outcome.tmin_attributions,
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
    }

    results: dict[str, Any] = {
        "experiment": "EXP-20260721-H0011-midnight-boundary",
        "hypothesis": "H0011",
        "preregistration": PREREGISTRATION_METADATA,
        "dataset": {
            "version": DATASET_VERSION,
            "frame": DATASET_FRAME,
            "content_hashes": {"observation_issuances": FROZEN_CONTENT_HASH},
            "row_count_expected": EXPECTED_ROW_COUNT,
            "dataset_build_git_commit": DATASET_BUILD_GIT_COMMIT,
            "source_db_revision": SOURCE_DB_REVISION,
            "analysis_git_commit": None,  # filled in by the CLI wrapper (git rev-parse)
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
        },
        "descriptive_results": descriptive or empty_descriptive,
        "reproducibility_verification": {
            "rerun_byte_identical": None,  # populated post-hoc by a second CLI invocation
            # Integrity-review Finding F3: computed mechanically in-run via
            # the independently re-typed _FROZEN_PREREG_REFERENCE, per the
            # H0005 verify_config_matches_prereg precedent.
            "config_matches_prereg": verify_config_matches_prereg(),
            # PREREG Sec 11's naive-UTC cross-check requires a live database
            # connection and is a manual/investigative pre-execution checklist
            # step -- not computed by run() itself, exactly as H0005's
            # close_time_immutability_verified was handled.
            "issuance_time_utc_verified": None,
            "serialization_policy": (
                "Every non-integer decision-relevant quantity in primary_results is a "
                "JSON string: the Decimal value quantized to 1e-12 via ROUND_HALF_EVEN, "
                "fixed-point notation, exactly 12 digits after the decimal point, no "
                "scientific notation, leading '-' only if negative -- AMENDMENT Finding "
                "4. Integer counts remain plain JSON integers."
            ),
        },
        "date_run": None,
    }
    validate_manifest(results)
    return results


def render_markdown(results: dict[str, Any]) -> str:
    decision = results["primary_results"]["decision"]
    part2 = results["primary_results"]["part_2_post_midnight_attribution"]
    descriptive = results["descriptive_results"]
    return f"""# EXP-20260721-H0011-midnight-boundary

Executed exactly as pre-registered in
`PREREG-20260721-H0011-midnight-boundary.md` and
`AMENDMENT-20260721-H0011-audit-resolution.md`.

## Decision

**{decision["outcome"].upper()}** ({decision["reason"]}).

## Part 2 (adjacent required reporting -- AMENDMENT Finding 1)

{results["config"]["part_2_scope_statement"]}

- tmax attribution rate: {descriptive.get("tmax_attribution_rate")}
- Stratified gap decomposition: {descriptive.get("stratified_gap_decomposition")}
- Part 2 result: {part2}

Full cell-level results: see the accompanying `-results.json`.
"""


if __name__ == "__main__":
    import contextlib
    import subprocess

    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args()

    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    output = run(args.dataset_dir)
    output["date_run"] = _datetime.now(_UTC).date().isoformat()
    with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
        output["dataset"]["analysis_git_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()

    args.out.write_text(json.dumps(output, indent=2, default=str))
    args.out_md.write_text(render_markdown(output))
    print(f"wrote {args.out}")
    print(f"wrote {args.out_md}")
    print(f"decision: {output['primary_results']['decision']['outcome']}")
