"""EXP H0014 -- Recency stability of the tmin/tmax revision asymmetry:
is the 2026 behavior at NYC/CHI sampling variation (A), an operational
artifact (C), a cross-station corroborated change (D), or an
uncorroborated signal needing more data (B)?

Implements exactly what is frozen in:

  - docs/research/preregistrations/PREREG-20260722-H0014-recency-stability.md
  - docs/research/preregistrations/TEMPLATE-H0014-manifest.json

Every constant, formula, and ordering decision cites the section it
implements; nothing here may change without amending those documents.
Fully deterministic -- no randomness, no float ever reaches a decision
comparison. No timezone conversion exists in this design (PREREG Sec 2).

Usage:
    python scripts/exp_h0014_recency_stability.py \
        --dataset-dir "<KALSHI_DATA_DIR>/datasets/exp-20260722-h0013-replication" \
        --out docs/research/experiments/EXP-20260722-H0014-recency-stability-results.json \
        --out-md docs/research/experiments/EXP-20260722-H0014-recency-stability.md
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Any, Literal

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kalshi_weather.dataset.manifest import frame_content_hash

# --- Frozen protocol constants (PREREG Sec 2, 3, 5, 6, 8, 10) ---------------

STATIONS: list[str] = ["CHI", "NYC"]  # PREREG Sec 2; DEN/LAX intentionally excluded
VARIABLES: list[str] = ["tmax_f", "tmin_f"]  # PREREG Sec 2

Z_95 = Decimal("1.959963984540054")  # reporting level, PREREG Sec 5
Z_DECISION = Decimal("2.497705474412374")  # 98.75% Bonferroni (family of 4), PREREG Sec 5
DECIMAL_CONTEXT_PRECISION = 50
COMPARISON_QUANTIZATION = Decimal("1E-12")
VALUE_QUANTIZATION = Decimal("0.01")
ROUNDTRIP_TOLERANCE = 1e-6
SERIALIZATION_QUANTIZATION = Decimal("1E-12")
ZERO = Decimal(0)

SM_YEARS: list[int] = [2023, 2024, 2025, 2026]  # PREREG Sec 3
HIST_YEARS: list[int] = [2023, 2024, 2025]
FY_YEARS: list[int] = [2023, 2024, 2025]
SM_END_MONTH_DAY: tuple[int, int] = (7, 21)  # windows end Jul 21, PREREG Sec 3

G3_MIN_CELL_N = 150  # PREREG Sec 8
G4_MIN_HIST_R = 30  # PREREG Sec 8

OI1_CADENCE_THRESHOLD = Decimal("0.15")  # PREREG Sec 6
OI2_MISSING_THRESHOLD = Decimal("0.05")
OI3_SINGLE_THRESHOLD = Decimal("0.05")

FROZEN_CONTENT_HASH = (
    "5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775"  # PREREG Sec 10, G1
)
EXPECTED_ROW_COUNT = 26310
DATASET_VERSION = "exp-20260722-h0013-replication"
DATASET_FRAME = "observation_issuances.parquet"
DATASET_BUILD_GIT_COMMIT = "9285a0145db39d5829b93b648a1e19e5a32c7237"
SOURCE_DB_REVISION = "0007"
MOTIVATING_RECORD = "docs/research/experiments/EXP-20260722-H0013-chi-replication-results.json"

#: Frozen Sec 7.2 interpretation wording, keyed by outcome.
INTERPRETATIONS: dict[str, str] = {
    "A": (
        "The 2026 behavior at both stations is consistent with the 2023-2025 "
        "season-matched baseline at the decision level; H0013's fig-4 observation is "
        "attributable to seasonal composition (Jan-Jul vs full-year) and/or sampling "
        "variation in an n~198 cell. The H0011/H0013 asymmetry stands as published, "
        "with the ~8pp-scale detectability limit of PREREG Sec 8 named. Downstream "
        "consumers may rely on the pooled asymmetry, re-checking after full-2026 data "
        "exists."
    ),
    "B": (
        "At least one station shows a decision-level shift without cross-station "
        "corroboration or operational co-occurrence. The asymmetry's current magnitude "
        "is unreliable at the shifted station(s); downstream work must not assume it "
        "there. Retry with full-2026 windows (FY(2026) vs FY(2023-2025), a new "
        "pre-registered entry) on or after 2027-01-15."
    ),
    "C": (
        "The shift co-occurs with a measured collection/reporting change (the specific "
        "OI(s) are named in the report). The result is evidence about the "
        "archive/reporting pipeline, not about label physics; the follow-up is an "
        "operational investigation (raw-payload inspection for the affected window) "
        "before any physics-flavored hypothesis."
    ),
    "D": (
        "A same-direction shift replicates across the two stations with no registered "
        "operational co-occurrence -- evidence the label-formation process itself "
        "changed in 2026. H0011/H0013's conclusions retain their 2023-2026 pooled "
        "validity but their forward-looking use is suspended pending a full-2026 "
        "re-run; downstream consumers must treat variable-specific label noise as "
        "time-varying."
    ),
    "blocked": ("An eligibility gate failed; not a scientific outcome. See eligibility_gates."),
}

#: Independently re-typed frozen values -- a genuine drift check
#: (H0005/H0011/H0013 precedent).
_FROZEN_PREREG_REFERENCE: dict[str, Any] = {
    "stations": ["CHI", "NYC"],
    "variables": ["tmax_f", "tmin_f"],
    "z_reporting": "1.959963984540054",
    "z_decision": "2.497705474412374",
    "decimal_context_precision": 50,
    "comparison_quantization": "1E-12",
    "value_quantization": "0.01",
    "sm_years": [2023, 2024, 2025, 2026],
    "hist_years": [2023, 2024, 2025],
    "sm_end": (7, 21),
    "g3_min_cell_n": 150,
    "g4_min_hist_r": 30,
    "oi1": "0.15",
    "oi2": "0.05",
    "oi3": "0.05",
    "frozen_content_hash": "5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775",
    "dataset_version": "exp-20260722-h0013-replication",
}


def verify_config_matches_prereg() -> bool:
    ref = _FROZEN_PREREG_REFERENCE
    checks = [
        ref["stations"] == STATIONS,
        ref["variables"] == VARIABLES,
        str(Z_95) == ref["z_reporting"],
        str(Z_DECISION) == ref["z_decision"],
        ref["decimal_context_precision"] == DECIMAL_CONTEXT_PRECISION,
        str(COMPARISON_QUANTIZATION) == ref["comparison_quantization"],
        str(VALUE_QUANTIZATION) == ref["value_quantization"],
        ref["sm_years"] == SM_YEARS,
        ref["hist_years"] == HIST_YEARS,
        ref["sm_end"] == SM_END_MONTH_DAY,
        ref["g3_min_cell_n"] == G3_MIN_CELL_N,
        ref["g4_min_hist_r"] == G4_MIN_HIST_R,
        str(OI1_CADENCE_THRESHOLD) == ref["oi1"],
        str(OI2_MISSING_THRESHOLD) == ref["oi2"],
        str(OI3_SINGLE_THRESHOLD) == ref["oi3"],
        ref["frozen_content_hash"] == FROZEN_CONTENT_HASH,
        ref["dataset_version"] == DATASET_VERSION,
    ]
    return all(checks)


def rederive_decision_z() -> bool:
    """PREREG Sec 11: recompute PHI^-1(0.99375) by bisection on
    NormalDist.cdf (a second method, independent of inv_cdf) and compare
    to the frozen literal to >=12 significant digits."""
    from statistics import NormalDist

    nd = NormalDist()
    lo, hi = 2.0, 3.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if nd.cdf(mid) < 0.99375:
            lo = mid
        else:
            hi = mid
    return abs((lo + hi) / 2 - float(Z_DECISION)) < 1e-12


# --- Decimal / serialization policy (inherited F3/F4) ------------------------


def quantize_value(raw: float) -> Decimal:
    return Decimal(str(raw)).quantize(VALUE_QUANTIZATION, rounding=ROUND_HALF_EVEN)


def value_roundtrips(raw: float) -> bool:
    return abs(float(quantize_value(raw)) - raw) < ROUNDTRIP_TOLERANCE


def quantize_for_comparison(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        return value.quantize(COMPARISON_QUANTIZATION, rounding=ROUND_HALF_EVEN)


def strictly_greater(a: Decimal, b: Decimal) -> bool:
    return quantize_for_comparison(a) > quantize_for_comparison(b)


def strictly_less(a: Decimal, b: Decimal) -> bool:
    return quantize_for_comparison(a) < quantize_for_comparison(b)


def at_least(a: Decimal, threshold: Decimal) -> bool:
    """PREREG Sec 6: '>= T' means NOT strictly-less than T; exactly-at-
    threshold fires."""
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


# --- Statistical methods (PREREG Sec 5) --------------------------------------


def wilson_interval(x: int, n: int, z: Decimal = Z_95) -> tuple[Decimal, Decimal]:
    if n <= 0:
        raise ValueError(f"wilson_interval requires n > 0, got {n}")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        n_dec = Decimal(n)
        p_hat = Decimal(x) / n_dec
        z2 = z * z
        denom = 1 + z2 / n_dec
        center = (p_hat + z2 / (2 * n_dec)) / denom
        inside = p_hat * (1 - p_hat) / n_dec + z2 / (4 * n_dec * n_dec)
        halfwidth = z * inside.sqrt() / denom
        return (center - halfwidth, center + halfwidth)


def newcombe_interval(
    *, x1: int, n1: int, x2: int, n2: int, z: Decimal = Z_95
) -> tuple[Decimal, Decimal]:
    """Newcombe (1998) hybrid score interval on (p1 - p2), independent
    samples, no continuity correction. Sample 1 = 2026 (or tmin for gaps),
    sample 2 = hist (or tmax) -- PREREG Sec 4/5 sign conventions."""
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        l1, u1 = wilson_interval(x1, n1, z)
        l2, u2 = wilson_interval(x2, n2, z)
        p1 = Decimal(x1) / Decimal(n1)
        p2 = Decimal(x2) / Decimal(n2)
        diff = p1 - p2
        lower = diff - ((p1 - l1) ** 2 + (u2 - p2) ** 2).sqrt()
        upper = diff + ((u1 - p1) ** 2 + (p2 - l2) ** 2).sqrt()
        return (lower, upper)


# --- Windows (PREREG Sec 3; fixed, no adaptive splitting) --------------------


def sm_window(year: int) -> tuple[date, date]:
    return (date(year, 1, 1), date(year, SM_END_MONTH_DAY[0], SM_END_MONTH_DAY[1]))


def fy_window(year: int) -> tuple[date, date]:
    return (date(year, 1, 1), date(year, 12, 31))


def in_window(d: date, window: tuple[date, date]) -> bool:
    return window[0] <= d <= window[1]


def calendar_days(window: tuple[date, date]) -> int:
    return (window[1] - window[0]).days + 1


# --- Variable-day records ----------------------------------------------------


@dataclass(slots=True)
class DayRecord:
    station: str
    variable: str
    observation_date: date
    n_issuances: int
    revised: bool


def build_day_records(frame: pl.DataFrame) -> list[DayRecord]:
    """One record per (station, variable, observation_date), from the frame
    sorted by the frozen order (PREREG Sec 10). Revision = first vs final
    quantized value."""
    sorted_frame = frame.sort(["station_id", "variable", "observation_date", "issuance_time"])
    records: dict[tuple[str, str, date], list[Decimal]] = {}
    for row in sorted_frame.iter_rows(named=True):
        key = (row["station_id"], row["variable"], row["observation_date"])
        records.setdefault(key, []).append(quantize_value(row["value"]))
    return [
        DayRecord(
            station=k[0],
            variable=k[1],
            observation_date=k[2],
            n_issuances=len(values),
            revised=values[0] != values[-1],
        )
        for k, values in sorted(records.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2]))
    ]


def cell_counts(
    records: list[DayRecord], station: str, variable: str, window: tuple[date, date]
) -> tuple[int, int]:
    """(n valid variable-days, r revised) for a station/variable/window."""
    n = r = 0
    for rec in records:
        if (
            rec.station == station
            and rec.variable == variable
            and in_window(rec.observation_date, window)
        ):
            n += 1
            if rec.revised:
                r += 1
    return n, r


def hist_counts(records: list[DayRecord], station: str, variable: str) -> tuple[int, int]:
    n = r = 0
    for year in HIST_YEARS:
        cn, cr = cell_counts(records, station, variable, sm_window(year))
        n += cn
        r += cr
    return n, r


# --- Eligibility gates (PREREG Sec 8) ----------------------------------------

NOT_EVALUATED: Literal["not_evaluated"] = "not_evaluated"


@dataclass(slots=True)
class GateOutcome:
    gates: dict[str, Any]
    all_gates_passed: bool
    records: list[DayRecord] | None


def run_gates(full_frame: pl.DataFrame) -> GateOutcome:
    """G1 (full-frame hash) -> {CHI, NYC} filter -> G2 -> G3 -> G4, strictly
    in order; NOT_EVALUATED sentinels on short-circuit (F6 inherited)."""
    gates: dict[str, Any] = {
        "g1_frame_hash_matches": NOT_EVALUATED,
        "g2_invariants": {
            "natural_key_unique": NOT_EVALUATED,
            "no_nulls": NOT_EVALUATED,
            "station_set_exact": NOT_EVALUATED,
            "variable_set_exact": NOT_EVALUATED,
            "value_roundtrip_verified": NOT_EVALUATED,
        },
        "g3_sm_cell_min_n": NOT_EVALUATED,
        "g4_hist_min_r": NOT_EVALUATED,
        "all_gates_passed": False,
    }

    recomputed_hash = frame_content_hash(full_frame)
    g1_pass = recomputed_hash == FROZEN_CONTENT_HASH
    gates["g1_frame_hash_matches"] = g1_pass
    if not g1_pass:
        return GateOutcome(gates, False, None)

    frame = full_frame.filter(pl.col("station_id").is_in(STATIONS))

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
    station_set_exact = set(frame["station_id"].unique().to_list()) == set(STATIONS)
    variable_set_exact = set(frame["variable"].unique().to_list()) == set(VARIABLES)
    value_roundtrip_verified = all(
        value_roundtrips(v) for v in frame["value"].to_list() if v is not None
    )
    g2 = {
        "natural_key_unique": natural_key_unique,
        "no_nulls": no_nulls,
        "station_set_exact": station_set_exact,
        "variable_set_exact": variable_set_exact,
        "value_roundtrip_verified": value_roundtrip_verified,
    }
    gates["g2_invariants"] = g2
    if not all(g2.values()):
        return GateOutcome(gates, False, None)

    records = build_day_records(frame)

    g3_detail: dict[str, dict[str, dict[str, int]]] = {}
    g3_pass = True
    for station in STATIONS:
        g3_detail[station] = {}
        for variable in VARIABLES:
            g3_detail[station][variable] = {}
            for year in SM_YEARS:
                n, _ = cell_counts(records, station, variable, sm_window(year))
                g3_detail[station][variable][str(year)] = n
                if n < G3_MIN_CELL_N:
                    g3_pass = False
    gates["g3_sm_cell_min_n"] = g3_detail
    if not g3_pass:
        return GateOutcome(gates, False, records)

    g4_detail: dict[str, dict[str, int]] = {}
    g4_pass = True
    for station in STATIONS:
        g4_detail[station] = {}
        for variable in VARIABLES:
            _, r_hist = hist_counts(records, station, variable)
            g4_detail[station][variable] = r_hist
            if r_hist < G4_MIN_HIST_R:
                g4_pass = False
    gates["g4_hist_min_r"] = g4_detail
    gates["all_gates_passed"] = g4_pass
    return GateOutcome(gates, g4_pass, records)


# --- Primary: contrasts, shift events, OIs, decision (PREREG Sec 4-7) --------


def compute_contrast(records: list[DayRecord], station: str, variable: str) -> dict[str, Any]:
    n_2026, r_2026 = cell_counts(records, station, variable, sm_window(2026))
    n_hist, r_hist = hist_counts(records, station, variable)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        p_2026 = Decimal(r_2026) / Decimal(n_2026)
        p_hist = Decimal(r_hist) / Decimal(n_hist)
        delta = p_2026 - p_hist
    ci95 = newcombe_interval(x1=r_2026, n1=n_2026, x2=r_hist, n2=n_hist, z=Z_95)
    ci_dec = newcombe_interval(x1=r_2026, n1=n_2026, x2=r_hist, n2=n_hist, z=Z_DECISION)
    shift = strictly_greater(ci_dec[0], ZERO) or strictly_less(ci_dec[1], ZERO)
    if strictly_greater(delta, ZERO):
        direction = "up"
    elif strictly_less(delta, ZERO):
        direction = "down"
    else:
        direction = "zero"
    return {
        "n_2026": n_2026,
        "r_2026": r_2026,
        "p_2026": serialize_decimal(p_2026),
        "n_hist": n_hist,
        "r_hist": r_hist,
        "p_hist": serialize_decimal(p_hist),
        "delta_hat": serialize_decimal(delta),
        "ci95_newcombe": serialize_ci(*ci95),
        "ci9875_newcombe": serialize_ci(*ci_dec),
        "shift_event": shift,
        "direction": direction,
    }


def station_window_diagnostics(
    records: list[DayRecord], station: str, windows: dict[str, list[tuple[date, date]]]
) -> dict[str, Any]:
    """PREREG Sec 6 diagnostics for one station, per named window (a named
    window may pool several date ranges, e.g. hist)."""
    out: dict[str, Any] = {}
    for name, ranges in windows.items():
        recs = [
            rec
            for rec in records
            if rec.station == station and any(in_window(rec.observation_date, w) for w in ranges)
        ]
        n_vd = len(recs)
        issuance_rows = sum(rec.n_issuances for rec in recs)
        single = sum(1 for rec in recs if rec.n_issuances == 1)
        observed_days = len({rec.observation_date for rec in recs})
        expected_days = sum(calendar_days(w) for w in ranges)
        missing = expected_days - observed_days
        dist: dict[str, int] = {}
        for rec in recs:
            dist[str(rec.n_issuances)] = dist.get(str(rec.n_issuances), 0) + 1
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            cadence = Decimal(issuance_rows) / Decimal(n_vd) if n_vd else None
            single_frac = Decimal(single) / Decimal(n_vd) if n_vd else None
            missing_frac = Decimal(missing) / Decimal(expected_days)
        out[name] = {
            "variable_days": n_vd,
            "issuance_rows": issuance_rows,
            "mean_issuances_per_day": serialize_decimal(cadence) if cadence is not None else None,
            "single_issuance_days": single,
            "single_issuance_fraction": (
                serialize_decimal(single_frac) if single_frac is not None else None
            ),
            "expected_days": expected_days,
            "observed_days": observed_days,
            "missing_days": missing,
            "missing_fraction": serialize_decimal(missing_frac),
            "issuance_count_distribution": dict(sorted(dist.items())),
        }
    return out


def compute_operational_indicators(
    diagnostics: dict[str, dict[str, Any]], shifted_stations: list[str]
) -> dict[str, Any] | str:
    """PREREG Sec 6: evaluated only at stations with >=1 shift event.
    Quantized; exactly-at-threshold FIRES."""
    if not shifted_stations:
        return NOT_EVALUATED
    out: dict[str, Any] = {}
    for station in shifted_stations:
        d26 = diagnostics[station]["sm_2026"]
        dh = diagnostics[station]["hist"]
        cadence_diff = abs(
            Decimal(d26["mean_issuances_per_day"]) - Decimal(dh["mean_issuances_per_day"])
        )
        missing_diff = abs(Decimal(d26["missing_fraction"]) - Decimal(dh["missing_fraction"]))
        single_diff = abs(
            Decimal(d26["single_issuance_fraction"]) - Decimal(dh["single_issuance_fraction"])
        )
        out[station] = {
            "oi1_cadence": {
                "value_2026": d26["mean_issuances_per_day"],
                "value_hist": dh["mean_issuances_per_day"],
                "abs_diff": serialize_decimal(cadence_diff),
                "fired": at_least(cadence_diff, OI1_CADENCE_THRESHOLD),
            },
            "oi2_missing_fraction": {
                "value_2026": d26["missing_fraction"],
                "value_hist": dh["missing_fraction"],
                "abs_diff": serialize_decimal(missing_diff),
                "fired": at_least(missing_diff, OI2_MISSING_THRESHOLD),
            },
            "oi3_single_issuance_fraction": {
                "value_2026": d26["single_issuance_fraction"],
                "value_hist": dh["single_issuance_fraction"],
                "abs_diff": serialize_decimal(single_diff),
                "fired": at_least(single_diff, OI3_SINGLE_THRESHOLD),
            },
        }
    return out


@dataclass(slots=True)
class Verdict:
    outcome: Literal["A", "B", "C", "D", "blocked"]
    evaluation_step: str
    reason: str


def evaluate(
    gate_outcome: GateOutcome,
    contrasts: dict[str, dict[str, dict[str, Any]]] | None,
    operational: dict[str, Any] | str | None,
) -> Verdict:
    """PREREG Sec 7.1's exact ordered table, each row terminal."""
    if not gate_outcome.all_gates_passed:
        return Verdict("blocked", "step_0", "an eligibility gate failed; no interval computed")
    assert contrasts is not None and operational is not None

    shift_sets = {s: [v for v in VARIABLES if contrasts[s][v]["shift_event"]] for s in STATIONS}
    any_shift = any(shift_sets[s] for s in STATIONS)
    if not any_shift:
        return Verdict(
            "A", "step_1", "no shift event at either station (all 4 decision CIs include 0)"
        )

    if isinstance(operational, dict):
        for station, ois in operational.items():
            if shift_sets[station] and any(oi["fired"] for oi in ois.values()):
                fired = [name for name, oi in ois.items() if oi["fired"]]
                return Verdict(
                    "C",
                    "step_2",
                    f"shift event(s) at {station} co-occur with fired operational "
                    f"indicator(s): {', '.join(fired)}",
                )

    for v in VARIABLES:
        if v in shift_sets["CHI"] and v in shift_sets["NYC"]:
            d_chi = contrasts["CHI"][v]["direction"]
            d_nyc = contrasts["NYC"][v]["direction"]
            if d_chi == d_nyc and d_chi != "zero":
                return Verdict(
                    "D",
                    "step_3",
                    f"{v} shifts {d_chi} at both stations with no operational "
                    "co-occurrence (cross-station corroborated)",
                )

    return Verdict(
        "B",
        "step_4",
        "shift event(s) exist but are not cross-station corroborated in the same "
        "direction and no operational indicator fired",
    )


