# PREREG-20260722-H0003-forecast-error-foundation

**Pre-registration package for H0003 — frozen 2026-07-22, before any
forecast-error statistic (bias, MAE, RMSE, dispersion, PIT, coverage,
log score) has been computed for any station, variable, or horizon
bucket.** This document elaborates the existing `HYPOTHESES.md` H0003
entry (opened 2026-07-21, `Status: Proposed`) to full mechanical
precision, exactly as prior PREREG documents did for H0005/H0011/H0013/
H0014/H0015/H0017. **It does not alter H0003's frozen hypothesis
statement, rationale, metrics, or decision rule** (the NYC/tmax
PIT-uniformity-vs-climatology claim, §1.1 below, quoted verbatim). It
*adds* a broader, purely descriptive/inferential characterization layer
(§1.2) — multi-station, multi-variable bias/MAE/RMSE/dispersion and two
single, pre-specified inferential questions (horizon degradation;
first-half-vs-second-half stability) — that the original entry's
Rationale already anticipated ("Multi-station accumulation... enables
pooled-vs-per-station comparison") but did not mechanize. This is
purely additive elaboration, not a re-interpretation.

**This is a descriptive and inferential study. It is not a trading
study, not a predictive model, and does not fit any distribution to
residuals** — it characterizes the empirical residual population that a
later, distinct modeling step (M-01, gated on this entry) would fit.

## 0. Provenance — what has been inspected pre-freeze

Per the established convention (H0013 §0, H0014 §0, H0015 §0, H0017
§0): **structural/coverage facts only**, disclosed here; no error
statistic on any forecast-vs-observation pair has been computed.

Inspected: `weather_forecasts` row counts and date range (354 rows,
`issue_time` spanning 2026-07-20 → 2026-07-22, four stations); per
station-day forecast-issuance counts and the single date per station
whose target day already has *any* settled observation (CHI/DEN/LAX
2026-07-21, NYC 2026-07-20 and 2026-07-21 — each with exactly **one**
forecast issuance before settlement); column precision (`point_estimate
numeric(6,2)`, matching `weather_observations.value`); the sole
provider present (`nws`); the existing `_forecast_issue_high_low` /
`build_weather_panel` aggregation convention in
`dataset/builder.py` (max/min of period point estimates touching the
target date — ADR 0004's documented approximation, reused verbatim
below, not reinvented).

**What this means for execution, stated plainly before any statistic is
computed:** the forecast archive is two calendar days old (collection
began 2026-07-20 per ADR 0003; no historical backfill exists for
forecasts, unlike observations). At most **one** forecast-observation
pair exists per station today. Every tier below is expected to report
**insufficient accrual** at first execution. This expectation is
disclosed *because* it follows from row counts and dates alone — it
does not depend on, and is not adjusted by, any forecast-error value.
The tiered floors (§8) are chosen for statistical soundness at the
volumes this study will eventually see (weeks to months of accrual),
not tuned to pass today's archive.

## 1. Hypotheses

### 1.1 Frozen original hypothesis (verbatim from HYPOTHESES.md; unaltered)

> The error of the latest NWS point forecast for NYC daily tmax
> (forecast − settled), grouped by forecast horizon bucket (e.g. 0-12h,
> 12-24h, 24-48h, 48h+), has a distribution stable enough that an
> empirical CDF estimated on one time window transfers to a later
> window — specifically, out-of-sample PIT (probability integral
> transform) values are approximately uniform.

Its decision rule (confirmed/rejected/inconclusive-if-<90-settled-
days-per-bucket) is unchanged and is **Tier 3** below (§6, §8). It
requires a climatological baseline, which is not meaningful with less
than roughly a full seasonal cycle of data; Tier 3 is therefore
expected to remain gated for months regardless of Tier 1/2 outcomes.

### 1.2 New additive descriptive/inferential layer (this document)

**Purpose (verbatim intent from the task driving this elaboration):**
quantify forecast behavior — bias, MAE, RMSE, dispersion, empirical
error distribution — across station, variable, horizon, issuance time,
and month, and determine (a) whether error/dispersion grows with
horizon, (b) whether stations differ, (c) whether quality is stable
over the accrued window. This layer is **descriptive/estimation
first, single-test inferential second** — it does not "confirm" or
"reject" a market or trading claim, and produces no probability model.

**Tier 1 (descriptive characterization).** For every (station,
variable, horizon bucket) cell with adequate accrual: bias (mean
signed error), MAE, RMSE, error SD (dispersion), and the empirical
residual distribution (quantiles) — the direct input a later model
(M-01) would fit.

**Tier 2 (two single pre-specified inferential questions).**
(a) *Horizon degradation*: does mean absolute error increase
monotonically, in the pre-specified bucket order, pooled across
stations/variables? (b) *Station difference*: descriptive pairwise MAE
comparison across stations with CIs (non-overlapping-CI heuristic;
explicitly NOT a corrected multi-comparison family — see §5).

**Tier 3 (frozen original, unchanged).** Walk-forward PIT-uniformity
and climatology-beating log score, NYC tmax only, per §1.1.

## 2. Population, datasets, inclusion/exclusion

| Item | Frozen value |
|---|---|
| Stations (Tier 1/2) | CHI, DEN, LAX, NYC — all four (additive breadth; does not alter Tier 3's NYC-only scope) |
| Station (Tier 3) | NYC only, per the frozen original hypothesis text |
| Variables | `tmax_f`, `tmin_f`, derived per-forecast-issuance from `weather_forecasts.point_estimate` via the **existing, reused** aggregation: for a given (station, target_date, issue_time), `forecast_high_f` = max(point_estimate) and `forecast_low_f` = min(point_estimate) over all periods whose validity touches target_date — verbatim ADR 0004 / `_forecast_issue_high_low` convention, not a new one. |
| Provider | `nws` only (the sole provider present; a future provider requires an amendment, not a silent pool) |
| Truth (ground truth for scoring) | the **settled** value: the latest-issuance CLI value per (station, variable, observation_date) — the same convention `_settled_observations`/`build_weather_panel` already use for `residual_high_f`/`residual_low_f`. **Not** `value_at_settlement` (the Kalshi-market-timestamped label used in H0012/H0017): this is pure forecast verification with no market concept, and restricting to dates with a mapped Kalshi market would be an arbitrary, unjustified exclusion here. |
| Unit of analysis | One row per (station, variable, target_date, issue_time): a single forecast issuance's prediction of one day's high or low, scored against that day's eventual settled value. |
| Horizon (frozen definition; leak-free) | `horizon_hours = min(valid_start) over the same (station, target_date, issue_time) period group used for the high/low aggregation, minus issue_time`, in hours (Decimal). This is the standard NWS-verification lead time (issuance to the start of the validity window it describes) — **not** measured from a fixed calendar reference, because a naive "target-day-midnight" reference produces negative horizons for ordinary same-day afternoon/evening forecasts (checked and rejected during design, before any error statistic existed — a horizon-definition correctness fix, not a data-driven adjustment). `valid_start` is metadata carried on the forecast product itself, known at issuance; using it introduces no leakage. |
| Horizon buckets (frozen, from the original entry) | [0, 12) h; [12, 24) h; [24, 48) h; [48, ∞) h |
| Quality floor | `horizon_hours` must be `> -1` (1-hour slack for period-boundary/collection-latency artifacts) and `<= 240` (10 days); violations excluded and counted, never silently dropped or clipped. |
| Missing data | A (station, variable, target_date) with forecast issuances but no settled observation yet is excluded from all error computation and counted (`not_yet_settled`). A (station, variable, bucket) with zero forecast issuances is simply absent from that cell — reported as a coverage fact, not an error. |
| Selection within a cell | For a given (station, variable, target_date, bucket), if multiple issuances fall in the same bucket, the **latest** such issuance is used (freshest information available at that horizon depth) — avoids pseudo-replication from near-duplicate same-bucket reissues inflating cell N. |

## 3. Leakage rules (stated explicitly, per the task's requirement)

- Every forecast is scored using only its own `point_estimate` and
  `valid_start`/`valid_end` (both known at `issue_time`) and the
  eventual settled truth. The settled truth is used **only to score**
  an already-made, independent forecast — never to construct, adjust,
  or select the forecast itself. This is the standard, non-leaking
  "verify against eventual truth" pattern (identical in kind to scoring
  any backtest against a settlement outcome); it is not the same as
  feeding future information into a prediction.
- No revised/superseding forecast product is substituted for an earlier
  one: each `issue_time` row is scored as it was issued, independent of
  any later reissue for the same target date.
- `horizon_hours` is computed from `issue_time` and `valid_start` only —
  never from the observation's occurrence time or issuance time (which
  would leak observation-side information into a forecast-side
  quantity).
- **Point-in-time integrity gate (G2c):** for every scored row, the
  settled value's finalizing (latest) observation issuance time must
  be strictly after the forecast's `issue_time`. This should hold by
  construction (forecasts necessarily precede their own verification);
  it is asserted and gated, not assumed.

## 4. Endpoints

**Tier 1 (estimation, CIs, no formal test):** per (station, variable,
bucket): N, bias (mean signed error, °F) with 95% CI, MAE with 95% CI,
RMSE, error SD, error quantiles (10/25/50/75/90th percentile). Same
cells pooled by month (descriptive only — "stability" formally lives in
Tier 2b).

**Tier 2a (horizon degradation — the single primary inferential test):**
pooled (across station/variable) MAE per bucket, in the frozen bucket
order; **monotonicity check**: MAE strictly non-decreasing across the
four ordered buckets (a "not-yet-worse-with-horizon" alternative would
refute the expected-degradation direction). Reported with a
bootstrap-free closed-form CI per bucket (independent-samples framing)
and a simple sign-based ordering statistic (§5).

**Tier 2b (stability over time — single test):** split the accrued
window at its temporal midpoint (first half vs second half of calendar
days with any scored forecast, pooled across stations/variables/
buckets); compare mean MAE between halves with a Newcombe-style CI on
the difference (independent day-sets).

**Tier 2c (station comparison — descriptive only, not decisional):**
pairwise MAE per station (variable pooled), 95% CIs, non-overlap
noted descriptively; explicitly excluded from the decision table (§6)
and from any multiplicity correction (§5) because it decides nothing.

**Tier 3 (frozen original; unchanged):** PIT histogram/KS-or-CvM
uniformity test per horizon bucket per non-overlapping walk-forward
window (≥3 windows); realized coverage of nominal 50/80% intervals;
mean log score vs a climatology-only baseline (NYC tmax only).

## 5. Statistical procedures and multiplicity

Program-wide frozen machinery, unchanged: Decimal precision-50
arithmetic; all strict decision comparisons quantized to 1e-12
(ROUND_HALF_EVEN); z = `Decimal("1.959963984540054")` for all closed-
form 95% intervals; fixed-point 12-digit serialization. Mean/SD/CI on
bounded or effectively-bounded quantities use the deterministic
normal-approximation CI (`mean ± z·s/√n`, the same construction used in
H0017 §5) rather than a bootstrap, for full determinism.

**Multiplicity, declared:** Tier 1 is estimation (CIs), not hypothesis
testing — no correction applies to reporting a CI. Tier 2 contains
**exactly two** pre-specified decisional tests (2a horizon degradation,
2b stability); Tier 2c is explicitly non-decisional and carries no
multiplicity burden. With two decisional tests declared in advance, no
formal family-wise correction is applied (matching the "small declared
family, no correction" pattern used when the family size is this small
and named in advance); this is a declaration, not an oversight. Tier 3
retains its own frozen, already-specified test structure (§1.1),
evaluated independently once its own gate (§8) is met.

## 6. Decision table

Two independent decision tracks, since Tiers 1/2 and Tier 3 have
different scopes, populations, and gates; each reports its own outcome.
Neither track can silently substitute for the other.

### 6.1 Track A — descriptive/inferential foundation (Tiers 1/2)

| Step | Condition (quantized) | Outcome |
|---|---|---|
| 0 | G1/G2 integrity gate fails | **BLOCKED** — not a scientific outcome |
| 1 | Fewer than TIER1_FLOOR (20) scored rows in **any** decisional (station, variable, bucket) cell | **INCONCLUSIVE — INSUFFICIENT ACCRUAL (Tier 1)**; report every cell's N and the shortfall |
| 2 | Tier 1 floor met everywhere, but pooled per-bucket N < TIER2_FLOOR (30) in any bucket | **PARTIAL — DESCRIPTIVE ONLY**; Tier 1 tables reported; Tier 2a/2b not attempted |
| 3 | Tier 2 floor met | **FULL — FOUNDATION ESTABLISHED (Track A)**; Tier 1 + Tier 2a/2b all computed and reported |

### 6.2 Track B — original PIT-vs-climatology claim (Tier 3, NYC tmax)

| Step | Condition (quantized) | Outcome |
|---|---|---|
| 0 | G1/G2 integrity gate fails | **BLOCKED** |
| 1 | Fewer than 90 settled NYC-tmax days in **any** horizon bucket, or fewer than 3 non-overlapping walk-forward windows constructible | **INCONCLUSIVE — INSUFFICIENT ACCRUAL (Tier 3)**, per the entry's own original frozen threshold |
| 2 | ≥90/bucket and ≥3 windows | Evaluate the frozen original rule verbatim: **CONFIRMED** / **REJECTED** per §1.1 |

Interpretation (frozen): an INCONCLUSIVE-ACCRUAL outcome on either
track is not a soft "confirmed" and must name the shortfall and an
explicit retry condition (§8), per RESEARCH.md's "inconclusive for
sample size" convention. A PARTIAL/DESCRIPTIVE-ONLY outcome reports
exactly what Tier 1 supports and states which questions (horizon
degradation, stability, the frozen PIT claim) remain unanswered — never
answered anyway from an underpowered cell. No outcome on either track
licenses any market, trading, or model-selection conclusion; M-01
(Phase 5 baselines) depends on Track A reaching at least PARTIAL with
Tier 1 populated, and ideally FULL.

## 7. Quality thresholds and sensitivity analyses (registered)

- **Quality exclusions** (counted, never silently dropped): null
  `point_estimate`; `horizon_hours` outside `(-1, 240]`; forecast rows
  from a provider other than `nws`.
- **Sensitivity (non-decisional):** (i) latest-issuance-per-bucket
  selection vs. a first-issuance-per-bucket variant; (ii) bucket
  boundaries shifted by ±3h; (iii) excluding the 1-hour collection-
  latency slack entirely (strict `horizon_hours > 0`). Point estimates
  only; none can alter the Track A/B decision.

## 8. Eligibility gates and power

| Gate | Frozen requirement |
|---|---|
| G1 | Recomputed canonical content hash of the pinned forecast-horizon extract == its manifest pin |
| G2a | Extract unique on (station_id, variable, target_date, issue_time); no nulls in key/value fields |
| G2b | Station set == {CHI,DEN,LAX,NYC}; variable set == {tmax_f, tmin_f}; provider set == {nws} |
| G2c | Point-in-time integrity: for every scored row, settlement finalizing time > issue_time (§3) |
| TIER1_FLOOR | ≥ 20 scored rows per (station, variable, bucket) cell — a bare floor for a mean+SD+CI to be more than a point estimate |
| TIER2_FLOOR | ≥ 30 scored rows pooled per bucket (Tier 2a); ≥ 30 scored days per half (Tier 2b) |
| TIER3_FLOOR | ≥ 90 settled NYC-tmax days per bucket (unchanged from the frozen original entry) and ≥ 3 non-overlapping walk-forward windows |

**Retry timing, stated now (not adjusted later):** the roadmap
(`docs/research/2026-07-22-research-roadmap.md`) already estimated
Tier-3-scale accrual at **90 days from forecast-collection start
(2026-07-20) ≈ 2026-10-18**. Track A's lighter floors (20-30 per cell,
pooled across 4 stations rather than 1) should be reachable
substantially earlier; the exact date depends on the collector's actual
per-day issuance rate and is not projectable from two days of data —
the report will state the achieved N and let the next execution's gate
report speak for itself, rather than guessing a date this document
cannot support.

