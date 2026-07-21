"""Tests for scripts/exp_h0005_calibration.py.

Implements exactly what H0005's own docstring cites:
  - docs/research/experiments/PREREG-20260721-H0005-calibration.md
  - docs/research/experiments/AMENDMENT-20260721-H0005-pre-execution.md

All data here is hand-constructed/synthetic; nothing in this file touches
the real pinned H0005 dataset or inspects a real calibration outcome. A
handful of tests near the end do call `run()` end-to-end, but only against
a fabricated `dataset_dir` fixture built in this file (a few synthetic
markets, not Kalshi data) -- this exercises the manifest-assembly/
validation code path, not H0005 itself. Everything else exercises the
script's building blocks in isolation, exactly as `scripts/h0007_fees.py`'s
and `tests/unit/test_dataset_asof.py`'s tests do for their own scripts/
modules.
"""

from __future__ import annotations

import json
import random
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from unittest.mock import patch

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import exp_h0005_calibration as h0005

SOURCE_SCHEMA = {
    "market_ticker": pl.Utf8,
    "close_time": pl.Datetime("us"),
    "target_date": pl.Date,
    "strike_type": pl.Utf8,
    "floor_strike": pl.Float64,
    "cap_strike": pl.Float64,
    "settlement_label_status": pl.Utf8,
    "value_at_settlement": pl.Float64,
    "period_end": pl.Datetime("us"),
    "period_interval_seconds": pl.Int64,
    "yes_bid_close_cents": pl.Int64,
    "yes_ask_close_cents": pl.Int64,
}


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=SOURCE_SCHEMA, orient="row")  # type: ignore[arg-type]


def _market_rows(
    ticker: str,
    *,
    close_time: datetime,
    target_date: date,
    strike_type: str = "greater",
    floor_strike: float | None = 50.0,
    cap_strike: float | None = None,
    settlement_label_status: str = "resolved",
    value_at_settlement: float | None = 60.0,
    candles: list[tuple[datetime, int, int]],
    period_interval_seconds: int = 60,
) -> list[dict[str, Any]]:
    """One row per candle for `ticker`; the market-static columns are
    repeated verbatim on every row, matching how market_price_weather's
    real export carries them (deduplicated later by `.unique()`)."""
    return [
        {
            "market_ticker": ticker,
            "close_time": close_time,
            "target_date": target_date,
            "strike_type": strike_type,
            "floor_strike": floor_strike,
            "cap_strike": cap_strike,
            "settlement_label_status": settlement_label_status,
            "value_at_settlement": value_at_settlement,
            "period_end": period_end,
            "period_interval_seconds": period_interval_seconds,
            "yes_bid_close_cents": bid,
            "yes_ask_close_cents": ask,
        }
        for period_end, bid, ask in candles
    ]


# --- Horizon assignment (PREREG Sec 6) ---------------------------------------


def test_select_horizon_observations_picks_latest_candle_at_or_before_target() -> None:
    close = datetime(2026, 7, 20, 0, 0)
    rows = _market_rows(
        "KXHIGHNY-26JUL20",
        close_time=close,
        target_date=date(2026, 7, 20),
        candles=[
            (datetime(2026, 7, 19, 21, 30), 40, 42),  # 2.5h before close: within tolerance? no
            (datetime(2026, 7, 19, 22, 0), 45, 47),  # exactly close - 2h: gap 0
            (datetime(2026, 7, 19, 23, 0), 90, 92),  # after target_instant: must never be picked
        ],
    )
    out = h0005.select_horizon_observations(_frame(rows), horizon_hours=2)
    assert out.height == 1
    row = out.row(0, named=True)
    assert row["selected_period_end"] == datetime(2026, 7, 19, 22, 0)
    assert row["yes_bid_close_cents"] == 45
    assert row["yes_ask_close_cents"] == 47


def test_select_horizon_observations_excludes_beyond_staleness_tolerance() -> None:
    close = datetime(2026, 7, 20, 0, 0)
    rows = _market_rows(
        "KXHIGHNY-26JUL20",
        close_time=close,
        target_date=date(2026, 7, 20),
        # target_instant for horizon=2 is 2026-07-19 22:00; nearest candle is
        # 20 minutes earlier -- beyond the 15-minute tolerance.
        candles=[(datetime(2026, 7, 19, 21, 40), 40, 42)],
    )
    out = h0005.select_horizon_observations(_frame(rows), horizon_hours=2)
    assert out.height == 0


def test_select_horizon_observations_within_staleness_tolerance_is_kept() -> None:
    close = datetime(2026, 7, 20, 0, 0)
    rows = _market_rows(
        "KXHIGHNY-26JUL20",
        close_time=close,
        target_date=date(2026, 7, 20),
        candles=[(datetime(2026, 7, 19, 21, 50), 40, 42)],  # 10 min before target
    )
    out = h0005.select_horizon_observations(_frame(rows), horizon_hours=2)
    assert out.height == 1


def test_select_horizon_observations_filters_crossed_quote_before_join() -> None:
    """PREREG Sec 11: a crossed/missing quote is removed from the candidate
    pool before the as-of join, never selected merely for being nearest."""
    close = datetime(2026, 7, 20, 0, 0)
    rows = _market_rows(
        "KXHIGHNY-26JUL20",
        close_time=close,
        target_date=date(2026, 7, 20),
        candles=[
            (datetime(2026, 7, 19, 21, 50), 30, 32),  # valid, within tolerance
            (datetime(2026, 7, 19, 22, 0), 60, 50),  # crossed (bid > ask), nearer: excluded
        ],
    )
    out = h0005.select_horizon_observations(_frame(rows), horizon_hours=2)
    assert out.height == 1
    row = out.row(0, named=True)
    assert row["selected_period_end"] == datetime(2026, 7, 19, 21, 50)


def test_build_observation_table_assigns_all_six_frozen_horizons() -> None:
    close = datetime(2026, 7, 20, 0, 0)
    candles = [(close - timedelta(hours=h), 40, 42) for h in h0005.HORIZON_HOURS]
    rows = _market_rows(
        "KXHIGHNY-26JUL20", close_time=close, target_date=date(2026, 7, 20), candles=candles
    )
    obs = h0005.build_observation_table(_frame(rows))
    assert sorted(o.horizon_hours for o in obs) == sorted(h0005.HORIZON_HOURS)
    assert {o.market_ticker for o in obs} == {"KXHIGHNY-26JUL20"}


# --- Decile assignment (PREREG Sec 7) ----------------------------------------


@pytest.mark.parametrize(
    "mid_cents,expected_decile",
    [
        (0, 0),
        (9.99, 0),
        (10, 1),
        (10.01, 1),
        (59.99, 5),
        (60.0, 6),  # boundary belongs to the closed-left bucket it opens
        (69.99, 6),
        (70.0, 7),
        (90.0, 9),
        (99.0, 9),
        (100.0, 9),  # defensive clip; real data never reaches 100
    ],
)
def test_assign_decile_boundaries(mid_cents: float, expected_decile: int) -> None:
    assert h0005.assign_decile(mid_cents) == expected_decile


# --- Duplicate-observation invariants (AMENDMENT Finding 9) -----------------


def _obs(ticker: str, horizon: int, **overrides: Any) -> h0005.HorizonObservation:
    base: dict[str, Any] = {
        "market_ticker": ticker,
        "horizon_hours": horizon,
        "target_date": date(2026, 7, 20),
        "strike_type": "greater",
        "floor_strike": 50.0,
        "cap_strike": None,
        "settlement_label_status": "resolved",
        "value_at_settlement": 60.0,
        "period_end": datetime(2026, 7, 19, 22, 0),
        "yes_bid_close_cents": 40,
        "yes_ask_close_cents": 42,
        "mid_price_cents": 41.0,
        "decile": 4,
        "cost_band_cents": 3,
        "outcome": 1,
    }
    base.update(overrides)
    return h0005.HorizonObservation(**base)