# --- Descriptives (PREREG Sec 9; non-decisional) -----------------------------


def rate_cell(n: int, r: int) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        rate = Decimal(r) / Decimal(n) if n else None
    lo, hi = wilson_interval(r, n) if n else (None, None)
    return {
        "n": n,
        "r": r,
        "rate": serialize_decimal(rate) if rate is not None else None,
        "ci95_wilson": serialize_ci(lo, hi) if lo is not None and hi is not None else None,
    }


def gap_cell(
    records: list[DayRecord], station: str, ranges: list[tuple[date, date]]
) -> dict[str, Any]:
    def pooled(variable: str) -> tuple[int, int]:
        n = r = 0
        for w in ranges:
            cn, cr = cell_counts(records, station, variable, w)
            n += cn
            r += cr
        return n, r

    n_tmax, r_tmax = pooled("tmax_f")
    n_tmin, r_tmin = pooled("tmin_f")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        d = Decimal(r_tmin) / Decimal(n_tmin) - Decimal(r_tmax) / Decimal(n_tmax)
    lo, hi = newcombe_interval(x1=r_tmin, n1=n_tmin, x2=r_tmax, n2=n_tmax, z=Z_95)
    return {"d_hat": serialize_decimal(d), "ci95_newcombe": serialize_ci(lo, hi)}