## 9. Descriptive outputs (registered; produced only for cells/tiers a
non-blocked decision actually reaches)

Full Tier 1 tables (station × variable × bucket, and station × variable
× month); the Tier 2a bucket-ordered MAE curve with CIs; the Tier 2b
half-vs-half comparison; Tier 2c pairwise station table (labeled
non-decisional). **Figures** (post-verdict, registered quantities
only): (1) horizon-error curve (MAE/bias by bucket, all stations
overlaid); (2) per-station error-distribution violins/quantile plots;
(3) monthly stability panel; (4) coverage/reliability diagram (Tier 3
only, if reached). If a tier is not reached (accrual insufficient),
its figures are **not** produced — matching the H0015 precedent that a
blocked/insufficient tier gets a documented reason, not a fabricated
plot.

## 10. Reproducibility

Pinned extract: dataset version `exp-20260722-h0003-forecast-extract`
(built fresh from `weather_forecasts` + `weather_observations`; hash
recorded in its own manifest and re-quoted here at execution). Two raw
executions must be byte-identical before reporting. Evaluation order:
G1 → G2(a/b/c) → Track A tiers 1→2 → Track B tiers → §9 descriptives
for whichever tiers were reached. Iteration order: stations
alphabetical, variables tmax_f then tmin_f, buckets in frozen order,
dates ascending. Point-in-time audit: every scored row's forecast
predates its own scoring truth (G2c); no forecast is ever re-derived
from a later reissue; horizon uses only issuance-side metadata (§3).

