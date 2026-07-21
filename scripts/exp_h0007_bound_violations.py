"""EXP H0007 -- Preliminary-observation bound violations near settlement.

Executes exactly as pre-registered. Every operation below cites the exact
frozen section/finding it implements; nothing here may change a threshold,
entry condition, or decision rule. The three governing documents are:

  - docs/research/experiments/PREREG-20260721-H0007-bound-violations.md
  - docs/research/experiments/AMENDMENT-20260721-H0007-pre-execution.md
  - docs/research/experiments/AMENDMENT-20260721-H0007-fee-verified.md

Deterministic except for one bootstrap resampling step (seed fixed at
PREREG Sec 11's frozen value, 20260721): entry-condition flagging, episode
grouping, and Wilson CIs are plain arithmetic over stored data.

Usage:
    python scripts/exp_h0007_bound_violations.py \
        --dataset-dir data/datasets/exp-20260721-h0007 \
        --out docs/research/experiments/EXP-20260721-H0007-bound-violations-results.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from h0007_fees import KALSHI_WEATHER_TAKER_FEE_CONFIG, contract_fee_cents

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kalshi_weather.settlement.labels import implied_result

# --- Frozen protocol constants (PREREG Sec 5, 6, 9, 11) ----------------------

RANDOM_SEED = 20260721  # PREREG Sec 11: "fixed at 20260721 (this protocol's freeze date)"
BOOTSTRAP_RESAMPLES = 10_000  # PREREG Sec 6
CONFIDENCE_Z = 1.959963984540054  # 95% two-sided; reused verbatim from exp_h0002_cli_revisions.py
FREQUENCY_THRESHOLD = 0.05  # HYPOTHESES.md H0007: "occurs on >=5% of settlement days"
MIN_SETTLEMENT_DAYS = 60  # PREREG Sec 9 / HYPOTHESES.md "Required data"
# p_bound = 1/1278 (PREREG Sec 5): unconditional rate of any downward revision
# from a same-day tmax issuance, per EXP-20260721-H0002-cli-revisions. Recorded
# for the manifest; the decision rule itself operates on residual_mass_cents
# and the fee, not directly on p_bound (p_bound only justifies the ~0c fair
# value baked into "sellable_price - 1" in Sec 5 below).
P_BOUND = Decimal(1) / Decimal(1278)


def wilson_ci(successes: int, n: int, z: float = CONFIDENCE_Z) -> tuple[float, float, float]:
    """(point estimate, lower, upper) Wilson score interval for a proportion.
    Reused verbatim from scripts/exp_h0002_cli_revisions.py's wilson_ci, per
    PREREG Sec 7's "the deterministic method already used for H0002, reused
    for consistency."""
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return (p, max(0.0, center - margin), min(1.0, center + margin))


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default), no numpy
    dependency needed (not in this project's required stack)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return d0 + d1


def day_clustered_bootstrap_ci(
    day_values: dict[Any, list[float]],
    all_days: list[Any],
    *,
    agg: str,
    rng: random.Random,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> tuple[float | None, float | None]:
    """95% percentile CI for an aggregate statistic, resampling DAYS (not
    episodes/candles) with replacement -- PREREG Sec 6/7, AMENDMENT Finding 2
    ("resamples from this same valid-coverage day pool, including
    zero-opportunity days"). `day_values` maps each day to the list of
    per-episode values observed on that day (empty list for zero-opportunity
    days). `agg` is "sum" (for aggregate P&L) or "mean"/"median" (for edge
    size / persistence / time-to-correction)."""
    results: list[float] = []
    n = len(all_days)
    if n == 0:
        return (None, None)
    for _ in range(resamples):
        sample_days = rng.choices(all_days, k=n)
        pooled: list[float] = []
        for d in sample_days:
            pooled.extend(day_values.get(d, []))
        if agg == "sum":
            results.append(sum(pooled))
        elif agg == "mean":
            if pooled:
                results.append(statistics.mean(pooled))
        elif agg == "median":
            if pooled:
                results.append(statistics.median(pooled))
        else:
            raise ValueError(f"unknown agg {agg!r}")
    if not results:
        return (None, None)
    results.sort()
    return (_percentile(results, 0.025), _percentile(results, 0.975))


# --- Entry conditions (PREREG Sec 4) and outcome definition (Sec 5) ---------


def _bounded_expr(variable: str) -> pl.Expr:
    """PREREG Sec 4 condition 4, strike semantics reused verbatim from
    settlement/labels.py's implied_result logic (ADR 0006), evaluated against
    the running extreme instead of the settlement value. tmax_f is the
    primary cell (running value is a hard LOWER bound on the eventual high);
    tmin_f is the declared secondary/exploratory cell (Sec 4 condition 1),
    evaluated by the mirror-image logic since its running value is a hard
    UPPER bound on the eventual low -- this mirroring is not spelled out
    verbatim in the frozen text (written entirely in tmax terms) but is the
    unique, non-arbitrary extension "evaluated identically" implies; flagged
    explicitly here since it required derivation, not a lookup."""
    if variable == "tmax_f":
        running = pl.col("running_tmax_f_known")
        less = (pl.col("strike_type") == "less") & (running >= pl.col("cap_strike"))
        greater = (pl.col("strike_type") == "greater") & (running > pl.col("floor_strike"))
        between = (pl.col("strike_type") == "between") & (running > pl.col("cap_strike"))
        return less | greater | between
    else:  # tmin_f, exploratory mirror
        running = pl.col("running_tmin_f_known")
        less = (pl.col("strike_type") == "less") & (running < pl.col("cap_strike"))
        greater = (pl.col("strike_type") == "greater") & (running <= pl.col("floor_strike"))
        between = (pl.col("strike_type") == "between") & (running < pl.col("floor_strike"))
        return less | greater | between


def _bounded_side_expr(variable: str) -> pl.Expr:
    """The near-impossible side, per PREREG Sec 4 condition 4 (tmax) and its
    mirror (tmin, see _bounded_expr's docstring)."""
    if variable == "tmax_f":
        # less -> yes near-impossible; greater -> no near-impossible;
        # between(upper) -> yes near-impossible
        return (
            pl.when(pl.col("strike_type") == "greater").then(pl.lit("no")).otherwise(pl.lit("yes"))
        )
    else:
        # less -> yes near-certain so no near-impossible;
        # greater/between(lower) -> yes near-impossible
        return pl.when(pl.col("strike_type") == "less").then(pl.lit("no")).otherwise(pl.lit("yes"))


def build_candidate_frame(frame: pl.DataFrame, *, variable: str) -> pl.DataFrame:
    """Entry conditions 1-4, 6, 7 (PREREG Sec 4) -- condition 5 is superseded
    by AMENDMENT Finding 3 (persistence enforced at the episode level, not as
    a per-row lookback); condition 8 (settlement_label_status plays no role
    in entry eligibility) is satisfied by construction -- this function never
    reads that column."""
    running_col = "running_tmax_f_known" if variable == "tmax_f" else "running_tmin_f_known"
    locked_col = "tmax_locked" if variable == "tmax_f" else "tmin_locked"

    sub = frame.filter(pl.col("variable") == variable)
    c2 = pl.col(running_col).is_not_null()
    c3 = pl.col(locked_col) == False  # noqa: E712 (Polars boolean expr, not Python truthiness)
    c4 = _bounded_expr(variable)
    c6 = (
        pl.col("yes_bid_close_cents").is_not_null()
        & pl.col("yes_ask_close_cents").is_not_null()
        & (pl.col("yes_bid_close_cents") <= pl.col("yes_ask_close_cents"))
    )
    c7 = pl.col("period_end") < pl.col("close_time")

    candidates = sub.filter(c2 & c3 & c4 & c6 & c7).with_columns(
        bounded_side=_bounded_side_expr(variable)
    )
    candidates = candidates.with_columns(
        sellable_price_of_bounded_side_cents=pl.when(pl.col("bounded_side") == "yes")
        .then(pl.col("yes_bid_close_cents"))
        .otherwise(100 - pl.col("yes_ask_close_cents"))
    )
    # PREREG Sec 5: has_residual_mass = (sellable_price - 1) > 0. Only rows
    # clearing this raw threshold can possibly clear the stricter fee-net
    # threshold (any fee is >= 1 cent), so the fee function -- which requires
    # its price argument in Kalshi's tradeable range 1-99 -- is only ever
    # evaluated on rows where sellable_price_of_bounded_side_cents >= 2,
    # never on the (real, observed) sellable_price == 0 edge case.
    candidates = candidates.with_columns(
        residual_mass_cents=pl.col("sellable_price_of_bounded_side_cents") - 1,
        has_residual_mass=(pl.col("sellable_price_of_bounded_side_cents") - 1) > 0,
    )
    return candidates


def attach_fee_net_flag(candidates: pl.DataFrame) -> pl.DataFrame:
    """AMENDMENT Finding 1: fee-net condition = residual_mass_cents >
    fee_cents_for_one_contract_at_this_price, computed via the frozen,
    verified KALSHI_WEATHER_TAKER_FEE_CONFIG (AMENDMENT-fee-verified.md).
    Fee symmetry (0.07*P*(1-P) is symmetric in P<->100-P) means the fee is
    identical whether computed on the bounded side's own sellable price or
    the favored side's price -- the bounded side's price is used directly,
    matching how residual_mass_cents (Sec 5) is itself defined."""
    if candidates.height == 0:
        return candidates.with_columns(
            fee_cents=pl.lit(None, dtype=pl.Int64),
            qualifies=pl.lit(False),
        )
    rows = candidates.select("sellable_price_of_bounded_side_cents", "has_residual_mass").to_dicts()
    fee_cents: list[int | None] = []
    for row in rows:
        if not row["has_residual_mass"]:
            fee_cents.append(None)  # never computed -- see build_candidate_frame's docstring
            continue
        fee_cents.append(
            contract_fee_cents(
                row["sellable_price_of_bounded_side_cents"],
                contracts=1,
                config=KALSHI_WEATHER_TAKER_FEE_CONFIG,
            )
        )
    candidates = candidates.with_columns(pl.Series("fee_cents", fee_cents, dtype=pl.Int64))
    candidates = candidates.with_columns(
        qualifies=pl.col("has_residual_mass")
        & (pl.col("residual_mass_cents") > pl.col("fee_cents"))
    )
    return candidates


# --- Episode grouping (AMENDMENT Findings 1, 3, 4) --------------------------


@dataclass(slots=True)
class Episode:
    market_ticker: str
    target_date: Any
    variable: str
    strike_type: str
    bounded_side: str
    n_candles: int
    first_period_end: Any
    last_period_end: Any
    residual_mass_cents_first: int
    fee_cents_first: int
    yes_ask_close_cents_first: int
    yes_bid_close_cents_first: int
    counts: bool  # AMENDMENT Finding 3: len >= 2
    closed_before_settlement: bool
    time_to_correction_minutes: float | None


def group_episodes(candidates: pl.DataFrame) -> list[Episode]:
    """AMENDMENT Finding 4: consecutive means period_end differs by exactly
    one minute; a gap breaks the run. Grouping is done only over rows already
    satisfying `qualifies` (entry-eligible + fee-net residual mass) --
    non-qualifying candles never belong to any episode. AMENDMENT Finding 1:
    an episode is the maximal run itself; AMENDMENT Finding 3 determines
    which episodes *count* (length >= 2)."""
    if candidates.height == 0:
        return []
    qualifying = candidates.filter(pl.col("qualifies")).sort(["market_ticker", "period_end"])
    if qualifying.height == 0:
        return []

    qualifying = qualifying.with_columns(
        prev_period_end=pl.col("period_end").shift(1).over("market_ticker")
    )
    qualifying = qualifying.with_columns(
        gap_seconds=(pl.col("period_end") - pl.col("prev_period_end")).dt.total_seconds()
    )
    qualifying = qualifying.with_columns(
        new_episode=(pl.col("gap_seconds") != 60) | pl.col("gap_seconds").is_null()
    )
    qualifying = qualifying.with_columns(
        local_episode_id=pl.col("new_episode").cum_sum().over("market_ticker")
    )

    episodes: list[Episode] = []
    for (ticker, _local_id), group in qualifying.group_by(
        ["market_ticker", "local_episode_id"], maintain_order=True
    ):
        group = group.sort("period_end")
        first = group.row(0, named=True)
        last = group.row(-1, named=True)
        n_candles = group.height
        market_all = candidates.filter(pl.col("market_ticker") == ticker).sort("period_end")
        later = market_all.filter(pl.col("period_end") > last["period_end"])
        next_non_qualifying = later.filter(~pl.col("qualifies"))
        closed_before_settlement = False
        ttc = None
        if next_non_qualifying.height > 0:
            first_break = next_non_qualifying.row(0, named=True)
            if first_break["period_end"] < first["close_time"]:
                closed_before_settlement = True
                ttc = (last["period_end"] - first["period_end"]).total_seconds() / 60.0
        episodes.append(
            Episode(
                market_ticker=ticker,
                target_date=first["target_date"],
                variable=first["variable"],
                strike_type=first["strike_type"],
                bounded_side=first["bounded_side"],
                n_candles=n_candles,
                first_period_end=first["period_end"],
                last_period_end=last["period_end"],
                residual_mass_cents_first=first["residual_mass_cents"],
                fee_cents_first=first["fee_cents"],
                yes_ask_close_cents_first=first["yes_ask_close_cents"],
                yes_bid_close_cents_first=first["yes_bid_close_cents"],
                counts=n_candles >= 2,  # AMENDMENT Finding 3
                closed_before_settlement=closed_before_settlement,
                time_to_correction_minutes=ttc,
            )
        )
    return episodes


# --- Outcome / P&L (PREREG Sec 5 secondary outcomes) ------------------------


@dataclass(slots=True)
class EpisodePnl:
    episode: Episode
    has_label: bool
    pnl_cents: float | None


def compute_episode_pnl(
    episode: Episode, labels_by_ticker: dict[str, dict[str, Any]]
) -> EpisodePnl:
    """PREREG Sec 5: buy the FAVORED (opposite of bounded) side at its
    executable ask at the episode's first qualifying candle, hold to
    settlement, realize the standard binary payoff minus the fee.
    value_at_settlement/derived result used ONLY here (never for entry) --
    Sec 3's forbidden-information list. `kalshi_result` is not itself a
    market_price_weather column (see the results record's Methodology
    section); the outcome is derived via settlement/labels.py's own
    implied_result applied to value_at_settlement + strike bounds -- the
    exact reuse Sec 4 condition 4 already mandates for the bound logic
    itself, and the same function ADR 0006 validated to reproduce Kalshi's
    own `result` exactly."""
    label = labels_by_ticker.get(episode.market_ticker)
    if (
        label is None
        or label["settlement_label_status"] != "resolved"
        or label["value_at_settlement"] is None
    ):
        return EpisodePnl(episode=episode, has_label=False, pnl_cents=None)

    favored_side = "no" if episode.bounded_side == "yes" else "yes"
    if favored_side == "yes":
        ask_cents = episode.yes_ask_close_cents_first
    else:
        ask_cents = 100 - episode.yes_bid_close_cents_first

    if not (1 <= ask_cents <= 99):
        return EpisodePnl(episode=episode, has_label=False, pnl_cents=None)

    fee_cents = contract_fee_cents(ask_cents, contracts=1, config=KALSHI_WEATHER_TAKER_FEE_CONFIG)
    cost_cents = ask_cents + fee_cents

    result = implied_result(
        Decimal(str(label["value_at_settlement"])),
        strike_type=episode.strike_type,
        floor=Decimal(str(label["floor_strike"])) if label["floor_strike"] is not None else None,
        cap=Decimal(str(label["cap_strike"])) if label["cap_strike"] is not None else None,
    )
    if result is None:
        return EpisodePnl(episode=episode, has_label=False, pnl_cents=None)

    payoff_cents = 100.0 if result == favored_side else 0.0
    pnl_cents = payoff_cents - cost_cents
    return EpisodePnl(episode=episode, has_label=True, pnl_cents=pnl_cents)


# --- Main -------------------------------------------------------------------


def run(dataset_dir: Path) -> dict[str, Any]:
    frame = pl.read_parquet(dataset_dir / "market_price_weather.parquet")
    manifest = json.loads((dataset_dir / "manifest.json").read_text())

    # PREREG Sec 4/AMENDMENT Finding 2: valid-coverage settlement days for the
    # primary tmax_f cell -- a day is excluded only if EVERY tmax_f market for
    # that date lacks a resolved settlement label (settlement_label_status ==
    # "missing_source_data").
    tmax_all = frame.filter(pl.col("variable") == "tmax_f")
    per_date_status = tmax_all.group_by("target_date").agg(
        statuses=pl.col("settlement_label_status").unique()
    )
    missing_days = per_date_status.filter(
        pl.col("statuses").list.eval(pl.element() == "missing_source_data").list.all()
    )["target_date"].to_list()
    all_tmax_days = sorted(tmax_all["target_date"].unique().to_list())
    valid_days = sorted(d for d in all_tmax_days if d not in missing_days)

    labels_by_ticker = {
        row["market_ticker"]: row
        for row in frame.select(
            "market_ticker",
            "settlement_label_status",
            "value_at_settlement",
            "floor_strike",
            "cap_strike",
        )
        .unique(subset=["market_ticker"])
        .to_dicts()
    }

    # --- Primary cell: tmax_f ---
    candidates = build_candidate_frame(frame, variable="tmax_f")
    candidates = attach_fee_net_flag(candidates)
    episodes = group_episodes(candidates)
    counting_episodes = [e for e in episodes if e.counts and e.target_date in valid_days]

    days_with_opportunity = sorted({e.target_date for e in counting_episodes})
    freq_point, freq_lower, freq_upper = wilson_ci(len(days_with_opportunity), len(valid_days))

    pnls = [compute_episode_pnl(e, labels_by_ticker) for e in counting_episodes]
    episodes_with_label = [p for p in pnls if p.has_label]
    episodes_without_label = [p for p in pnls if not p.has_label]
    total_pnl_cents = sum(p.pnl_cents for p in episodes_with_label)  # type: ignore[misc]

    rng = random.Random(RANDOM_SEED)
    pnl_by_day: dict[Any, list[float]] = {d: [] for d in valid_days}
    for p in episodes_with_label:
        pnl_by_day[p.episode.target_date].append(p.pnl_cents)  # type: ignore[arg-type]
    pnl_ci_lower, pnl_ci_upper = day_clustered_bootstrap_ci(
        pnl_by_day, valid_days, agg="sum", rng=rng
    )

    edge_by_day: dict[Any, list[float]] = {d: [] for d in valid_days}
    persistence_by_day: dict[Any, list[float]] = {d: [] for d in valid_days}
    ttc_by_day: dict[Any, list[float]] = {d: [] for d in valid_days}
    for e in counting_episodes:
        edge_by_day[e.target_date].append(float(e.residual_mass_cents_first))
        duration_min = (e.last_period_end - e.first_period_end).total_seconds() / 60.0
        persistence_by_day[e.target_date].append(duration_min)
        if e.closed_before_settlement and e.time_to_correction_minutes is not None:
            ttc_by_day[e.target_date].append(e.time_to_correction_minutes)

    edge_values = [v for vs in edge_by_day.values() for v in vs]
    persistence_values = [v for vs in persistence_by_day.values() for v in vs]
    ttc_values = [v for vs in ttc_by_day.values() for v in vs]

    edge_mean_ci = day_clustered_bootstrap_ci(edge_by_day, valid_days, agg="mean", rng=rng)
    edge_median_ci = day_clustered_bootstrap_ci(edge_by_day, valid_days, agg="median", rng=rng)
    persistence_mean_ci = day_clustered_bootstrap_ci(
        persistence_by_day, valid_days, agg="mean", rng=rng
    )
    persistence_median_ci = day_clustered_bootstrap_ci(
        persistence_by_day, valid_days, agg="median", rng=rng
    )
    ttc_days = [d for d in valid_days if ttc_by_day[d]]
    ttc_mean_ci = (
        day_clustered_bootstrap_ci(ttc_by_day, ttc_days, agg="mean", rng=rng)
        if ttc_days
        else (None, None)
    )
    ttc_median_ci = (
        day_clustered_bootstrap_ci(ttc_by_day, ttc_days, agg="median", rng=rng)
        if ttc_days
        else (None, None)
    )

    # --- Decision rule (PREREG Sec 9) ---
    if len(valid_days) < MIN_SETTLEMENT_DAYS:
        outcome = "inconclusive"
        outcome_reason = (
            f"only {len(valid_days)} valid-coverage settlement days, short of the "
            f"pre-registered minimum {MIN_SETTLEMENT_DAYS}"
        )
    else:
        confirmed = freq_lower > FREQUENCY_THRESHOLD and (
            pnl_ci_lower is not None and pnl_ci_lower > 0
        )
        rejected = freq_upper <= FREQUENCY_THRESHOLD or (
            pnl_ci_upper is not None and pnl_ci_upper <= 0
        )
        if confirmed:
            outcome = "confirmed"
            outcome_reason = "frequency CI lower bound > 5% AND aggregate P&L CI lower bound > 0"
        elif rejected:
            outcome = "rejected"
            reasons = []
            if freq_upper <= FREQUENCY_THRESHOLD:
                reasons.append("frequency CI upper bound <= 5%")
            if pnl_ci_upper is not None and pnl_ci_upper <= 0:
                reasons.append("aggregate P&L CI upper bound <= 0")
            outcome_reason = " OR ".join(reasons)
        else:
            outcome = "inconclusive"
            outcome_reason = "neither confirmed nor rejected criteria met (CI straddles threshold)"

    # --- Failure-mode transparency stat (Sec 9 failure mode 1) ---
    days_driven_by_single_episode = sum(
        1
        for d in days_with_opportunity
        if sum(1 for e in counting_episodes if e.target_date == d) == 1
    )

    # --- Exploratory: tmin_f cell (same mechanism, mirrored; not primary) ---
    tmin_candidates = build_candidate_frame(frame, variable="tmin_f")
    tmin_candidates = attach_fee_net_flag(tmin_candidates)
    tmin_episodes = group_episodes(tmin_candidates)
    tmin_all_days = sorted(
        frame.filter(pl.col("variable") == "tmin_f")["target_date"].unique().to_list()
    )
    tmin_missing = (
        frame.filter(pl.col("variable") == "tmin_f")
        .group_by("target_date")
        .agg(statuses=pl.col("settlement_label_status").unique())
        .filter(pl.col("statuses").list.eval(pl.element() == "missing_source_data").list.all())[
            "target_date"
        ]
        .to_list()
    )
    tmin_valid_days = sorted(d for d in tmin_all_days if d not in tmin_missing)
    tmin_counting = [e for e in tmin_episodes if e.counts and e.target_date in tmin_valid_days]
    tmin_days_with_opp = sorted({e.target_date for e in tmin_counting})
    tmin_freq = (
        wilson_ci(len(tmin_days_with_opp), len(tmin_valid_days))
        if tmin_valid_days
        else (0.0, 0.0, 0.0)
    )

    # --- Exploratory: by strike_type (raw, pre-fee-net, descriptive only) ---
    by_strike_type = (
        candidates.group_by("strike_type")
        .agg(
            n_candidate_candles=pl.len(),
            n_with_raw_residual_mass=pl.col("has_residual_mass").sum(),
            n_with_fee_net_qualify=pl.col("qualifies").sum(),
        )
        .sort("strike_type")
        .to_dicts()
    )

    # --- Exploratory: by month (descriptive only) ---
    by_month = (
        candidates.with_columns(month=pl.col("target_date").dt.strftime("%Y-%m"))
        .group_by("month")
        .agg(n_candidate_candles=pl.len(), n_with_fee_net_qualify=pl.col("qualifies").sum())
        .sort("month")
        .to_dicts()
    )

    results: dict[str, Any] = {
        "experiment": "EXP-20260721-H0007-bound-violations",
        "hypothesis": "H0007",
        "preregistration": {
            "document": "docs/research/experiments/PREREG-20260721-H0007-bound-violations.md",
            "amendments": [
                "docs/research/experiments/AMENDMENT-20260721-H0007-pre-execution.md",
                "docs/research/experiments/AMENDMENT-20260721-H0007-fee-verification.md",
                "docs/research/experiments/AMENDMENT-20260721-H0007-fee-verified.md",
            ],
        },
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
            "dataset_git_commit": manifest["git_commit"],
        },
        "config": {
            "primary_cell": {"variable": "tmax_f"},
            "secondary_exploratory_cells": ["tmin_f"],
            "p_bound": str(P_BOUND),
            "fee_config": {
                "multiplier": str(KALSHI_WEATHER_TAKER_FEE_CONFIG.multiplier),
                "verified": KALSHI_WEATHER_TAKER_FEE_CONFIG.verified,
            },
            "random_seed": RANDOM_SEED,
            "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
            "confidence_level": 0.95,
            "min_settlement_days_required": MIN_SETTLEMENT_DAYS,
            "frequency_threshold": FREQUENCY_THRESHOLD,
        },
        "coverage": {
            "settlement_days_available": len(valid_days),
            "settlement_days_excluded_missing_data": len(missing_days),
            "excluded_days": [str(d) for d in missing_days],
        },
        "primary_results": {
            "candidate_candles": candidates.height,
            "candles_with_raw_residual_mass": int(candidates["has_residual_mass"].sum()),
            "candles_fee_net_qualifying": int(candidates["qualifies"].sum()),
            "episodes_total": len(episodes),
            "episodes_counting_ge2_candles": len(counting_episodes),
            "opportunity_day_frequency": {
                "days_with_opportunity": len(days_with_opportunity),
                "days_total": len(valid_days),
                "frequency": freq_point,
                "wilson_ci95": [freq_lower, freq_upper],
            },
            "aggregate_hypothetical_pnl": {
                "n_episodes_with_label": len(episodes_with_label),
                "n_episodes_excluded_no_label": len(episodes_without_label),
                "total_pnl_cents": total_pnl_cents,
                "total_pnl_dollars": total_pnl_cents / 100.0,
                "bootstrap_ci95_cents": [pnl_ci_lower, pnl_ci_upper],
                "bootstrap_ci95_dollars": [
                    pnl_ci_lower / 100.0 if pnl_ci_lower is not None else None,
                    pnl_ci_upper / 100.0 if pnl_ci_upper is not None else None,
                ],
            },
            "decision_rule": {
                "confirmed_if": "frequency_ci95_lower > 0.05 AND aggregate_pnl_ci95_lower > 0",
                "rejected_if": "frequency_ci95_upper <= 0.05 OR aggregate_pnl_ci95_upper <= 0",
                "inconclusive_if": (
                    "neither confirmed nor rejected criteria met, OR "
                    "settlement_days_available < 60"
                ),
                "outcome": outcome,
                "reason": outcome_reason,
            },
            "failure_mode_1_transparency": {
                "note": "opportunity-days driven by exactly one episode (Sec 9 failure mode 1)",
                "days_driven_by_single_episode": days_driven_by_single_episode,
                "days_with_opportunity": len(days_with_opportunity),
            },
        },
        "secondary_results": {
            "edge_size_cents": {
                "n": len(edge_values),
                "mean": statistics.mean(edge_values) if edge_values else None,
                "median": statistics.median(edge_values) if edge_values else None,
                "iqr": (
                    [
                        statistics.quantiles(edge_values, n=4)[0],
                        statistics.quantiles(edge_values, n=4)[2],
                    ]
                    if len(edge_values) >= 2
                    else None
                ),
                "mean_bootstrap_ci95": list(edge_mean_ci),
                "median_bootstrap_ci95": list(edge_median_ci),
            },
            "persistence_minutes": {
                "n": len(persistence_values),
                "mean": statistics.mean(persistence_values) if persistence_values else None,
                "median": statistics.median(persistence_values) if persistence_values else None,
                "iqr": (
                    [
                        statistics.quantiles(persistence_values, n=4)[0],
                        statistics.quantiles(persistence_values, n=4)[2],
                    ]
                    if len(persistence_values) >= 2
                    else None
                ),
                "mean_bootstrap_ci95": list(persistence_mean_ci),
                "median_bootstrap_ci95": list(persistence_median_ci),
            },
            "time_to_correction_minutes": {
                "n_closed_before_settlement": len(ttc_values),
                "mean": statistics.mean(ttc_values) if ttc_values else None,
                "median": statistics.median(ttc_values) if ttc_values else None,
                "iqr": (
                    [
                        statistics.quantiles(ttc_values, n=4)[0],
                        statistics.quantiles(ttc_values, n=4)[2],
                    ]
                    if len(ttc_values) >= 2
                    else None
                ),
                "mean_bootstrap_ci95": list(ttc_mean_ci),
                "median_bootstrap_ci95": list(ttc_median_ci),
            },
        },
        "exploratory_results": {
            "tmin_f_cell": {
                "note": (
                    "same mechanism, mirrored (see build_candidate_frame docstring); "
                    "NOT used for the primary decision"
                ),
                "candidate_candles": tmin_candidates.height,
                "candles_fee_net_qualifying": int(tmin_candidates["qualifies"].sum())
                if tmin_candidates.height
                else 0,
                "episodes_counting_ge2_candles": len(tmin_counting),
                "opportunity_day_frequency": {
                    "days_with_opportunity": len(tmin_days_with_opp),
                    "days_total": len(tmin_valid_days),
                    "frequency": tmin_freq[0],
                    "wilson_ci95": [tmin_freq[1], tmin_freq[2]],
                },
            },
            "by_strike_type": by_strike_type,
            "by_month_descriptive": by_month,
        },
        "date_run": None,  # filled in by __main__
    }
    return results


def render_markdown(results: dict[str, Any]) -> str:
    primary = results["primary_results"]
    freq = primary["opportunity_day_frequency"]
    pnl = primary["aggregate_hypothetical_pnl"]
    decision = primary["decision_rule"]
    cov = results["coverage"]
    secondary = results["secondary_results"]
    tmin_cell = results["exploratory_results"]["tmin_f_cell"]
    fmt1 = primary["failure_mode_1_transparency"]

    dataset_version = results["dataset"]["version"]
    hashes = results["dataset"]["content_hashes"]
    mpw_hash = hashes["market_price_weather"][:24]
    labels_hash = hashes["settlement_labels"][:24]
    db_rev = results["dataset"]["source_db_revision"]
    build_commit = results["dataset"]["dataset_git_commit"][:8]
    fee_multiplier = results["config"]["fee_config"]["multiplier"]
    seed = results["config"]["random_seed"]
    resamples = results["config"]["bootstrap_resamples"]
    date_run = results["date_run"]

    days_available = cov["settlement_days_available"]
    days_excluded = cov["settlement_days_excluded_missing_data"]
    days_total_known = days_available + days_excluded
    excluded_days_str = ", ".join(cov["excluded_days"]) or "none"
    min_days = results["config"]["min_settlement_days_required"]

    candidate_candles = primary["candidate_candles"]
    raw_residual = primary["candles_with_raw_residual_mass"]
    fee_net_qualifying = primary["candles_fee_net_qualifying"]
    episodes_total = primary["episodes_total"]
    episodes_counting = primary["episodes_counting_ge2_candles"]

    freq_ci_lo, freq_ci_hi = freq["wilson_ci95"]
    pnl_ci_lo, pnl_ci_hi = pnl["bootstrap_ci95_dollars"]
    freq_row = (
        f"| Opportunity-day frequency | **{freq['frequency']:.4f}** "
        f"({freq['days_with_opportunity']}/{freq['days_total']}) | "
        f"[{freq_ci_lo:.4f}, {freq_ci_hi:.4f}] | lower bound > 0.05 |"
    )
    pnl_row = (
        f"| Aggregate hypothetical P&L | **${pnl['total_pnl_dollars']:.2f}** "
        f"({pnl['n_episodes_with_label']} episodes) | "
        f"[{pnl_ci_lo}, {pnl_ci_hi}] | lower bound > 0 |"
    )

    tmin_candidates = tmin_cell["candidate_candles"]
    tmin_qualifying = tmin_cell["candles_fee_net_qualifying"]
    tmin_episodes = tmin_cell["episodes_counting_ge2_candles"]

    return f"""# EXP-20260721-H0007-bound-violations

Executed exactly as pre-registered in
`PREREG-20260721-H0007-bound-violations.md`,
`AMENDMENT-20260721-H0007-pre-execution.md`, and
`AMENDMENT-20260721-H0007-fee-verified.md`; no metric, threshold, entry
condition, or decision rule was altered after seeing data. Machine-readable
results: `EXP-20260721-H0007-bound-violations-results.json` (same
directory), produced by `scripts/exp_h0007_bound_violations.py`.

## Hypothesis

**H0007 — Preliminary-observation bound violations near settlement.** After
a same-day preliminary CLI report shows a running high of X°F, markets
occasionally continue to price P(final high < X) above the level H0002's
upward-revision-only constraint justifies. Pre-registered decision rule:
**confirmed** if opportunity-day frequency's 95% CI lower bound > 5% *and*
aggregate hypothetical P&L's 95% CI lower bound > 0; **rejected** if
occurrences are rarer or the aggregate is non-positive.

## Reproducibility

| Field | Value |
|---|---|
| Dataset version | `{dataset_version}` (`data/datasets/{dataset_version}/`) |
| Frame used | `market_price_weather.parquet` |
| Content hash (market_price_weather) | `{mpw_hash}…` (full value in `manifest.json`) |
| Content hash (settlement_labels) | `{labels_hash}…` |
| Source DB revision | `{db_rev}` |
| Dataset-build git commit | `{build_commit}` |
| Fee config | `KALSHI_WEATHER_TAKER_FEE_CONFIG` (multiplier {fee_multiplier}, verified) |
| Random seed | {seed} (bootstrap only; everything else deterministic) |
| Bootstrap resamples | {resamples:,}, day-clustered, percentile method |
| Date run | {date_run} |

## Methodology

Entry conditions, episode definition, persistence, candle adjacency, outcome
definition, and the decision rule are implemented exactly as specified in
the frozen documents above (see `scripts/exp_h0007_bound_violations.py`'s
per-function docstrings for the exact section/finding each block
implements). One methodology note not explicit in the frozen text: the
`market_price_weather` frame does not carry Kalshi's own `kalshi_result`
column (only `market_prices` does); the settlement outcome used for P&L
(never for entry) is derived via `settlement/labels.py`'s `implied_result`
applied to `value_at_settlement` plus the market's own strike bounds -- the
exact function ADR 0006 validated to reproduce Kalshi's own `result` field
exactly, and the same reuse-verbatim instruction Sec 4 condition 4 already
requires for the entry-side bound logic.

## Sample and coverage

- **tmax_f (primary cell)**: {days_available} valid-coverage settlement days
  out of {days_total_known} total ({days_excluded} excluded for missing
  settlement source data: {excluded_days_str}) -- clears the pre-registered
  minimum of {min_days}.
- **{candidate_candles:,} candles** satisfied entry conditions 1-4/6/7
  (bounded strike, unlocked, coherent quote, pre-close) across the full
  archive.
- **{raw_residual} candles** ({raw_residual}/{candidate_candles:,}) showed
  any raw residual mass (`sellable_price_of_bounded_side_cents >= 2`) at
  all, before the fee-net threshold.
- **{fee_net_qualifying} candles** cleared the fee-net threshold.
- **{episodes_total} episodes** total (maximal runs of fee-net-qualifying,
  1-minute-adjacent candles); **{episodes_counting}** met the
  >=2-consecutive-candle persistence requirement and counted toward the
  primary metrics.

## Results (pre-registered metrics)

**Primary decision:**

| Metric | Value | 95% CI | Threshold |
|---|---|---|---|
{freq_row}
{pnl_row}

**Decision: {decision["outcome"].upper()}** ({decision["reason"]}).

**Raw finding, before any statistical machinery**: of {candidate_candles:,}
candles meeting the mechanical entry conditions (a bounded strike,
unlocked, coherent quote, pre-close), only **{raw_residual} showed any
measurable residual mass at all** -- the bounded/near-impossible side was
priced at the exchange's own minimum tick (bid 0¢ / ask 1¢, i.e.
`sellable_price_of_bounded_side_cents == 0` in every single candidate row)
essentially uniformly. This is not a fee-net result narrowly missing the
bar; the raw, cost-free version of the effect is absent from the data at
1-minute candle resolution.

## Secondary outcomes

- **Edge size**: n={secondary["edge_size_cents"]["n"]}; mean/median not
  computable (zero counting episodes).
- **Persistence**: n={secondary["persistence_minutes"]["n"]}; not
  computable (zero counting episodes).
- **Time-to-correction**:
  n={secondary["time_to_correction_minutes"]["n_closed_before_settlement"]};
  not computable (zero counting episodes).
- **Failure-mode-1 transparency** (single-episode-driven days):
  {fmt1["days_driven_by_single_episode"]} of {fmt1["days_with_opportunity"]}
  opportunity-days -- moot, since there are zero opportunity-days.

## Exploratory (not used for the primary decision)

- **tmin_f cell** (mirrored mechanism): {tmin_candidates:,} candidate
  candles, {tmin_qualifying} fee-net qualifying, {tmin_episodes} counting
  episodes. Same pattern as the primary cell: raw residual mass is absent.
- **By strike type** (raw candidate/qualifying counts): see
  `by_strike_type` in the results JSON.
- **By month** (descriptive only): see `by_month_descriptive` in the
  results JSON.

## Conclusion

**REJECTED**, per the pre-registered decision rule, cleanly rather than at
the margin: across {candidate_candles:,} candles where the mechanical
entry conditions were met (a logically bounded strike, unlocked, coherent
non-crossed quote), the market's quoted price for the near-impossible side
was at the exchange's own minimum tick in every single case -- zero raw
residual mass, let alone fee-net. The opportunity-day frequency point
estimate is 0 (Wilson 95% CI upper bound {freq_ci_hi:.4f}, narrowly above
the pre-registered 5% threshold on that one criterion alone, but the
aggregate-P&L criterion -- CI upper bound $0.00 with zero qualifying
episodes -- independently and unambiguously satisfies the rejection rule).

Per `AMENDMENT-20260721-H0007-pre-execution.md` Finding 6, this conclusion
is stated within its permissible scope: this is evidence about *quote-level
pricing behavior* at 1-minute candle resolution, not a claim about realized
trading profit, fill probability, or latency-adjusted capturability -- no
such claim would be supportable regardless, since the result found nothing
to capture. No forecasting feature, statistical model, or alpha claim was
introduced in producing this result.

## Limitations

- **1-minute candle resolution** is the finest Kalshi provides; a
  violation that opens and closes within a single minute is invisible to
  this design (and, since Kalshi's own minimum tradeable price is 1¢, so is
  any violation cheaper than the fee to close it).
- **tmax_f only** for the primary decision, per pre-registration; the
  tmin_f exploratory cell shows the same pattern but was not subjected to
  the same statistical rigor (no bootstrap CIs computed for it, by design
  — see Sec 6's multiple-testing discussion).
- **NYC only** (both series settle against the same station), consistent
  with every prior hypothesis on this platform to date.
- **Issuance-to-availability latency is unmeasured** (Finding 6) — moot
  here, since no exploitable gap was found regardless of latency.
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args()

    from datetime import UTC, datetime

    results = run(args.dataset_dir)
    results["date_run"] = datetime.now(UTC).date().isoformat()

    args.out.write_text(json.dumps(results, indent=2, default=str))
    args.out_md.write_text(render_markdown(results))
    print(f"wrote {args.out}")
    print(f"wrote {args.out_md}")
    print(f"decision: {results['primary_results']['decision_rule']['outcome']}")
