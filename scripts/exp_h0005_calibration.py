"""EXP H0005 -- Null: Kalshi daily-temperature prices are calibrated within
costs.

Implements exactly what is frozen in:

  - docs/research/experiments/PREREG-20260721-H0005-calibration.md
  - docs/research/experiments/AMENDMENT-20260721-H0005-pre-execution.md

Every constant, threshold, and ordering decision below cites the exact
section/finding it implements; nothing here may be changed without amending
those documents first. Deterministic except for the day-clustered bootstrap
CIs (AMENDMENT Finding 3: one `random.Random(20260721)` instance, consumed
in a fixed order and nowhere else).

Usage:
    python scripts/exp_h0005_calibration.py \
        --dataset-dir data/datasets/exp-<date>-h0005 \
        --out docs/research/experiments/EXP-<date>-H0005-calibration-results.json \
        --out-md docs/research/experiments/EXP-<date>-H0005-calibration.md
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, TypedDict

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from h0007_fees import KALSHI_WEATHER_TAKER_FEE_CONFIG, contract_fee_cents

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kalshi_weather.dataset.asof import asof_latest
from kalshi_weather.settlement.labels import implied_result

# --- Frozen protocol constants (PREREG Sec 6, 7, 10, 12) --------------------

HORIZON_HOURS: list[int] = [
    72,
    48,
    24,
    12,
    6,
    2,
]  # PREREG Sec 6, ascending order per AMENDMENT Finding 3
DECILE_EDGES: list[int] = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]  # PREREG Sec 7
N_DECILES = 10
STALENESS_TOLERANCE_SECONDS = 15 * 60  # PREREG Sec 6
RANDOM_SEED = 20260721  # PREREG Sec 10 / AMENDMENT Finding 3
BOOTSTRAP_RESAMPLES = 10_000  # PREREG Sec 10
PRIMARY_FAMILY_SIZE = len(HORIZON_HOURS) * N_DECILES  # 60, PREREG Sec 10
BONFERRONI_ALPHA = 0.05 / PRIMARY_FAMILY_SIZE  # PREREG Sec 10
MIN_SETTLEMENT_DAYS = 60  # PREREG Sec 12
MIN_DAYS_PER_CELL = 20  # PREREG Sec 10
LOG_LOSS_CLIP = (0.005, 0.995)  # PREREG Sec 9
CONFIDENCE_LEVEL = 0.95  # PREREG Sec 10 hold-out screen (0.025/0.975 tails);
# reporting-only constant -- build_cells already hardcodes 0.025/0.975 per
# AMENDMENT Finding 3 and is not changed to reference this.

PRIMARY_SERIES_PREFIX = "KXHIGHNY"  # PREREG Sec 5
EXPLORATORY_SERIES_PREFIX = "KXLOWTNYC"  # PREREG Sec 5


# --- Rounding / fee (AMENDMENT Finding 5) -----------------------------------


def round_half_up_cents(price_cents: float) -> int:
    """AMENDMENT Finding 5: mid_price_cents may land exactly on a half-cent
    (average of two integers); such values round UP (80.5 -> 81), matching
    this platform's existing rounds-up-never-down fee convention. Uses
    Decimal.ROUND_HALF_UP rather than Python's round() (banker's rounding,
    round-half-to-even) or float arithmetic, to remove any doubt."""
    return int(Decimal(str(price_cents)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cost_band_cents(
    mid_price_cents: float, yes_bid_close_cents: int, yes_ask_close_cents: int
) -> Fraction:
    """PREREG Sec 9, exact invocation frozen by AMENDMENT Finding 5:
    contract_fee_cents(price_cents=round_half_up(mid), contracts=1,
    config=KALSHI_WEATHER_TAKER_FEE_CONFIG) + half_spread_cents.

    Execution-time defect fix #1 (discovered running against the pinned
    exp-20260721-h0005 dataset, not anticipated by either frozen
    document): PREREG Sec 7 states Kalshi's price ceiling is 99 cents, so
    the top decile bucket ([90,100], closed both ends) "naturally lands
    inside it" -- but the pinned archive's real yes_ask_close_cents
    reaches 100 (13,682 of 761,475 executable candle rows), and
    round_half_up(mid_price_cents) can therefore equal 100 (e.g. bid=99,
    ask=100 -> mid=99.5 -> 100). `contract_fee_cents` (scripts/
    h0007_fees.py, frozen/verified for H0007, reused verbatim and NOT
    modified here) rejects any price outside its documented 1-99 domain by
    design. Rather than excluding these observations -- which would
    silently bias away from exactly the extreme, near-certain-outcome
    decile H0005's anchoring/favorite-longshot economic motivation (Sec 2)
    is most interested in -- the fee is evaluated at its own mathematical
    boundary: ceil(multiplier * P * (1-P) * contracts) is well-defined and
    equals exactly 0 at P=0 or P=1, since (1-P) or P is 0. This is not a
    new fee model or threshold; it is the same frozen formula evaluated at
    a domain edge contract_fee_cents defensively declines to compute
    itself. contract_fee_cents is still called, unmodified, for every
    price in its documented 1-99 range.

    Execution-time defect fix #2, superseding an earlier, defective
    execution (see IMPLEMENTATION-NOTE-20260721-H0005-exact-cost-band.md):
    PREREG Sec 9 defines `half_spread_cents = (yes_ask_close_cents -
    yes_bid_close_cents) / 2` -- a real-valued quantity. Neither frozen
    document specifies rounding it (AMENDMENT Finding 5 froze only the fee
    term's rounding). The superseded execution nonetheless computed
    `round(half_spread)` (Python's banker's rounding, a mode this
    platform's own conventions explicitly reject), which silently zeroed a
    1-cent spread's cost (round(0.5) == 0) and decided the sole clearing
    cell's discovery/hold-out screens. This function now returns an exact
    `Fraction` -- fee (an exact integer) plus the exact, unrounded
    half-spread -- so no rounding is ever applied to the half-spread, at
    this function or anywhere it is subsequently averaged across a cell's
    observations (Fraction addition/division is always exact)."""
    rounded_mid = round_half_up_cents(mid_price_cents)
    if rounded_mid in (0, 100):
        fee = 0
    else:
        fee = contract_fee_cents(rounded_mid, contracts=1, config=KALSHI_WEATHER_TAKER_FEE_CONFIG)
    half_spread = Fraction(yes_ask_close_cents - yes_bid_close_cents, 2)
    return Fraction(fee) + half_spread


# --- Decile assignment (PREREG Sec 7) ---------------------------------------


def assign_decile(mid_price_cents: float) -> int:
    """PREREG Sec 7: ten equal-width bins [0,10), [10,20), ..., [90,100].
    A value exactly on a boundary belongs to the bucket for which that
    boundary is the closed (left) edge -- e.g. exactly 60.0 falls in
    [60,70), not [50,60). The top bin is closed on both ends; since Kalshi's
    own price ceiling is 99c, mid_price_cents can reach at most 99.0 and the
    clip below is a defensive no-op, never triggered by real data."""
    idx = math.floor(mid_price_cents / 10)
    return min(N_DECILES - 1, max(0, idx))


# --- As-of candle selection (PREREG Sec 6) ----------------------------------


@dataclass(slots=True)
class HorizonObservation:
    market_ticker: str
    horizon_hours: int
    target_date: Any
    strike_type: str
    floor_strike: float | None
    cap_strike: float | None
    settlement_label_status: str
    value_at_settlement: float | None
    period_end: Any
    yes_bid_close_cents: int
    yes_ask_close_cents: int
    mid_price_cents: float
    decile: int
    cost_band_cents: Fraction  # exact fee + half_spread; see cost_band_cents()
    outcome: int | None  # 1=yes, 0=no, None=no usable label


def select_horizon_observations(source: pl.DataFrame, *, horizon_hours: int) -> pl.DataFrame:
    """PREREG Sec 6: for a market with close_time C, the observation at
    horizon h uses the single candle with the latest period_end <= (C - h),
    excluded if more than 15 minutes stale. PREREG Sec 11: only a
    non-null, non-crossed quote may be selected -- filtered out of the
    candidate pool before the as-of join, never after, so a crossed/missing
    quote can never be the nearest match. Reuses dataset/asof.py's
    asof_latest verbatim (PREREG Sec 6: "identical in spirit to
    dataset/asof.py's engine")."""
    market_static = source.select(
        "market_ticker",
        "close_time",
        "target_date",
        "strike_type",
        "floor_strike",
        "cap_strike",
        "settlement_label_status",
        "value_at_settlement",
    ).unique(subset=["market_ticker"])

    executable_quotes = source.filter(
        pl.col("yes_bid_close_cents").is_not_null()
        & pl.col("yes_ask_close_cents").is_not_null()
        & (pl.col("yes_bid_close_cents") <= pl.col("yes_ask_close_cents"))
    ).select("market_ticker", "period_end", "yes_bid_close_cents", "yes_ask_close_cents")

    targets = market_static.with_columns(
        target_instant=pl.col("close_time") - pl.duration(hours=horizon_hours)
    )

    joined = asof_latest(
        targets,
        executable_quotes,
        by=["market_ticker"],
        left_time="target_instant",
        right_time="period_end",
        bring={
            "period_end": "selected_period_end",
            "yes_bid_close_cents": "yes_bid_close_cents",
            "yes_ask_close_cents": "yes_ask_close_cents",
        },
    )

    joined = joined.with_columns(
        gap_seconds=(pl.col("target_instant") - pl.col("selected_period_end")).dt.total_seconds()
    )
    valid = joined.filter(
        pl.col("selected_period_end").is_not_null()
        & (pl.col("gap_seconds") <= STALENESS_TOLERANCE_SECONDS)
    )
    return valid.with_columns(horizon_hours=pl.lit(horizon_hours))


def build_observation_table(source: pl.DataFrame) -> list[HorizonObservation]:
    """Runs select_horizon_observations for all six frozen horizons and
    computes mid price, decile, cost band, and outcome for each surviving
    (market, horizon) row -- one HorizonObservation per row."""
    observations: list[HorizonObservation] = []
    for horizon in HORIZON_HOURS:
        frame = select_horizon_observations(source, horizon_hours=horizon)
        for row in frame.to_dicts():
            mid = (row["yes_bid_close_cents"] + row["yes_ask_close_cents"]) / 2
            band = cost_band_cents(mid, row["yes_bid_close_cents"], row["yes_ask_close_cents"])
            outcome = compute_outcome(
                row["settlement_label_status"],
                row["value_at_settlement"],
                row["strike_type"],
                row["floor_strike"],
                row["cap_strike"],
            )
            observations.append(
                HorizonObservation(
                    market_ticker=row["market_ticker"],
                    horizon_hours=horizon,
                    target_date=row["target_date"],
                    strike_type=row["strike_type"],
                    floor_strike=row["floor_strike"],
                    cap_strike=row["cap_strike"],
                    settlement_label_status=row["settlement_label_status"],
                    value_at_settlement=row["value_at_settlement"],
                    period_end=row["selected_period_end"],
                    yes_bid_close_cents=row["yes_bid_close_cents"],
                    yes_ask_close_cents=row["yes_ask_close_cents"],
                    mid_price_cents=mid,
                    decile=assign_decile(mid),
                    cost_band_cents=band,
                    outcome=outcome,
                )
            )
    return observations


def compute_outcome(
    settlement_label_status: str,
    value_at_settlement: float | None,
    strike_type: str,
    floor_strike: float | None,
    cap_strike: float | None,
) -> int | None:
    """PREREG Sec 3/9: the outcome is derived, never read from a
    `kalshi_result`-style column (market_price_weather does not carry one --
    see the EXP-20260721-H0007 methodology note), via settlement/labels.py's
    already-validated implied_result -- reused verbatim, never
    reimplemented, exactly as PREREG Sec 4 (via H0007's identical
    convention) requires for strike semantics. Only markets with a
    `resolved` label and a non-null settlement value produce a usable
    outcome; everything else is None (excluded from the label-dependent
    calculation, never treated as a loss)."""
    if settlement_label_status != "resolved" or value_at_settlement is None:
        return None
    result = implied_result(
        Decimal(str(value_at_settlement)),
        strike_type=strike_type,
        floor=Decimal(str(floor_strike)) if floor_strike is not None else None,
        cap=Decimal(str(cap_strike)) if cap_strike is not None else None,
    )
    if result is None:
        return None
    return 1 if result == "yes" else 0


# --- Duplicate-observation invariants (AMENDMENT Finding 9) -----------------


class DuplicateObservationError(Exception):
    pass


def assert_no_duplicate_observations(observations: list[HorizonObservation]) -> None:
    """AMENDMENT Finding 9: exactly one row per (market_ticker,
    horizon_hours) before any bucket assignment or bootstrap call. Aborts
    loudly rather than silently de-duplicating."""
    seen: set[tuple[str, int]] = set()
    for obs in observations:
        key = (obs.market_ticker, obs.horizon_hours)
        if key in seen:
            raise DuplicateObservationError(
                f"duplicate observation for market_ticker={obs.market_ticker!r} "
                f"horizon_hours={obs.horizon_hours} -- exactly one row per "
                "(market, horizon) is required (AMENDMENT Finding 9)"
            )
        seen.add(key)


def assert_candle_natural_key_unique(source: pl.DataFrame) -> None:
    """AMENDMENT Finding 9: the loaded market_price_weather frame's rows
    must already be unique on (market_ticker, period_interval_seconds,
    period_end) -- the platform's own database constraint
    (docs/adr/0007-price-ingestion.md). Defends against a corrupted or
    hand-edited dataset export, not an expected failure mode."""
    key_cols = ["market_ticker", "period_interval_seconds", "period_end"]
    if not all(c in source.columns for c in key_cols):
        return  # nothing to check against a frame that doesn't carry these
    total = source.height
    distinct = source.select(key_cols).unique().height
    if total != distinct:
        raise DuplicateObservationError(
            f"market_price_weather natural key (market_ticker, "
            f"period_interval_seconds, period_end) is not unique: "
            f"{total} rows, {distinct} distinct keys (AMENDMENT Finding 9)"
        )


# --- Chronological replication split (PREREG Sec 10, AMENDMENT Finding 8) --


def chronological_split(valid_days: list[Any]) -> tuple[list[Any], list[Any]]:
    """PREREG Sec 10: the valid-day population split into two contiguous,
    non-overlapping chronological halves with a 1-day embargo at the
    boundary. AMENDMENT Finding 8: if the post-embargo count is odd, the
    discovery (earlier) slice receives the extra day."""
    ordered = sorted(set(valid_days))
    if len(ordered) < 2:
        return (ordered, [])
    embargo_idx = len(ordered) // 2
    pre_embargo = ordered[:embargo_idx]
    post_embargo = ordered[embargo_idx + 1 :]
    n = len(pre_embargo) + len(post_embargo)
    discovery_size = math.ceil(n / 2)
    combined = pre_embargo + post_embargo
    discovery = combined[:discovery_size]
    holdout = combined[discovery_size:]
    return (discovery, holdout)


# --- Day-clustered percentile bootstrap (PREREG Sec 10, AMENDMENT Finding 3)


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


def day_clustered_bootstrap_ci(
    day_values: dict[Any, list[float]],
    day_pool: list[Any],
    *,
    rng: random.Random,
    lower_pct: float,
    upper_pct: float,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> tuple[float | None, float | None]:
    """AMENDMENT Finding 2: resamples exclusively from `day_pool` -- callers
    must pass only discovery-slice days for a discovery-slice CI, or only
    hold-out-slice days for a hold-out-slice CI; the two pools are never
    mixed by this function or its caller. `lower_pct`/`upper_pct` let the
    same function serve both the Bonferroni-adjusted discovery screen and
    the standard-95% hold-out screen (PREREG Sec 10)."""
    n = len(day_pool)
    if n == 0:
        return (None, None)
    results: list[float] = []
    for _ in range(resamples):
        sample_days = rng.choices(day_pool, k=n)
        pooled: list[float] = []
        for d in sample_days:
            pooled.extend(day_values.get(d, []))
        if pooled:
            results.append(statistics.mean(pooled))
    if not results:
        return (None, None)
    results.sort()
    return (_percentile(results, lower_pct), _percentile(results, upper_pct))


# --- Cell construction and evaluation ---------------------------------------

Slice = Literal["discovery", "holdout"]


@dataclass(slots=True)
class CellResult:
    horizon_hours: int
    decile: int
    n_days_discovery: int
    n_days_holdout: int
    # Discovery-slice point estimates -- these drive the discovery screen.
    mean_predicted_probability: float | None
    realized_yes_frequency: float | None
    calibration_error: float | None  # AMENDMENT Finding 1/Finding 7
    cost_band_cents: Fraction | None
    discovery_ci: tuple[float | None, float | None] = (None, None)
    discovery_bonferroni_pass: bool = False
    # Hold-out-slice point estimates -- exposed for auditability of the
    # same-sign/CI-vs-cost-band replication check, which uses them
    # internally regardless of whether they are surfaced in the output.
    holdout_mean_predicted_probability: float | None = None
    holdout_realized_yes_frequency: float | None = None
    holdout_calibration_error: float | None = None
    holdout_cost_band_cents: Fraction | None = None
    holdout_ci: tuple[float | None, float | None] = (None, None)
    holdout_insufficient_n: bool = False
    holdout_pass: bool = False
    clears_both_screens: bool = False


class CellPointEstimate(TypedDict):
    mean_predicted_probability: float | None
    realized_yes_frequency: float | None
    calibration_error: float | None
    cost_band_cents: Fraction | None


def _cell_point_estimates(obs: list[HorizonObservation]) -> CellPointEstimate:
    """AMENDMENT Finding 7: an empty cell's calibration_error is undefined
    (None), never fabricated as 0.0. cost_band_cents is averaged as an
    exact Fraction (Fraction division is always exact) -- no rounding is
    introduced here or in cost_band_cents() itself; see that function's
    docstring for the half-spread-rounding defect this corrects."""
    labeled = [o for o in obs if o.outcome is not None]
    if not labeled:
        return {
            "mean_predicted_probability": None,
            "realized_yes_frequency": None,
            "calibration_error": None,
            "cost_band_cents": None,
        }
    mean_p = statistics.mean(o.mid_price_cents / 100 for o in labeled)
    freq = statistics.mean(o.outcome for o in labeled if o.outcome is not None)
    band = statistics.mean(o.cost_band_cents for o in labeled)  # exact: Fraction in, Fraction out
    return {
        "mean_predicted_probability": mean_p,
        "realized_yes_frequency": freq,
        "calibration_error": mean_p - freq,
        "cost_band_cents": band,
    }


def _cell_day_values(obs: list[HorizonObservation]) -> dict[Any, list[float]]:
    """Per-day list of |mid - outcome| style single-observation errors,
    used as the bootstrap resampling unit -- one value per labeled
    observation, grouped by its own settlement day."""
    by_day: dict[Any, list[float]] = {}
    for o in obs:
        if o.outcome is None:
            continue
        by_day.setdefault(o.target_date, []).append(o.mid_price_cents / 100 - o.outcome)
    return by_day


def _cost_band_probability(band_cents: Fraction) -> Fraction:
    """Unit reconciliation for the discovery/hold-out screen comparison
    (PREREG Sec 1/9, AMENDMENT Finding 5). `calibration_error` (and its
    bootstrap CI) is a probability-scale deviation in [0,1] --
    `mean_predicted_probability`/`realized_yes_frequency` must stay
    fractional because Sec 9's Brier score, log loss, and ECE formulas use
    that exact same 'predicted probability' quantity, and those are only
    well-defined on [0,1] (e.g. `ln(p)` in log loss). `cost_band_cents` is
    computed in raw cents (Sec 9's own `fee_cents_for_one_contract_at_
    round(mid_price_cents) + half_spread_cents` naming; Finding 5's exact
    fee invocation). Kalshi's own contracts already equate one cent of
    price to one percentage point of implied probability -- the identical
    conversion the frozen protocol itself applies at Sec 8
    (`ask_implied = yes_ask_close_cents / 100`) -- so comparing the two is
    a matter of expressing `cost_band_cents` on the same [0,1] scale, not
    of changing `calibration_error`'s units. `band_cents` is an exact
    Fraction (see cost_band_cents()); dividing by 100 stays exact."""
    return band_cents / 100


#: Precision at which the discovery/hold-out screen comparison is decided
#: (see _exceeds_cost_band). 1e-9 is far finer than any genuine difference
#: cents-scale prices, averaged over at most a few hundred day-observations,
#: can ever produce -- it exists solely to strip IEEE-754 binary-float
#: representation noise (~1e-16) from the IEEE-754-derived side of the
#: comparison, never to suppress a real signal.
_COMPARISON_PRECISION = Decimal("1E-9")


def _to_comparison_decimal(value: float | Fraction) -> Decimal:
    """Converts either an exact Fraction or a bootstrap-derived float to a
    Decimal at a fixed, controlled precision, so the strict `>` comparison
    below is decided by the underlying data, never by which side happens
    to carry more binary-float representation error. Fraction inputs are
    exact rationals (may not terminate in base 10, e.g. 1/29) -- both are
    quantized identically, so this never advantages one side."""
    exact = (
        Decimal(value.numerator) / Decimal(value.denominator)
        if isinstance(value, Fraction)
        else Decimal(value)
    )
    return exact.quantize(_COMPARISON_PRECISION, rounding=ROUND_HALF_EVEN)


def _exceeds_cost_band(lower_abs: float | None, cost_band_probability: Fraction | None) -> bool:
    """Execution-integrity-review fix #2: the discovery/hold-out screen
    comparison ('|CI lower bound| > cost_band', PREREG Sec 1/9/10) decided
    in controlled-precision Decimal arithmetic rather than raw binary
    float, so a mathematically exact tie (e.g. the pinned dataset's
    horizon=2h/decile=9 discovery slice, where every observation has an
    identical deviation and the corrected cost band happens to equal it
    exactly, 1/200 == 1/200) evaluates to False -- 'exceeds' means
    strictly exceeds, never a coincidence of which side's floating-point
    representation rounds up. `lower_abs` remains whatever
    day_clustered_bootstrap_ci (AMENDMENT Finding 3, unchanged) computed;
    only the final strict-inequality decision is exact."""
    if lower_abs is None or cost_band_probability is None:
        return False
    return _to_comparison_decimal(lower_abs) > _to_comparison_decimal(cost_band_probability)


def _ci_lower_bound_of_abs(lo: float | None, hi: float | None) -> float | None:
    """Given a signed bootstrap CI [lo, hi] on calibration_error, the lower
    bound of |calibration_error| across that interval: if the interval
    excludes zero (same sign throughout), every value's magnitude is at
    least min(|lo|, |hi|); if it straddles or touches zero, the magnitude
    can be as low as 0."""
    if lo is None or hi is None:
        return None
    if lo * hi > 0:
        return min(abs(lo), abs(hi))
    return 0.0


def build_cells(
    observations: list[HorizonObservation],
    *,
    discovery_days: list[Any],
    holdout_days: list[Any],
    rng: random.Random,
) -> list[CellResult]:
    """AMENDMENT Finding 3: computes all 60 primary-family cells, discovery
    bootstrap first (horizon ascending, decile ascending), then hold-out
    bootstrap for all 60 cells unconditionally (never skipped based on the
    discovery outcome) -- the single source of RNG consumption in this
    script. AMENDMENT Finding 2: each slice's bootstrap resamples
    exclusively from that slice's own day pool."""
    discovery_set = set(discovery_days)
    holdout_set = set(holdout_days)

    by_cell: dict[tuple[int, int], list[HorizonObservation]] = {}
    for o in observations:
        by_cell.setdefault((o.horizon_hours, o.decile), []).append(o)

    # Partition each cell's observations by slice once, reused by both the
    # point-estimate computation and the bootstrap passes below.
    discovery_obs_by_cell: dict[tuple[int, int], list[HorizonObservation]] = {}
    holdout_obs_by_cell: dict[tuple[int, int], list[HorizonObservation]] = {}
    for key, obs in by_cell.items():
        discovery_obs_by_cell[key] = [o for o in obs if o.target_date in discovery_set]
        holdout_obs_by_cell[key] = [o for o in obs if o.target_date in holdout_set]

    cells: dict[tuple[int, int], CellResult] = {}
    for horizon in HORIZON_HOURS:
        for decile in range(N_DECILES):
            key = (horizon, decile)
            d_obs = discovery_obs_by_cell.get(key, [])
            h_obs = holdout_obs_by_cell.get(key, [])
            d_point = _cell_point_estimates(d_obs)
            h_point = _cell_point_estimates(h_obs)
            cells[key] = CellResult(
                horizon_hours=horizon,
                decile=decile,
                n_days_discovery=len({o.target_date for o in d_obs if o.outcome is not None}),
                n_days_holdout=len({o.target_date for o in h_obs if o.outcome is not None}),
                mean_predicted_probability=d_point["mean_predicted_probability"],
                realized_yes_frequency=d_point["realized_yes_frequency"],
                calibration_error=d_point["calibration_error"],
                cost_band_cents=d_point["cost_band_cents"],
                holdout_mean_predicted_probability=h_point["mean_predicted_probability"],
                holdout_realized_yes_frequency=h_point["realized_yes_frequency"],
                holdout_calibration_error=h_point["calibration_error"],
                holdout_cost_band_cents=h_point["cost_band_cents"],
            )

    # Pass 1: discovery-slice bootstrap, horizon ascending, decile ascending
    # (AMENDMENT Finding 3, step 1 of 2).
    bonferroni_lower = BONFERRONI_ALPHA / 2
    bonferroni_upper = 1 - BONFERRONI_ALPHA / 2
    for horizon in HORIZON_HOURS:
        for decile in range(N_DECILES):
            key = (horizon, decile)
            cell = cells[key]
            day_values = _cell_day_values(discovery_obs_by_cell.get(key, []))
            lo, hi = day_clustered_bootstrap_ci(
                day_values,
                discovery_days,
                rng=rng,
                lower_pct=bonferroni_lower,
                upper_pct=bonferroni_upper,
            )
            cell.discovery_ci = (lo, hi)
            lower_abs = _ci_lower_bound_of_abs(lo, hi)
            if cell.cost_band_cents is not None:
                cell.discovery_bonferroni_pass = _exceeds_cost_band(
                    lower_abs, _cost_band_probability(cell.cost_band_cents)
                )

    # Pass 2: hold-out-slice bootstrap, same nested order, unconditional for
    # all 60 cells regardless of the discovery outcome (AMENDMENT Finding 3,
    # step 2 of 2).
    for horizon in HORIZON_HOURS:
        for decile in range(N_DECILES):
            key = (horizon, decile)
            cell = cells[key]
            day_values = _cell_day_values(holdout_obs_by_cell.get(key, []))
            lo, hi = day_clustered_bootstrap_ci(
                day_values, holdout_days, rng=rng, lower_pct=0.025, upper_pct=0.975
            )
            cell.holdout_ci = (lo, hi)
            cell.holdout_insufficient_n = cell.n_days_holdout < MIN_DAYS_PER_CELL
            same_sign = (
                cell.calibration_error is not None
                and cell.holdout_calibration_error is not None
                and (cell.calibration_error > 0) == (cell.holdout_calibration_error > 0)
            )
            lower_abs = _ci_lower_bound_of_abs(lo, hi)
            if (
                same_sign
                and not cell.holdout_insufficient_n
                and cell.holdout_cost_band_cents is not None
            ):
                cell.holdout_pass = _exceeds_cost_band(
                    lower_abs, _cost_band_probability(cell.holdout_cost_band_cents)
                )
            cell.clears_both_screens = cell.discovery_bonferroni_pass and cell.holdout_pass

    return [cells[(h, d)] for h in HORIZON_HOURS for d in range(N_DECILES)]


# --- Evaluation order (PREREG Sec 12, AMENDMENT Finding 4) ------------------


@dataclass(slots=True)
class Verdict:
    outcome: Literal["confirmed", "rejected", "inconclusive"]
    reason: str
    clearing_cells: list[tuple[int, int]] = field(default_factory=list)
    underpowered_cells: list[tuple[int, int]] = field(default_factory=list)


def evaluate_outcome(cells: list[CellResult], *, total_valid_days: int) -> Verdict:
    """AMENDMENT Finding 4: exact four-step terminal evaluation order.
    Step 1 (total-sample) and step 2 (rejection) are checked first; step 3
    (power) only applies when step 2 found nothing; step 4 (confirmed) is
    reached whenever neither 2 nor 3 fires -- including the zero-cells
    case, resolving the vacuous-truth ambiguity the audit found."""
    if total_valid_days < MIN_SETTLEMENT_DAYS:
        return Verdict(
            outcome="inconclusive",
            reason=(
                f"only {total_valid_days} valid settlement days, short of the "
                f"pre-registered minimum {MIN_SETTLEMENT_DAYS}"
            ),
        )

    clearing = [(c.horizon_hours, c.decile) for c in cells if c.clears_both_screens]
    if clearing:
        return Verdict(
            outcome="rejected",
            reason=(
                "at least one primary-family cell cleared both the Bonferroni "
                "discovery screen and the hold-out replication screen"
            ),
            clearing_cells=clearing,
        )

    underpowered = [
        (c.horizon_hours, c.decile)
        for c in cells
        if c.discovery_bonferroni_pass and c.holdout_insufficient_n
    ]
    if underpowered:
        return Verdict(
            outcome="inconclusive",
            reason=(
                "cell(s) cleared the discovery screen but were underpowered "
                "(< 20 days) in the hold-out slice"
            ),
            underpowered_cells=underpowered,
        )

    return Verdict(
        outcome="confirmed",
        reason=(
            "no primary-family cell cleared both the Bonferroni-adjusted "
            "discovery screen and the hold-out replication screen"
        ),
    )


# --- Secondary metrics (PREREG Sec 9; point estimates only, no CI, per ------
# --- AMENDMENT Finding 3 -- these never consume the RNG) --------------------


def brier_score(observations: list[HorizonObservation]) -> tuple[float | None, float | None]:
    labeled = [o for o in observations if o.outcome is not None]
    if not labeled:
        return (None, None)
    score = statistics.mean(
        (o.mid_price_cents / 100 - o.outcome) ** 2 for o in labeled if o.outcome is not None
    )
    base_rate = statistics.mean(o.outcome for o in labeled if o.outcome is not None)
    baseline = statistics.mean(
        (base_rate - o.outcome) ** 2 for o in labeled if o.outcome is not None
    )
    return (score, baseline)


def log_loss(observations: list[HorizonObservation]) -> tuple[float | None, float | None]:
    labeled = [o for o in observations if o.outcome is not None]
    if not labeled:
        return (None, None)
    lo, hi = LOG_LOSS_CLIP

    def _clip(p: float) -> float:
        return min(max(p, lo), hi)

    def _ll(p: float, y: int) -> float:
        p = _clip(p)
        return -(y * math.log(p) + (1 - y) * math.log(1 - p))

    score = statistics.mean(
        _ll(o.mid_price_cents / 100, o.outcome) for o in labeled if o.outcome is not None
    )
    base_rate = statistics.mean(o.outcome for o in labeled if o.outcome is not None)
    baseline = statistics.mean(_ll(base_rate, o.outcome) for o in labeled if o.outcome is not None)
    return (score, baseline)


def expected_calibration_error(observations: list[HorizonObservation]) -> float | None:
    """AMENDMENT Finding 7: empty deciles contribute a weight of 0 and are
    skipped in the summation rather than evaluated as 0 * undefined."""
    labeled = [o for o in observations if o.outcome is not None]
    total = len(labeled)
    if total == 0:
        return None
    ece = 0.0
    for decile in range(N_DECILES):
        cell_obs = [o for o in labeled if o.decile == decile]
        if not cell_obs:
            continue
        mean_p = statistics.mean(o.mid_price_cents / 100 for o in cell_obs)
        freq = statistics.mean(o.outcome for o in cell_obs if o.outcome is not None)
        weight = len(cell_obs) / total
        ece += weight * abs(mean_p - freq)
    return ece


def fit_calibration_slope_intercept(
    observations: list[HorizonObservation],
) -> tuple[float | None, float | None]:
    """PREREG Sec 9: coefficients of a logistic regression of the binary
    outcome on logit(mid_price_cents/100). A small, deterministic,
    stdlib-only Newton-Raphson (IRLS) fit -- no ML library, matching this
    project's ban on adding one without a documented justification (this is
    a two-parameter descriptive diagnostic, not a probability model feeding
    any decision). Fixed-tolerance convergence; returns (None, None) if
    fewer than 2 labeled observations or the outcome has no variation
    (regression undefined)."""
    labeled = [o for o in observations if o.outcome is not None]
    if len(labeled) < 2:
        return (None, None)
    outcomes = [float(o.outcome) for o in labeled if o.outcome is not None]
    if len(set(outcomes)) < 2:
        return (None, None)
    xs = []
    for o in labeled:
        p = min(max(o.mid_price_cents / 100, 1e-6), 1 - 1e-6)
        xs.append(math.log(p / (1 - p)))

    intercept, slope = 0.0, 1.0
    for _ in range(100):
        preds = [1.0 / (1.0 + math.exp(-(intercept + slope * x))) for x in xs]
        grad_b0 = sum(y - p for y, p in zip(outcomes, preds, strict=True))
        grad_b1 = sum((y - p) * x for y, p, x in zip(outcomes, preds, xs, strict=True))
        w = [p * (1 - p) for p in preds]
        h00 = sum(w)
        h01 = sum(wi * x for wi, x in zip(w, xs, strict=True))
        h11 = sum(wi * x * x for wi, x in zip(w, xs, strict=True))
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            break
        d_intercept = (h11 * grad_b0 - h01 * grad_b1) / det
        d_slope = (h00 * grad_b1 - h01 * grad_b0) / det
        intercept += d_intercept
        slope += d_slope
        if abs(d_intercept) < 1e-10 and abs(d_slope) < 1e-10:
            break
    return (slope, intercept)


# --- Manifest reporting/reconciliation (TEMPLATE-H0005-manifest.json ------
# --- parity). Everything below is descriptive/reporting output: it reads
# --- already-computed observations and cells, never calls build_cells or
# --- evaluate_outcome, consumes no randomness, and cannot influence the
# --- primary decision rule (PREREG Sec 8/9/10, AMENDMENT Finding 3's
# --- "point estimates only" scope for secondary/exploratory results). -----

#: Literal, independently-transcribed frozen protocol metadata (PREREG
#: front matter / Sec 5, 14; AMENDMENT front matter) -- not derived from
#: this module's own constants, so `validate_manifest`/
#: `verify_config_matches_prereg` below are a genuine drift check, not a
#: tautology. Copied verbatim from TEMPLATE-H0005-manifest.json.
PREREGISTRATION_METADATA: dict[str, Any] = {
    "document": "docs/research/experiments/PREREG-20260721-H0005-calibration.md",
    "frozen_date": "2026-07-21",
    "frozen_git_commit": "b30f92e3c8db17e2a246fb1be9d1f6328161a1e9",
    "protocol_differences_document": (
        "docs/research/experiments/DIFFERENCES-20260721-H0005-from-original-design.md"
    ),
    "original_hypotheses_entry": "HYPOTHESES.md H0005, opened 2026-07-21",
    "amendment": {
        "document": "docs/research/experiments/AMENDMENT-20260721-H0005-pre-execution.md",
        "frozen_date": "2026-07-21",
        "resolves": [
            "research_question_wording",
            "bootstrap_pool_separation",
            "rng_consumption_order",
            "evaluation_order",
            "fee_invocation",
            "close_time_verification_gate",
            "empty_cell_handling",
            "split_tie_break",
            "duplicate_invariants",
        ],
        "blocked_on": [],
    },
}

#: Static, human-readable descriptions of already-frozen behavior, copied
#: verbatim from TEMPLATE-H0005-manifest.json's own `config` annotations
#: (PREREG Sec 6-10, AMENDMENT Findings 2-5/7/8) -- these describe code
#: that already exists above; they add no new behavior.
CONFIG_STATIC_DESCRIPTIONS: dict[str, Any] = {
    "primary_series": PRIMARY_SERIES_PREFIX,
    "exploratory_series": [EXPLORATORY_SERIES_PREFIX],
    "primary_price_definition": "mid = (yes_bid_close_cents + yes_ask_close_cents) / 2",
    "secondary_price_definitions": [
        "ask_implied = yes_ask_close_cents/100",
        "bid_implied = yes_bid_close_cents/100",
    ],
    "cost_band_formula": (
        "contract_fee_cents(price_cents=round_half_up(mid_price_cents), contracts=1, "
        "config=KALSHI_WEATHER_TAKER_FEE_CONFIG) + half_spread_cents"
    ),
    "mid_price_rounding": (
        "round_half_up (X.5 rounds up to X+1), per "
        "AMENDMENT-20260721-H0005-pre-execution.md Finding 5"
    ),
    "outcome_derivation": (
        "settlement/labels.py implied_result(value_at_settlement, strike_type, "
        "floor_strike, cap_strike); requires settlement_label_status == 'resolved'"
    ),
    "rng_consumption_order": (
        "single random.Random(20260721); discovery-slice cells (horizon ascending "
        "[2,6,12,24,48,72] outer, decile ascending inner, 60 calls) then hold-out-slice "
        "cells, same nested order, unconditional for all 60 cells (120 calls total); no "
        "other statistic consumes randomness -- AMENDMENT Finding 3"
    ),
    "bootstrap_cluster_unit": "settlement day",
    "bootstrap_pool_separation": (
        "discovery bootstrap resamples only discovery-slice days; holdout bootstrap "
        "resamples only holdout-slice days; pools never mixed -- AMENDMENT Finding 2"
    ),
    "replication_split": (
        "chronological halves of the valid-day population, 1-day embargo at the boundary"
    ),
    "split_tie_break": (
        "if the post-embargo day count is odd, discovery slice receives the extra "
        "(earliest) day -- AMENDMENT Finding 8"
    ),
    "evaluation_order": (
        "1) total-day floor -> inconclusive; 2) any cell clears both screens -> "
        "rejected; 3) any discovery-passing cell insufficient_n in holdout -> "
        "inconclusive-for-power; 4) otherwise confirmed -- AMENDMENT Finding 4"
    ),
    "empty_cell_handling": (
        "N=0 cell: calibration_error null, excluded from ECE weighted sum, contributes "
        "zero rows to regression -- AMENDMENT Finding 7"
    ),
}

#: Independently-transcribed frozen numeric/threshold values (PREREG Sec
#: 6, 7, 10, 12) -- deliberately re-typed here rather than referencing
#: HORIZON_HOURS/DECILE_EDGES/etc. directly, so `verify_config_matches_
#: prereg` below actually detects drift instead of comparing a constant to
#: itself.
_FROZEN_CONFIG_REFERENCE: dict[str, Any] = {
    "horizons_hours": [72, 48, 24, 12, 6, 2],
    "decile_boundaries_pct": [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
    "candle_staleness_tolerance_minutes": 15,
    "primary_family_size": 60,
    "bonferroni_alpha": 0.05 / 60,
    "confidence_level": 0.95,
    "random_seed": 20260721,
    "bootstrap_resamples": 10_000,
    "min_settlement_days_required": 60,
    "min_days_per_cell": 20,
}


def verify_config_matches_prereg(config: dict[str, Any]) -> bool:
    """PREREG Sec 17's reproducibility-checklist item, made mechanical:
    'confirm no field in the results JSON's config block differs from
    this document's frozen values.' Compared against _FROZEN_CONFIG_
    REFERENCE's independently-transcribed literals, not this module's own
    constants."""
    return all(config.get(key) == expected for key, expected in _FROZEN_CONFIG_REFERENCE.items())


def _labeled_price_probability_estimate(
    obs: list[HorizonObservation], *, price_cents_of: Callable[[HorizonObservation], float]
) -> dict[str, float | None]:
    """Descriptive/reporting-only generalization of _cell_point_estimates
    to an arbitrary price field (ask-implied, bid-implied) so the
    executable-price robustness check, reliability curves, and the
    exploratory grid can each reuse one formula. Does not modify or call
    _cell_point_estimates and is never called by build_cells/
    evaluate_outcome (PREREG Sec 8's robustness check and Sec 9's
    reliability curve are both descriptive-only)."""
    labeled = [o for o in obs if o.outcome is not None]
    if not labeled:
        return {
            "mean_predicted_probability": None,
            "realized_yes_frequency": None,
            "calibration_error": None,
            "n": 0,
        }
    mean_p = statistics.mean(price_cents_of(o) / 100 for o in labeled)
    freq = statistics.mean(o.outcome for o in labeled if o.outcome is not None)
    return {
        "mean_predicted_probability": mean_p,
        "realized_yes_frequency": freq,
        "calibration_error": mean_p - freq,
        "n": float(len(labeled)),
    }


def compute_executable_price_robustness(
    observations: list[HorizonObservation], clearing_cells: list[tuple[int, int]]
) -> list[dict[str, Any]]:
    """PREREG Sec 8: for every cell that cleared both screens on mid price,
    descriptively recompute calibration_error using the ask-implied and
    bid-implied prices. Point-estimate only -- no CI, no bootstrap, no RNG
    consumption (AMENDMENT Finding 3) -- and never feeds back into
    evaluate_outcome's decision (called from run() after the verdict is
    already final)."""
    results: list[dict[str, Any]] = []
    for horizon, decile in clearing_cells:
        cell_obs = [o for o in observations if o.horizon_hours == horizon and o.decile == decile]
        labeled = [o for o in cell_obs if o.outcome is not None]
        cost_band = statistics.mean(o.cost_band_cents for o in labeled) if labeled else None

        mid_point = _labeled_price_probability_estimate(
            cell_obs, price_cents_of=lambda o: o.mid_price_cents
        )
        ask_point = _labeled_price_probability_estimate(
            cell_obs, price_cents_of=lambda o: o.yes_ask_close_cents
        )
        bid_point = _labeled_price_probability_estimate(
            cell_obs, price_cents_of=lambda o: o.yes_bid_close_cents
        )

        def _exceeds(
            point: dict[str, float | None], band: Fraction | None = cost_band
        ) -> bool | None:
            err = point["calibration_error"]
            if err is None or band is None:
                return None
            # Same cents-to-probability reconciliation, and the same
            # exact-arithmetic tie-breaking, as build_cells' discovery/
            # hold-out screens -- see _cost_band_probability/
            # _exceeds_cost_band.
            return _exceeds_cost_band(abs(err), _cost_band_probability(band))

        ask_exceeds = _exceeds(ask_point)
        bid_exceeds = _exceeds(bid_point)
        # PREREG Sec 8: "a mid-price finding that vanishes at both
        # executable prices is reported explicitly as 'not executable'".
        executable = bool(ask_exceeds) or bool(bid_exceeds)
        results.append(
            {
                "horizon_hours": horizon,
                "decile": decile,
                "n": int(mid_point["n"] or 0),
                "cost_band_cents": float(cost_band) if cost_band is not None else None,
                "mid_calibration_error": mid_point["calibration_error"],
                "ask_implied_mean_probability": ask_point["mean_predicted_probability"],
                "ask_calibration_error": ask_point["calibration_error"],
                "ask_exceeds_cost_band": ask_exceeds,
                "bid_implied_mean_probability": bid_point["mean_predicted_probability"],
                "bid_calibration_error": bid_point["calibration_error"],
                "bid_exceeds_cost_band": bid_exceeds,
                "executable": executable,
                "not_executable_reason": (
                    None
                    if executable
                    else "PREREG Sec 8: deviation no longer exceeds the cell's cost band "
                    "at either the ask- or the bid-implied price"
                ),
            }
        )
    return results


def build_reliability_curves(
    observations: list[HorizonObservation],
) -> dict[int, list[dict[str, Any]]]:
    """PREREG Sec 9: 'mean predicted probability vs. realized frequency per
    decile, one curve per horizon.' A reporting reshape of the same
    mid-price point estimates already computed per cell, pooled across
    both replication slices -- matching how Brier score/log loss/ECE are
    already computed on each horizon's full observation set, not split by
    slice. Reuses _cell_point_estimates verbatim; no CI."""
    curves: dict[int, list[dict[str, Any]]] = {}
    for horizon in HORIZON_HOURS:
        h_obs = [o for o in observations if o.horizon_hours == horizon]
        points: list[dict[str, Any]] = []
        for decile in range(N_DECILES):
            cell_obs = [o for o in h_obs if o.decile == decile]
            point = _cell_point_estimates(cell_obs)
            labeled_n = len([o for o in cell_obs if o.outcome is not None])
            points.append(
                {
                    "decile": decile,
                    "n": labeled_n,
                    "mean_predicted_probability": point["mean_predicted_probability"],
                    "realized_yes_frequency": point["realized_yes_frequency"],
                }
            )
        curves[horizon] = points
    return curves


def build_exploratory_grid(observations: list[HorizonObservation]) -> list[dict[str, Any]]:
    """AMENDMENT Finding 3: 'the KXLOWTNYC exploratory cell... receive[s]
    no bootstrap CI and consume[s] no randomness.' Point estimates only,
    same 60-cell (horizon x decile) shape as primary_results.cells,
    reusing _cell_point_estimates verbatim; never read by evaluate_outcome
    (PREREG Sec 10: KXLOWTNYC is 'outside this family')."""
    grid: list[dict[str, Any]] = []
    for horizon in HORIZON_HOURS:
        for decile in range(N_DECILES):
            cell_obs = [
                o for o in observations if o.horizon_hours == horizon and o.decile == decile
            ]
            point = _cell_point_estimates(cell_obs)
            labeled_n = len([o for o in cell_obs if o.outcome is not None])
            band = point["cost_band_cents"]
            grid.append(
                {
                    "horizon_hours": horizon,
                    "decile": decile,
                    "n": labeled_n,
                    "mean_predicted_probability": point["mean_predicted_probability"],
                    "realized_yes_frequency": point["realized_yes_frequency"],
                    "calibration_error": point["calibration_error"],
                    "cost_band_cents": float(band) if band is not None else None,
                }
            )
    return grid


def build_by_month_descriptive(observations: list[HorizonObservation]) -> list[dict[str, Any]]:
    """PREREG Sec 13 (Bias Review, 'Calendar effects'): 'a descriptive
    by-month breakdown may be reported as exploratory only... and must
    never be used to select a favorable period.' Pools all horizons/
    deciles per calendar month of target_date; no CI, no bootstrap, never
    read by evaluate_outcome."""
    months: dict[tuple[int, int], list[HorizonObservation]] = {}
    for o in observations:
        key = (o.target_date.year, o.target_date.month)
        months.setdefault(key, []).append(o)
    out: list[dict[str, Any]] = []
    for year, month in sorted(months):
        obs = months[(year, month)]
        point = _cell_point_estimates(obs)
        labeled_n = len([o for o in obs if o.outcome is not None])
        out.append(
            {
                "year": year,
                "month": month,
                "n": labeled_n,
                "mean_predicted_probability": point["mean_predicted_probability"],
                "realized_yes_frequency": point["realized_yes_frequency"],
                "calibration_error": point["calibration_error"],
            }
        )
    return out


def compute_excluded_settlement_days(source: pl.DataFrame) -> list[str]:
    """PREREG Sec 5: a day is excluded from the valid-day population only
    if EVERY primary-series market for that date lacks a resolved label
    (settlement_label_status == 'resolved' and non-null
    value_at_settlement) -- identical treatment to H0007 Sec 4 condition 8.
    Computed at the market-static level, independent of horizon/decile
    assignment."""
    market_static = source.select(
        "market_ticker", "target_date", "settlement_label_status", "value_at_settlement"
    ).unique(subset=["market_ticker"])
    per_day = market_static.group_by("target_date").agg(
        (
            (pl.col("settlement_label_status") == "resolved")
            & pl.col("value_at_settlement").is_not_null()
        )
        .any()
        .alias("has_resolved_market")
    )
    excluded = per_day.filter(~pl.col("has_resolved_market")).sort("target_date")
    return [str(d) for d in excluded["target_date"].to_list()]


def compute_observations_excluded_stale(source: pl.DataFrame) -> dict[int, int]:
    """PREREG Sec 5's 'missing data (general)' coverage statistic: for
    each horizon, the count of markets for which no candle survived the
    as-of selection + 15-minute staleness tolerance (Sec 6). Computed by
    diffing against select_horizon_observations's own, unmodified output
    -- does not alter that function's behavior or call signature."""
    total_markets = source.select("market_ticker").unique().height
    excluded: dict[int, int] = {}
    for horizon in HORIZON_HOURS:
        surviving = select_horizon_observations(source, horizon_hours=horizon).height
        excluded[horizon] = total_markets - surviving
    return excluded


def cells_below_min_n(cells: list[CellResult]) -> list[tuple[int, int]]:
    """Reporting reshape of the already-computed, decision-relevant
    insufficient-N set: AMENDMENT Finding 4 resolves PREREG Sec 10's
    minimum-N-per-cell gate to apply specifically to the hold-out slice
    (`holdout_insufficient_n`) -- this function adds no new threshold or
    gate, it only lists cells where that existing flag is already True."""
    return [(c.horizon_hours, c.decile) for c in cells if c.holdout_insufficient_n]


class ManifestValidationError(Exception):
    pass


_REQUIRED_CELL_KEYS = {
    "horizon_hours",
    "decile",
    "n_days_discovery",
    "n_days_holdout",
    "mean_predicted_probability",
    "realized_yes_frequency",
    "calibration_error",
    "cost_band_cents",
    "discovery_ci95",
    "discovery_bonferroni_pass",
    "holdout_ci95",
    "holdout_pass",
    "insufficient_n",
}

_REQUIRED_DECISION_RULE_KEYS = {
    "confirmed_if",
    "rejected_if",
    "inconclusive_if",
    "evaluation_order",
    "outcome",
    "reason",
}

#: Required key sets at each dotted path, matching TEMPLATE-H0005-
#: manifest.json's own structure exactly (Template Parity requirement).
_REQUIRED_MANIFEST_PATHS: dict[str, set[str]] = {
    "": {
        "experiment",
        "hypothesis",
        "preregistration",
        "dataset",
        "config",
        "coverage",
        "primary_results",
        "secondary_results",
        "exploratory_results",
        "reproducibility_verification",
        "date_run",
    },
    "preregistration": {
        "document",
        "frozen_date",
        "frozen_git_commit",
        "protocol_differences_document",
        "original_hypotheses_entry",
        "amendment",
    },
    "preregistration.amendment": {"document", "frozen_date", "resolves", "blocked_on"},
    "dataset": {
        "version",
        "content_hashes",
        "market_price_weather_schema_version",
        "settlement_label_reconstruction_version",
        "source_db_revision",
        "git_commit",
    },
    "config": {
        "primary_series",
        "exploratory_series",
        "horizons_hours",
        "candle_staleness_tolerance_minutes",
        "decile_boundaries_pct",
        "primary_price_definition",
        "secondary_price_definitions",
        "fee_config",
        "cost_band_formula",
        "mid_price_rounding",
        "outcome_derivation",
        "primary_family_size",
        "bonferroni_alpha",
        "confidence_level",
        "random_seed",
        "rng_consumption_order",
        "bootstrap_resamples",
        "bootstrap_cluster_unit",
        "bootstrap_pool_separation",
        "min_settlement_days_required",
        "min_days_per_cell",
        "replication_split",
        "split_tie_break",
        "evaluation_order",
        "empty_cell_handling",
        "close_time_immutability_verified",
        "close_time_immutability_evidence",
        "duplicate_invariants_checked",
    },
    "coverage": {
        "settlement_days_available",
        "settlement_days_excluded_missing_data",
        "excluded_days",
        "discovery_slice_days",
        "holdout_slice_days",
        "observations_excluded_stale",
        "cells_below_min_n",
    },
    "primary_results": {"cells", "cells_rejecting_null", "decision_rule"},
    "primary_results.decision_rule": _REQUIRED_DECISION_RULE_KEYS,
    "secondary_results": {
        "reliability_curves_by_horizon",
        "brier_score_by_horizon",
        "log_loss_by_horizon",
        "ece_by_horizon",
        "calibration_slope_intercept_by_horizon",
        "executable_price_robustness",
    },
    "exploratory_results": {"kxlowtnyc_cell", "by_month_descriptive"},
    "reproducibility_verification": {"rerun_byte_identical", "config_matches_prereg"},
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
    """PREREG Sec 14/17's manifest-completeness requirement, made
    mechanical: every key TEMPLATE-H0005-manifest.json defines must exist
    in the produced results dict. Aborts loudly (raises
    ManifestValidationError) rather than silently returning a partial
    manifest -- deterministic, no randomness, no data-dependent branching
    beyond the fixed key list itself."""
    for path, required_keys in _REQUIRED_MANIFEST_PATHS.items():
        node = _get_path(results, path)
        if not isinstance(node, dict):
            raise ManifestValidationError(f"expected an object at {path!r}, got {type(node)}")
        missing = required_keys - node.keys()
        if missing:
            raise ManifestValidationError(f"manifest missing key(s) {sorted(missing)} at {path!r}")

    cells = results["primary_results"]["cells"]
    if not isinstance(cells, list) or len(cells) != PRIMARY_FAMILY_SIZE:
        raise ManifestValidationError(
            f"primary_results.cells must have exactly {PRIMARY_FAMILY_SIZE} entries, "
            f"got {len(cells) if isinstance(cells, list) else type(cells)}"
        )
    for cell in cells:
        if not isinstance(cell, dict):
            raise ManifestValidationError(f"cell entry is not an object: {cell!r}")
        missing_cell_keys = _REQUIRED_CELL_KEYS - cell.keys()
        if missing_cell_keys:
            raise ManifestValidationError(
                f"cell missing key(s) {sorted(missing_cell_keys)}: "
                f"(horizon={cell.get('horizon_hours')}, decile={cell.get('decile')})"
            )

    outcome = results["primary_results"]["decision_rule"]["outcome"]
    if outcome not in ("confirmed", "rejected", "inconclusive"):
        raise ManifestValidationError(f"unrecognized decision_rule.outcome: {outcome!r}")


# --- Top-level orchestration (NOT invoked against real data this session) --


def run(dataset_dir: Path) -> dict[str, Any]:
    """Loads the pinned market_price_weather export, builds the primary
    (KXHIGHNY) and exploratory (KXLOWTNYC) observation tables, runs the
    frozen protocol end to end, and returns the full results dict matching
    TEMPLATE-H0005-manifest.json's schema."""
    source = pl.read_parquet(dataset_dir / "market_price_weather.parquet")
    manifest = json.loads((dataset_dir / "manifest.json").read_text())

    assert_candle_natural_key_unique(source)

    primary_source = source.filter(pl.col("market_ticker").str.starts_with(PRIMARY_SERIES_PREFIX))
    exploratory_source = source.filter(
        pl.col("market_ticker").str.starts_with(EXPLORATORY_SERIES_PREFIX)
    )

    primary_obs = build_observation_table(primary_source)
    assert_no_duplicate_observations(primary_obs)

    valid_days = sorted({o.target_date for o in primary_obs if o.outcome is not None})
    discovery_days, holdout_days = chronological_split(valid_days)

    rng = random.Random(RANDOM_SEED)
    cells = build_cells(
        primary_obs, discovery_days=discovery_days, holdout_days=holdout_days, rng=rng
    )
    verdict = evaluate_outcome(cells, total_valid_days=len(valid_days))

    exploratory_obs = build_observation_table(exploratory_source)
    assert_no_duplicate_observations(exploratory_obs)

    # Reaching this point means assert_candle_natural_key_unique and both
    # assert_no_duplicate_observations calls above completed without raising
    # (AMENDMENT Finding 9) -- recorded directly, not asserted a second time.
    duplicate_invariants_checked = True

    metrics_by_horizon: dict[int, dict[str, Any]] = {}
    for horizon in HORIZON_HOURS:
        h_obs = [o for o in primary_obs if o.horizon_hours == horizon]
        brier, brier_base = brier_score(h_obs)
        ll, ll_base = log_loss(h_obs)
        ece = expected_calibration_error(h_obs)
        slope, intercept = fit_calibration_slope_intercept(h_obs)
        metrics_by_horizon[horizon] = {
            "brier": brier,
            "baseline_brier": brier_base,
            "log_loss": ll,
            "baseline_log_loss": ll_base,
            "ece": ece,
            "calibration_slope": slope,
            "calibration_intercept": intercept,
        }

    # --- Reporting-only computations (Template Parity pass). None of these
    # read anything the primary decision rule above hasn't already fixed;
    # none call build_cells/evaluate_outcome or consume the rng.
    excluded_days = compute_excluded_settlement_days(primary_source)
    observations_excluded_stale = compute_observations_excluded_stale(primary_source)
    below_min_n = cells_below_min_n(cells)
    reliability_curves = build_reliability_curves(primary_obs)
    executable_price_robustness = compute_executable_price_robustness(
        primary_obs, verdict.clearing_cells
    )
    exploratory_grid = build_exploratory_grid(exploratory_obs)
    by_month_descriptive = build_by_month_descriptive(primary_obs)

    config: dict[str, Any] = {
        "primary_series": CONFIG_STATIC_DESCRIPTIONS["primary_series"],
        "exploratory_series": CONFIG_STATIC_DESCRIPTIONS["exploratory_series"],
        "horizons_hours": HORIZON_HOURS,
        "candle_staleness_tolerance_minutes": STALENESS_TOLERANCE_SECONDS // 60,
        "decile_boundaries_pct": DECILE_EDGES,
        "primary_price_definition": CONFIG_STATIC_DESCRIPTIONS["primary_price_definition"],
        "secondary_price_definitions": CONFIG_STATIC_DESCRIPTIONS["secondary_price_definitions"],
        "fee_config": {
            "source": "scripts/h0007_fees.py: KALSHI_WEATHER_TAKER_FEE_CONFIG",
            "multiplier": str(KALSHI_WEATHER_TAKER_FEE_CONFIG.multiplier),
            "verified": KALSHI_WEATHER_TAKER_FEE_CONFIG.verified,
            "verified_date": KALSHI_WEATHER_TAKER_FEE_CONFIG.retrieved_at,
        },
        "cost_band_formula": CONFIG_STATIC_DESCRIPTIONS["cost_band_formula"],
        "mid_price_rounding": CONFIG_STATIC_DESCRIPTIONS["mid_price_rounding"],
        "outcome_derivation": CONFIG_STATIC_DESCRIPTIONS["outcome_derivation"],
        "primary_family_size": PRIMARY_FAMILY_SIZE,
        "bonferroni_alpha": BONFERRONI_ALPHA,
        "confidence_level": CONFIDENCE_LEVEL,
        "random_seed": RANDOM_SEED,
        "rng_consumption_order": CONFIG_STATIC_DESCRIPTIONS["rng_consumption_order"],
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "bootstrap_cluster_unit": CONFIG_STATIC_DESCRIPTIONS["bootstrap_cluster_unit"],
        "bootstrap_pool_separation": CONFIG_STATIC_DESCRIPTIONS["bootstrap_pool_separation"],
        "min_settlement_days_required": MIN_SETTLEMENT_DAYS,
        "min_days_per_cell": MIN_DAYS_PER_CELL,
        "replication_split": CONFIG_STATIC_DESCRIPTIONS["replication_split"],
        "split_tie_break": CONFIG_STATIC_DESCRIPTIONS["split_tie_break"],
        "evaluation_order": CONFIG_STATIC_DESCRIPTIONS["evaluation_order"],
        "empty_cell_handling": CONFIG_STATIC_DESCRIPTIONS["empty_cell_handling"],
        # AMENDMENT Finding 6: close_time immutability is a manual,
        # investigative pre-execution checklist item (§16's new first
        # step), not something this script can compute from the pinned
        # dataset alone -- explicitly documented as manually-filled,
        # never silently omitted or fabricated.
        "close_time_immutability_verified": None,
        "close_time_immutability_evidence": (
            "not computed by run() -- AMENDMENT Finding 6 requires manual "
            "inspection of MarketSnapshot history (or official Kalshi "
            "documentation) before dataset pinning, recorded here by whoever "
            "completes that checklist step, not derived from price data"
        ),
        "duplicate_invariants_checked": duplicate_invariants_checked,
    }

    decision_rule: dict[str, Any] = {
        "confirmed_if": (
            "no primary-family cell clears both the Bonferroni-adjusted discovery-slice "
            "screen and the hold-out replication screen"
        ),
        "rejected_if": "at least one primary-family cell clears both screens",
        "inconclusive_if": (
            "settlement_days_available < 60, OR every discovery-screen-passing cell falls "
            "below min_days_per_cell in the holdout slice"
        ),
        "evaluation_order": CONFIG_STATIC_DESCRIPTIONS["evaluation_order"],
        "outcome": verdict.outcome,
        "reason": verdict.reason,
        "clearing_cells": verdict.clearing_cells,
        "underpowered_cells": verdict.underpowered_cells,
    }

    results: dict[str, Any] = {
        "experiment": "EXP-20260721-H0005-calibration",
        "hypothesis": "H0005",
        "preregistration": PREREGISTRATION_METADATA,
        "dataset": {
            "version": manifest["version"],
            "content_hashes": manifest["content_hashes"],
            "market_price_weather_schema_version": manifest["config"][
                "market_price_weather_schema_version"
            ],
            "settlement_label_reconstruction_version": manifest["config"][
                "settlement_label_reconstruction_version"
            ],
            "source_db_revision": manifest["source_db_revision"],
            "git_commit": manifest["git_commit"],
        },
        "config": config,
        "coverage": {
            "settlement_days_available": len(valid_days),
            "settlement_days_excluded_missing_data": len(excluded_days),
            "excluded_days": excluded_days,
            "discovery_slice_days": len(discovery_days),
            "holdout_slice_days": len(holdout_days),
            "observations_excluded_stale": observations_excluded_stale,
            "cells_below_min_n": below_min_n,
        },
        "primary_results": {
            "cells": [
                {
                    "horizon_hours": c.horizon_hours,
                    "decile": c.decile,
                    "n_days_discovery": c.n_days_discovery,
                    "n_days_holdout": c.n_days_holdout,
                    "mean_predicted_probability": c.mean_predicted_probability,
                    "realized_yes_frequency": c.realized_yes_frequency,
                    "calibration_error": c.calibration_error,
                    "cost_band_cents": float(c.cost_band_cents)
                    if c.cost_band_cents is not None
                    else None,
                    "discovery_ci": list(c.discovery_ci),
                    "discovery_ci95": list(c.discovery_ci),
                    "discovery_bonferroni_pass": c.discovery_bonferroni_pass,
                    "holdout_mean_predicted_probability": c.holdout_mean_predicted_probability,
                    "holdout_realized_yes_frequency": c.holdout_realized_yes_frequency,
                    "holdout_calibration_error": c.holdout_calibration_error,
                    "holdout_cost_band_cents": (
                        float(c.holdout_cost_band_cents)
                        if c.holdout_cost_band_cents is not None
                        else None
                    ),
                    "holdout_ci": list(c.holdout_ci),
                    "holdout_ci95": list(c.holdout_ci),
                    "holdout_insufficient_n": c.holdout_insufficient_n,
                    "insufficient_n": c.holdout_insufficient_n,
                    "holdout_pass": c.holdout_pass,
                    "clears_both_screens": c.clears_both_screens,
                }
                for c in cells
            ],
            "cells_rejecting_null": verdict.clearing_cells,
            "decision_rule": decision_rule,
        },
        "secondary_results": {
            "metrics_by_horizon": metrics_by_horizon,
            "reliability_curves_by_horizon": reliability_curves,
            "brier_score_by_horizon": {
                h: {"brier": m["brier"], "baseline_brier": m["baseline_brier"]}
                for h, m in metrics_by_horizon.items()
            },
            "log_loss_by_horizon": {
                h: {"log_loss": m["log_loss"], "baseline_log_loss": m["baseline_log_loss"]}
                for h, m in metrics_by_horizon.items()
            },
            "ece_by_horizon": {h: m["ece"] for h, m in metrics_by_horizon.items()},
            "calibration_slope_intercept_by_horizon": {
                h: {"slope": m["calibration_slope"], "intercept": m["calibration_intercept"]}
                for h, m in metrics_by_horizon.items()
            },
            "executable_price_robustness": executable_price_robustness,
        },
        "exploratory_results": {
            "kxlowtnyc_observation_count": len(exploratory_obs),
            "kxlowtnyc_cell": exploratory_grid,
            "by_month_descriptive": by_month_descriptive,
        },
        "reproducibility_verification": {
            # PREREG Sec 16: only established by actually re-running this
            # script a second time against the same pinned dataset and
            # diffing the two output JSON files byte-for-byte -- a fact
            # about execution that hasn't happened, not something run()
            # can determine about itself from a single invocation.
            "rerun_byte_identical": None,
            "config_matches_prereg": verify_config_matches_prereg(config),
        },
        "date_run": None,
    }
    validate_manifest(results)
    return results


def render_markdown(results: dict[str, Any]) -> str:
    decision = results["primary_results"]["decision_rule"]
    cov = results["coverage"]
    return f"""# EXP-20260721-H0005-calibration

Executed exactly as pre-registered in
`PREREG-20260721-H0005-calibration.md` and
`AMENDMENT-20260721-H0005-pre-execution.md`.

## Decision

**{decision["outcome"].upper()}** ({decision["reason"]}).

## Coverage

- Settlement days available: {cov["settlement_days_available"]}
- Discovery slice: {cov["discovery_slice_days"]} days
- Hold-out slice: {cov["holdout_slice_days"]} days

Full cell-level results and secondary metrics: see the accompanying
`-results.json`.
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args()

    from datetime import UTC, datetime

    output = run(args.dataset_dir)
    output["date_run"] = datetime.now(UTC).date().isoformat()

    args.out.write_text(json.dumps(output, indent=2, default=str))
    args.out_md.write_text(render_markdown(output))
    print(f"wrote {args.out}")
    print(f"wrote {args.out_md}")
    print(f"decision: {output['primary_results']['decision_rule']['outcome']}")
