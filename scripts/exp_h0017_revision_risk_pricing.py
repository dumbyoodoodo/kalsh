"""EXP H0017 -- Market-efficiency event study: do post-preliminary prices
of adjacent at-risk contracts match realized qualifying-revision
frequency?

Implements exactly what is frozen in:

  - docs/research/preregistrations/PREREG-20260722-H0017-revision-risk-pricing.md
  - docs/research/preregistrations/TEMPLATE-H0017-manifest.json

Fully deterministic; no randomness; no float reaches a decision
comparison. Occurrence-time data is excluded by the frozen leakage rule.

Usage:
    python scripts/exp_h0017_revision_risk_pricing.py \
        --market-dir "<KALSHI_DATA_DIR>/datasets/exp-20260722-h0017-market" \
        --issuance-dir "<KALSHI_DATA_DIR>/datasets/exp-20260722-h0013-replication" \
        --out docs/research/experiments/EXP-20260722-H0017-revision-risk-pricing-results.json \
        --out-md docs/research/experiments/EXP-20260722-H0017-revision-risk-pricing.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Any, Literal

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kalshi_weather.dataset.manifest import frame_content_hash

# --- Frozen protocol constants (PREREG Sec 2, 3, 5, 6, 8) --------------------

STATION = "NYC"
VARIABLES: list[str] = ["tmax_f", "tmin_f"]

Z_95 = Decimal("1.959963984540054")
DECIMAL_CONTEXT_PRECISION = 50
COMPARISON_QUANTIZATION = Decimal("1E-12")
VALUE_QUANTIZATION = Decimal("0.01")
ROUNDTRIP_TOLERANCE = 1e-6
SERIALIZATION_QUANTIZATION = Decimal("1E-12")
ZERO = Decimal(0)

EQUIVALENCE_MARGIN = Decimal("0.075")  # PREREG Sec 6

PRE_START_MIN, PRE_END_MIN = -90, -10  # PREREG Sec 3
POST_START_MIN, POST_END_MIN = 15, 120
PLACEBO_OFFSET_MIN = -240

G3_MIN_SEMANTICS_AGREEMENT = Decimal("0.99")  # PREREG Sec 8
G3_MIN_EVENT_TIME_PRESENCE = Decimal("0.90")
G4_MIN_N = 80
G4_MIN_EVENTS = 5
G4_MIN_QUOTE_COVERAGE = Decimal("0.50")

MARKET_PRICES_HASH = "f364132289e7f453ddd0e6c969b0905809bc5f03ac000feab989e42e4083eb53"
LABELS_HASH = "49bc34dfc4d78da5494a23192780790f068dbb74f0f85a9c671b78258e9d7bf7"
ISSUANCE_HASH = "5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775"

INTERPRETATIONS: dict[str, str] = {
    "OVERPRICED": (
        "Post-preliminary prices of adjacent at-risk contracts exceed realized "
        "qualifying-revision frequency beyond sampling variation -- markets overreact "
        "to revision risk. NYC-scoped, dated 2026-05..07, quantified by the CI; report "
        "with locked-control and placebo diagnostics adjacent."
    ),
    "UNDERPRICED": (
        "Post-preliminary prices of adjacent at-risk contracts fall below realized "
        "qualifying-revision frequency beyond sampling variation -- the anchoring "
        "direction: markets underreact to known revision risk. The program's first "
        "actionable-inefficiency finding; NYC-scoped and dated, and it requires an "
        "independent replication on post-freeze data before any Phase-6 use."
    ),
    "EFFICIENT-WITHIN-MARGIN": (
        "The 95% CI on the mean calibration residual lies within +/-7.5pp -- a genuine "
        "equivalence claim at the stated margin: near-settlement NYC weather prices "
        "incorporated known revision risk to within economic materiality over this "
        "window. Not merely absence of evidence."
    ),
    "INCONCLUSIVE": (
        "The CI is too wide to classify against the margin -- the honest underpowered "
        "branch. The report names the achieved half-width and the accumulation "
        "required to shrink it; no efficiency conclusion is licensed."
    ),
    "blocked": ("An eligibility gate failed; not a scientific outcome. See eligibility_gates."),
}

_FROZEN_PREREG_REFERENCE: dict[str, Any] = {
    "station": "NYC",
    "variables": ["tmax_f", "tmin_f"],
    "z": "1.959963984540054",
    "margin": "0.075",
    "pre": (-90, -10),
    "post": (15, 120),
    "placebo_offset": -240,
    "g3_semantics": "0.99",
    "g3_event_presence": "0.90",
    "g4_n": 80,
    "g4_events": 5,
    "g4_coverage": "0.50",
    "market_prices_hash": "f364132289e7f453ddd0e6c969b0905809bc5f03ac000feab989e42e4083eb53",
    "labels_hash": "49bc34dfc4d78da5494a23192780790f068dbb74f0f85a9c671b78258e9d7bf7",
    "issuance_hash": "5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775",
}


def verify_config_matches_prereg() -> bool:
    ref = _FROZEN_PREREG_REFERENCE
    checks = [
        ref["station"] == STATION,
        ref["variables"] == VARIABLES,
        str(Z_95) == ref["z"],
        str(EQUIVALENCE_MARGIN) == ref["margin"],
        ref["pre"] == (PRE_START_MIN, PRE_END_MIN),
        ref["post"] == (POST_START_MIN, POST_END_MIN),
        ref["placebo_offset"] == PLACEBO_OFFSET_MIN,
        str(G3_MIN_SEMANTICS_AGREEMENT) == ref["g3_semantics"],
        str(G3_MIN_EVENT_TIME_PRESENCE) == ref["g3_event_presence"],
        ref["g4_n"] == G4_MIN_N,
        ref["g4_events"] == G4_MIN_EVENTS,
        str(G4_MIN_QUOTE_COVERAGE) == ref["g4_coverage"],
        ref["market_prices_hash"] == MARKET_PRICES_HASH,
        ref["labels_hash"] == LABELS_HASH,
        ref["issuance_hash"] == ISSUANCE_HASH,
    ]
    return all(checks)


# --- Decimal policy (program-wide, inherited) --------------------------------


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


def mean_ci(residuals: list[Decimal]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """(mean, sd, lower, upper) -- deterministic normal-approximation CI on a
    mean of bounded residuals (PREREG Sec 5)."""
    n = len(residuals)
    if n < 2:
        raise ValueError("mean_ci requires n >= 2")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        n_dec = Decimal(n)
        mean = sum(residuals, ZERO) / n_dec
        var = sum(((r - mean) ** 2 for r in residuals), ZERO) / (n_dec - 1)
        sd = var.sqrt()
        half = Z_95 * sd / n_dec.sqrt()
        return (mean, sd, mean - half, mean + half)


# --- Strike semantics and contract selection (PREREG Sec 4) ------------------


def region_yes(
    strike_type: str, floor: Decimal | None, cap: Decimal | None, value: Decimal
) -> bool:
    """Frozen strike semantics; validated against realized payouts by G3."""
    if strike_type == "between":
        assert floor is not None and cap is not None
        return floor <= value <= cap
    if strike_type == "greater":
        assert floor is not None
        return value > floor
    if strike_type == "less":
        assert cap is not None
        return value < cap
    raise ValueError(f"unknown strike_type {strike_type!r}")


@dataclass(slots=True)
class MarketMeta:
    ticker: str
    variable: str
    target_date: date
    strike_type: str
    floor: Decimal | None
    cap: Decimal | None
    kalshi_result: str | None
    close_time: datetime | None
    label_status: str | None


def select_adjacent(
    markets: list[MarketMeta], variable: str, prelim: Decimal
) -> tuple[MarketMeta | None, MarketMeta | None]:
    """(adjacent at-risk, adjacent locked-NO) for one variable-day.
    tmax: at-risk = region entirely above prelim (floor > M); locked-NO =
    region entirely below (cap < M). tmin mirrored. Deterministic
    tie-break by ticker."""
    at_risk: list[MarketMeta] = []
    locked_no: list[MarketMeta] = []
    for m in markets:
        if variable == "tmax_f":
            if m.strike_type in ("between", "greater") and m.floor is not None and m.floor > prelim:
                at_risk.append(m)
            if m.strike_type in ("between", "less") and m.cap is not None and m.cap < prelim:
                locked_no.append(m)
        else:  # tmin_f
            if m.strike_type in ("between", "less") and m.cap is not None and m.cap < prelim:
                at_risk.append(m)
            if m.strike_type in ("between", "greater") and m.floor is not None and m.floor > prelim:
                locked_no.append(m)
    if variable == "tmax_f":
        at_risk.sort(key=lambda m: (m.floor, m.ticker))  # type: ignore[arg-type, return-value]
        locked_no.sort(key=lambda m: (-m.cap, m.ticker))  # type: ignore[operator]
    else:
        at_risk.sort(key=lambda m: (-m.cap, m.ticker))  # type: ignore[operator]
        locked_no.sort(key=lambda m: (m.floor, m.ticker))  # type: ignore[arg-type, return-value]
    return (at_risk[0] if at_risk else None, locked_no[0] if locked_no else None)


def at_risk_distance(m: MarketMeta, variable: str, prelim: Decimal) -> Decimal:
    if variable == "tmax_f":
        assert m.floor is not None
        return m.floor - prelim
    assert m.cap is not None
    return prelim - m.cap


# --- Price measurement (PREREG Sec 3) ----------------------------------------


@dataclass(slots=True)
class Candle:
    period_start: datetime
    bid_close: int | None
    ask_close: int | None
    price_close: int | None
    carried_forward: bool
    volume: int


def window_midpoint(
    candles: list[Candle], start: datetime, end: datetime
) -> tuple[Decimal | None, Candle | None]:
    """Midpoint of the LAST fully-quoted candle with period_start in
    [start, end]; (None, None) if no such candle."""
    chosen: Candle | None = None
    for c in candles:  # candles pre-sorted ascending
        if start <= c.period_start <= end and c.bid_close is not None and c.ask_close is not None:
            chosen = c
    if chosen is None:
        return None, None
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        mid = (Decimal(chosen.bid_close) + Decimal(chosen.ask_close)) / Decimal(200)
    return mid, chosen


def window_trade_price(candles: list[Candle], start: datetime, end: datetime) -> Decimal | None:
    """Sensitivity: last non-carried-forward trade close in-window."""
    chosen: Candle | None = None
    for c in candles:
        if start <= c.period_start <= end and c.price_close is not None and not c.carried_forward:
            chosen = c
    if chosen is None or chosen.price_close is None:
        return None
    return Decimal(chosen.price_close) / Decimal(100)


# --- Gates (PREREG Sec 8) ----------------------------------------------------

NOT_EVALUATED: Literal["not_evaluated"] = "not_evaluated"


@dataclass(slots=True)
class LoadedInputs:
    markets: dict[tuple[str, date], list[MarketMeta]]  # (variable, target_date) -> ladder
    candles: dict[str, list[Candle]]  # ticker -> ascending candles
    preliminaries: dict[tuple[str, date], tuple[Decimal, datetime]]  # (variable, d) -> (M, E)


def load_inputs(market_frame: pl.DataFrame, issuance: pl.DataFrame) -> LoadedInputs:
    meta_rows = (
        market_frame.sort(["market_ticker", "period_start"])
        .group_by("market_ticker", maintain_order=True)
        .first()
    )
    markets: dict[tuple[str, date], list[MarketMeta]] = defaultdict(list)
    for r in meta_rows.iter_rows(named=True):
        if r["station_id"] != STATION or r["variable"] not in VARIABLES:
            continue
        if r["target_date"] is None or r["strike_type"] is None:
            continue
        markets[(r["variable"], r["target_date"])].append(
            MarketMeta(
                ticker=r["market_ticker"],
                variable=r["variable"],
                target_date=r["target_date"],
                strike_type=r["strike_type"],
                floor=Decimal(str(r["floor_strike"])) if r["floor_strike"] is not None else None,
                cap=Decimal(str(r["cap_strike"])) if r["cap_strike"] is not None else None,
                kalshi_result=r["kalshi_result"],
                close_time=r["close_time"],
                label_status=r["settlement_label_status"],
            )
        )
    for ladder in markets.values():
        ladder.sort(key=lambda m: m.ticker)

    candles: dict[str, list[Candle]] = defaultdict(list)
    for r in market_frame.sort(["market_ticker", "period_start"]).iter_rows(named=True):
        candles[r["market_ticker"]].append(
            Candle(
                period_start=r["period_start"],
                bid_close=r["yes_bid_close_cents"],
                ask_close=r["yes_ask_close_cents"],
                price_close=r["price_close_cents"],
                carried_forward=bool(r["price_close_is_carried_forward"]),
                volume=r["volume"] or 0,
            )
        )

    prelims: dict[tuple[str, date], tuple[Decimal, datetime]] = {}
    nyc = issuance.filter(pl.col("station_id") == STATION).sort(
        ["variable", "observation_date", "issuance_time"]
    )
    for r in nyc.iter_rows(named=True):
        key = (r["variable"], r["observation_date"])
        if key not in prelims:
            prelims[key] = (quantize_value(r["value"]), r["issuance_time"])
    return LoadedInputs(markets=dict(markets), candles=dict(candles), preliminaries=prelims)


@dataclass(slots=True)
class GateOutcome:
    gates: dict[str, Any]
    all_gates_passed: bool
    inputs: LoadedInputs | None
    cohort: list[dict[str, Any]] | None  # primary cohort rows
    exclusions: dict[str, int] | None


def build_cohort(
    inputs: LoadedInputs,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Primary cohort: one adjacent at-risk contract per variable-day with a
    POST-window quote and a resolved label. Exclusions counted by reason
    (PREREG Sec 2)."""
    cohort: list[dict[str, Any]] = []
    exclusions = {
        "no_preliminary": 0,
        "no_ladder": 0,
        "no_at_risk_contract": 0,
        "label_unresolved": 0,
        "no_post_quote": 0,
    }
    all_days = sorted({k for k in inputs.markets} | {k for k in inputs.preliminaries})
    for variable, d in all_days:
        if (variable, d) not in inputs.markets:
            continue  # a preliminary without any markets is not an event-day
        if (variable, d) not in inputs.preliminaries:
            exclusions["no_preliminary"] += 1
            continue
        prelim, event_time = inputs.preliminaries[(variable, d)]
        ladder = inputs.markets[(variable, d)]
        at_risk, locked = select_adjacent(ladder, variable, prelim)
        if at_risk is None:
            exclusions["no_at_risk_contract"] += 1
            continue
        if at_risk.label_status != "resolved" or at_risk.kalshi_result not in ("yes", "no"):
            exclusions["label_unresolved"] += 1
            continue
        candles = inputs.candles.get(at_risk.ticker, [])
        post_start = event_time + timedelta(minutes=POST_START_MIN)
        post_end = event_time + timedelta(minutes=POST_END_MIN)
        p_post, post_candle = window_midpoint(candles, post_start, post_end)
        if p_post is None:
            exclusions["no_post_quote"] += 1
            continue
        pre_mid, _ = window_midpoint(
            candles,
            event_time + timedelta(minutes=PRE_START_MIN),
            event_time + timedelta(minutes=PRE_END_MIN),
        )
        cohort.append(
            {
                "variable": variable,
                "target_date": d,
                "ticker": at_risk.ticker,
                "prelim": prelim,
                "event_time": event_time,
                "distance": at_risk_distance(at_risk, variable, prelim),
                "p_post": p_post,
                "p_pre": pre_mid,
                "post_candle": post_candle,
                "y": 1 if at_risk.kalshi_result == "yes" else 0,
                "locked": locked,
            }
        )
    return cohort, exclusions