def test_assert_no_duplicate_observations_passes_on_unique_market_horizon_pairs() -> None:
    obs = [_obs("A", 2), _obs("A", 6), _obs("B", 2)]
    h0005.assert_no_duplicate_observations(obs)  # must not raise


def test_assert_no_duplicate_observations_raises_on_duplicate_market_horizon_pair() -> None:
    obs = [_obs("A", 2), _obs("A", 2)]
    with pytest.raises(h0005.DuplicateObservationError, match="A"):
        h0005.assert_no_duplicate_observations(obs)


def test_assert_candle_natural_key_unique_raises_on_duplicate_key() -> None:
    rows = _market_rows(
        "A",
        close_time=datetime(2026, 7, 20, 0, 0),
        target_date=date(2026, 7, 20),
        candles=[(datetime(2026, 7, 19, 22, 0), 40, 42), (datetime(2026, 7, 19, 22, 0), 41, 43)],
    )
    with pytest.raises(h0005.DuplicateObservationError):
        h0005.assert_candle_natural_key_unique(_frame(rows))


def test_assert_candle_natural_key_unique_passes_on_unique_keys() -> None:
    rows = _market_rows(
        "A",
        close_time=datetime(2026, 7, 20, 0, 0),
        target_date=date(2026, 7, 20),
        candles=[(datetime(2026, 7, 19, 21, 0), 40, 42), (datetime(2026, 7, 19, 22, 0), 41, 43)],
    )
    h0005.assert_candle_natural_key_unique(_frame(rows))  # must not raise


def test_assert_candle_natural_key_unique_is_a_noop_without_key_columns() -> None:
    frame = pl.DataFrame({"unrelated": [1, 2]})
    h0005.assert_candle_natural_key_unique(frame)  # must not raise


# --- Empty-cell behavior (AMENDMENT Finding 7) -------------------------------


def test_cell_point_estimates_empty_returns_none_not_zero() -> None:
    est = h0005._cell_point_estimates([])
    assert est == {
        "mean_predicted_probability": None,
        "realized_yes_frequency": None,
        "calibration_error": None,
        "cost_band_cents": None,
    }


def test_cell_point_estimates_all_unlabeled_returns_none() -> None:
    est = h0005._cell_point_estimates([_obs("A", 2, outcome=None)])
    assert est["calibration_error"] is None


def test_cell_point_estimates_nonempty_computes_calibration_error() -> None:
    obs = [
        _obs("A", 2, mid_price_cents=60.0, outcome=1, cost_band_cents=3),
        _obs("B", 2, mid_price_cents=60.0, outcome=0, cost_band_cents=5),
    ]
    est = h0005._cell_point_estimates(obs)
    assert est["mean_predicted_probability"] == pytest.approx(0.6)
    assert est["realized_yes_frequency"] == pytest.approx(0.5)
    assert est["calibration_error"] == pytest.approx(0.1)
    assert est["cost_band_cents"] == pytest.approx(4.0)


def test_expected_calibration_error_skips_empty_deciles() -> None:
    """Only deciles 0 and 9 have observations; ECE must weight strictly by
    those two deciles' own share of the total, never treating the eight
    empty deciles as 0-weight contributions that still touch an undefined
    calibration_error."""
    obs = [
        _obs("A", 2, mid_price_cents=5.0, decile=0, outcome=0),  # calib err 0
        _obs("B", 2, mid_price_cents=95.0, decile=9, outcome=0),  # calib err 0.95
    ]
    ece = h0005.expected_calibration_error(obs)
    # decile 0: |0.05 - 0| = 0.05; decile 9: |0.95 - 0| = 0.95; equal weight (1 obs each)
    assert ece == pytest.approx(0.5 * 0.05 + 0.5 * 0.95)


def test_ci_lower_bound_of_abs_straddling_zero_is_zero() -> None:
    assert h0005._ci_lower_bound_of_abs(-0.01, 0.02) == 0.0
    assert h0005._ci_lower_bound_of_abs(0.0, 0.02) == 0.0


def test_ci_lower_bound_of_abs_same_sign_is_min_magnitude() -> None:
    assert h0005._ci_lower_bound_of_abs(0.02, 0.05) == pytest.approx(0.02)
    assert h0005._ci_lower_bound_of_abs(-0.05, -0.02) == pytest.approx(0.02)


def test_ci_lower_bound_of_abs_none_input_is_none() -> None:
    assert h0005._ci_lower_bound_of_abs(None, 0.02) is None


# --- Bootstrap pool separation (AMENDMENT Finding 2) -------------------------


def test_bootstrap_never_draws_a_day_outside_its_pool() -> None:
    """day_values carries both slices' days, but day_pool only names one --
    the resample must never touch the other slice's data, regardless of
    what day_values happens to contain."""
    day_values = {date(2026, 7, 1): [5.0], date(2026, 7, 2): [100.0]}
    rng = random.Random(1)
    lo, hi = h0005.day_clustered_bootstrap_ci(
        day_values, [date(2026, 7, 1)], rng=rng, lower_pct=0.025, upper_pct=0.975, resamples=200
    )
    assert lo == pytest.approx(5.0)
    assert hi == pytest.approx(5.0)


def test_bootstrap_pool_disjoint_from_day_values_yields_no_data() -> None:
    """If day_pool names only days that have no entry in day_values (e.g. a
    cell had zero labeled observations in that slice), every resample's
    pooled set is empty and the CI is (None, None) -- it must never fall
    back to some other day's data."""
    day_values = {date(2026, 7, 2): [1.0]}
    rng = random.Random(1)
    lo, hi = h0005.day_clustered_bootstrap_ci(
        day_values, [date(2026, 7, 1)], rng=rng, lower_pct=0.025, upper_pct=0.975, resamples=50
    )
    assert (lo, hi) == (None, None)


def test_bootstrap_empty_pool_returns_none() -> None:
    lo, hi = h0005.day_clustered_bootstrap_ci(
        {}, [], rng=random.Random(1), lower_pct=0.025, upper_pct=0.975
    )
    assert (lo, hi) == (None, None)


def test_build_cells_discovery_and_holdout_slices_never_mix() -> None:
    """End-to-end through build_cells: a cell whose only labeled
    observation falls on a holdout day must show zero discovery days/None
    discovery point estimates, and vice versa."""
    obs = [
        _obs("A", 72, decile=4, target_date=date(2026, 7, 1), outcome=1, mid_price_cents=45.0),
        _obs("B", 72, decile=4, target_date=date(2026, 7, 10), outcome=0, mid_price_cents=45.0),
    ]
    cells = h0005.build_cells(
        obs,
        discovery_days=[date(2026, 7, 1)],
        holdout_days=[date(2026, 7, 10)],
        rng=random.Random(h0005.RANDOM_SEED),
    )
    cell = next(c for c in cells if c.horizon_hours == 72 and c.decile == 4)
    assert cell.n_days_discovery == 1
    assert cell.n_days_holdout == 1
    assert cell.mean_predicted_probability == pytest.approx(0.45)  # discovery only: obs A
    assert cell.holdout_mean_predicted_probability == pytest.approx(0.45)  # holdout only: obs B
    assert cell.realized_yes_frequency == pytest.approx(1.0)  # A's outcome
    assert cell.holdout_realized_yes_frequency == pytest.approx(0.0)  # B's outcome


# --- Deterministic RNG ordering (AMENDMENT Finding 3) ------------------------