## 11. Implementation-integrity checklist

- [ ] Horizon computation tests: the corrected (`valid_start`-based, not
      calendar-midnight-based) definition on synthetic same-day,
      next-day, and multi-day-ahead forecasts, including the negative-
      horizon case the naive definition would have produced (documented
      as the reason for this design, not found empirically).
- [ ] Aggregation-consistency test: the extract's forecast_high_f/
      forecast_low_f exactly reproduces `_forecast_issue_high_low`'s
      output on a synthetic multi-period sample (guards against a
      second, silently-different convention).
- [ ] Bucket assignment boundary tests (11.99h/12h/12.01h etc.).
- [ ] Latest-issuance-per-bucket selection tests (ties, single-issuance
      cells).
- [ ] Quality-exclusion tests: null point_estimate, horizon outside
      `(-1,240]`, non-`nws` provider — each excluded and counted.
- [ ] G2c point-in-time integrity test: a synthetic row where
      settlement time precedes issue_time must fail the gate.
- [ ] Decision-table tests: every Track A/B branch reachable via
      synthetic cell counts, including the boundary N exactly at each
      floor (passes) and one below (does not).
- [ ] Tier 2a/2b closed-form CI hand checks against an independent
      arrangement of the same formula (as in prior studies).
- [ ] Determinism: run-twice byte-identity on synthetic data.
- [ ] Code review against §2-§6 before first execution.

## 12. Known risks

- **R1 — Near-zero accrual at first execution** (§0): expected and
  disclosed before any statistic exists; the honest outcome is
  INCONCLUSIVE-ACCRUAL on both tracks, not a forced result.
- **R2 — Horizon-definition choice**: the `valid_start`-based lead time
  is the standard verification convention but is not the only possible
  one; the naive calendar-midnight alternative was tried and rejected
  at design time for producing negative horizons on ordinary forecasts
  (documented in §2, not discovered from real data).
- **R3 — Single summer window**: even once Track A's lighter floors are
  met, a few weeks of one season cannot support a seasonal-stability
  claim; Tier 2b tests only short-run (within-window) stability, not
  cross-season stability (that remains H0004's explicit scope, per the
  original H0003 entry's failure-mode note).
- **R4 — "Daily high period" aggregation adds noise** (inherited,
  ADR 0004): quantified as part of Tier 1's dispersion numbers once
  computable, not treated as a separate unknown.
- **R5 — Provider concentration**: only `nws` exists; no cross-provider
  comparison is possible or attempted.

---

**No bias, MAE, RMSE, dispersion, PIT, coverage, or log-score value has
been computed for any station, variable, or bucket. H0003 has not been
executed.**