def run_gates(
    market_frame: pl.DataFrame, labels: pl.DataFrame, issuance: pl.DataFrame
) -> GateOutcome:
    gates: dict[str, Any] = {
        "g1a_market_hashes_match": NOT_EVALUATED,
        "g1b_issuance_hash_matches": NOT_EVALUATED,
        "g2_invariants": NOT_EVALUATED,
        "g3_semantics": NOT_EVALUATED,
        "g4_cohort": NOT_EVALUATED,
        "all_gates_passed": False,
    }

    g1a = (
        frame_content_hash(market_frame) == MARKET_PRICES_HASH
        and frame_content_hash(labels) == LABELS_HASH
    )
    gates["g1a_market_hashes_match"] = g1a
    if not g1a:
        return GateOutcome(gates, False, None, None, None)
    g1b = frame_content_hash(issuance) == ISSUANCE_HASH
    gates["g1b_issuance_hash_matches"] = g1b
    if not g1b:
        return GateOutcome(gates, False, None, None, None)

    g2 = {
        "market_key_unique": (
            market_frame.select(["market_ticker", "period_start"]).unique().height
            == market_frame.height
        ),
        "labels_key_unique": (labels.select(["market_ticker"]).unique().height == labels.height),
        "issuance_key_unique": (
            issuance.select(["station_id", "variable", "issuance_time"]).unique().height
            == issuance.height
        ),
        "issuance_station_set_exact": set(issuance["station_id"].unique().to_list())
        == {"CHI", "DEN", "LAX", "NYC"},
        "issuance_value_roundtrip_verified": all(
            value_roundtrips(v) for v in issuance["value"].to_list() if v is not None
        ),
    }
    gates["g2_invariants"] = g2
    if not all(g2.values()):
        return GateOutcome(gates, False, None, None, None)

    # G3a: strike semantics vs realized payouts, over resolved labels with a
    # numeric settlement value and strike fields (joined from market metadata).
    meta = (
        market_frame.sort(["market_ticker", "period_start"])
        .group_by("market_ticker", maintain_order=True)
        .first()
        .select(["market_ticker", "strike_type", "floor_strike", "cap_strike"])
    )
    joined = labels.join(meta, on="market_ticker", how="inner")
    agree = disagree = 0
    for r in joined.iter_rows(named=True):
        if (
            r["settlement_label_status"] != "resolved"
            or r["kalshi_result"] not in ("yes", "no")
            or r["value_at_settlement"] is None
            or r["strike_type"] is None
        ):
            continue
        floor = Decimal(str(r["floor_strike"])) if r["floor_strike"] is not None else None
        cap = Decimal(str(r["cap_strike"])) if r["cap_strike"] is not None else None
        predicted = region_yes(
            r["strike_type"], floor, cap, quantize_value(r["value_at_settlement"])
        )
        if predicted == (r["kalshi_result"] == "yes"):
            agree += 1
        else:
            disagree += 1
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        agreement = Decimal(agree) / Decimal(agree + disagree)

    inputs = load_inputs(market_frame, issuance)
    label_days = {
        (r["variable"], r["target_date"])
        for r in labels.iter_rows(named=True)
        if r["station_id"] == STATION and r["settlement_label_status"] == "resolved"
    }
    with_event = sum(1 for k in label_days if k in inputs.preliminaries)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        presence = Decimal(with_event) / Decimal(len(label_days))
    g3 = {
        "strike_semantics_agreement": serialize_decimal(agreement),
        "semantics_counts": {"agree": agree, "disagree": disagree},
        "event_time_presence": serialize_decimal(presence),
        "pass": (
            not strictly_less(agreement, G3_MIN_SEMANTICS_AGREEMENT)
            and not strictly_less(presence, G3_MIN_EVENT_TIME_PRESENCE)
        ),
    }
    gates["g3_semantics"] = g3
    if not g3["pass"]:
        return GateOutcome(gates, False, inputs, None, None)

    cohort, exclusions = build_cohort(inputs)
    n = len(cohort)
    events = sum(row["y"] for row in cohort)
    at_risk_days = n + exclusions["no_post_quote"]
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        coverage = Decimal(n) / Decimal(at_risk_days) if at_risk_days else ZERO
    g4 = {
        "n_primary": n,
        "qualifying_events": events,
        "quote_coverage": serialize_decimal(coverage),
        "exclusion_counts_by_reason": exclusions,
        "pass": (
            n >= G4_MIN_N
            and events >= G4_MIN_EVENTS
            and not strictly_less(coverage, G4_MIN_QUOTE_COVERAGE)
        ),
    }
    gates["g4_cohort"] = g4
    gates["all_gates_passed"] = bool(g4["pass"])
    return GateOutcome(gates, bool(g4["pass"]), inputs, cohort, exclusions)


