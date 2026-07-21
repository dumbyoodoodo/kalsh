# EXP-20260721-H0005-calibration (SUPERSEDED)

> **SUPERSEDED 2026-07-21.** An execution-integrity review found that the
> implementation used to produce this report had two undisclosed defects:
> the cost band's half-spread term was rounded with Python's banker's
> rounding (never specified by the frozen protocol, and contrary to this
> platform's own rounds-up convention), and the discovery/hold-out screen
> comparison was decided in raw binary-float arithmetic, letting an exact
> mathematical tie resolve to `True` by floating-point representation
> noise. Both defects happened to be decision-relevant at this report's
> sole clearing cell (horizon=2h, decile=90-100%): under the corrected,
> exact-arithmetic implementation, that cell's discovery-slice comparison
> is an exact tie (correctly `False`) and its hold-out-slice comparison
> fails outright. **This report's REJECTED verdict does not reflect the
> frozen protocol correctly implemented and must not be cited as H0005's
> outcome.** See `IMPLEMENTATION-NOTE-20260721-H0005-exact-cost-band.md`
> for the full correction, and `EXP-20260721-H0005-calibration.md` (same
> directory, no `-SUPERSEDED` suffix) for the corrected re-execution
> against the identical pinned dataset. Retained here, not deleted, as an
> accurate record of what was actually run and why it was superseded.

Executed exactly as pre-registered in
`PREREG-20260721-H0005-calibration.md` and
`AMENDMENT-20260721-H0005-pre-execution.md`; no horizon, decile boundary,
threshold, bootstrap parameter, replication rule, or decision-rule step
was altered after seeing data. Machine-readable results:
`EXP-20260721-H0005-calibration-results.json` (same directory), produced
by `scripts/exp_h0005_calibration.py`.

## Hypothesis

**H0005 — Null: Kalshi daily-temperature prices are calibrated within
costs.** Kalshi NYC daily-high (`KXHIGHNY`) market prices, observed at six
pre-registered fixed times-to-close and grouped into ten pre-registered
probability deciles, are calibrated within transaction costs: the claim
survives unless at least one (horizon × decile) cell shows a deviation
that both clears a Bonferroni-adjusted discovery-slice screen and
replicates (same sign, still exceeding cost) in a temporally later,
non-overlapping hold-out slice.

## Reproducibility

| Field | Value |
|---|---|
| Dataset version | `exp-20260721-h0005` (`data/datasets/exp-20260721-h0005/`) |
| Frame used | `market_price_weather.parquet` |
| Content hash (market_price_weather) | `d88e6ccb5ace784f22d251c6c43ab2d733cc23ec592c51abf0c40816a1814da1` |
| Content hash (settlement_labels) | `b0d907c051ed2d8b3710717ba4bfbbddc2b66de36cc93239e865b26e95c5b050` |
| Source DB revision | `0007` |
| Dataset-build / analysis git commit | `a9df34eea0284965b6fb333ea2fab6fd9bae5e75` |
| Fee config | `KALSHI_WEATHER_TAKER_FEE_CONFIG` (multiplier 0.07, verified 2026-07-21) |
| Random seed | 20260721 (bootstrap only; everything else deterministic) |
| Bootstrap resamples | 10,000 per cell per slice, day-clustered, percentile method |
| Bonferroni alpha | 0.05 / 60 = 0.0008333... (discovery screen only) |
| Date run | 2026-07-21 |
| Re-run determinism | **Byte-identical** — run twice against the same pinned dataset; the two `-results.json` outputs are identical (verified `diff` and MD5, both files match exactly) |
| `close_time` immutability | **Verified true** — queried `market_snapshots` directly: all 828 `KXHIGHNY`/`KXLOWTNYC` markets in the archive have exactly one distinct `close_time` value across their full snapshot history (0/828 with a revision) — AMENDMENT Finding 6's blocking pre-execution gate |
| `config_matches_prereg` | **True** (independently-transcribed frozen values matched, see results JSON `reproducibility_verification`) |

## Methodology

As-of candle selection, decile bucketing, cost-band computation, the
day-clustered bootstrap, the discovery/hold-out replication split, and the
four-step evaluation order are implemented exactly as specified in the
frozen documents (see `scripts/exp_h0005_calibration.py`'s per-function
docstrings for the exact section/finding each block implements).

**One implementation defect was found and fixed before this run, and one
more during this execution** (both are implementation corrections, not
protocol changes — see the git history for the exact diffs):

1. **Cost-band unit mismatch (found before this run, in a prior review
   pass).** `calibration_error` is fractional `[0,1]`; `cost_band_cents` is
   raw cents (always ≥ 1). The original comparison (`lower_abs >
   cost_band_cents`) could never be satisfied by construction, making
   "Rejected" mathematically unreachable. Fixed by converting
   `cost_band_cents` to the same `[0,1]` scale at the point of comparison
   (`/ 100`, the identical conversion PREREG Sec 8 already uses for
   ask/bid-implied prices) — `calibration_error`'s own units, the fee
   formula, and every threshold/horizon/decile are unchanged.
2. **Fee-domain boundary (found during this execution).** Real
   `yes_ask_close_cents` reaches 100 (13,682 of 761,475 executable candle
   rows, ~1.8%) — contradicting PREREG Sec 7's stated assumption that
   Kalshi's price ceiling is 99¢. `round_half_up(mid_price_cents)` can
   therefore equal 100, outside `contract_fee_cents`'s documented 1-99
   domain (it raises rather than compute there, by design, for H0007's own
   context). Rather than excluding these observations — which would bias
   away from exactly the extreme decile H0005's anchoring/favorite-longshot
   economic motivation is most interested in — the fee is evaluated at its
   own well-defined mathematical limit (`ceil(multiplier·P·(1−P)·contracts)
   = 0` at `P = 1`), without modifying the frozen, verified
   `contract_fee_cents` function itself, which is still called unmodified
   for every price in its documented domain.

Both fixes are unit/domain-consistency corrections to the implementation,
not changes to the frozen research question, thresholds, or decision
logic; both are covered by dedicated regression tests
(`tests/unit/test_h0005_calibration.py`).

## Sample and coverage

- **402** distinct `KXHIGHNY` markets in the pinned archive; **66** valid
  settlement days (2026-05-15 → 2026-07-20; 1 day excluded, 2026-07-02,
  for missing settlement source data) — clears the pre-registered minimum
  of 60.
- Chronological split (1-day embargo, 2026-06-17): **discovery** slice 33
  days (2026-05-15 → 2026-06-16), **hold-out** slice 32 days (2026-06-18 →
  2026-07-20).
- **Horizon coverage is materially incomplete at the two longest
  horizons.** Zero `KXHIGHNY` markets have any candle within the
  15-minute staleness tolerance of `close − 72h` or `close − 48h` — every
  one of the 20 primary-family cells at those two horizons is empty
  (`N=0`, `calibration_error: null`, per Finding 7). This directly
  contradicts PREREG Sec 6's stated justification for including 72h
  ("the candle archive's own backfill window... comfortably covers it for
  every market") — empirically, in this pinned archive, it does not. This
  is a data-coverage fact, not an implementation defect: `observations_
  excluded_stale` in the results JSON records 402 markets excluded at both
  72h and 48h (all of them), versus 19/12/109/118 at 24h/12h/6h/2h
  respectively. The effective primary family for this run is 40 cells (4
  horizons × 10 deciles), not the nominal 60 — reported plainly here, not
  silently absorbed into the empty-cell handling.
- Of the 60 nominal cells, **53** are listed in `cells_below_min_n` (fewer
  than 20 distinct hold-out days) — all 20 of 72h/48h's cells (zero
  coverage, above) plus the sparse mid-probability deciles at the
  remaining horizons. Only **7** cells clear the 20-day hold-out gate and
  are eligible to support "rejected": (2h, decile 0), (2h, decile 9),
  (6h, decile 0), (6h, decile 9), (12h, decile 0), (24h, decile 0), and
  (24h, decile 3) — consistent with daily-high strikes concentrating most
  days' mid-price near the extremes (deciles 0 and 9) rather than the
  middle of the range.

## Results (pre-registered primary decision)

**Decision: REJECTED.** At least one primary-family cell cleared both the
Bonferroni-adjusted discovery screen and the hold-out replication screen.

**Clearing cell: horizon = 2h, decile 9 (90-100%).**

| Slice | N (days) | Mean predicted probability | Realized YES frequency | Calibration error | Cost band (cents) | 95%-equivalent CI (signed) |
|---|---|---|---|---|---|---|
| Discovery (Bonferroni, α=0.05/60) | 29 | 0.9950 | 1.0000 | −0.0050 | 0 | [−0.0050, −0.0050] |
| Hold-out (95%) | 26 | 0.9929 | 1.0000 | −0.0071 | 0.154 | [−0.0119, −0.0050] |

Both slices show the same sign (market slightly **under**-priced YES, not
over-priced) and a lower CI bound on `|calibration_error|` that exceeds
the (near-zero) cost band in both slices. The cost band here rounds to
essentially zero because, at this extreme a price, the quadratic fee term
`multiplier·P·(1−P)` is itself close to zero — meaning even a very small
absolute deviation clears the screen. This is a mechanical, correctly-
implemented consequence of the frozen cost-band formula at its own
extreme-decile boundary, not a separate statistical choice; it is reported
here explicitly so the magnitude of the "cost" the deviation exceeds is
not mistaken for a typical mid-decile cost band.

**Executable-price robustness (PREREG Sec 8, required disclosure for this
cell):**

| Price definition | Mean probability | Calibration error | Exceeds cost band? |
|---|---|---|---|
| Mid (primary) | 0.9994 (n=56, pooled) | −0.0060 | — (primary, not itself re-screened) |
| Ask-implied | 0.9995 | −0.0005 | **No** |
| Bid-implied | 0.9886 | −0.0114 | **Yes** |

The finding is **not** "not executable" (at least one of ask/bid exceeds
cost — PREREG Sec 8's disclosure test), but it is asymmetric: the
deviation is visible on the sell side (bid-implied) and essentially absent
on the buy side (ask-implied). Per Sec 8, this robustness check is
descriptive only and does not alter the primary mid-price decision.

No other cell cleared both screens. `underpowered_cells` (discovery-passing
but hold-out-insufficient-N) is empty — the clearing cell was not
borderline on power.

## Secondary metrics (mid price, primary series, by horizon)

| Horizon | Brier | Baseline Brier | Log loss | Baseline log loss | ECE | Calib. slope | Calib. intercept |
|---|---|---|---|---|---|---|---|
| 72h | — (0 labeled obs) | — | — | — | — | — | — |
| 48h | — (0 labeled obs) | — | — | — | — | — | — |
| 24h | 0.0987 | 0.1444 | 0.3085 | 0.4638 | 0.0243 | 1.132 | 0.033 |
| 12h | 0.0714 | 0.1423 | 0.2168 | 0.4588 | 0.0347 | 1.052 | −0.032 |
| 6h | 0.0157 | 0.1700 | 0.0524 | 0.5234 | 0.0110 | 1.052 | −0.156 |
| 2h | 0.0000465 | 0.1596 | 0.00538 | 0.4994 | 0.0054 | 5.786 | 0.902 |

Brier/log loss beat the always-base-rate baseline at every horizon with
data, and improve monotonically as the horizon shortens (prices converge
toward the eventually-realized outcome, as expected). The 2h calibration
slope (5.79, far from the 1.0 "well-calibrated" reference) is a reporting
artifact of the mostly-near-certain, low-variance predictor values at that
horizon (a standard small-sample/near-boundary instability of the
logistic-regression diagnostic, not a separate finding) rather than a
distinct miscalibration claim — it is reported per protocol, not
interpreted further, per this report's scope limits.

## Exploratory (not used for the primary decision)

- **KXLOWTNYC** (exploratory series, no CI/bootstrap by design — AMENDMENT
  Finding 3): 1,052 observations. Point estimates are reported for every
  (horizon, decile) cell with data in the results JSON
  (`exploratory_results.kxlowtnyc_cell`); most cells have very few days
  (n ≤ 5) and are not statistically informative on their own. The largest,
  best-populated cells (decile 0 at every horizon, n=145-171) show small
  calibration errors (−0.004 to +0.004) similar in direction and magnitude
  to the primary series' own decile-0 cells.
- **By-month descriptive** (exploratory only, never used to select a
  favorable period): May (n=341, calibration error +0.004), June (n=614,
  +0.005), July partial (n=377, +0.004) — stable across the sample window,
  no month stands out.

## Conclusion

**Rejected**, per the pre-registered decision rule: the horizon=2h,
decile=90-100% cell clears both the Bonferroni-adjusted discovery screen
and the hold-out replication screen, with the same sign in both slices.
The effect is small in absolute terms (calibration error ≈ 0.5-0.7
percentage points) and clears a cost band that is itself near zero at this
extreme a price — this is a real, reproducible, protocol-compliant
rejection of strict cost-band calibration at the very top decile,
immediately before close, not a large or economically dramatic
mispricing. The executable-price robustness check shows the effect on the
sell side (bid-implied) but not the buy side (ask-implied), consistent
with (but not proof of) the deviation being at or near the platform's
minimum-tick/spread floor rather than a directional forecasting edge.
Per AMENDMENT Finding 5's framing (Sec 1, Economic Motivation), this
result establishes only that a cost-band-exceeding calibration deviation
exists at this one cell — it does not itself identify a mechanism
(anchoring, inattention, tail-probability misestimation, or liquidity
segmentation all remain candidate explanations per H0006/H0008/H0009/H0010)
and makes no claim about realized trading profit, fill probability, or
latency-adjusted capturability.

## Limitations

- **72h and 48h horizons have zero coverage in this pinned dataset** — see
  "Sample and coverage" above. The effective primary family for this run
  is 40 cells (4 horizons × 10 deciles), not the nominal 60; this does not
  change the Bonferroni denominator (still 60, per the frozen family
  definition) or any threshold, but should be understood when comparing
  this run's power to the pre-registered design's stated intent.
  Extending the candle archive further back before market close (or
  starting collection earlier relative to `close_time`) would be required
  to populate these two horizons in a future re-run; that is a data
  platform question, out of scope for this experiment.
- **Sparse mid-deciles.** 50 of 60 nominal cells are below the 20-day
  minimum-N gate (mostly deciles 1-8, where relatively few daily-high
  strikes land); only decile 0 (every horizon) and decile 9 (shorter
  horizons) had enough days to be eligible to support "rejected." This
  reflects how few `KXHIGHNY` strikes land near 50/50 on a given day, not
  a data-quality issue.
- **NYC only** (both series settle against the same station), consistent
  with every prior hypothesis on this platform to date.
- **One weather regime / one summer window** (2026-05-15 → 2026-07-20) —
  a dated finding, not necessarily stable across seasons; a scheduled
  re-run with more accumulated history (particularly to populate the two
  empty long horizons) is the natural next step, not a re-interpretation
  of this result.