def test_day_clustered_bootstrap_ci_is_deterministic_for_a_fixed_seed() -> None:
    day_values = {date(2026, 7, 1): [1.0, 2.0], date(2026, 7, 2): [3.0]}
    pool = [date(2026, 7, 1), date(2026, 7, 2)]
    first = h0005.day_clustered_bootstrap_ci(
        day_values,
        pool,
        rng=random.Random(h0005.RANDOM_SEED),
        lower_pct=0.025,
        upper_pct=0.975,
        resamples=500,
    )
    second = h0005.day_clustered_bootstrap_ci(
        day_values,
        pool,
        rng=random.Random(h0005.RANDOM_SEED),
        lower_pct=0.025,
        upper_pct=0.975,
        resamples=500,
    )
    assert first == second


def test_build_cells_consumes_rng_in_discovery_then_holdout_nested_order() -> None:
    """AMENDMENT Finding 3: a single rng instance, 60 discovery-slice calls
    (HORIZON_HOURS order outer, decile 0..9 inner) followed by 60
    unconditional hold-out-slice calls in the same nested order -- nothing
    else in build_cells may consume randomness."""
    calls: list[tuple[Any, float, float, Any]] = []
    original = h0005.day_clustered_bootstrap_ci

    def spy(
        day_values: dict[Any, list[float]],
        day_pool: list[Any],
        *,
        rng: random.Random,
        lower_pct: float,
        upper_pct: float,
        resamples: int = h0005.BOOTSTRAP_RESAMPLES,
    ) -> tuple[float | None, float | None]:
        calls.append((tuple(day_pool), lower_pct, upper_pct, rng))
        return (None, None)

    h0005.day_clustered_bootstrap_ci = spy
    try:
        rng = random.Random(h0005.RANDOM_SEED)
        discovery_days = [date(2026, 7, 1)]
        holdout_days = [date(2026, 7, 10)]
        h0005.build_cells([], discovery_days=discovery_days, holdout_days=holdout_days, rng=rng)
    finally:
        h0005.day_clustered_bootstrap_ci = original

    assert len(calls) == 120

    expected_cell_order = [(h, d) for h in h0005.HORIZON_HOURS for d in range(h0005.N_DECILES)]
    assert len(expected_cell_order) == 60

    discovery_calls = calls[:60]
    holdout_calls = calls[60:]

    bonferroni_lower = h0005.BONFERRONI_ALPHA / 2
    bonferroni_upper = 1 - h0005.BONFERRONI_ALPHA / 2
    for pool, lower_pct, upper_pct, call_rng in discovery_calls:
        assert pool == tuple(discovery_days)
        assert lower_pct == pytest.approx(bonferroni_lower)
        assert upper_pct == pytest.approx(bonferroni_upper)
        assert call_rng is rng  # single shared instance, never re-seeded

    for pool, lower_pct, upper_pct, call_rng in holdout_calls:
        assert pool == tuple(holdout_days)
        assert lower_pct == pytest.approx(0.025)
        assert upper_pct == pytest.approx(0.975)
        assert call_rng is rng

    # All 60 holdout cells are present even though `obs` was empty --
    # unconditional computation, never skipped based on discovery outcome.
    assert len(holdout_calls) == 60


# --- Bonferroni correction ----------------------------------------------------


def test_bonferroni_alpha_matches_frozen_family_size() -> None:
    assert h0005.PRIMARY_FAMILY_SIZE == 60
    assert pytest.approx(0.05 / 60) == h0005.BONFERRONI_ALPHA


def test_discovery_bonferroni_pass_requires_ci_lower_bound_above_cost_band() -> None:
    """Exercises the same comparison build_cells performs in Pass 1: a
    discovery CI whose lower bound of |calibration_error| exceeds the cost
    band clears the screen; one that doesn't, does not."""
    cost_band = 0.02
    clears = h0005._ci_lower_bound_of_abs(0.1, 0.4)  # lower bound 0.1 > 0.02
    fails = h0005._ci_lower_bound_of_abs(0.01, 0.015)  # lower bound 0.01 < 0.02
    assert clears is not None and clears > cost_band
    assert fails is not None and not (fails > cost_band)


# === Cost-band unit-consistency fix ==========================================
#
# The implementation review found that build_cells originally compared
# calibration_error (a fractional [0,1] probability deviation) directly
# against cost_band_cents (raw cents, always >= 1) -- since a fractional
# deviation can never exceed 1, and cost_band_cents is never below 1, the
# discovery/hold-out screens could never clear for ANY data, making
# "Rejected" structurally unreachable. The fix converts cost_band_cents to
# the same [0,1] scale via _cost_band_probability (cents / 100, the same
# conversion PREREG Sec 8 already uses for ask/bid-implied prices) at the
# three comparison sites (build_cells' two screens,
# compute_executable_price_robustness's descriptive check) -- it does not
# change calibration_error's own units, cost_band_cents's own formula, any
# threshold, horizon, decile, or the bootstrap/RNG mechanics themselves.


def test_cost_band_probability_converts_cents_to_fraction() -> None:
    assert h0005._cost_band_probability(3.0) == pytest.approx(0.03)
    assert h0005._cost_band_probability(0.0) == pytest.approx(0.0)
    assert h0005._cost_band_probability(100.0) == pytest.approx(1.0)


def test_regression_old_unscaled_comparison_would_never_clear_the_screen() -> None:
    """Demonstrates the exact bug the fix corrects: comparing a fractional
    calibration_error directly against raw cost_band_cents (no unit
    conversion) can never be exceeded for any realistic cost band, since
    cost_band_cents is always >= 1 (contract_fee_cents never returns zero)
    while a probability deviation is bounded to [0, 1] -- the previous
    implementation made 'Rejected' structurally unreachable regardless of
    the true data. The fixed comparison correctly recognizes this same,
    obviously miscalibrated cell as exceeding cost."""
    lower_abs = 0.90  # a hugely miscalibrated cell's CI lower bound
    cost_band_cents = 2.0  # a realistic, small round-trip cost

    old_buggy_result = lower_abs > cost_band_cents  # the pre-fix comparison
    fixed_result = lower_abs > h0005._cost_band_probability(cost_band_cents)

    assert old_buggy_result is False  # the bug: blatant miscalibration never passed
    assert fixed_result is True  # the fix: correctly recognized as exceeding cost


def _identical_value_cell_observations(
    *,
    horizon: int,
    decile: int,
    mid_price_cents: float,
    outcome: int,
    cost_band_cents: float,
    n_days: int,
    start: date = date(2026, 1, 1),
) -> list[h0005.HorizonObservation]:
    """n_days distinct markets/days, each contributing the identical
    (mid_price_cents, outcome, cost_band_cents) to one (horizon, decile)
    cell -- every bootstrap resample therefore pools the same constant
    value, collapsing the CI to that exact point regardless of resamples
    or RNG draw, which is what makes the equality/boundary tests below
    exact rather than approximate."""
    return [
        _obs(
            f"M{i}",
            horizon,
            decile=decile,
            target_date=start + timedelta(days=i),
            mid_price_cents=mid_price_cents,
            outcome=outcome,
            cost_band_cents=cost_band_cents,
        )
        for i in range(n_days)
    ]


def test_build_cells_discovery_screen_boundary_equality_does_not_clear() -> None:
    """calibration_error == cost_band exactly (post-conversion) must NOT
    clear the screen -- the frozen comparison is strict ('exceeds'/'>'),
    never '>='. mid=60, outcome=0 -> calibration_error = 0.60 exactly;
    cost_band_cents = 60 -> _cost_band_probability = 0.60 exactly."""
    obs = _identical_value_cell_observations(
        horizon=72, decile=6, mid_price_cents=60.0, outcome=0, cost_band_cents=60.0, n_days=5
    )
    with patch.object(h0005, "day_clustered_bootstrap_ci", _fast_bootstrap):
        cells = h0005.build_cells(
            obs,
            discovery_days=[o.target_date for o in obs],
            holdout_days=[],
            rng=random.Random(h0005.RANDOM_SEED),
        )
    cell = next(c for c in cells if c.horizon_hours == 72 and c.decile == 6)
    assert cell.calibration_error == pytest.approx(0.60)
    assert cell.discovery_ci == (pytest.approx(0.60), pytest.approx(0.60))
    assert cell.discovery_bonferroni_pass is False


