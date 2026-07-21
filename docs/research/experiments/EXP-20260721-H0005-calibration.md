# EXP-20260721-H0005-calibration

Executed exactly as pre-registered in
`PREREG-20260721-H0005-calibration.md` and
`AMENDMENT-20260721-H0005-pre-execution.md`; no horizon, decile boundary,
threshold, bootstrap parameter, replication rule, or decision-rule step
was altered after seeing data. This is the **corrected** execution,
re-run against the identical pinned dataset after an execution-integrity
review found two implementation defects in the first attempt — see
`IMPLEMENTATION-NOTE-20260721-H0005-exact-cost-band.md` and
`EXP-20260721-H0005-calibration-SUPERSEDED.md` (same directory) for the
full account. Machine-readable results:
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
| Dataset version | `exp-20260721-h0005` (`data/datasets/exp-20260721-h0005/`) — **unchanged from the superseded run**; no rebuild was needed, only the analysis script was corrected |
| Frame used | `market_price_weather.parquet` |
| Content hash (market_price_weather) | `d88e6ccb5ace784f22d251c6c43ab2d733cc23ec592c51abf0c40816a1814da1` |
| Content hash (settlement_labels) | `b0d907c051ed2d8b3710717ba4bfbbddc2b66de36cc93239e865b26e95c5b050` |
| Source DB revision | `0007` |
| Dataset-build git commit | `a9df34eea0284965b6fb333ea2fab6fd9bae5e75` (unchanged) |
| Analysis git commit (this corrected execution) | see the commit accompanying this report |
| Fee config | `KALSHI_WEATHER_TAKER_FEE_CONFIG` (multiplier 0.07, verified 2026-07-21) |
| Random seed | 20260721 (bootstrap only; everything else deterministic) |
| Bootstrap resamples | 10,000 per cell per slice, day-clustered, percentile method |
| Bonferroni alpha | 0.05 / 60 = 0.0008333... (discovery screen only) |
| Date run | 2026-07-21 |
| Re-run determinism | **Byte-identical** — run twice against the same pinned dataset; MD5-identical `-results.json` outputs |
| `close_time` immutability | **Verified true** — all 828 `KXHIGHNY`/`KXLOWTNYC` markets have exactly one distinct `close_time` across their full snapshot history |
| `config_matches_prereg` | **True** |

## Methodology

As-of candle selection, decile bucketing, the day-clustered bootstrap, the
discovery/hold-out replication split, and the four-step evaluation order
are implemented exactly as specified in the frozen documents. Two
implementation defects were found and corrected before this execution
(full detail in `IMPLEMENTATION-NOTE-20260721-H0005-exact-cost-band.md`):

1. **Fee-domain boundary** (found during the first execution attempt,
   already correct going into this run): real `yes_ask_close_cents`
   reaches 100 (13,682/761,475 executable candle rows), contradicting
   PREREG Sec 7's stated 99¢-ceiling assumption. The fee is evaluated at
   its own well-defined mathematical limit (0 at P=1), without modifying
   the frozen, verified `contract_fee_cents`.
2. **Exact half-spread and comparison arithmetic** (found by an
   execution-integrity review of the first attempt's results, corrected
   for this run): the first attempt rounded `half_spread_cents` with
   Python's banker's rounding — never specified by the frozen protocol —
   which silently zeroed 1-cent-spread cost bands, and decided the
   discovery/hold-out screens with raw binary-float comparison, letting
   an exact mathematical tie resolve `True` by floating-point
   representation noise rather than by the data. Both are now computed in
   exact (`Fraction`/`Decimal`) arithmetic. Neither the fee formula, nor
   any threshold, horizon, or decile, changed.

## Sample and coverage

Identical to the superseded run (same pinned dataset): **402** distinct
`KXHIGHNY` markets; **66** valid settlement days (2026-05-15 → 2026-07-20;
1 day excluded, 2026-07-02, for missing settlement source data) — clears
the pre-registered minimum of 60. Chronological split (1-day embargo,
2026-06-17): discovery 33 days, hold-out 32 days.

**Horizon coverage is materially incomplete at the two longest
horizons** — zero `KXHIGHNY` markets have any candle within the 15-minute
staleness tolerance of `close − 72h` or `close − 48h` in this pinned
archive (contradicting PREREG Sec 6's stated backfill-coverage
justification for 72h). All 20 of those two horizons' cells are empty.
The effective primary family for this run is 40 cells (4 horizons × 10
deciles), not the nominal 60.

Of the 60 nominal cells, **53** fall below the 20-day hold-out minimum-N
gate. Only **7** cells are eligible to support "rejected": (2h, decile 0),
(2h, decile 9), (6h, decile 0), (6h, decile 9), (12h, decile 0), (24h,
decile 0), (24h, decile 3) — none of them clear the discovery screen.

## Results (pre-registered primary decision)

**Decision: INCONCLUSIVE** (reason: cell(s) cleared the discovery screen
but were underpowered — fewer than 20 hold-out days — per AMENDMENT
Finding 4 step 3).

No cell clears both screens (`cells_rejecting_null` is empty). Six sparse
cells cleared the Bonferroni-adjusted discovery screen alone, each on 1-8
discovery days and 1-7 hold-out days — far below the 20-day minimum-N
gate needed to support a hold-out-replicated finding:

| Horizon | Decile | Discovery days | Hold-out days | Discovery calibration error | Discovery 95%-equiv. CI |
|---|---|---|---|---|---|
| 24h | 6 | 2 | 3 | −0.328 | [−0.350, −0.305] |
| 24h | 8 | 5 | 1 | −0.138 | [−0.175, −0.105] |
| 12h | 3 | 8 | 7 | +0.356 | [0.323, 0.390] |
| 12h | 8 | 2 | 4 | −0.180 | [−0.185, −0.175] |
| 6h | 1 | 1 | 1 | +0.105 | [0.105, 0.105] |
| 6h | 8 | 1 | 2 | −0.115 | [−0.115, −0.115] |

These deviations are large in magnitude (10-35 percentage points), but on
1-8 days each — exactly the sparse-cell scenario PREREG Sec 10's minimum-N
gate exists to catch ("an underpowered cell is reported as insufficient
rather than misinterpreted"). None of the 7 well-powered cells (≥20
hold-out days: both decile-0 and decile-9 cells at the shorter horizons,
plus 24h/decile-3) clear the discovery screen at all.

**The corrected (2h, decile 9) cell** — the superseded run's sole
"clearing" cell — now correctly does not clear either screen:

| Slice | N (days) | Calibration error | Cost band (¢, exact) | CI |
|---|---|---|---|---|
| Discovery | 29 | −0.0050000000000000044 | 0.5 (exact 1/2) | [−0.005, −0.005] — an **exact mathematical tie** with the cost band (1/200 == 1/200); strict `>` correctly evaluates False |
| Hold-out | 26 | −0.0071153846153846345 | 0.6346153846153846 (exact 33/52) | [−0.01188, −0.005] |

## Secondary metrics (mid price, primary series, by horizon)

Unaffected by either correction (neither touches Brier/log loss/ECE/
calibration slope, which never involve `cost_band_cents`) — identical to
the superseded run:

| Horizon | Brier | Baseline Brier | Log loss | Baseline log loss | ECE | Calib. slope | Calib. intercept |
|---|---|---|---|---|---|---|---|
| 72h | — (0 labeled obs) | — | — | — | — | — | — |
| 48h | — (0 labeled obs) | — | — | — | — | — | — |
| 24h | 0.0987 | 0.1444 | 0.3085 | 0.4638 | 0.0243 | 1.132 | 0.033 |
| 12h | 0.0714 | 0.1423 | 0.2168 | 0.4588 | 0.0347 | 1.052 | −0.032 |
| 6h | 0.0157 | 0.1700 | 0.0524 | 0.5234 | 0.0110 | 1.052 | −0.156 |
| 2h | 0.0000465 | 0.1596 | 0.00538 | 0.4994 | 0.0054 | 5.786 | 0.902 |

## Exploratory (not used for the primary decision)

Unchanged from the superseded run (neither correction touches
exploratory-series or by-month reporting): `KXLOWTNYC`, 1,052
observations, largest cells (decile 0, every horizon, n=145-171) show
small calibration errors similar in direction/magnitude to the primary
series' own decile-0 cells; by-month descriptive stable across May/June/
July (calibration error +0.004 to +0.005).

## Conclusion

**Inconclusive**, per the pre-registered decision rule (AMENDMENT Finding
4 step 3): every cell that cleared the Bonferroni-adjusted discovery
screen did so on too few hold-out days (1-7, versus the pre-registered
20-day minimum) to independently support a replicated finding, and no
adequately-powered cell (7 cells have ≥20 hold-out days) cleared the
discovery screen at all. This is a clean application of the pre-registered
power gate to a dataset whose within-cell sample sizes are, at most
horizon/decile combinations, small — not a near-miss or an ambiguous
result. Unlike the superseded first-attempt execution, no cell in this
corrected run clears both screens; there is no "rejected" cell to report,
and no executable-price robustness disclosure applies (PREREG Sec 8's
check only runs for cells that clear both screens).

## Limitations

- **72h and 48h horizons have zero coverage** in this pinned dataset (see
  "Sample and coverage"). Extending the candle archive further back
  before market close would be required to populate these two horizons in
  a future re-run.
- **Most cells are underpowered.** 53 of 60 nominal cells (including all
  20 at 72h/48h) fall below the 20-day hold-out minimum; only 7 cells
  (concentrated in decile 0 and, at shorter horizons, decile 9) have
  enough day-coverage to be eligible for "rejected." The six cells that
  did clear the discovery screen alone are exactly the sparsest ones (1-8
  discovery days), where a handful of unusual days can produce a large
  point estimate that a properly-powered hold-out slice cannot confirm or
  refute.
- **NYC only**, one weather regime (2026-05-15 → 2026-07-20) — the same
  limitations as the superseded run's report.
- **Re-run trigger**: more accumulated settlement days, concentrated in
  the sparse mid-probability deciles and the two currently-empty long
  horizons, would be needed before this design could produce a
  well-powered "confirmed" or "rejected" verdict rather than
  "inconclusive." This is a data-accumulation gate, not a design flaw —
  consistent with PREREG Sec 10's own acknowledged "known power risk
  carried into execution."
