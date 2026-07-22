"""EXP H0015 -- Confirmatory test of the late-maximum composition
mechanism on station-disjoint evaluation data (DEN, LAX), with CHI/NYC
as prespecified non-decisional reference cohorts.

Implements exactly what is frozen in:

  - docs/research/preregistrations/PREREG-20260722-H0015-late-max-mechanism.md
  - docs/research/preregistrations/TEMPLATE-H0015-manifest.json

Fully deterministic; no randomness; no float reaches a decision
comparison; no timezone conversion exists (occurrence hours and cutoffs
are the products' own local wall-clock values).

Usage:
    python scripts/exp_h0015_late_max_mechanism.py \
        --issuance-dir "<KALSHI_DATA_DIR>/datasets/exp-20260722-h0013-replication" \
        --extract-dir "<KALSHI_DATA_DIR>/datasets/exp-20260722-h0015-occurrence" \
        --out docs/research/experiments/EXP-20260722-H0015-late-max-mechanism-results.json \
        --out-md docs/research/experiments/EXP-20260722-H0015-late-max-mechanism.md
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

# --- Frozen protocol constants (PREREG Sec 2, 4, 5, 8) -----------------------

ALL_STATIONS: list[str] = ["CHI", "DEN", "LAX", "NYC"]
EVAL_STATIONS: list[str] = ["DEN", "LAX"]  # PREREG Sec 2, decisional
REF_STATIONS: list[str] = ["CHI", "NYC"]  # non-decisional reference cohorts

Z_95 = Decimal("1.959963984540054")
DECIMAL_CONTEXT_PRECISION = 50
COMPARISON_QUANTIZATION = Decimal("1E-12")
VALUE_QUANTIZATION = Decimal("0.01")
ROUNDTRIP_TOLERANCE = 1e-6
SERIALIZATION_QUANTIZATION = Decimal("1E-12")
ZERO = Decimal(0)

SEP_STRONG_MARGIN = Decimal("0.10")  # PREREG Sec 5
EARLY_FLOOR_UPPER = Decimal("0.15")
AF_MIN = Decimal("0.50")

G3_MIN_PARSE_OK = Decimal("0.90")  # PREREG Sec 8
G3_MIN_MODAL_SHARE = Decimal("0.60")
G3_MIN_VALUE_AGREEMENT = Decimal("0.98")
G4_MIN_REVISED = 50
G4_MIN_STRATUM_N = 30

ISSUANCE_HASH = "5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775"
EXTRACT_HASH = "9bf01d8fc708c17215434ad6c0a7767137f00504f360a164d57133580e4e1d77"
ISSUANCE_ROWS = 26310
EXTRACT_ROWS = 5142

INTERPRETATIONS: dict[str, str] = {
    "A": (
        "At both unseen stations, late-max days carry decisively higher revision risk "
        "with the early stratum near-floor and the majority of each station's revision "
        "rate attributable to the late channel, and the four stations' revision rates "
        "order exactly as their late-max compositions -- the asymmetry's station-level "
        "structure is composition-driven. Downstream label-noise handling should "
        "condition on occurrence timing (or its observable proxies) rather than on "
        "station identity."
    ),
    "B": (
        "The stratum effect replicates at >=1 unseen station but the full quantitative "
        "package (margin, floor, attribution, or cross-station ordering) does not -- "
        "composition is a real but partial driver; the report must name which "
        "criterion failed and the follow-up it implies."
    ),
    "C": (
        "No confident stratum effect at either unseen station -- the E0001 "
        "decomposition does not generalize; the mechanism may be CHI/NYC-specific or "
        "confounded; downstream work must not condition on occurrence timing."
    ),
    "D": (
        "The effect reverses at an unseen station -- the mechanism as stated is "
        "contradicted; E0001's exploratory record stands as published but its "
        "interpretation must be reopened by a new hypothesis, not an edit."
    ),
    "blocked": ("An eligibility gate failed; not a scientific outcome. See eligibility_gates."),
}

_FROZEN_PREREG_REFERENCE: dict[str, Any] = {
    "eval_stations": ["DEN", "LAX"],
    "ref_stations": ["CHI", "NYC"],
    "z": "1.959963984540054",
    "sep_strong": "0.10",
    "early_floor": "0.15",
    "af_min": "0.50",
    "g3_parse": "0.90",
    "g3_modal": "0.60",
    "g3_agree": "0.98",
    "g4_revised": 50,
    "g4_stratum": 30,
    "issuance_hash": "5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775",
    "extract_hash": "9bf01d8fc708c17215434ad6c0a7767137f00504f360a164d57133580e4e1d77",
}


def verify_config_matches_prereg() -> bool:
    ref = _FROZEN_PREREG_REFERENCE
    checks = [
        ref["eval_stations"] == EVAL_STATIONS,
        ref["ref_stations"] == REF_STATIONS,
        str(Z_95) == ref["z"],
        str(SEP_STRONG_MARGIN) == ref["sep_strong"],
        str(EARLY_FLOOR_UPPER) == ref["early_floor"],
        str(AF_MIN) == ref["af_min"],
        str(G3_MIN_PARSE_OK) == ref["g3_parse"],
        str(G3_MIN_MODAL_SHARE) == ref["g3_modal"],
        str(G3_MIN_VALUE_AGREEMENT) == ref["g3_agree"],
        ref["g4_revised"] == G4_MIN_REVISED,
        ref["g4_stratum"] == G4_MIN_STRATUM_N,
        ref["issuance_hash"] == ISSUANCE_HASH,
        ref["extract_hash"] == EXTRACT_HASH,
    ]
    return all(checks)


# --- Decimal policy (inherited F3/F4) ----------------------------------------


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


def wilson_interval(x: int, n: int) -> tuple[Decimal, Decimal]:
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


def newcombe_interval(*, x1: int, n1: int, x2: int, n2: int) -> tuple[Decimal, Decimal]:
    """CI on p1 - p2; sample 1 = late stratum, sample 2 = early stratum
    (disjoint day sets -- independent samples)."""
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        l1, u1 = wilson_interval(x1, n1)
        l2, u2 = wilson_interval(x2, n2)
        p1 = Decimal(x1) / Decimal(n1)
        p2 = Decimal(x2) / Decimal(n2)
        diff = p1 - p2
        lower = diff - ((p1 - l1) ** 2 + (u2 - p2) ** 2).sqrt()
        upper = diff + ((u1 - p1) ** 2 + (p2 - l2) ** 2).sqrt()
        return (lower, upper)


# --- Day-level joined records (PREREG Sec 2) ---------------------------------


@dataclass(slots=True)
class DayRecord:
    station: str
    observation_date: date
    revised: bool
    occurrence_hour: Decimal | None  # None = parse-fail (excluded from strata)
    value_agrees: bool | None  # parsed final product value == frame final value
    n_products: int


def build_day_records(
    issuance: pl.DataFrame, extract: pl.DataFrame
) -> tuple[list[DayRecord], dict[str, int], dict[str, dict[str, int]]]:
    """Join the two pinned inputs per (station, observation_date).

    Returns (records, cutoffs, asof_distributions). Cutoffs come from the
    frozen modal-AS-OF procedure (PREREG Sec 2)."""
    tmax = issuance.filter(pl.col("variable") == "tmax_f").sort(
        ["station_id", "observation_date", "issuance_time"]
    )
    vals: dict[tuple[str, date], list[float]] = defaultdict(list)
    for r in tmax.iter_rows(named=True):
        vals[(r["station_id"], r["observation_date"])].append(r["value"])

    asof_dist: dict[str, dict[str, int]] = {s: {} for s in ALL_STATIONS}
    ext_rows: dict[tuple[str, date], dict[str, Any]] = {}
    for r in extract.sort(["station_id", "observation_date"]).iter_rows(named=True):
        ext_rows[(r["station_id"], r["observation_date"])] = r
        if r["first_asof_hour"] is not None:
            d = asof_dist[r["station_id"]]
            key = str(int(r["first_asof_hour"]))
            d[key] = d.get(key, 0) + 1

    cutoffs: dict[str, int] = {}
    for station in ALL_STATIONS:
        dist = asof_dist[station]
        # modal hour; deterministic tie-break by hour ascending
        modal_hour = max(sorted(dist), key=lambda k: (dist[k], -int(k)))
        cutoffs[station] = int(modal_hour)

    records: list[DayRecord] = []
    for key in sorted(set(vals) & set(ext_rows)):
        station, obs_date = key
        first_v, final_v = vals[key][0], vals[key][-1]
        revised = quantize_value(first_v) != quantize_value(final_v)
        ext = ext_rows[key]
        occ = ext["final_max_occurrence_hour"]
        occ_dec = Decimal(str(occ)) if occ is not None else None
        agrees: bool | None = None
        if occ_dec is not None and ext["final_max_value"] is not None:
            agrees = int(ext["final_max_value"]) == int(
                quantize_value(final_v).to_integral_value(rounding=ROUND_HALF_EVEN)
            )
        records.append(
            DayRecord(
                station=station,
                observation_date=obs_date,
                revised=revised,
                occurrence_hour=occ_dec,
                value_agrees=agrees,
                n_products=ext["n_products"],
            )
        )
    return records, cutoffs, asof_dist


def is_late(rec: DayRecord, cutoff: int) -> bool:
    """PREREG Sec 2: at-or-after the station cutoff, quantized."""
    assert rec.occurrence_hour is not None
    return at_least(rec.occurrence_hour, Decimal(cutoff))


# --- Eligibility gates (PREREG Sec 8) ----------------------------------------

NOT_EVALUATED: Literal["not_evaluated"] = "not_evaluated"


@dataclass(slots=True)
class GateOutcome:
    gates: dict[str, Any]
    all_gates_passed: bool
    records: list[DayRecord] | None
    cutoffs: dict[str, int] | None
    asof_dist: dict[str, dict[str, int]] | None


def run_gates(issuance: pl.DataFrame, extract: pl.DataFrame) -> GateOutcome:
    gates: dict[str, Any] = {
        "g1a_issuance_hash_matches": NOT_EVALUATED,
        "g1b_extract_hash_matches": NOT_EVALUATED,
        "g2_invariants": {
            "issuance_natural_key_unique": NOT_EVALUATED,
            "issuance_no_nulls": NOT_EVALUATED,
            "issuance_station_set_exact": NOT_EVALUATED,
            "issuance_value_roundtrip_verified": NOT_EVALUATED,
            "extract_key_unique": NOT_EVALUATED,
            "extract_station_set_exact": NOT_EVALUATED,
        },
        "g3_quality": NOT_EVALUATED,
        "g4_counts": NOT_EVALUATED,
        "all_gates_passed": False,
    }

    g1a = frame_content_hash(issuance) == ISSUANCE_HASH
    gates["g1a_issuance_hash_matches"] = g1a
    if not g1a:
        return GateOutcome(gates, False, None, None, None)
    g1b = frame_content_hash(extract) == EXTRACT_HASH
    gates["g1b_extract_hash_matches"] = g1b
    if not g1b:
        return GateOutcome(gates, False, None, None, None)

    g2 = {
        "issuance_natural_key_unique": (
            issuance.select(["station_id", "variable", "issuance_time"]).unique().height
            == issuance.height
        ),
        "issuance_no_nulls": bool(
            issuance.select(
                (
                    pl.col("value").is_null()
                    | pl.col("issuance_time").is_null()
                    | pl.col("observation_date").is_null()
                    | pl.col("station_id").is_null()
                    | pl.col("variable").is_null()
                ).sum()
            ).item()
            == 0
        ),
        "issuance_station_set_exact": set(issuance["station_id"].unique().to_list())
        == set(ALL_STATIONS),
        "issuance_value_roundtrip_verified": all(
            value_roundtrips(v) for v in issuance["value"].to_list() if v is not None
        ),
        "extract_key_unique": (
            extract.select(["station_id", "observation_date"]).unique().height == extract.height
        ),
        "extract_station_set_exact": set(extract["station_id"].unique().to_list())
        == set(ALL_STATIONS),
    }
    gates["g2_invariants"] = g2
    if not all(g2.values()):
        return GateOutcome(gates, False, None, None, None)

    records, cutoffs, asof_dist = build_day_records(issuance, extract)

    g3_detail: dict[str, Any] = {}
    g3_pass = True
    for station in EVAL_STATIONS:
        recs = [r for r in records if r.station == station]
        n = len(recs)
        parse_ok = [r for r in recs if r.occurrence_hour is not None]
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            parse_frac = Decimal(len(parse_ok)) / Decimal(n)
            modal_n = asof_dist[station].get(str(cutoffs[station]), 0)
            asof_total = sum(asof_dist[station].values())
            modal_share = Decimal(modal_n) / Decimal(asof_total)
            agree_n = sum(1 for r in parse_ok if r.value_agrees)
            agree_denom = sum(1 for r in parse_ok if r.value_agrees is not None)
            agree_frac = Decimal(agree_n) / Decimal(agree_denom)
        ok = (
            at_least(parse_frac, G3_MIN_PARSE_OK)
            and at_least(modal_share, G3_MIN_MODAL_SHARE)
            and at_least(agree_frac, G3_MIN_VALUE_AGREEMENT)
        )
        g3_detail[station] = {
            "parse_ok_fraction": serialize_decimal(parse_frac),
            "asof_modal_share": serialize_decimal(modal_share),
            "value_agreement": serialize_decimal(agree_frac),
            "pass": ok,
        }
        g3_pass = g3_pass and ok
    gates["g3_quality"] = g3_detail
    if not g3_pass:
        return GateOutcome(gates, False, records, cutoffs, asof_dist)

    g4_detail: dict[str, Any] = {}
    g4_pass = True
    for station in EVAL_STATIONS:
        parse_ok = [r for r in records if r.station == station and r.occurrence_hour is not None]
        late = [r for r in parse_ok if is_late(r, cutoffs[station])]
        early_n = len(parse_ok) - len(late)
        revised_total = sum(1 for r in parse_ok if r.revised)
        ok = revised_total >= G4_MIN_REVISED and min(len(late), early_n) >= G4_MIN_STRATUM_N
        g4_detail[station] = {
            "revised_total": revised_total,
            "late_n": len(late),
            "early_n": early_n,
            "pass": ok,
        }
        g4_pass = g4_pass and ok
    gates["g4_counts"] = g4_detail
    gates["all_gates_passed"] = g4_pass
    return GateOutcome(gates, g4_pass, records, cutoffs, asof_dist)


# --- Station statistics (PREREG Sec 3/5) -------------------------------------


def station_stats(records: list[DayRecord], station: str, cutoff: int) -> dict[str, Any]:
    parse_ok = [r for r in records if r.station == station and r.occurrence_hour is not None]
    late = [r for r in parse_ok if is_late(r, cutoff)]
    early = [r for r in parse_ok if not is_late(r, cutoff)]
    n = len(parse_ok)
    r_late = sum(1 for r in late if r.revised)
    r_early = sum(1 for r in early if r.revised)
    r_total = r_late + r_early
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        big_l = Decimal(len(late)) / Decimal(n)
        p_late = Decimal(r_late) / Decimal(len(late)) if late else None
        p_early = Decimal(r_early) / Decimal(len(early)) if early else None
        big_r = Decimal(r_total) / Decimal(n)
        af = (big_r - p_early) / big_r if (p_early is not None and big_r > 0) else None

    late_ci = wilson_interval(r_late, len(late)) if late else None
    early_ci = wilson_interval(r_early, len(early)) if early else None
    sep_ci = (
        newcombe_interval(x1=r_late, n1=len(late), x2=r_early, n2=len(early))
        if late and early
        else None
    )
    sep_hat = p_late - p_early if (p_late is not None and p_early is not None) else None

    sep_state = NOT_EVALUATED
    if sep_ci is not None:
        lower, upper = sep_ci
        if strictly_less(upper, ZERO):
            sep_state = "inverted"
        elif strictly_greater(lower, SEP_STRONG_MARGIN):
            sep_state = "strong"
        elif strictly_greater(lower, ZERO):
            sep_state = "positive"
        else:
            sep_state = "straddle"

    early_floor_pass = early_ci is not None and strictly_less(early_ci[1], EARLY_FLOOR_UPPER)
    af_pass = af is not None and at_least(af, AF_MIN)
    full_pass = sep_state == "strong" and early_floor_pass and af_pass

    return {
        "cutoff_hour": cutoff,
        "n": n,
        "L": serialize_decimal(big_l),
        "late": {
            "n": len(late),
            "r": r_late,
            "rate": serialize_decimal(p_late) if p_late is not None else None,
            "ci95": serialize_ci(*late_ci) if late_ci else None,
        },
        "early": {
            "n": len(early),
            "r": r_early,
            "rate": serialize_decimal(p_early) if p_early is not None else None,
            "ci95": serialize_ci(*early_ci) if early_ci else None,
        },
        "R": serialize_decimal(big_r),
        "sep_hat": serialize_decimal(sep_hat) if sep_hat is not None else None,
        "sep_ci95": serialize_ci(*sep_ci) if sep_ci else None,
        "sep_state": sep_state,
        "early_floor_pass": early_floor_pass,
        "af": serialize_decimal(af) if af is not None else None,
        "af_pass": af_pass,
        "full_pass": full_pass,
    }


def compute_rank_agreement(stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """PREREG Sec 3: order the four stations by L and by R (descending,
    quantized); exact ordering match required; any tie -> non-agreement."""
    ls = {s: quantize_for_comparison(Decimal(stats[s]["L"])) for s in ALL_STATIONS}
    rs = {s: quantize_for_comparison(Decimal(stats[s]["R"])) for s in ALL_STATIONS}
    tie = len(set(ls.values())) < len(ls) or len(set(rs.values())) < len(rs)
    order_l = sorted(ALL_STATIONS, key=lambda s: (-ls[s], s))
    order_r = sorted(ALL_STATIONS, key=lambda s: (-rs[s], s))
    return {
        "order_by_L": order_l,
        "order_by_R": order_r,
        "tie_present": tie,
        "agree": (not tie) and order_l == order_r,
    }


# --- Decision (PREREG Sec 7) -------------------------------------------------


@dataclass(slots=True)
class Verdict:
    outcome: Literal["A", "B", "C", "D", "blocked"]
    evaluation_step: str
    reason: str


def evaluate(
    gate_outcome: GateOutcome,
    eval_stats: dict[str, dict[str, Any]] | None,
    rank: dict[str, Any] | None,
) -> Verdict:
    if not gate_outcome.all_gates_passed:
        return Verdict("blocked", "step_0", "an eligibility gate failed; no interval computed")
    assert eval_stats is not None and rank is not None

    inverted = [s for s in EVAL_STATIONS if eval_stats[s]["sep_state"] == "inverted"]
    if inverted:
        return Verdict(
            "D",
            "step_1",
            f"separation inverted (CI entirely below 0) at: {', '.join(inverted)}",
        )

    if all(eval_stats[s]["full_pass"] for s in EVAL_STATIONS) and rank["agree"]:
        return Verdict(
            "A",
            "step_2",
            "both evaluation stations full-pass (strong separation, early floor, "
            "attribution) and the four-station composition ordering matches the "
            "revision-rate ordering exactly",
        )

    positives = [s for s in EVAL_STATIONS if eval_stats[s]["sep_state"] in ("positive", "strong")]
    if positives:
        return Verdict(
            "B",
            "step_3",
            f"confident positive separation at {', '.join(positives)} but the full "
            "step-2 package did not hold",
        )

    return Verdict("C", "step_4", "no confident separation at either evaluation station")


# --- Descriptives (PREREG Sec 9; non-decisional) -----------------------------


def kitagawa_pairs(stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Two-stratum Kitagawa decomposition for every station pair:
    raw = composition + rate components, exactly."""
    out: dict[str, Any] = {}
    pairs = [(a, b) for i, a in enumerate(ALL_STATIONS) for b in ALL_STATIONS[i + 1 :]]
    for a, b in pairs:
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            la, lb = Decimal(stats[a]["L"]), Decimal(stats[b]["L"])
            pla, plb = Decimal(stats[a]["late"]["rate"]), Decimal(stats[b]["late"]["rate"])
            pea, peb = Decimal(stats[a]["early"]["rate"]), Decimal(stats[b]["early"]["rate"])
            ra, rb = Decimal(stats[a]["R"]), Decimal(stats[b]["R"])
            mean_pl = (pla + plb) / 2
            mean_pe = (pea + peb) / 2
            mean_l = (la + lb) / 2
            comp = (la - lb) * (mean_pl - mean_pe)
            rate = mean_l * (pla - plb) + (1 - mean_l) * (pea - peb)
        out[f"{a}-{b}"] = {
            "raw_diff": serialize_decimal(ra - rb),
            "composition_component": serialize_decimal(comp),
            "rate_component": serialize_decimal(rate),
        }
    return out