def test_build_cells_discovery_screen_just_inside_cost_band_does_not_clear() -> None:
    """calibration_error (0.60) slightly below the cost band (0.61) must
    not clear the screen."""
    obs = _identical_value_cell_observations(
        horizon=72, decile=6, mid_price_cents=60.0, outcome=0, cost_band_cents=61.0, n_days=5
    )
    with patch.object(h0005, "day_clustered_bootstrap_ci", _fast_bootstrap):
        cells = h0005.build_cells(
            obs,
            discovery_days=[o.target_date for o in obs],
            holdout_days=[],
            rng=random.Random(h0005.RANDOM_SEED),
        )
    cell = next(c for c in cells if c.horizon_hours == 72 and c.decile == 6)
    assert cell.discovery_bonferroni_pass is False


def test_build_cells_discovery_screen_just_outside_cost_band_clears() -> None:
    """calibration_error (0.60) slightly above the cost band (0.59) must
    clear the screen -- proving the screen is satisfiable at all, which
    the pre-fix comparison could never do for any input."""
    obs = _identical_value_cell_observations(
        horizon=72, decile=6, mid_price_cents=60.0, outcome=0, cost_band_cents=59.0, n_days=5
    )
    with patch.object(h0005, "day_clustered_bootstrap_ci", _fast_bootstrap):
        cells = h0005.build_cells(
            obs,
            discovery_days=[o.target_date for o in obs],
            holdout_days=[],
            rng=random.Random(h0005.RANDOM_SEED),
        )
    cell = next(c for c in cells if c.horizon_hours == 72 and c.decile == 6)
    assert cell.discovery_bonferroni_pass is True


def test_evaluate_outcome_rejection_is_mathematically_reachable() -> None:
    """End-to-end proof that 'Rejected' is reachable post-fix: a single,
    obviously and persistently miscalibrated cell (mid=90, outcome always
    0, i.e. a 90%-priced contract that never resolves YES) with a small
    cost band, enough total days to clear the Sec 12 floor, and enough
    per-slice days to clear the Sec 10 min-N gate in both the discovery
    and hold-out slices, must clear both screens and produce outcome ==
    'rejected'. Pre-fix, no input could ever satisfy this."""
    obs = _identical_value_cell_observations(
        horizon=72, decile=9, mid_price_cents=90.0, outcome=0, cost_band_cents=2.0, n_days=70
    )
    valid_days = sorted({o.target_date for o in obs})
    discovery_days, holdout_days = h0005.chronological_split(valid_days)
    assert len(discovery_days) >= h0005.MIN_DAYS_PER_CELL
    assert len(holdout_days) >= h0005.MIN_DAYS_PER_CELL

    with patch.object(h0005, "day_clustered_bootstrap_ci", _fast_bootstrap):
        cells = h0005.build_cells(
            obs,
            discovery_days=discovery_days,
            holdout_days=holdout_days,
            rng=random.Random(h0005.RANDOM_SEED),
        )
    verdict = h0005.evaluate_outcome(cells, total_valid_days=len(valid_days))

    assert verdict.outcome == "rejected"
    assert (72, 9) in verdict.clearing_cells


# --- Chronological replication split (AMENDMENT Finding 8) ------------------


def test_chronological_split_excludes_one_day_embargo_at_boundary() -> None:
    days = [date(2026, 7, d) for d in range(1, 11)]  # 10 days
    discovery, holdout = h0005.chronological_split(days)
    embargoed = set(days) - set(discovery) - set(holdout)
    assert len(embargoed) == 1
    assert max(discovery) < min(embargoed) < min(holdout)


def test_chronological_split_odd_post_embargo_count_gives_extra_day_to_discovery() -> None:
    # 9 days -> embargo_idx = 4 -> pre=4, post=4 -> post-embargo total = 8 (even)
    # Use 8 days so post-embargo count is odd instead: embargo_idx=4, pre=4,
    # post=days[5:]=3 -> total post-embargo = 7 (odd).
    days = [date(2026, 7, d) for d in range(1, 9)]  # 8 days
    discovery, holdout = h0005.chronological_split(days)
    assert len(discovery) + len(holdout) == 7
    assert len(discovery) == len(holdout) + 1


def test_chronological_split_even_post_embargo_count_splits_equally() -> None:
    days = [date(2026, 7, d) for d in range(1, 8)]  # 7 days: embargo_idx=3, pre=3, post=3 -> 6
    discovery, holdout = h0005.chronological_split(days)
    assert len(discovery) + len(holdout) == 6
    assert len(discovery) == len(holdout)


def test_chronological_split_fewer_than_two_days_returns_all_as_discovery() -> None:
    assert h0005.chronological_split([date(2026, 7, 1)]) == ([date(2026, 7, 1)], [])
    assert h0005.chronological_split([]) == ([], [])


def test_chronological_split_deduplicates_and_sorts_input() -> None:
    days = [date(2026, 7, 3), date(2026, 7, 1), date(2026, 7, 3), date(2026, 7, 2)]
    discovery, holdout = h0005.chronological_split(days)
    assert (discovery + holdout).count(date(2026, 7, 3)) <= 1


# --- Fee calculation invocation (AMENDMENT Finding 5) ------------------------


def test_round_half_up_cents_rounds_half_up_not_banker_rounding() -> None:
    assert h0005.round_half_up_cents(80.5) == 81  # not 80 (Python round() would give 80)
    assert h0005.round_half_up_cents(79.5) == 80  # not 80 via banker's-rounding coincidence check
    assert h0005.round_half_up_cents(80.4) == 80
    assert h0005.round_half_up_cents(80.6) == 81


def test_cost_band_cents_invokes_contract_fee_cents_with_frozen_arguments() -> None:
    calls: list[dict[str, Any]] = []
    original = h0005.contract_fee_cents

    def spy(price_cents: int, *, contracts: int, config: Any) -> int:
        calls.append({"price_cents": price_cents, "contracts": contracts, "config": config})
        return int(original(price_cents, contracts=contracts, config=config))

    h0005.contract_fee_cents = spy
    try:
        h0005.cost_band_cents(mid_price_cents=80.5, yes_bid_close_cents=79, yes_ask_close_cents=82)
    finally:
        h0005.contract_fee_cents = original

    assert len(calls) == 1
    assert calls[0]["price_cents"] == 81  # round_half_up(80.5)
    assert calls[0]["contracts"] == 1
    assert calls[0]["config"] is h0005.KALSHI_WEATHER_TAKER_FEE_CONFIG


def test_cost_band_cents_adds_exact_half_spread_to_fee() -> None:
    fee = h0005.contract_fee_cents(
        h0005.round_half_up_cents(50.0), contracts=1, config=h0005.KALSHI_WEATHER_TAKER_FEE_CONFIG
    )
    band = h0005.cost_band_cents(
        mid_price_cents=50.0, yes_bid_close_cents=45, yes_ask_close_cents=55
    )
    assert band == fee + 5  # half_spread = (55-45)/2 = 5, an exact integer here
    assert isinstance(band, Fraction)