def build_descriptives(records: list[DayRecord]) -> dict[str, Any]:
    cells_sm: dict[str, Any] = {}
    cells_fy: dict[str, Any] = {}
    cells_hist: dict[str, Any] = {}
    gaps_sm: dict[str, Any] = {}
    monthly: dict[str, Any] = {}
    for station in STATIONS:
        cells_sm[station] = {}
        cells_fy[station] = {}
        cells_hist[station] = {}
        for variable in VARIABLES:
            cells_sm[station][variable] = {
                str(year): rate_cell(*cell_counts(records, station, variable, sm_window(year)))
                for year in SM_YEARS
            }
            cells_fy[station][variable] = {
                str(year): rate_cell(*cell_counts(records, station, variable, fy_window(year)))
                for year in FY_YEARS
            }
            cells_hist[station][variable] = rate_cell(*hist_counts(records, station, variable))
        gaps_sm[station] = {
            str(year): gap_cell(records, station, [sm_window(year)]) for year in SM_YEARS
        }
        gaps_sm[station]["hist"] = gap_cell(records, station, [sm_window(y) for y in HIST_YEARS])

        monthly[station] = {}
        for variable in VARIABLES:
            monthly[station][variable] = {}
            for month in range(1, 8):
                hist_n = hist_r = n26 = r26 = 0
                for rec in records:
                    if rec.station != station or rec.variable != variable:
                        continue
                    d = rec.observation_date
                    if d.month != month:
                        continue
                    if any(in_window(d, sm_window(y)) for y in HIST_YEARS):
                        hist_n += 1
                        hist_r += int(rec.revised)
                    elif in_window(d, sm_window(2026)):
                        n26 += 1
                        r26 += int(rec.revised)
                with localcontext() as ctx:
                    ctx.prec = DECIMAL_CONTEXT_PRECISION
                    hist_rate = Decimal(hist_r) / Decimal(hist_n) if hist_n else None
                    rate26 = Decimal(r26) / Decimal(n26) if n26 else None
                monthly[station][variable][str(month)] = {
                    "hist": {
                        "n": hist_n,
                        "r": hist_r,
                        "rate": serialize_decimal(hist_rate) if hist_rate is not None else None,
                    },
                    "y2026": {
                        "n": n26,
                        "r": r26,
                        "rate": serialize_decimal(rate26) if rate26 is not None else None,
                    },
                }

    all_ranges = [sm_window(y) for y in SM_YEARS] + [fy_window(y) for y in FY_YEARS]
    excluded = sum(
        1 for rec in records if not any(in_window(rec.observation_date, w) for w in all_ranges)
    )
    return {
        "cells_sm": cells_sm,
        "cells_fy": cells_fy,
        "cells_hist_pooled": cells_hist,
        "gaps_sm": gaps_sm,
        "monthly_rates": monthly,
        "excluded_days": excluded,
    }


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
        "inherited_conventions",
        "elaboration_flags_for_audit",
    },
    "dataset": {
        "version",
        "frame",
        "content_hashes",
        "row_count_expected",
        "dataset_build_git_commit",
        "source_db_revision",
        "motivating_record_reference",
        "analysis_git_commit",
    },
    "config": {
        "stations",
        "variables",
        "unit_of_analysis",
        "revision_definition",
        "windows",
        "timezone",
        "estimands",
        "ci_method",
        "z_reporting",
        "z_decision",
        "shift_event",
        "operational_indicators",
        "decision_rule",
        "exact_arithmetic",
        "random_seed",
        "software_versions",
    },
    "eligibility_gates": {
        "g1_frame_hash_matches",
        "g2_invariants",
        "g3_sm_cell_min_n",
        "g4_hist_min_r",
        "all_gates_passed",
    },
    "primary_results": {"contrasts", "shift_sets", "operational_indicators", "decision"},
    "primary_results.decision": {"outcome", "evaluation_step", "reason", "interpretation"},
    "descriptive_results": {
        "cells_sm",
        "cells_fy",
        "cells_hist_pooled",
        "gaps_sm",
        "contrasts_reporting_level",
        "diagnostics",
        "monthly_rates",
        "excluded_days",
    },
    "reproducibility_verification": {
        "rerun_byte_identical",
        "config_matches_prereg",
        "decision_z_rederived",
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
    outcome = results["primary_results"]["decision"]["outcome"]
    if outcome not in ("A", "B", "C", "D", "blocked"):
        raise ManifestValidationError(f"unrecognized decision outcome: {outcome!r}")


# --- Orchestration -----------------------------------------------------------

CONFIG_BLOCK: dict[str, Any] = {
    "stations": list(STATIONS),
    "variables": list(VARIABLES),
    "unit_of_analysis": (
        "variable-day (station_id, variable, observation_date); >=1 issuance valid; "
        "single-issuance days unrevised"
    ),
    "revision_definition": (
        "first-issued value != final-issued value, Decimal-quantized to 0.01; "
        "Decimal(str(v)) construction; ROUND_HALF_EVEN; round-trip invariant < 1e-6 under G2"
    ),
    "windows": (
        "SM(y) = [y-01-01, y-07-21] for y in 2023..2026 (2026 YTD == SM(2026)); hist = "
        "pooled SM(2023..2025), decisional; FY(2023..2025) tables only; 2022-12-31 "
        "excluded and counted -- PREREG Sec 3, no adaptive splitting"
    ),
    "timezone": (
        "none -- windows are calendar dates of observation_date; no local-time "
        "computation exists in this design (PREREG Sec 2)"
    ),
    "estimands": (
        "Delta_{v,s} = p_{v,s,SM(2026)} - p_{v,s,hist}, the declared family of 4 "
        "(2 stations x 2 variables); independent samples (disjoint day sets); "
        "positive = 2026 revises more"
    ),
    "ci_method": (
        "Newcombe (1998) hybrid score interval (contrasts, gaps) and Wilson score "
        "interval (rates), no continuity correction -- PREREG Sec 5"
    ),
    "z_reporting": f"{Z_95} (95%, frozen literal)",
    "z_decision": (
        f"{Z_DECISION} (98.75% per contrast = Bonferroni family-wise 0.05 over 4; "
        "frozen literal, independently re-derived at implementation per PREREG Sec 11)"
    ),
    "shift_event": (
        "98.75% Newcombe CI on Delta excludes 0 (quantized); direction = sign of point "
        "estimate; touching 0 is NOT a shift"
    ),
    "operational_indicators": (
        "evaluated only at stations with >=1 shift event; OI-1 |cadence diff| >= 0.15; "
        "OI-2 |missing-day fraction diff| >= 0.05; OI-3 |single-issuance fraction "
        "diff| >= 0.05; quantized; exactly-at-threshold FIRES -- PREREG Sec 6"
    ),
    "decision_rule": (
        "0) gate fails -> BLOCKED; 1) no shift events -> A; 2) shifts and any OI fires "
        "at a shifted station -> C; 3) same-variable same-direction shifts at both "
        "stations -> D; 4) otherwise -> B (retry >= 2027-01-15) -- PREREG Sec 7.1"
    ),
    "exact_arithmetic": (
        "integer counts; Decimal precision-50 bounds; strict decisions after 1e-12 "
        "ROUND_HALF_EVEN quantization of both operands; fixed-point 12-digit serialization"
    ),
    "random_seed": "not applicable -- fully deterministic, no resampling",
}

PREREGISTRATION_METADATA: dict[str, Any] = {
    "document": "docs/research/preregistrations/PREREG-20260722-H0014-recency-stability.md",
    "frozen_date": "2026-07-22",
    "provenance_note": (
        "PREREG Sec 0: CHI full-year cells are published motivating data (partial "
        "post-hoc); NYC by-variable yearly cells and all season-matched cells were "
        "never computed pre-freeze"
    ),
    "original_hypotheses_entry": "HYPOTHESES.md H0014, opened 2026-07-22",
    "inherited_conventions": {
        "documents": [
            "AMENDMENT-20260721-H0011-audit-resolution.md (F3 decimal policy, F4 "
            "serialization, F6 gate sentinel, F7 null ratios)",
            "PREREG-20260722-H0013-chi-replication.md (dataset pin, revision "
            "definition, exact-arithmetic policy)",
        ],
        "blocked_on": [],
    },
    "elaboration_flags_for_audit": [
        "decision_family_bonferroni_z_frozen (PREREG Sec 5)",
        "season_matched_windows_decisional_full_year_tables_only (PREREG Sec 3)",
        "operational_indicators_cooccurrence_not_causation (PREREG Sec 6)",
        "cross_station_corroboration_as_interaction_rendering (PREREG Sec 7.1 step 3)",
        "chi_partial_posthoc_disclosed (PREREG Sec 0/12 R1)",
    ],
}


def run(dataset_dir: Path) -> dict[str, Any]:
    full_frame = pl.read_parquet(dataset_dir / DATASET_FRAME)
    gate_outcome = run_gates(full_frame)

    contrasts: dict[str, dict[str, dict[str, Any]]] | None = None
    operational: dict[str, Any] | str | None = None
    diagnostics: dict[str, dict[str, Any]] | None = None
    if gate_outcome.all_gates_passed:
        assert gate_outcome.records is not None
        records = gate_outcome.records
        contrasts = {s: {v: compute_contrast(records, s, v) for v in VARIABLES} for s in STATIONS}
        window_spec: dict[str, list[tuple[date, date]]] = {
            **{f"sm_{y}": [sm_window(y)] for y in SM_YEARS},
            "hist": [sm_window(y) for y in HIST_YEARS],
            **{f"fy_{y}": [fy_window(y)] for y in FY_YEARS},
        }
        diagnostics = {s: station_window_diagnostics(records, s, window_spec) for s in STATIONS}
        shifted = [s for s in STATIONS if any(contrasts[s][v]["shift_event"] for v in VARIABLES)]
        operational = compute_operational_indicators(diagnostics, shifted)

    # PREREG Sec 10 evaluation order: the Sec 7.1 decision is finalized
    # before Sec 9 descriptives are assembled.
    verdict = evaluate(gate_outcome, contrasts, operational)

    descriptive: dict[str, Any]
    if gate_outcome.all_gates_passed:
        assert gate_outcome.records is not None and contrasts is not None
        assert diagnostics is not None
        descriptive = build_descriptives(gate_outcome.records)
        descriptive["contrasts_reporting_level"] = {
            s: {
                v: {
                    "delta_hat": contrasts[s][v]["delta_hat"],
                    "ci95_newcombe": contrasts[s][v]["ci95_newcombe"],
                }
                for v in VARIABLES
            }
            for s in STATIONS
        }
        descriptive["diagnostics"] = diagnostics
    else:
        descriptive = {
            "cells_sm": {},
            "cells_fy": {},
            "cells_hist_pooled": {},
            "gaps_sm": {},
            "contrasts_reporting_level": {},
            "diagnostics": {},
            "monthly_rates": {},
            "excluded_days": 0,
        }

    empty_contrasts = {s: {v: {} for v in VARIABLES} for s in STATIONS}
    shift_sets = (
        {s: [v for v in VARIABLES if contrasts[s][v]["shift_event"]] for s in STATIONS}
        if contrasts is not None
        else {s: [] for s in STATIONS}
    )

    results: dict[str, Any] = {
        "experiment": "EXP-20260722-H0014-recency-stability",
        "hypothesis": "H0014",
        "preregistration": PREREGISTRATION_METADATA,
        "dataset": {
            "version": DATASET_VERSION,
            "frame": DATASET_FRAME,
            "content_hashes": {"observation_issuances": FROZEN_CONTENT_HASH},
            "row_count_expected": EXPECTED_ROW_COUNT,
            "dataset_build_git_commit": DATASET_BUILD_GIT_COMMIT,
            "source_db_revision": SOURCE_DB_REVISION,
            "motivating_record_reference": MOTIVATING_RECORD,
            "analysis_git_commit": None,
        },
        "config": {**CONFIG_BLOCK, "software_versions": None},
        "eligibility_gates": gate_outcome.gates,
        "primary_results": {
            "contrasts": contrasts if contrasts is not None else empty_contrasts,
            "shift_sets": shift_sets,
            "operational_indicators": operational if operational is not None else NOT_EVALUATED,
            "decision": {
                "outcome": verdict.outcome,
                "evaluation_step": verdict.evaluation_step,
                "reason": verdict.reason,
                "interpretation": INTERPRETATIONS[verdict.outcome],
            },
        },
        "descriptive_results": descriptive,
        "reproducibility_verification": {
            "rerun_byte_identical": None,  # populated after the second invocation
            "config_matches_prereg": verify_config_matches_prereg(),
            "decision_z_rederived": rederive_decision_z(),
            "serialization_policy": (
                "Every non-integer decision-relevant quantity is a JSON string: Decimal "
                "quantized to 1e-12 via ROUND_HALF_EVEN, fixed-point, 12 digits after "
                "the point -- H0011 AMENDMENT Finding 4, inherited. Integer counts "
                "remain plain JSON integers."
            ),
        },
        "date_run": None,
    }
    validate_manifest(results)
    return results


def _md_cell_table(cells: dict[str, Any], years: list[int]) -> str:
    lines = ["| station | variable | " + " | ".join(str(y) for y in years) + " |"]
    lines.append("|---|---|" + "---|" * len(years))
    for station in STATIONS:
        for variable in VARIABLES:
            row = [station, variable]
            for y in years:
                cell = cells[station][variable][str(y)]
                pct = float(Decimal(cell["rate"])) * 100
                row.append(f"{cell['r']}/{cell['n']} ({pct:.1f}%)")
            lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render_markdown(results: dict[str, Any]) -> str:
    decision = results["primary_results"]["decision"]
    contrasts = results["primary_results"]["contrasts"]
    d = results["descriptive_results"]

    contrast_lines = []
    for station in STATIONS:
        for variable in VARIABLES:
            c = contrasts[station][variable]
            if not c:
                continue
            contrast_lines.append(
                f"- {station} {variable}: Δ = {c['delta_hat']} "
                f"(95% {c['ci95_newcombe']}; decision 98.75% {c['ci9875_newcombe']}) "
                f"→ shift_event = {c['shift_event']} ({c['direction']})"
            )

    sm_table = _md_cell_table(d["cells_sm"], SM_YEARS) if d["cells_sm"] else "(blocked)"
    fy_table = _md_cell_table(d["cells_fy"], FY_YEARS) if d["cells_fy"] else "(blocked)"

    gaps_lines = []
    if d["gaps_sm"]:
        for station in STATIONS:
            per_year = ", ".join(f"{y}: {d['gaps_sm'][station][str(y)]['d_hat']}" for y in SM_YEARS)
            gaps_lines.append(
                f"- {station}: {per_year}; hist: {d['gaps_sm'][station]['hist']['d_hat']}"
            )

    return f"""# EXP-20260722-H0014-recency-stability

Executed exactly as pre-registered in
`PREREG-20260722-H0014-recency-stability.md`.

## Decision

**{decision["outcome"]}** ({decision["evaluation_step"]}: {decision["reason"]}).

{decision["interpretation"]}

## The declared family of 4 contrasts (2026 vs pooled 2023-2025, season-matched)

{chr(10).join(contrast_lines)}

## Season-matched yearly cells (Jan 1 - Jul 21; decisional windows)

{sm_table}

## Full-year cells (tables only, never decisional)

{fy_table}

## Season-matched gap D = p_tmin - p_tmax per year

{chr(10).join(gaps_lines)}

Full cell-level results, diagnostics, and operational indicators: see the
accompanying `-results.json`. Figures, interpretation narrative, and
limitations: `docs/research/postmortems/2026-07-22-h0014-closeout.md`.
"""


if __name__ == "__main__":
    import contextlib
    import subprocess

    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--raw", action="store_true")
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