# --- Decision (PREREG Sec 6) -------------------------------------------------


@dataclass(slots=True)
class Verdict:
    outcome: Literal[
        "OVERPRICED", "UNDERPRICED", "EFFICIENT-WITHIN-MARGIN", "INCONCLUSIVE", "blocked"
    ]
    evaluation_step: str
    reason: str


def evaluate(gate_outcome: GateOutcome, lower: Decimal | None, upper: Decimal | None) -> Verdict:
    if not gate_outcome.all_gates_passed:
        return Verdict("blocked", "step_0", "an eligibility gate failed; no interval computed")
    assert lower is not None and upper is not None
    if strictly_greater(lower, ZERO):
        return Verdict("OVERPRICED", "step_1", "L(D) > 0: mean residual confidently positive")
    if strictly_less(upper, ZERO):
        return Verdict("UNDERPRICED", "step_2", "U(D) < 0: mean residual confidently negative")
    if strictly_greater(lower, -EQUIVALENCE_MARGIN) and strictly_less(upper, EQUIVALENCE_MARGIN):
        return Verdict(
            "EFFICIENT-WITHIN-MARGIN",
            "step_3",
            "CI within (-0.075, +0.075): equivalence established at the frozen margin",
        )
    return Verdict("INCONCLUSIVE", "step_4", "CI straddles the margin; underpowered branch")