def test_cost_band_cents_at_100_cent_boundary_uses_zero_fee_not_a_crash() -> None:
    """Execution-time defect fix #1: bid=99/ask=100 -> mid=99.5 ->
    round_half_up -> 100, outside contract_fee_cents's documented 1-99
    domain. The fee at that boundary is the frozen formula's own
    well-defined limit (ceil(multiplier * 1 * (1-1) * contracts) == 0),
    not a crash and not a clamp to a different price. The half-spread here
    (1 cent, per fix #2 below) is exact, never rounded."""
    band = h0005.cost_band_cents(
        mid_price_cents=99.5, yes_bid_close_cents=99, yes_ask_close_cents=100
    )
    assert band == Fraction(1, 2)  # fee=0, half_spread=(100-99)/2=1/2 exactly


def test_cost_band_cents_at_0_cent_boundary_uses_zero_fee_not_a_crash() -> None:
    """Symmetric boundary: bid=0/ask=1 -> mid=0.5 -> round_half_up -> 1,
    which IS in contract_fee_cents's domain, so this exercises the exact
    P=0 case: bid=0/ask=0 -> mid=0.0 -> round_half_up -> 0."""
    band = h0005.cost_band_cents(mid_price_cents=0.0, yes_bid_close_cents=0, yes_ask_close_cents=0)
    assert band == 0


def test_cost_band_cents_normal_range_still_delegates_to_contract_fee_cents() -> None:
    """The 1-99 fix path must not change behavior anywhere inside
    contract_fee_cents's own documented domain -- exercised across the
    full 1-99 range, comparing against a direct contract_fee_cents call."""
    for price in (1, 2, 25, 49, 50, 51, 75, 98, 99):
        expected_fee = h0005.contract_fee_cents(
            price, contracts=1, config=h0005.KALSHI_WEATHER_TAKER_FEE_CONFIG
        )
        band = h0005.cost_band_cents(
            mid_price_cents=float(price), yes_bid_close_cents=price, yes_ask_close_cents=price
        )
        assert band == expected_fee  # zero spread at bid==ask


# === Execution-integrity remediation: exact half-spread / exact comparison ==
#
# The first H0005 execution attempt (superseded -- see
# IMPLEMENTATION-NOTE-20260721-H0005-exact-cost-band.md) computed
# `fee + round(half_spread)` (Python banker's rounding), which the frozen
# protocol never specifies, and compared the result against a bootstrap CI
# bound using raw binary float `>`. Both defects are corrected below:
# half_spread_cents is now an exact Fraction, never rounded, and the
# discovery/hold-out screen comparison is decided in controlled-precision
# Decimal arithmetic so an exact tie is never resolved by which side's
# float representation happens to round up.


def test_one_cent_spread_yields_exact_half_cent_half_spread() -> None:
    """The case that silently broke the superseded execution: a 1-cent
    spread's true half-spread is 0.5 cents, not round(0.5) == 0."""
    band = h0005.cost_band_cents(
        mid_price_cents=50.0, yes_bid_close_cents=50, yes_ask_close_cents=51
    )
    fee = h0005.contract_fee_cents(50, contracts=1, config=h0005.KALSHI_WEATHER_TAKER_FEE_CONFIG)
    assert band == fee + Fraction(1, 2)
    assert band != fee  # the superseded, rounded implementation would assert this


def test_three_cent_spread_yields_exact_one_and_a_half_cent_half_spread() -> None:
    """A second odd-cent spread, at a different rounding parity
    (round(1.5) == 2 under banker's rounding) -- exact arithmetic must not
    round either direction."""
    band = h0005.cost_band_cents(
        mid_price_cents=50.0, yes_bid_close_cents=49, yes_ask_close_cents=52
    )
    fee = h0005.contract_fee_cents(50, contracts=1, config=h0005.KALSHI_WEATHER_TAKER_FEE_CONFIG)
    assert band == fee + Fraction(3, 2)


def test_cell_cost_band_averages_exactly_across_observations() -> None:
    """_cell_point_estimates averages cost_band_cents as Fraction, so a
    cell mixing exact fractional per-observation bands (e.g. two
    observations with a 1-cent spread each) produces an exact mean, never
    a float-rounded one."""
    obs = [
        _obs("A", 2, decile=9, mid_price_cents=99.5, outcome=1, cost_band_cents=Fraction(1, 2)),
        _obs("B", 2, decile=9, mid_price_cents=99.5, outcome=1, cost_band_cents=Fraction(1, 2)),
    ]
    point = h0005._cell_point_estimates(obs)
    assert point["cost_band_cents"] == Fraction(1, 2)


def test_exceeds_cost_band_exact_tie_is_false() -> None:
    """AMENDMENT/PREREG's strict '>' comparison: a mathematically exact
    tie (1/200 vs 1/200) must evaluate to False, not True-by-float-noise.
    lower_abs is passed as the actual IEEE-754 float
    abs(99.5/100 - 1) produces, which carries ~4e-18 of representation
    error above the true value 1/200 -- exactly the situation that made
    the superseded execution's sole clearing cell pass its discovery
    screen on floating-point noise."""
    lower_abs = abs(99.5 / 100 - 1)
    assert lower_abs != 0.005  # the float is NOT bit-identical to 1/200
    # Fraction(1, 200) is already probability-scale (cost_band_cents=1/2 ->
    # _cost_band_probability divides by 100 -> 1/200), matching lower_abs.
    cost_band_probability = Fraction(1, 200)
    assert h0005._exceeds_cost_band(lower_abs, cost_band_probability) is False


def test_exceeds_cost_band_strictly_greater_passes() -> None:
    """A genuine, non-tie excess must still pass -- the exact-arithmetic
    fix must not make the screen impossible to clear."""
    assert h0005._exceeds_cost_band(0.10, Fraction(1, 200)) is True


def test_exceeds_cost_band_strictly_less_fails() -> None:
    assert h0005._exceeds_cost_band(0.001, Fraction(1, 200)) is False


def test_exceeds_cost_band_none_inputs_are_false_not_a_crash() -> None:
    assert h0005._exceeds_cost_band(None, Fraction(1, 200)) is False
    assert h0005._exceeds_cost_band(0.5, None) is False


def test_to_comparison_decimal_strips_float_representation_noise() -> None:
    """Direct test of the Decimal-quantization helper: two values that
    differ only in IEEE-754 representation error (~1e-16) at the 9th
    decimal place and beyond must quantize to the identical Decimal."""
    exact = h0005._to_comparison_decimal(Fraction(1, 200))
    noisy_float = h0005._to_comparison_decimal(0.005000000000000004)
    assert exact == noisy_float == Decimal("0.005000000")


def test_to_comparison_decimal_does_not_erase_a_real_difference() -> None:
    """The controlled precision (1e-9) must not be so coarse that it
    erases a genuine, data-driven difference far above float noise."""
    a = h0005._to_comparison_decimal(Fraction(1, 200))  # 0.005
    b = h0005._to_comparison_decimal(Fraction(1, 100))  # 0.01
    assert a != b
    assert b > a


def test_regression_rounded_half_spread_would_have_zeroed_the_one_cent_case() -> None:
    """Reconstructs the superseded implementation's exact arithmetic
    (`fee + round(half_spread)`) and shows it silently zeroes a 1-cent
    spread's cost, unlike the corrected exact-Fraction implementation."""
    fee = 0  # at mid=99.5, rounded_mid=100 -> fee=0, per the frozen limit fix
    superseded_band = fee + round((100 - 99) / 2)  # round(0.5) == 0 (banker's rounding)
    corrected_band = h0005.cost_band_cents(
        mid_price_cents=99.5, yes_bid_close_cents=99, yes_ask_close_cents=100
    )
    assert superseded_band == 0
    assert corrected_band == Fraction(1, 2)
    assert superseded_band != corrected_band


