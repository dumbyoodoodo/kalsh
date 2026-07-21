# IMPLEMENTATION-NOTE-20260721-H0005-exact-cost-band

**2026-07-21, after an execution-integrity review of the first H0005
execution attempt.** This note documents two implementation corrections
to `scripts/exp_h0005_calibration.py`, made in response to that review,
before H0005 was re-executed. It does not alter, and is not an amendment
to, `PREREG-20260721-H0005-calibration.md` or
`AMENDMENT-20260721-H0005-pre-execution.md` — no horizon, decile, price
definition, statistical threshold, bootstrap parameter, replication rule,
evaluation order, or success criterion changed. Both corrections bring the
*implementation* into agreement with wording the frozen protocol already
contains.

## What was wrong

The first execution attempt (results superseded, see below) computed the
cost band's half-spread term as `fee_cents + round((yes_ask_close_cents -
yes_bid_close_cents) / 2)`, using Python's built-in `round()` — banker's
rounding (round-half-to-even) — and then compared the resulting band
against the bootstrap CI bound as raw binary floats
(`lower_abs > cost_band_cents / 100`).

Neither behavior is specified by the frozen text:

- PREREG Sec 9 defines `half_spread_cents = (yes_ask_close_cents −
  yes_bid_close_cents) / 2` as a real-valued quantity. AMENDMENT
  Finding 5 froze the **fee term's** rounding (`round_half_up`,
  explicitly citing this platform's rounds-up-never-to-nearest-even
  convention) but says nothing about rounding the half-spread. Applying
  `round()` to it was an unstated implementation choice, and the wrong
  one by this platform's own established convention (which never rounds
  to nearest-even).
- Neither document specifies the arithmetic mode of the discovery/
  hold-out screen comparison. Deciding it in raw IEEE-754 float
  arithmetic is what let an exact mathematical tie resolve to `True` or
  `False` depending on which side of the comparison happened to carry
  more floating-point representation error — a decision made by binary
  representation, not by the data.

Both defects were latent (present in the implementation from the prior
session, not introduced during execution) but only became decision-
relevant because the pinned dataset's sole clearing cell — horizon=2h,
decile=90-100% — happened to sit at exactly the point both defects bite:
every discovery-slice observation carried an identical 1-cent spread
(`round(0.5) == 0`, silently zeroing that component of the cost band) and
an identical calibration error that is, under the corrected exact band,
mathematically tied with the band itself (`1/200 == 1/200`).

## Correction 1 — exact half-spread arithmetic

`cost_band_cents()` now computes `half_spread_cents` as
`Fraction(yes_ask_close_cents - yes_bid_close_cents, 2)` — an exact
rational, never rounded — and returns `Fraction(fee) + half_spread_cents`
instead of an `int`. `HorizonObservation.cost_band_cents` and
`CellResult.cost_band_cents` / `holdout_cost_band_cents` are typed
`Fraction` accordingly. Averaging a cell's per-observation cost bands
(`_cell_point_estimates`, via `statistics.mean`) over `Fraction` values is
itself exact — `Fraction` arithmetic never introduces rounding at any
step. The fee term itself, and the fee-at-100¢/0¢ boundary limit
(execution-time defect fix from the prior session, retained unchanged —
see `cost_band_cents()`'s own docstring), are untouched.

Values are converted to `float` only at the point of JSON serialization
(`run()`'s cell-assembly block, `build_exploratory_grid`,
`compute_executable_price_robustness`) — a display-only conversion that
happens strictly after every discovery/hold-out screen decision has
already been made in exact arithmetic.

## Correction 2 — exact-arithmetic screen comparison

A new `_exceeds_cost_band(lower_abs, cost_band_probability)` function
decides the `|CI lower bound| > cost_band` comparison (PREREG Sec 1/9/10,
AMENDMENT Finding 4's rejection/underpowered checks) by converting both
operands to `Decimal`, quantized to a fixed `1e-9` precision
(`_to_comparison_decimal`), before the strict `>` comparison. `1e-9` is
far finer than any genuine difference cents-scale prices, averaged over at
most a few hundred day-observations, can produce — it exists solely to
strip IEEE-754 representation noise (on the order of `1e-16`–`1e-18`) from
the bootstrap-derived side of the comparison, never to suppress a real
signal. Applied at both call sites in `build_cells` (discovery Bonferroni
screen, hold-out replication screen) and in
`compute_executable_price_robustness`'s ask/bid robustness check (the same
comparison pattern, extended for consistency — this function has no CI/
bootstrap component, so only the `Fraction`-vs-`float` precision issue
applied there, not the tie-breaking issue).

**What is deliberately unchanged**: `day_clustered_bootstrap_ci` and
`_percentile` (AMENDMENT Finding 3's bootstrap, resampling, and percentile
mechanics) still operate on plain floats exactly as before — the
correction is scoped to the final comparison, not to the bootstrap itself,
per this task's explicit instruction to preserve it. The bootstrap CI
bounds reported in the results JSON (`discovery_ci`, `holdout_ci`) are
therefore numerically identical to what the prior (defective) execution
reported; only the pass/fail decision made *from* those bounds changed.

## Why neither correction changes the preregistration

- The half-spread formula's *shape* — `(ask − bid) / 2` — is unchanged;
  only whether it is rounded before being added to the fee changed, and
  the frozen text never specified rounding it.
- The comparison's *operator* — strict `>` between `|calibration_error|`'s
  CI lower bound and the cost band — is unchanged; only the numeric
  representation used to evaluate that operator changed, from raw binary
  float to a controlled-precision decimal that removes representation
  artifacts the frozen text never intended to be decision-relevant.
- No horizon, decile boundary, price definition, fee formula, Bonferroni
  alpha, confidence level, minimum-day gate, replication-split rule, or
  evaluation-order step was touched.

## Why the first execution is superseded

Re-deriving the sole clearing cell's arithmetic from the frozen text
(rather than from the code) during the integrity review showed that, under
the corrected exact formula, the discovery-slice comparison is an exact
tie (`1/200 == 1/200`, correctly `False` under a strict `>`) and the
hold-out-slice comparison fails outright (`1/200 < 33/52/100`). The first
execution's REJECTED verdict at that cell was therefore an artifact of the
two defects above, not a finding the frozen protocol, correctly
implemented, actually supports. Per the review's recommendation
(INVALIDATE AND RERUN), the first execution's `-results.json` and `.md`
report are retained under a `-SUPERSEDED` suffix in this directory (never
deleted, per this project's data-integrity conventions — they were never
committed to git and record a real, if defective, run) and are superseded
by the rerun this note accompanies.

## Regression coverage

`tests/unit/test_h0005_calibration.py` (see the "Execution-integrity
remediation: exact half-spread / exact comparison" section) covers: a
1-cent spread's exact 0.5-cent half-spread; a 3-cent spread's exact
1.5-cent half-spread; exact averaging of `Fraction`-valued cost bands
across a cell; the exact tie evaluating `False`; a genuine excess still
evaluating `True`; a genuine shortfall evaluating `False`; `None`-input
handling; the Decimal-quantization helper stripping float noise without
erasing a real difference; and two explicit regression tests reconstructing
the superseded implementation's rounded half-spread and raw-float
comparison, both of which are shown to disagree with the corrected
behavior at the exact cases that decided the first execution's verdict.