# --- Descriptives (PREREG Sec 7; non-decisional) -----------------------------


def calibration_cell(rows: list[dict[str, Any]], price_key: str = "p_post") -> dict[str, Any]:
    usable = [r for r in rows if r.get(price_key) is not None]
    if len(usable) < 2:
        return {"n": len(usable), "note": "insufficient for CI"}
    residuals = [r[price_key] - Decimal(r["y"]) for r in usable]
    mean, sd, lower, upper = mean_ci(residuals)
    events = sum(r["y"] for r in usable)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        mean_p = sum((r[price_key] for r in usable), ZERO) / Decimal(len(usable))
        rate = Decimal(events) / Decimal(len(usable))
    return {
        "n": len(usable),
        "qualifying_events": events,
        "mean_price": serialize_decimal(mean_p),
        "realized_rate": serialize_decimal(rate),
        "d_hat": serialize_decimal(mean),
        "sd": serialize_decimal(sd),
        "ci95": serialize_ci(lower, upper),
    }


def build_descriptives(
    gate_outcome: GateOutcome,
) -> dict[str, Any]:
    assert gate_outcome.cohort is not None and gate_outcome.inputs is not None
    cohort = gate_outcome.cohort
    inputs = gate_outcome.inputs

    per_variable = {
        v: calibration_cell([r for r in cohort if r["variable"] == v]) for v in VARIABLES
    }
    per_distance: dict[str, Any] = {}
    for r in cohort:
        key = serialize_decimal(r["distance"])
        per_distance.setdefault(key, []).append(r)
    per_distance = {k: calibration_cell(v) for k, v in sorted(per_distance.items())}

    pre_cal = calibration_cell(cohort, price_key="p_pre")

    # Locked-NO control: certain-NO contracts' post-event price (target 0).
    locked_prices: list[Decimal] = []
    locked_n_candidates = 0
    for r in cohort:
        locked = r["locked"]
        if locked is None:
            continue
        locked_n_candidates += 1
        candles = inputs.candles.get(locked.ticker, [])
        p, _ = window_midpoint(
            candles,
            r["event_time"] + timedelta(minutes=POST_START_MIN),
            r["event_time"] + timedelta(minutes=POST_END_MIN),
        )
        if p is not None:
            locked_prices.append(p)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        locked_mean = (
            sum(locked_prices, ZERO) / Decimal(len(locked_prices)) if locked_prices else None
        )
    locked_control = {
        "candidates": locked_n_candidates,
        "priced": len(locked_prices),
        "mean_post_price": serialize_decimal(locked_mean) if locked_mean is not None else None,
    }

    # Placebo vs event reaction on the at-risk contract.
    def reactions(offset_min: int) -> dict[str, Any]:
        deltas: list[Decimal] = []
        for r in cohort:
            base = r["event_time"] + timedelta(minutes=offset_min)
            candles = inputs.candles.get(r["ticker"], [])
            pre, _ = window_midpoint(
                candles,
                base + timedelta(minutes=PRE_START_MIN),
                base + timedelta(minutes=PRE_END_MIN),
            )
            post, _ = window_midpoint(
                candles,
                base + timedelta(minutes=POST_START_MIN),
                base + timedelta(minutes=POST_END_MIN),
            )
            if pre is not None and post is not None:
                deltas.append(abs(post - pre))
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            mean_d = sum(deltas, ZERO) / Decimal(len(deltas)) if deltas else None
        return {
            "n_with_both_windows": len(deltas),
            "mean_abs_change": serialize_decimal(mean_d) if mean_d is not None else None,
        }

    placebo = {"event": reactions(0), "placebo": reactions(PLACEBO_OFFSET_MIN)}

    monthly: dict[str, Any] = {}
    for r in cohort:
        monthly.setdefault(str(r["target_date"].month), []).append(r)
    monthly = {k: calibration_cell(v) for k, v in sorted(monthly.items())}

    spreads: list[Decimal] = []
    volumes: list[int] = []
    for r in cohort:
        c = r["post_candle"]
        if c is not None and c.bid_close is not None and c.ask_close is not None:
            spreads.append(Decimal(c.ask_close - c.bid_close) / Decimal(100))
            volumes.append(c.volume)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        mean_spread = sum(spreads, ZERO) / Decimal(len(spreads)) if spreads else None
    liquidity = {
        "mean_post_window_spread": (
            serialize_decimal(mean_spread) if mean_spread is not None else None
        ),
        "total_post_window_candle_volume": sum(volumes),
    }

    # Sensitivities (points only).
    def sensitivity_variant(post_start: int, post_end_mode: str) -> dict[str, Any]:
        rows = []
        for r in cohort_all_at_risk(gate_outcome):
            candles = inputs.candles.get(r["ticker"], [])
            start = r["event_time"] + timedelta(minutes=post_start)
            if post_end_mode == "close":
                end = r["close_dt"] if r["close_dt"] is not None else start + timedelta(hours=12)
            else:
                end = r["event_time"] + timedelta(minutes=int(post_end_mode))
            p, _ = window_midpoint(candles, start, end)
            if p is not None:
                rows.append({**r, "p_post": p})
        return calibration_cell(rows)

    def cohort_all_at_risk(go: GateOutcome) -> list[dict[str, Any]]:
        assert go.cohort is not None
        return [{**r, "close_dt": None} for r in go.cohort]

    trade_rows = []
    for r in cohort:
        candles = inputs.candles.get(r["ticker"], [])
        p = window_trade_price(
            candles,
            r["event_time"] + timedelta(minutes=POST_START_MIN),
            r["event_time"] + timedelta(minutes=POST_END_MIN),
        )
        if p is not None:
            trade_rows.append({**r, "p_post": p})
    sensitivity = {
        "post_extended_to_12h": sensitivity_variant(POST_START_MIN, "close"),
        "trade_price": calibration_cell(trade_rows),
        "post_shift_minus15": sensitivity_variant(0, "105"),
        "post_shift_plus15": sensitivity_variant(30, "135"),
    }

    return {
        "per_variable": per_variable,
        "per_distance": per_distance,
        "pre_window_calibration": pre_cal,
        "locked_no_control": locked_control,
        "placebo_reaction": placebo,
        "monthly": monthly,
        "liquidity": liquidity,
        "exclusions": gate_outcome.exclusions,
        "sensitivity": sensitivity,
    }


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
        "elaboration_flags_for_audit",
    },
    "datasets": {"market", "issuance_frame", "analysis_git_commit"},
    "eligibility_gates": {
        "g1a_market_hashes_match",
        "g1b_issuance_hash_matches",
        "g2_invariants",
        "g3_semantics",
        "g4_cohort",
        "all_gates_passed",
    },
    "primary_results": {"cohort", "calibration", "decision"},
    "primary_results.decision": {"outcome", "evaluation_step", "reason", "interpretation"},
    "descriptive_results": {
        "per_variable",
        "per_distance",
        "pre_window_calibration",
        "locked_no_control",
        "placebo_reaction",
        "monthly",
        "liquidity",
        "exclusions",
        "sensitivity",
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
    if outcome not in (
        "OVERPRICED",
        "UNDERPRICED",
        "EFFICIENT-WITHIN-MARGIN",
        "INCONCLUSIVE",
        "blocked",
    ):
        raise ManifestValidationError(f"unrecognized decision outcome: {outcome!r}")


# --- Orchestration -----------------------------------------------------------

PREREGISTRATION_METADATA: dict[str, Any] = {
    "document": "docs/research/preregistrations/PREREG-20260722-H0017-revision-risk-pricing.md",
    "frozen_date": "2026-07-22",
    "provenance_note": (
        "PREREG Sec 0: structural/coverage facts only seen pre-freeze; no event-window "
        "price, implied probability, at-risk identity, or price-vs-outcome pairing "
        "computed; occurrence-time data excluded by the leakage rule"
    ),
    "original_hypotheses_entry": "HYPOTHESES.md H0017, opened 2026-07-22",
    "elaboration_flags_for_audit": [
        "single_primary_cell_no_multiplicity (Sec 5)",
        "quote_midpoint_price_measurement (Sec 3)",
        "deterministic_normal_ci_in_place_of_bootstrap (Sec 5)",
        "equivalence_margin_0075_frozen (Sec 1/6)",
        "occurrence_time_excluded_leakage_rule (Sec 0)",
        "nyc_only_coverage_exclusion (Sec 2)",
    ],
}


def run(market_dir: Path, issuance_dir: Path) -> dict[str, Any]:
    market_frame = pl.read_parquet(market_dir / "market_prices.parquet")
    labels = pl.read_parquet(market_dir / "settlement_labels.parquet")
    issuance = pl.read_parquet(issuance_dir / "observation_issuances.parquet")

    gate_outcome = run_gates(market_frame, labels, issuance)

    calibration: dict[str, Any] | None = None
    cohort_summary: dict[str, Any] | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None
    if gate_outcome.all_gates_passed:
        assert gate_outcome.cohort is not None
        residuals = [r["p_post"] - Decimal(r["y"]) for r in gate_outcome.cohort]
        mean, sd, lo, hi = mean_ci(residuals)
        lower, upper = lo, hi
        events = sum(r["y"] for r in gate_outcome.cohort)
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            mean_p = sum((r["p_post"] for r in gate_outcome.cohort), ZERO) / Decimal(
                len(gate_outcome.cohort)
            )
            rate = Decimal(events) / Decimal(len(gate_outcome.cohort))
        cohort_summary = {
            "n": len(gate_outcome.cohort),
            "qualifying_events": events,
            "mean_p_post": serialize_decimal(mean_p),
            "realized_rate": serialize_decimal(rate),
        }
        calibration = {
            "d_hat": serialize_decimal(mean),
            "sd": serialize_decimal(sd),
            "ci95": serialize_ci(lo, hi),
        }

    verdict = evaluate(gate_outcome, lower, upper)

    descriptive: dict[str, Any]
    if gate_outcome.all_gates_passed:
        descriptive = build_descriptives(gate_outcome)
    else:
        descriptive = {
            "per_variable": {},
            "per_distance": {},
            "pre_window_calibration": {},
            "locked_no_control": {},
            "placebo_reaction": {},
            "monthly": {},
            "liquidity": {},
            "exclusions": gate_outcome.exclusions or {},
            "sensitivity": {},
        }

    results: dict[str, Any] = {
        "experiment": "EXP-20260722-H0017-revision-risk-pricing",
        "hypothesis": "H0017",
        "preregistration": PREREGISTRATION_METADATA,
        "datasets": {
            "market": {
                "version": "exp-20260722-h0017-market",
                "frames": {
                    "market_prices": MARKET_PRICES_HASH,
                    "settlement_labels": LABELS_HASH,
                },
                "row_counts_expected": {"market_prices": 761475, "settlement_labels": 2862},
            },
            "issuance_frame": {
                "version": "exp-20260722-h0013-replication",
                "frame": "observation_issuances.parquet",
                "content_hash": ISSUANCE_HASH,
            },
            "analysis_git_commit": None,
        },
        "config": {
            "station_cohort": (
                "NYC only (KXHIGHNY tmax_f, KXLOWTNYC tmin_f); CHI/DEN/LAX excluded for "
                "zero candle coverage -- Sec 2"
            ),
            "unit": (
                "variable-day; adjacent at-risk contract (primary) + adjacent locked-NO "
                "contract (control)"
            ),
            "event_time": (
                "archived publication timestamp of the day's first CLI product (naive-UTC) -- Sec 3"
            ),
            "windows": "PRE [E-90m, E-10m]; POST [E+15m, E+120m]; placebo P = E-240m -- Sec 3",
            "price": (
                "bid/ask midpoint of last fully-quoted candle in-window (Decimal "
                "cents/100); trade-price is sensitivity only"
            ),
            "strike_semantics": (
                "between: floor<=v<=cap; greater: v>floor; less: v<cap -- Sec 4, validated by G3"
            ),
            "at_risk": (
                "YES region entirely beyond the preliminary in the physically possible "
                "direction; adjacent = nearest such region -- Sec 4"
            ),
            "outcome": "kalshi_result == 'yes' (payout-validated labels)",
            "primary_estimand": (
                "D = mean(p_POST - y) over the primary cohort; D<0 = underpriced revision risk"
            ),
            "ci_method": (
                "D +/- z*s/sqrt(N), z = 1.959963984540054, deterministic; Decimal "
                "precision-50; 1e-12 quantized decisions"
            ),
            "equivalence_margin": str(EQUIVALENCE_MARGIN),
            "decision_rule": (
                "0 gate fails -> BLOCKED; 1 L(D)>0 -> OVERPRICED; 2 U(D)<0 -> "
                "UNDERPRICED; 3 CI within (-0.075,+0.075) -> EFFICIENT-WITHIN-MARGIN; "
                "4 else INCONCLUSIVE -- Sec 6"
            ),
            "multiplicity": "one primary cell, pooled variables; everything else non-decisional",
            "leakage_rule": "occurrence-time data excluded entirely (final-product-derived)",
            "random_seed": "not applicable -- fully deterministic",
            "software_versions": None,
        },
        "eligibility_gates": gate_outcome.gates,
        "primary_results": {
            "cohort": cohort_summary or {},
            "calibration": calibration or {},
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
                "quantized to 1e-12 via ROUND_HALF_EVEN, fixed-point, 12 digits. Integer "
                "counts remain plain JSON integers."
            ),
        },
        "date_run": None,
    }
    validate_manifest(results)
    return results