def test_regression_float_comparison_would_have_passed_the_exact_tie() -> None:
    """Reconstructs the superseded implementation's raw-float comparison
    (`lower_abs > cost_band_cents / 100`, no Decimal quantization) at the
    exact tie that decided the superseded execution's sole clearing cell,
    and shows it evaluates True purely from float representation noise --
    contrasted with the corrected, exact-arithmetic result (False)."""
    lower_abs = abs(99.5 / 100 - 1)  # what the bootstrap pipeline actually computes
    superseded_band_float = 0.5 / 100  # the (corrected) band, compared as raw float
    superseded_result = lower_abs > superseded_band_float
    assert superseded_result is True  # passes only due to ~4e-18 float noise

    corrected_result = h0005._exceeds_cost_band(lower_abs, Fraction(1, 200))
    assert corrected_result is False


# --- Evaluation-order logic (AMENDMENT Finding 4) ----------------------------


def _cell(**overrides: Any) -> h0005.CellResult:
    base: dict[str, Any] = {
        "horizon_hours": 72,
        "decile": 0,
        "n_days_discovery": 25,
        "n_days_holdout": 25,
        "mean_predicted_probability": 0.5,
        "realized_yes_frequency": 0.5,
        "calibration_error": 0.0,
        "cost_band_cents": 2.0,
        "discovery_bonferroni_pass": False,
        "holdout_insufficient_n": False,
        "holdout_pass": False,
        "clears_both_screens": False,
    }
    base.update(overrides)
    return h0005.CellResult(**base)


def test_evaluate_outcome_step1_total_day_floor_is_inconclusive() -> None:
    cells = [_cell(clears_both_screens=True)]  # would otherwise be "rejected"
    verdict = h0005.evaluate_outcome(cells, total_valid_days=h0005.MIN_SETTLEMENT_DAYS - 1)
    assert verdict.outcome == "inconclusive"
    assert "valid settlement days" in verdict.reason


def test_evaluate_outcome_step2_any_clearing_cell_is_rejected() -> None:
    cells = [
        _cell(horizon_hours=72, decile=0, clears_both_screens=True),
        _cell(
            horizon_hours=48,
            decile=1,
            discovery_bonferroni_pass=True,
            holdout_insufficient_n=True,
        ),  # would independently trigger step 3 -- step 2 must still win
    ]
    verdict = h0005.evaluate_outcome(cells, total_valid_days=h0005.MIN_SETTLEMENT_DAYS)
    assert verdict.outcome == "rejected"
    assert verdict.clearing_cells == [(72, 0)]


def test_evaluate_outcome_step3_underpowered_holdout_is_inconclusive() -> None:
    cells = [
        _cell(
            horizon_hours=72,
            decile=0,
            discovery_bonferroni_pass=True,
            holdout_insufficient_n=True,
            clears_both_screens=False,
        )
    ]
    verdict = h0005.evaluate_outcome(cells, total_valid_days=h0005.MIN_SETTLEMENT_DAYS)
    assert verdict.outcome == "inconclusive"
    assert verdict.underpowered_cells == [(72, 0)]


def test_evaluate_outcome_step4_zero_discovery_passes_is_confirmed_not_vacuous() -> None:
    """The audit's resolved ambiguity: no cell ever clearing discovery is
    NOT treated as vacuously satisfying step 3 -- it falls through to
    step 4, Confirmed."""
    cells = [_cell(discovery_bonferroni_pass=False)]
    verdict = h0005.evaluate_outcome(cells, total_valid_days=h0005.MIN_SETTLEMENT_DAYS)
    assert verdict.outcome == "confirmed"


def test_evaluate_outcome_step4_discovery_pass_but_holdout_screen_fails_is_confirmed() -> None:
    """A cell that cleared discovery, had sufficient hold-out days, but
    failed the hold-out replication screen itself (not underpowered) must
    not be reported as underpowered, and the overall verdict is Confirmed."""
    cells = [
        _cell(
            discovery_bonferroni_pass=True,
            holdout_insufficient_n=False,
            holdout_pass=False,
            clears_both_screens=False,
        )
    ]
    verdict = h0005.evaluate_outcome(cells, total_valid_days=h0005.MIN_SETTLEMENT_DAYS)
    assert verdict.outcome == "confirmed"
    assert verdict.underpowered_cells == []


# === Manifest/reporting parity (Final Implementation Polish) ================
#
# Everything below tests the descriptive/reporting-only additions: ask/bid
# robustness, reliability curves, exploratory grid, by-month descriptive,
# coverage bookkeeping, config-drift detection, and manifest-schema
# validation. None of it touches build_cells/evaluate_outcome, and none of
# it is exercised against real H0005 data.

# --- Secondary robustness outputs (ask/bid, PREREG Sec 8) -------------------


def test_executable_price_robustness_empty_when_no_clearing_cells() -> None:
    assert h0005.compute_executable_price_robustness([_obs("A", 72)], []) == []


def test_executable_price_robustness_recomputes_ask_and_bid_implied() -> None:
    obs = [
        _obs(
            "A",
            72,
            decile=6,
            target_date=date(2026, 7, 1),
            mid_price_cents=65.0,
            yes_bid_close_cents=60,
            yes_ask_close_cents=70,
            cost_band_cents=0.01,
            outcome=0,
        ),
        _obs(
            "B",
            72,
            decile=6,
            target_date=date(2026, 7, 2),
            mid_price_cents=65.0,
            yes_bid_close_cents=60,
            yes_ask_close_cents=70,
            cost_band_cents=0.01,
            outcome=0,
        ),
    ]
    out = h0005.compute_executable_price_robustness(obs, [(72, 6)])
    assert len(out) == 1
    row = out[0]
    assert row["horizon_hours"] == 72
    assert row["decile"] == 6
    assert row["n"] == 2
    assert row["cost_band_cents"] == pytest.approx(0.01)
    assert row["mid_calibration_error"] == pytest.approx(0.65)  # mean(0.65) - 0
    assert row["ask_implied_mean_probability"] == pytest.approx(0.70)
    assert row["ask_calibration_error"] == pytest.approx(0.70)
    assert row["bid_implied_mean_probability"] == pytest.approx(0.60)
    assert row["bid_calibration_error"] == pytest.approx(0.60)
    # Both ask (0.70) and bid (0.60) calibration errors exceed this
    # (deliberately tiny) cost band -- the finding is executable. This
    # mirrors, not judges, build_cells' own existing calibration_error-
    # vs-cost_band_cents comparison convention (see the polish pass's
    # final report for a flagged scale-consistency question about that
    # existing comparison).
    assert row["ask_exceeds_cost_band"] is True
    assert row["bid_exceeds_cost_band"] is True
    assert row["executable"] is True
    assert row["not_executable_reason"] is None


def test_executable_price_robustness_reports_not_executable_when_it_vanishes() -> None:
    """A large cost band swallows both the ask- and bid-implied deviation
    -- PREREG Sec 8's 'vanishes at both executable prices' case."""
    obs = [
        _obs(
            "A",
            72,
            decile=6,
            target_date=date(2026, 7, 1),
            mid_price_cents=65.0,
            yes_bid_close_cents=60,
            yes_ask_close_cents=70,
            cost_band_cents=99,
            outcome=0,
        )
    ]
    out = h0005.compute_executable_price_robustness(obs, [(72, 6)])
    row = out[0]
    assert row["ask_exceeds_cost_band"] is False
    assert row["bid_exceeds_cost_band"] is False
    assert row["executable"] is False
    assert row["not_executable_reason"] is not None


def test_executable_price_robustness_never_touches_evaluate_outcome() -> None:
    """Robustness computation must not require, mutate, or depend on any
    CellResult/Verdict object -- it is purely descriptive, computed from
    raw observations and a list of (horizon, decile) pairs, and calling it
    (as run() does) happens strictly after the verdict is already final."""
    import inspect

    params = inspect.signature(h0005.compute_executable_price_robustness).parameters
    assert list(params) == ["observations", "clearing_cells"]
    assert "HorizonObservation" in str(params["observations"].annotation)
    assert "tuple" in str(params["clearing_cells"].annotation)