def per_year_tables(records: list[DayRecord], cutoffs: dict[str, int]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for station in ALL_STATIONS:
        out[station] = {}
        for year in (2023, 2024, 2025, 2026):
            recs = [
                r
                for r in records
                if r.station == station
                and r.observation_date.year == year
                and r.occurrence_hour is not None
            ]
            late = [r for r in recs if is_late(r, cutoffs[station])]
            early = [r for r in recs if not is_late(r, cutoffs[station])]
            out[station][str(year)] = {
                "n": len(recs),
                "late_n": len(late),
                "late_r": sum(1 for r in late if r.revised),
                "early_n": len(early),
                "early_r": sum(1 for r in early if r.revised),
            }
    return out


def sensitivity_analyses(records: list[DayRecord], cutoffs: dict[str, int]) -> dict[str, Any]:
    """PREREG Sec 6: cutoff +/- 1h and missing-as-early, evaluation
    stations, point estimates only."""
    out: dict[str, Any] = {}
    for station in EVAL_STATIONS:
        cell: dict[str, Any] = {}
        for label, cutoff in (
            ("minus_1h", cutoffs[station] - 1),
            ("frozen", cutoffs[station]),
            ("plus_1h", cutoffs[station] + 1),
        ):
            parse_ok = [
                r for r in records if r.station == station and r.occurrence_hour is not None
            ]
            late = [r for r in parse_ok if is_late(r, cutoff)]
            early = [r for r in parse_ok if not is_late(r, cutoff)]
            with localcontext() as ctx:
                ctx.prec = DECIMAL_CONTEXT_PRECISION
                cell[label] = {
                    "L": serialize_decimal(Decimal(len(late)) / Decimal(len(parse_ok))),
                    "p_late": (
                        serialize_decimal(
                            Decimal(sum(1 for r in late if r.revised)) / Decimal(len(late))
                        )
                        if late
                        else None
                    ),
                    "p_early": (
                        serialize_decimal(
                            Decimal(sum(1 for r in early if r.revised)) / Decimal(len(early))
                        )
                        if early
                        else None
                    ),
                }
        all_recs = [r for r in records if r.station == station]
        late = [
            r for r in all_recs if r.occurrence_hour is not None and is_late(r, cutoffs[station])
        ]
        early_incl = [r for r in all_recs if r not in late]
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            cell["missing_as_early"] = {
                "p_late": (
                    serialize_decimal(
                        Decimal(sum(1 for r in late if r.revised)) / Decimal(len(late))
                    )
                    if late
                    else None
                ),
                "p_early": (
                    serialize_decimal(
                        Decimal(sum(1 for r in early_incl if r.revised)) / Decimal(len(early_incl))
                    )
                    if early_incl
                    else None
                ),
            }
        out[station] = cell
    return out


def occurrence_histograms(records: list[DayRecord]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {s: [0] * 24 for s in ALL_STATIONS}
    for r in records:
        if r.occurrence_hour is not None:
            out[r.station][int(r.occurrence_hour)] += 1
    return out


def diagnostics_block(
    records: list[DayRecord], cutoffs: dict[str, int], asof_dist: dict[str, dict[str, int]]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for station in ALL_STATIONS:
        recs = [r for r in records if r.station == station]
        parse_ok = [r for r in recs if r.occurrence_hour is not None]
        agree_n = sum(1 for r in parse_ok if r.value_agrees)
        agree_denom = sum(1 for r in parse_ok if r.value_agrees is not None)
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            out[station] = {
                "days_joined": len(recs),
                "parse_ok": len(parse_ok),
                "missing_occurrence": len(recs) - len(parse_ok),
                "value_agreement": (
                    serialize_decimal(Decimal(agree_n) / Decimal(agree_denom))
                    if agree_denom
                    else None
                ),
                "cutoff_hour": cutoffs[station],
                "asof_distribution": dict(sorted(asof_dist[station].items())),
                "mean_products_per_day": serialize_decimal(
                    Decimal(sum(r.n_products for r in recs)) / Decimal(len(recs))
                ),
            }
    return out


# --- Manifest validation -----------------------------------------------------


class ManifestValidationError(Exception):
    pass


_REQUIRED_MANIFEST_PATHS: dict[str, set[str]] = {
    "": {
        "experiment",
        "hypothesis",
        "preregistration",
        "datasets",
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
        "exploratory_source_frozen",
        "elaboration_flags_for_audit",
    },
    "datasets": {"issuance_frame", "occurrence_extract", "analysis_git_commit"},
    "eligibility_gates": {
        "g1a_issuance_hash_matches",
        "g1b_extract_hash_matches",
        "g2_invariants",
        "g3_quality",
        "g4_counts",
        "all_gates_passed",
    },
    "primary_results": {"stations", "rank_agreement", "decision"},
    "primary_results.decision": {"outcome", "evaluation_step", "reason", "interpretation"},
    "descriptive_results": {
        "reference_stations",
        "composition_identity_table",
        "per_year_tables",
        "kitagawa_decomposition",
        "diagnostics",
        "sensitivity",
        "occurrence_hour_histograms",
    },
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
    outcome = results["primary_results"]["decision"]["outcome"]
    if outcome not in ("A", "B", "C", "D", "blocked"):
        raise ManifestValidationError(f"unrecognized decision outcome: {outcome!r}")


# --- Orchestration -----------------------------------------------------------

PREREGISTRATION_METADATA: dict[str, Any] = {
    "document": "docs/research/preregistrations/PREREG-20260722-H0015-late-max-mechanism.md",
    "frozen_date": "2026-07-22",
    "provenance_note": (
        "PREREG Sec 0: DEN/LAX revision+timing behavior never computed pre-freeze; "
        "CHI/NYC are generating-data reference cohorts (rank-agreement reuse "
        "disclosed, Sec 12 R6); structural extract facts seen pre-freeze"
    ),
    "original_hypotheses_entry": "HYPOTHESES.md H0015, opened 2026-07-22 (from E0001)",
    "exploratory_source_frozen": (
        "E0001 outputs are published and unmodified "
        "(docs/research/investigations/2026-07-22-e0001-*)"
    ),
    "elaboration_flags_for_audit": [
        "evaluation_cohort_station_disjoint_not_time_disjoint (Sec 0/12 R1)",
        "cutoff_procedure_frozen_not_numbers (Sec 2)",
        "reference_cohort_reuse_limited_to_rank_agreement (Sec 12 R6)",
        "af_point_estimate_declared_no_ci (Sec 3)",
        "conjunctive_A_multiplicity_declaration (Sec 4)",
    ],
}


def run(issuance_dir: Path, extract_dir: Path) -> dict[str, Any]:
    issuance = pl.read_parquet(issuance_dir / "observation_issuances.parquet")
    extract = pl.read_parquet(extract_dir / "cli_occurrences.parquet")

    gate_outcome = run_gates(issuance, extract)

    eval_stats: dict[str, dict[str, Any]] | None = None
    all_stats: dict[str, dict[str, Any]] | None = None
    rank: dict[str, Any] | None = None
    if gate_outcome.all_gates_passed:
        assert gate_outcome.records is not None and gate_outcome.cutoffs is not None
        all_stats = {
            s: station_stats(gate_outcome.records, s, gate_outcome.cutoffs[s]) for s in ALL_STATIONS
        }
        eval_stats = {s: all_stats[s] for s in EVAL_STATIONS}
        rank = compute_rank_agreement(all_stats)

    verdict = evaluate(gate_outcome, eval_stats, rank)

    descriptive: dict[str, Any]
    if gate_outcome.all_gates_passed:
        assert gate_outcome.records is not None
        assert gate_outcome.cutoffs is not None and gate_outcome.asof_dist is not None
        assert all_stats is not None
        identity = {}
        for s in ALL_STATIONS:
            with localcontext() as ctx:
                ctx.prec = DECIMAL_CONTEXT_PRECISION
                lhs = Decimal(all_stats[s]["L"]) * Decimal(all_stats[s]["late"]["rate"]) + (
                    1 - Decimal(all_stats[s]["L"])
                ) * Decimal(all_stats[s]["early"]["rate"])
            identity[s] = {
                "L_pl_plus_1mL_pe": serialize_decimal(lhs),
                "R": all_stats[s]["R"],
            }
        descriptive = {
            "reference_stations": {s: all_stats[s] for s in REF_STATIONS},
            "composition_identity_table": identity,
            "per_year_tables": per_year_tables(gate_outcome.records, gate_outcome.cutoffs),
            "kitagawa_decomposition": kitagawa_pairs(all_stats),
            "diagnostics": diagnostics_block(
                gate_outcome.records, gate_outcome.cutoffs, gate_outcome.asof_dist
            ),
            "sensitivity": sensitivity_analyses(gate_outcome.records, gate_outcome.cutoffs),
            "occurrence_hour_histograms": occurrence_histograms(gate_outcome.records),
        }
    else:
        descriptive = {
            "reference_stations": {},
            "composition_identity_table": {},
            "per_year_tables": {},
            "kitagawa_decomposition": {},
            "diagnostics": {},
            "sensitivity": {},
            "occurrence_hour_histograms": {},
        }

    results: dict[str, Any] = {
        "experiment": "EXP-20260722-H0015-late-max-mechanism",
        "hypothesis": "H0015",
        "preregistration": PREREGISTRATION_METADATA,
        "datasets": {
            "issuance_frame": {
                "version": "exp-20260722-h0013-replication",
                "frame": "observation_issuances.parquet",
                "content_hash": ISSUANCE_HASH,
                "row_count_expected": ISSUANCE_ROWS,
            },
            "occurrence_extract": {
                "version": "exp-20260722-h0015-occurrence",
                "frame": "cli_occurrences.parquet",
                "content_hash": EXTRACT_HASH,
                "row_count_expected": EXTRACT_ROWS,
                "parser": "kalshi_weather.weather.cli_products",
            },
            "analysis_git_commit": None,
        },
        "config": {
            "variable": "tmax_f",
            "evaluation_stations": list(EVAL_STATIONS),
            "reference_cohorts": list(REF_STATIONS),
            "window": "2022-12-31 .. 2026-07-21 (full archive; per-year splits descriptive only)",
            "population": (
                "days in both inputs with parsed final-product occurrence time (parse-ok); "
                "missing-occurrence days excluded and counted, with an as-early sensitivity"
            ),
            "cutoff_procedure": (
                "per station: modal first_asof_hour among non-null preliminary AS-OF hours; "
                "gate on modal share -- PREREG Sec 2"
            ),
            "late_day": "final max occurrence hour >= station cutoff (quantized at-or-after)",
            "revision_definition": (
                "first-issued != final-issued value, Decimal-quantized 0.01 (H0002 convention)"
            ),
            "estimands": (
                "L_s, p_late_s, p_early_s, R_s, sep_s = p_late - p_early (Newcombe 95%), "
                "AF_s = (R_s - p_early_s)/R_s (point), 4-station rank agreement, pairwise "
                "Kitagawa decomposition -- PREREG Sec 3"
            ),
            "ci_method": (
                "Wilson (rates) / Newcombe (differences), no continuity correction, "
                "two-sided 95%, z = 1.959963984540054"
            ),
            "decision_criteria": (
                "per evaluation station: sep strong (L > 0.10) / positive (L > 0) / straddle "
                "/ inverted (U < 0); early floor (Wilson upper of p_early < 0.15); AF >= "
                "0.50 (not-strictly-less) -- PREREG Sec 5"
            ),
            "decision_rule": (
                "0) gate fails -> BLOCKED; 1) any inversion -> D; 2) both full_pass AND rank "
                "agreement -> A; 3) any positive-or-strong sep -> B; 4) otherwise -> C -- "
                "PREREG Sec 7"
            ),
            "timezone": "none -- local wall-clock hours from the products themselves",
            "exact_arithmetic": (
                "integer counts; Decimal precision-50 bounds; 1e-12 ROUND_HALF_EVEN quantized "
                "strict comparisons; fixed-point 12-digit serialization"
            ),
            "random_seed": "not applicable -- fully deterministic",
            "software_versions": None,
        },
        "eligibility_gates": gate_outcome.gates,
        "primary_results": {
            "stations": eval_stats or {s: {} for s in EVAL_STATIONS},
            "rank_agreement": rank
            or {"order_by_L": [], "order_by_R": [], "tie_present": False, "agree": False},
            "decision": {
                "outcome": verdict.outcome,
                "evaluation_step": verdict.evaluation_step,
                "reason": verdict.reason,
                "interpretation": INTERPRETATIONS[verdict.outcome],
            },
        },
        "descriptive_results": descriptive,
        "reproducibility_verification": {
            "rerun_byte_identical": None,
            "config_matches_prereg": verify_config_matches_prereg(),
            "serialization_policy": (
                "Every non-integer decision-relevant quantity is a JSON string: Decimal "
                "quantized to 1e-12 via ROUND_HALF_EVEN, fixed-point, 12 digits -- inherited "
                "F4. Integer counts remain plain JSON integers."
            ),
        },
        "date_run": None,
    }
    validate_manifest(results)
    return results


def render_markdown(results: dict[str, Any]) -> str:
    decision = results["primary_results"]["decision"]
    stations = results["primary_results"]["stations"]
    rank = results["primary_results"]["rank_agreement"]
    ref = results["descriptive_results"].get("reference_stations", {})

    def row(s: str, st: dict[str, Any]) -> str:
        if not st:
            return f"| {s} | (blocked) | | | | | | |"
        return (
            f"| {s} | {st['cutoff_hour']}:00 | {st['n']} | {float(st['L']) * 100:.1f}% "
            f"| {st['late']['r']}/{st['late']['n']} ({float(st['late']['rate']) * 100:.1f}%) "
            f"| {st['early']['r']}/{st['early']['n']} ({float(st['early']['rate']) * 100:.1f}%) "
            f"| {float(st['R']) * 100:.1f}% | {st['sep_state']}, AF={float(st['af']):.2f} |"
        )

    lines = [
        "| station | cutoff | n | late-max L | revised late | revised early | R | verdict cells |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in ("DEN", "LAX"):
        lines.append(row(s, stations.get(s, {})))
    for s in ("CHI", "NYC"):
        lines.append(row(f"{s} (ref)", ref.get(s, {})))

    return f"""# EXP-20260722-H0015-late-max-mechanism

Executed exactly as pre-registered in
`PREREG-20260722-H0015-late-max-mechanism.md`.

## Decision

**{decision["outcome"]}** ({decision["evaluation_step"]}: {decision["reason"]}).

{decision["interpretation"]}

## Station table (evaluation stations decisional; reference cohorts descriptive)

{chr(10).join(lines)}

## Rank agreement (step-2 conjunct)

- order by late-max fraction L: {rank["order_by_L"]}
- order by revision rate R: {rank["order_by_R"]}
- agree: {rank["agree"]}

Full cell-level results, decomposition tables, diagnostics, and
sensitivity analyses: see the accompanying `-results.json`. Figures,
interpretation narrative, and limitations:
`docs/research/postmortems/2026-07-22-h0015-closeout.md`.
"""


if __name__ == "__main__":
    import contextlib
    import subprocess

    parser = argparse.ArgumentParser()
    parser.add_argument("--issuance-dir", type=Path, required=True)
    parser.add_argument("--extract-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()

    output = run(args.issuance_dir, args.extract_dir)
    if not args.raw:
        output["date_run"] = datetime.now(UTC).date().isoformat()
        with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
            output["datasets"]["analysis_git_commit"] = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip()

    args.out.write_text(json.dumps(output, indent=2, default=str))
    args.out_md.write_text(render_markdown(output))
    print(f"wrote {args.out}")
    print(f"wrote {args.out_md}")