def render_markdown(results: dict[str, Any]) -> str:
    decision = results["primary_results"]["decision"]
    cohort = results["primary_results"]["cohort"]
    cal = results["primary_results"]["calibration"]
    d = results["descriptive_results"]

    body = ""
    if cal:
        rate_pct = float(cohort["realized_rate"]) * 100
        ci_lo, ci_hi = (float(v) * 100 for v in cal["ci95"])
        body = f"""## Primary result

- cohort: N = {cohort["n"]} adjacent at-risk variable-days; qualifying revisions = \
{cohort["qualifying_events"]} (realized rate {rate_pct:.1f}%)
- mean post-event implied probability: {float(cohort["mean_p_post"]) * 100:.1f}%
- **D = {float(cal["d_hat"]) * 100:+.2f}pp**, 95% CI [{ci_lo:+.2f}, {ci_hi:+.2f}]pp \
(sd {float(cal["sd"]):.3f})

## Controls (non-decisional)

- locked-NO control: {d.get("locked_no_control")}
- event vs placebo reaction: {d.get("placebo_reaction")}
- pre-window calibration: {d.get("pre_window_calibration")}
"""
    return f"""# EXP-20260722-H0017-revision-risk-pricing

Executed exactly as pre-registered in
`PREREG-20260722-H0017-revision-risk-pricing.md`.

## Decision

**{decision["outcome"]}** ({decision["evaluation_step"]}: {decision["reason"]}).

{decision["interpretation"]}

{body}
Full cell-level results, exclusion accounting, sensitivities, and
liquidity descriptives: see the accompanying `-results.json`. Figures,
narrative, and limitations:
`docs/research/postmortems/2026-07-22-h0017-closeout.md`.
"""


if __name__ == "__main__":
    import contextlib
    import subprocess

    parser = argparse.ArgumentParser()
    parser.add_argument("--market-dir", type=Path, required=True)
    parser.add_argument("--issuance-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()

    output = run(args.market_dir, args.issuance_dir)
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