# --- Reliability outputs (PREREG Sec 9) --------------------------------------


def test_build_reliability_curves_has_one_entry_per_horizon_and_decile() -> None:
    curves = h0005.build_reliability_curves([])
    assert set(curves.keys()) == set(h0005.HORIZON_HOURS)
    for horizon in h0005.HORIZON_HOURS:
        assert len(curves[horizon]) == h0005.N_DECILES
        deciles = [p["decile"] for p in curves[horizon]]
        assert deciles == list(range(h0005.N_DECILES))


def test_build_reliability_curves_empty_decile_reports_none_not_zero() -> None:
    curves = h0005.build_reliability_curves([])
    point = curves[72][0]
    assert point["n"] == 0
    assert point["mean_predicted_probability"] is None
    assert point["realized_yes_frequency"] is None


def test_build_reliability_curves_matches_hand_calculated_point() -> None:
    obs = [_obs("A", 72, decile=3, mid_price_cents=35.0, outcome=1)]
    curves = h0005.build_reliability_curves(obs)
    point = curves[72][3]
    assert point["n"] == 1
    assert point["mean_predicted_probability"] == pytest.approx(0.35)
    assert point["realized_yes_frequency"] == pytest.approx(1.0)


# --- Coverage bookkeeping -----------------------------------------------------


def test_compute_excluded_settlement_days_excludes_only_all_missing_days() -> None:
    fully_missing = _market_rows(
        "A",
        close_time=datetime(2026, 7, 1, 0, 0),
        target_date=date(2026, 7, 1),
        settlement_label_status="missing_source_data",
        value_at_settlement=None,
        candles=[(datetime(2026, 6, 30, 23, 0), 40, 42)],
    )
    partially_resolved_day = _market_rows(
        "B",
        close_time=datetime(2026, 7, 2, 0, 0),
        target_date=date(2026, 7, 2),
        settlement_label_status="missing_source_data",
        value_at_settlement=None,
        candles=[(datetime(2026, 7, 1, 23, 0), 40, 42)],
    ) + _market_rows(
        "C",
        close_time=datetime(2026, 7, 2, 0, 0),
        target_date=date(2026, 7, 2),
        settlement_label_status="resolved",
        value_at_settlement=60.0,
        candles=[(datetime(2026, 7, 1, 23, 0), 40, 42)],
    )
    frame = _frame(fully_missing + partially_resolved_day)
    excluded = h0005.compute_excluded_settlement_days(frame)
    assert excluded == ["2026-07-01"]


def test_compute_observations_excluded_stale_counts_markets_with_no_surviving_candle() -> None:
    close = datetime(2026, 7, 20, 0, 0)
    # "A": one candle exactly at close_time -- after every horizon's
    # target_instant (close - h), so never selectable (asof only picks
    # period_end <= target_instant); excluded at every horizon.
    future_relative_candle = _market_rows(
        "A", close_time=close, target_date=date(2026, 7, 20), candles=[(close, 40, 42)]
    )
    # "B": one candle far enough in the past to exceed the 15-minute
    # staleness tolerance for every frozen horizon; present in
    # market_static (so it counts toward total_markets) but excluded from
    # every horizon's surviving set.
    stale_candle = _market_rows(
        "B",
        close_time=close,
        target_date=date(2026, 7, 20),
        candles=[(close - timedelta(hours=200), 40, 42)],
    )
    frame = _frame(future_relative_candle + stale_candle)
    excluded = h0005.compute_observations_excluded_stale(frame)
    assert set(excluded.keys()) == set(h0005.HORIZON_HOURS)
    for horizon in h0005.HORIZON_HOURS:
        assert excluded[horizon] == 2


def test_cells_below_min_n_reports_only_holdout_insufficient_cells() -> None:
    cells = [
        _cell(horizon_hours=72, decile=0, holdout_insufficient_n=True),
        _cell(horizon_hours=48, decile=1, holdout_insufficient_n=False),
    ]
    assert h0005.cells_below_min_n(cells) == [(72, 0)]


# --- Config-drift / reproducibility parity -----------------------------------


def test_verify_config_matches_prereg_true_for_frozen_values() -> None:
    assert h0005.verify_config_matches_prereg(dict(h0005._FROZEN_CONFIG_REFERENCE)) is True


def test_verify_config_matches_prereg_false_when_a_value_drifts() -> None:
    drifted = dict(h0005._FROZEN_CONFIG_REFERENCE)
    drifted["random_seed"] = 1
    assert h0005.verify_config_matches_prereg(drifted) is False


def test_verify_config_matches_prereg_false_when_a_key_is_missing() -> None:
    incomplete = dict(h0005._FROZEN_CONFIG_REFERENCE)
    del incomplete["min_days_per_cell"]
    assert h0005.verify_config_matches_prereg(incomplete) is False


# --- Exploratory-field population --------------------------------------------


def test_build_exploratory_grid_has_60_cells_with_no_ci_fields() -> None:
    grid = h0005.build_exploratory_grid([])
    assert len(grid) == h0005.PRIMARY_FAMILY_SIZE
    for row in grid:
        assert "discovery_ci" not in row
        assert "holdout_ci" not in row
        assert row["mean_predicted_probability"] is None


def test_build_exploratory_grid_matches_hand_calculated_point() -> None:
    obs = [_obs("A", 6, decile=2, mid_price_cents=25.0, outcome=0, cost_band_cents=3)]
    grid = h0005.build_exploratory_grid(obs)
    row = next(r for r in grid if r["horizon_hours"] == 6 and r["decile"] == 2)
    assert row["n"] == 1
    assert row["mean_predicted_probability"] == pytest.approx(0.25)
    assert row["realized_yes_frequency"] == pytest.approx(0.0)
    assert row["calibration_error"] == pytest.approx(0.25)
    assert row["cost_band_cents"] == pytest.approx(3.0)


def test_build_by_month_descriptive_groups_by_calendar_month() -> None:
    obs = [
        _obs("A", 72, target_date=date(2026, 7, 15), mid_price_cents=50.0, outcome=1),
        _obs("B", 72, target_date=date(2026, 7, 20), mid_price_cents=50.0, outcome=0),
        _obs("C", 72, target_date=date(2026, 8, 1), mid_price_cents=50.0, outcome=1),
    ]
    rows = h0005.build_by_month_descriptive(obs)
    assert [(r["year"], r["month"]) for r in rows] == [(2026, 7), (2026, 8)]
    july = rows[0]
    assert july["n"] == 2
    assert july["realized_yes_frequency"] == pytest.approx(0.5)


def test_build_by_month_descriptive_never_used_to_filter_evaluate_outcome() -> None:
    import inspect

    src = inspect.getsource(h0005.evaluate_outcome)
    assert "by_month" not in src.lower()


# --- Manifest validation (Template Parity) -----------------------------------


def test_validate_manifest_accepts_a_well_formed_minimal_manifest() -> None:
    results = _minimal_valid_results()
    h0005.validate_manifest(results)  # must not raise


def test_validate_manifest_raises_on_missing_top_level_key() -> None:
    results = _minimal_valid_results()
    del results["coverage"]
    with pytest.raises(h0005.ManifestValidationError, match="coverage"):
        h0005.validate_manifest(results)


def test_validate_manifest_raises_on_missing_nested_config_key() -> None:
    results = _minimal_valid_results()
    del results["config"]["random_seed"]
    with pytest.raises(h0005.ManifestValidationError, match="config"):
        h0005.validate_manifest(results)


def test_validate_manifest_raises_on_missing_cell_key() -> None:
    results = _minimal_valid_results()
    del results["primary_results"]["cells"][0]["discovery_ci95"]
    with pytest.raises(h0005.ManifestValidationError):
        h0005.validate_manifest(results)


def test_validate_manifest_raises_on_wrong_cell_count() -> None:
    results = _minimal_valid_results()
    results["primary_results"]["cells"].pop()
    with pytest.raises(h0005.ManifestValidationError, match="60"):
        h0005.validate_manifest(results)


def test_validate_manifest_raises_on_unrecognized_outcome() -> None:
    results = _minimal_valid_results()
    results["primary_results"]["decision_rule"]["outcome"] = "maybe"
    with pytest.raises(h0005.ManifestValidationError, match="outcome"):
        h0005.validate_manifest(results)


def _minimal_valid_results() -> dict[str, Any]:
    """A hand-built, schema-complete (but data-empty) results dict --
    exercises validate_manifest's own logic in isolation from run()."""
    cell_template = {
        "horizon_hours": 72,
        "decile": 0,
        "n_days_discovery": 0,
        "n_days_holdout": 0,
        "mean_predicted_probability": None,
        "realized_yes_frequency": None,
        "calibration_error": None,
        "cost_band_cents": None,
        "discovery_ci95": [None, None],
        "discovery_bonferroni_pass": False,
        "holdout_ci95": [None, None],
        "holdout_pass": False,
        "insufficient_n": False,
    }
    cells = [
        {**cell_template, "horizon_hours": h, "decile": d}
        for h in h0005.HORIZON_HOURS
        for d in range(h0005.N_DECILES)
    ]
    return {
        "experiment": "EXP-TEST",
        "hypothesis": "H0005",
        "preregistration": h0005.PREREGISTRATION_METADATA,
        "dataset": {
            "version": "test",
            "content_hashes": {},
            "market_price_weather_schema_version": "1",
            "settlement_label_reconstruction_version": "1",
            "source_db_revision": "0007",
            "git_commit": "test",
        },
        "config": {key: None for key in h0005._REQUIRED_MANIFEST_PATHS["config"]},
        "coverage": {key: None for key in h0005._REQUIRED_MANIFEST_PATHS["coverage"]},
        "primary_results": {
            "cells": cells,
            "cells_rejecting_null": [],
            "decision_rule": {
                "confirmed_if": "",
                "rejected_if": "",
                "inconclusive_if": "",
                "evaluation_order": "",
                "outcome": "confirmed",
                "reason": "",
            },
        },
        "secondary_results": {
            key: None for key in h0005._REQUIRED_MANIFEST_PATHS["secondary_results"]
        },
        "exploratory_results": {"kxlowtnyc_cell": [], "by_month_descriptive": []},
        "reproducibility_verification": {
            "rerun_byte_identical": None,
            "config_matches_prereg": None,
        },
        "date_run": None,
    }


# --- Complete manifest generation (run() end to end, synthetic fixture) -----


def _write_synthetic_dataset(tmp_path: Path) -> Path:
    """A tiny, fully synthetic dataset_dir -- a handful of KXHIGHNY and
    KXLOWTNYC markets, not real Kalshi data -- so run() can be exercised
    end-to-end without touching H0005's real pinned dataset or any real
    calibration outcome."""
    close_base = datetime(2026, 7, 1, 0, 0)
    rows: list[dict[str, Any]] = []
    for i in range(3):
        close = close_base + timedelta(days=i)
        candles = [(close - timedelta(hours=h), 40, 42) for h in h0005.HORIZON_HOURS]
        rows += _market_rows(
            f"KXHIGHNY-TEST{i}",
            close_time=close,
            target_date=close.date(),
            settlement_label_status="resolved",
            value_at_settlement=60.0,
            candles=candles,
        )
        rows += _market_rows(
            f"KXLOWTNYC-TEST{i}",
            close_time=close,
            target_date=close.date(),
            settlement_label_status="resolved",
            value_at_settlement=30.0,
            floor_strike=20.0,
            candles=candles,
        )
    dataset_dir = tmp_path / "exp-test-h0005"
    dataset_dir.mkdir()
    _frame(rows).write_parquet(dataset_dir / "market_price_weather.parquet")
    manifest = {
        "version": "exp-test-h0005",
        "content_hashes": {"market_price_weather": "deadbeef", "settlement_labels": "deadbeef"},
        "config": {
            "market_price_weather_schema_version": "1",
            "settlement_label_reconstruction_version": "1",
        },
        "source_db_revision": "0007",
        "git_commit": "0000000000000000000000000000000000000000",
    }
    (dataset_dir / "manifest.json").write_text(json.dumps(manifest))
    return dataset_dir


#: Captured at import time, before any test monkeypatches
#: h0005.day_clustered_bootstrap_ci -- avoids _fast_bootstrap recursing
#: into itself once patched in.
_ORIGINAL_BOOTSTRAP: Callable[..., tuple[float | None, float | None]] = (
    h0005.day_clustered_bootstrap_ci
)


def _fast_bootstrap(
    day_values: dict[Any, list[float]],
    day_pool: list[Any],
    *,
    rng: random.Random,
    lower_pct: float,
    upper_pct: float,
    resamples: int = h0005.BOOTSTRAP_RESAMPLES,
) -> tuple[float | None, float | None]:
    """Test-speed-only stand-in for day_clustered_bootstrap_ci: calls the
    real, unmodified function with a small resample count. Never used by
    the script itself -- only monkeypatched in for these two run()-level
    tests, which care about manifest shape, not bootstrap precision."""
    return _ORIGINAL_BOOTSTRAP(
        day_values, day_pool, rng=rng, lower_pct=lower_pct, upper_pct=upper_pct, resamples=50
    )


def test_run_end_to_end_on_synthetic_fixture_produces_a_complete_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercises run()'s full manifest-assembly path (Template Parity)
    against a fabricated, non-Kalshi fixture -- run() calls
    validate_manifest internally, so simply not raising already proves
    schema completeness; the assertions below spot-check the specific
    gaps this polish pass closed. Bootstrap resamples are monkeypatched
    down purely for test speed -- the algorithm itself is untouched."""
    monkeypatch.setattr(h0005, "day_clustered_bootstrap_ci", _fast_bootstrap)

    dataset_dir = _write_synthetic_dataset(tmp_path)
    results = h0005.run(dataset_dir)  # run() itself calls validate_manifest and would raise

    assert results["primary_results"]["decision_rule"]["outcome"] in (
        "confirmed",
        "rejected",
        "inconclusive",
    )
    assert results["config"]["close_time_immutability_verified"] is None
    assert "AMENDMENT Finding 6" in results["config"]["close_time_immutability_evidence"]
    assert results["reproducibility_verification"]["rerun_byte_identical"] is None
    assert isinstance(results["reproducibility_verification"]["config_matches_prereg"], bool)
    assert len(results["exploratory_results"]["kxlowtnyc_cell"]) == h0005.PRIMARY_FAMILY_SIZE
    assert isinstance(results["exploratory_results"]["by_month_descriptive"], list)
    assert isinstance(results["coverage"]["excluded_days"], list)
    assert isinstance(results["coverage"]["observations_excluded_stale"], dict)
    assert isinstance(results["coverage"]["cells_below_min_n"], list)
    assert isinstance(results["secondary_results"]["executable_price_robustness"], list)
    assert isinstance(results["secondary_results"]["reliability_curves_by_horizon"], dict)


def test_run_end_to_end_is_deterministic_across_two_invocations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PREREG Sec 14: re-running against the same pinned dataset must
    reproduce byte-identically. Approximated here (small resamples, for
    test speed) by comparing two full runs against the same synthetic
    fixture."""
    monkeypatch.setattr(h0005, "day_clustered_bootstrap_ci", _fast_bootstrap)

    dataset_dir = _write_synthetic_dataset(tmp_path)
    first = h0005.run(dataset_dir)
    second = h0005.run(dataset_dir)
    assert first == second
