# Hypotheses

The purpose of this project is to test hypotheses, not to assume profitable strategies exist. Every strategy or model idea starts here, as a hypothesis, before any implementation begins.

**Most hypotheses will fail. This is expected, and a well-documented failure is a successful research outcome.** The value of this document is as much in what it rules out as in what it confirms.

## Process

1. **Before writing any code**, add a new entry below using the template, with everything up to and including "Metrics" filled in. Do not fill in "Results" or "Conclusion" yet.
2. Get the entry reviewed (per `CLAUDE.md`'s engineering workflow: state the hypothesis and stop for review before implementing).
3. Implement the smallest experiment needed to evaluate it, per `RESEARCH.md`'s methodology (correct dataset version, walk-forward validation, calibration-first evaluation, pre-registered metric).
4. Fill in "Results" and "Conclusion" from the actual experiment output. Do not adjust the hypothesis or metric after seeing results to make it look better — if the original framing turns out to be the wrong question, close this entry as concluded (with that observation) and open a new entry for the better-framed question.
5. Update `Status`. A hypothesis is never left "In Progress" indefinitely without a follow-up date or explicit abandonment note.

## Status values

- `Proposed` — written, not yet started.
- `In Progress` — experiment running.
- `Confirmed` — evidence supports the hypothesis under the pre-registered metric and validation methodology.
- `Rejected` — evidence does not support it. This is a normal, useful outcome.
- `Inconclusive` — insufficient data or a flawed experiment design; needs a follow-up hypothesis, not a verdict.

## Template

```markdown
### H<NNNN> — <short title>

- **Status:** Proposed
- **Opened:** <YYYY-MM-DD>
- **Closed:** <YYYY-MM-DD or empty>
- **Related:** <other hypothesis IDs, ARCHITECTURE.md/STRATEGY_SPEC.md sections, etc.>

**Hypothesis.** <One or two sentences: the specific, falsifiable claim being tested. Not "weather markets are mispriced" — specific enough to be wrong.>

**Rationale.** <Why this might be true. Mechanism, not vibes — e.g. a specific reason participants might misestimate a distribution, a specific data quirk, a specific structural feature of the market.>

**Required data.** <Datasets, stations, date ranges, dataset version(s) once known.>

**Experiment design.** <Exactly what will be computed, against what baseline, over what validation scheme (see RESEARCH.md walk-forward validation). State the decision rule for confirm/reject *before* running it — e.g. "confirmed if out-of-sample Brier score improves by >X with non-overlapping CI across >=N walk-forward windows.">

**Metrics.** <The specific pre-registered metric(s) from the experiment design above — restated here so Results can be compared directly against what was promised.>

**Required features.** <The minimal feature set, referencing the feature IDs in the current research plan (docs/research/) — existing dataset columns vs. Phase 4 work.>

**Statistical tests.** <The specific tests/CI procedures (see RESEARCH.md): e.g. bootstrap CI on a proportion, reliability-curve deviation test, paired comparison across walk-forward windows.>

**Failure modes.** <How this experiment could mislead even if executed as designed — small sample, regime dependence, label noise, selection effects — and what mitigates each.>

**Difficulty.** <Trivial / Small / Moderate / Large — implementation effort for the minimal experiment, not the full strategy.>

**Dependencies.** <Other hypothesis IDs, accumulation prerequisites (data that must exist first), or platform work.>

**Results.** <Filled in after the experiment. Numbers, sample sizes, confidence intervals. Link to the experiment record per RESEARCH.md.>

**Conclusion.** <Confirmed / Rejected / Inconclusive, and why, referencing the pre-registered decision rule. If rejected, note anything learned that should inform future hypotheses.>
```

## Log

### H0001 — Forecast-distribution misestimation near thresholds

- **Status:** Proposed
- **Opened:** 2026-07-20
- **Closed:**
- **Related:** `STRATEGY_SPEC.md` (thesis section), `ROADMAP.md` 0.5-0.7

**Hypothesis.** For at least one daily-maximum-temperature Kalshi series, the market-implied probability of a threshold/bucket outcome differs from a well-calibrated forecast-error-based probability estimate by more than transaction costs, persistently enough to be exploitable — specifically, mispricing concentrated near strike boundaries where participants may anchor on the point forecast rather than its distribution.

**Rationale.** Kalshi weather markets settle against objective, publicly available forecasts and observations, but the market-clearing price reflects participants' beliefs about the *distribution* around the forecast, not just its center. If participants underweight forecast uncertainty (treating the point forecast as near-certain), prices near the forecast value would be too extreme (too close to 0 or 100) relative to a properly calibrated residual distribution, especially at longer horizons where forecast error is larger.

**Required data.** Kalshi market/order-book history and settlement outcomes for at least one daily-temperature series (Phase 2, Milestones 2-3); matched official forecast issue-time history and observations for the corresponding station; a Phase 2 Milestone 4 point-in-time joined dataset with a recorded version.

**Experiment design.** Not yet specified in detail — this requires the Phase 2 dataset to exist first. At minimum it will: fit a baseline forecast-error model (empirical, Gaussian, Student-t per `STRATEGY_SPEC.md`) using only data available before each simulated decision; compare the resulting contract probability to the market's executable-price-implied probability at matched horizons; evaluate using walk-forward validation with a pre-registered net-edge-after-costs threshold and minimum sample size, per `RESEARCH.md`.

**Metrics.** To be finalized alongside the experiment design above before Phase 6 backtesting begins; expected to include calibration (Brier score, reliability curve) of the model in isolation, and, separately, net edge after fees/spread/slippage versus the market.

**Results.** Not yet run. (Status note, 2026-07-21: the Phase 2 data platform this entry was waiting on is now complete; what remains is accumulated market/forecast *history*. This umbrella thesis has been decomposed into testable components — see H0003 (forecast-error foundation), H0005 (market-calibration null), and H0006 (the threshold-anchoring mechanism, which is this hypothesis's sharpened, directly falsifiable form). H0001 will be concluded from H0006's outcome rather than run as its own experiment.)

**Conclusion.** Not yet concluded.

### H0002 — CLI settlement-revision risk is material

- **Status:** Confirmed
- **Opened:** 2026-07-21
- **Closed:** 2026-07-21
- **Related:** docs/adr/0003-weather-data-source.md (CLI reissuance), docs/research/2026-07-21-research-plan.md (§4, first experiment), H0011 (follow-up)

**Hypothesis.** For NYC daily max/min temperature, the first-issued CLI report value differs from the final settled value on more than 5% of station/variable-days, and differs by more than 1°F on more than 1% of days — i.e., settlement-label noise between preliminary and final reports is material, not negligible, for modeling and near-settlement decision-making.

**Rationale.** CLI reports are reissued (a same-day preliminary report, then a final report after local midnight — confirmed in Milestone 3). A same-day preliminary high can only be revised *upward* by later same-day warmth, and midnight-boundary conventions can shift either value. Kalshi's own rules_secondary text explicitly warns traders about preliminary NWS values. If revisions are frequent or large, (a) every downstream hypothesis's "label" carries quantifiable noise, and (b) near-settlement prices that treat the preliminary value as final are systematically wrong in a measurable direction. The 30 days currently loaded show first≠final on 11/60 (18%) of station/variable-days — the hypothesis pre-registers the threshold on a much larger backfilled sample.

**Required data.** `weather_observations` with full issuance history for NYC, backfilled ≥3 years via IEM (`WEATHER_BACKFILL_DAYS` config change only — no pipeline modification). A versioned `weather_panel`-adjacent extract of first/last issuance per station/variable/day. Dataset version recorded at run time.

**Experiment design.** For every (station, variable, observation_date) with ≥2 issuances: compute first-vs-final delta, plus time-of-day of each issuance. Report: revision frequency, |delta| distribution, delta sign asymmetry for same-day preliminary highs (physical prior: non-negative), and monthly/seasonal variation. Decision rule, pre-registered: **confirmed** if the 95% CI lower bound for revision frequency exceeds 5% *and* the 95% CI lower bound for P(|delta|>1°F) exceeds 1%; **rejected** if the upper bounds fall below those thresholds; otherwise inconclusive. Days with a single issuance are counted and reported separately (coverage caveat), never silently dropped.

**Metrics.** Revision frequency with bootstrap 95% CI; P(|delta| > 1°F) with CI; full |delta| histogram; sign asymmetry rate for same-day preliminary tmax.

**Required features.** F7 (issuance history — exists), F8 (revision delta — trivial derivation in the experiment, not pipeline work).

**Statistical tests.** Bootstrap (or Wilson) CIs on proportions; sign test for the upward-revision asymmetry of preliminary highs. No model fitting; no multiple-comparison burden beyond the two pre-registered thresholds.

**Failure modes.** (1) IEM backfill gaps under-represent revision-heavy days → report coverage (days with ≥2 issuances / total days) alongside results. (2) NWS format/practice changes over the backfill window → report frequency by year, not only pooled. (3) NYC-specific conventions → conclusion is claimed for NYC only until replicated on other stations.

**Difficulty.** Trivial-to-Small (a measurement over existing data; the only "cost" is one slow backfill collection cycle).

**Dependencies.** None — runnable now. Feeds H0005/H0006/H0007 (label-noise handling) and any Phase 6+ near-settlement logic.

**Results.** Run 2026-07-21 — [EXP-20260721-H0002-cli-revisions](docs/research/experiments/EXP-20260721-H0002-cli-revisions.md), dataset version `exp-20260721-h0002`. N = 2,564 station/variable-days (NYC, 2022-12-31→2026-07-20; 99.5% of days had ≥2 issuances, so the pre-registered coverage caveat did not bite). Revision frequency **18.76%**, Wilson 95% CI [17.30%, 20.32%] — lower bound far above the 5% threshold. P(|Δ|>1°F) **12.25%**, CI [11.03%, 13.57%] — lower bound far above 1%. Mean |Δ| when revised 3.32°F (max 18°F). Same-day preliminary tmax revisions: 169/170 upward (sign test p ≈ 2.3×10⁻⁴⁹). Stable across years (17.1-21.7%, overlapping CIs); material in every calendar month (11-26%).

**Conclusion.** **Confirmed**, per the pre-registered decision rule (both CI lower bounds cleared their thresholds by wide margins). Settlement labels carry material revision noise; downstream hypotheses must treat the first-issued CLI value as provisional (~19% revised, mean miss 3.3°F when revised), and H0007 should use the measured 169/170 upward-asymmetry rather than assuming a perfectly hard preliminary-high floor. Exploratory observation (not pre-registered): tmin labels revise ~2× as often as tmax (24.3% vs 13.3%) — spun out as H0011. Conclusion claimed for NYC, window 2023-2026.

### H0003 — NWS forecast error has a stable, estimable distribution by horizon

- **Status:** Blocked (integrity gate; retry ≥ 2026-10-18, with an added eligibility-criterion amendment)
- **Opened:** 2026-07-21
- **Closed:** 2026-07-22 (this execution only; the frozen hypothesis remains open for a future retry)
- **Related:** H0001, STRATEGY_SPEC.md (baseline model), ROADMAP.md 0.5
- **Pre-registration package:** `docs/research/preregistrations/PREREG-20260722-H0003-forecast-error-foundation.md` (frozen 2026-07-22, before any forecast-error statistic was computed for any station/variable/bucket). Elaborates this entry to full mechanical precision without altering the hypothesis/rationale/metrics/decision-rule below (verbatim, §1.1 of that document); additively registers a broader descriptive/inferential layer (§1.2) — multi-station/multi-variable bias/MAE/RMSE/dispersion (Tier 1) plus two single pre-specified inferential questions, horizon degradation and first-half-vs-second-half stability (Tier 2) — evaluated on an independent decision track (Track A) alongside this entry's original NYC/tmax PIT-vs-climatology claim (Track B, unchanged, still gated at ≥90 settled days/bucket).

**Hypothesis.** The error of the latest NWS point forecast for NYC daily tmax (forecast − settled), grouped by forecast horizon bucket (e.g. 0-12h, 12-24h, 24-48h, 48h+), has a distribution stable enough that an empirical CDF estimated on one time window transfers to a later window — specifically, out-of-sample PIT (probability integral transform) values are approximately uniform.

**Rationale.** The entire pricing thesis (H0001/H0006) rests on knowing the forecast-error distribution better than the market prices it. Before comparing anything to markets, the foundation must be validated on its own: if NWS errors are unstable or the sample is too thin to estimate a usable CDF, every downstream model inherits that failure invisibly. This is deliberately a *model-validation* hypothesis with no market data involved.

**Required data.** ≥90 days of accumulated forecast issuances joined to settled observations (accrues from 2026-07-20; no historical backfill exists — ADR 0003). Multi-station accumulation (registry expansion) shortens the wall-clock proportionally and enables pooled-vs-per-station comparison. Versioned `weather_panel` + as-of forecast extracts.

**Experiment design.** Walk-forward: estimate the empirical residual CDF per horizon bucket on an expanding window; evaluate PIT uniformity and interval coverage (e.g. nominal 80% interval's realized coverage) on the subsequent window; advance and repeat. Baseline comparison: a naive climatology-only distribution (no forecast) must be beaten decisively on log score, else the forecast adds no distributional value. Decision rule: **confirmed** if across ≥3 non-overlapping evaluation windows, PIT uniformity is not rejected (see tests) in at least 2/3 windows *and* forecast-conditional log score beats climatology in all windows; **rejected** if climatology is not beaten; **inconclusive** if sample <90 settled days per bucket under test.

**Metrics.** PIT histogram/uniformity per horizon bucket per window; realized coverage of nominal 50%/80% intervals; mean log score vs climatology baseline.

**Required features.** F1, F5 (exist/trivial), F10 (walk-forward-safe residuals — the first genuine Phase 4 feature item).

**Statistical tests.** Kolmogorov-Smirnov (or Cramér-von Mises) on PIT values per window with the caveat of small N reported; bootstrap CI on coverage rates; paired log-score comparison across windows.

**Failure modes.** (1) Season captured in the estimation window differs from evaluation window (summer-only data) → report explicitly; full seasonal claim deferred to H0004. (2) Horizon-bucket sparsity → merge buckets before, not after, seeing results. (3) The "daily high period" aggregation (max of forecast periods, ADR 0004's documented approximation) adds noise → quantified as part of this experiment, not discovered later.

**Difficulty.** Small-to-Moderate (first real use of walk-forward tooling; modeling is deliberately just an empirical CDF).

**Dependencies.** Accumulation (weather collector running continuously); H0002's revision-risk number for label-noise context; ideally registry expansion. Feeds H0006 and Phase 5 baselines directly — its conclusion *is* the evidence for the Phase 5 model progression.

**Results.** Run 2026-07-22 — [EXP-20260722-H0003-forecast-error-foundation](docs/research/experiments/EXP-20260722-H0003-forecast-error-foundation.md), extract `exp-20260722-h0003-forecast-extract` (hash `f2d8b3e9…`, 362 rows; analysis commit `3a2c008`; reruns byte-identical). **BLOCKED at gate G2c (point-in-time integrity)** on both decision tracks — 2 of 362 rows (DEN tmax_f/tmin_f, target_date = the day of execution, 2026-07-22) had a forecast issuance postdating their "settled" value, because the reused `_settled_observations` convention ("latest issuance so far") is unsound for a target date still in progress — the gate caught this correctly, before any statistic was computed; no fix was applied mid-run. Separately and independently (descriptive only): accrual was already far below every registered floor regardless — at most 5 of 362 rows per (station, variable) carried any settled truth at all, against a TIER1_FLOOR of 20 *per bucket*, and Track B's ≥90-settled-NYC-tmax-days-per-bucket floor was short by roughly two orders of magnitude.

**Conclusion.** **Blocked** (step 0 of the frozen decision table — not a scientific outcome on either track; the original NYC/tmax PIT-vs-climatology hypothesis remains open, neither confirmed nor rejected). This execution surfaced a genuine, useful defect rather than a fabricated number: a target_date must be required to have received a genuinely final (post-midnight) report before it is eligible for scoring, not merely "the latest issuance seen so far" — a fix to be added explicitly to the next execution's pre-registration, not patched retroactively into this one. M-01 (Phase 5 baselines) remains blocked on H0003 reaching at least a PARTIAL-DESCRIPTIVE Track A outcome. Retry no earlier than **2026-10-18** (≈90 accrual days from forecast collection's 2026-07-20 start, per the roadmap's own estimate), with the eligibility-criterion fix named in the closeout. Full detail: `docs/research/postmortems/2026-07-22-h0003-closeout.md`.

### H0004 — Forecast error has exploitable structure (bias/persistence/season)

- **Status:** Proposed
- **Opened:** 2026-07-21
- **Closed:**
- **Related:** H0003, STRATEGY_SPEC.md (conditioning variables)

**Hypothesis.** NWS daily-high forecast errors for a given station exhibit at least one of: (a) nonzero mean bias by season, (b) positive lag-1 autocorrelation (yesterday's error predicts today's), or (c) materially different distributions across stations — such that conditioning the H0003 residual CDF on that structure improves out-of-sample log score by a pre-registered margin.

**Rationale.** Forecast systems have known systematic tendencies (e.g. warm-season bias at specific stations, error persistence during blocked weather regimes). If present and stable, conditioning is nearly free model improvement; if absent, that null result stops Phase 5 from adding complexity the data doesn't support (per CLAUDE.md's simple-models-first rule).

**Required data.** Same as H0003 but longer/wider: ≥2 contrasting seasons for the seasonal claim, or ≥5 stations pooled for the cross-station claim. This hypothesis explicitly waits for its sample; it must not be run early and "concluded" underpowered.

**Experiment design.** For each structure candidate: augment the H0003 empirical CDF with the conditioning variable (season bucket / lag-1 error sign / station) and compare out-of-sample log score walk-forward against the unconditioned H0003 model. Decision rule: **confirmed** for a candidate if conditioned log score improves in ≥3/4 of walk-forward windows with a bootstrap 95% CI on the mean improvement excluding zero; **rejected** for that candidate otherwise. Each of the three candidates (a/b/c) is pre-registered separately; testing all three is declared here, and a Bonferroni-adjusted significance level (α/3) applies per RESEARCH.md's multiple-comparison rule.

**Metrics.** Per-candidate out-of-sample log-score delta with CI; lag-1 error autocorrelation with CI; per-season mean bias with CI.

**Required features.** F4, F5, F10 (as H0003) plus lag-1 residual (part of F10's design).

**Statistical tests.** Bootstrap CIs on score deltas; Ljung-Box (lag-1) on residual series; per-season t-test on mean bias (or bootstrap if non-normal) — all at Bonferroni-adjusted levels.

**Failure modes.** (1) Underpowered seasonal contrast → hard sample gate in the design. (2) Multiple-comparison inflation across the three candidates → pre-registered and adjusted here. (3) Station pooling masking heterogeneity → per-station results reported alongside pooled.

**Difficulty.** Small (incremental over H0003's tooling).

**Dependencies.** H0003 confirmed (there is no point conditioning a CDF that doesn't transfer at all); accumulation ≥2 seasons or ≥5 stations.

**Results.** Not yet run.

**Conclusion.** Not yet concluded.

### H0005 — Null: Kalshi daily-temperature prices are calibrated within costs

- **Status:** Inconclusive
- **Opened:** 2026-07-21
- **Closed:** 2026-07-21
- **Related:** H0006, RESEARCH.md (calibration methodology)
- **Pre-registration package:** `docs/research/experiments/PREREG-20260721-H0005-calibration.md` (frozen 2026-07-21, before any calibration outcome inspected — updates the data source from the originally-assumed prospective 5-minute snapshot collection, which never accrued useful depth, to the archived 1-minute candle record; freezes horizons, deciles, price definition, cost band, bootstrap/replication mechanics, and success thresholds to full mechanical precision. Every deviation from this entry's original text is catalogued in `docs/research/experiments/DIFFERENCES-20260721-H0005-from-original-design.md`; this entry's Hypothesis/Rationale/Required data/Experiment design/Metrics/Statistical tests/Failure modes/Difficulty/Dependencies remain the canonical, unedited record below.)
- **Pre-execution amendment:** `docs/research/experiments/AMENDMENT-20260721-H0005-pre-execution.md` (2026-07-21, before execution — resolves an independent audit's nine precision gaps (research-question wording, bootstrap pool separation and RNG consumption order, Confirmed/Rejected/Inconclusive evaluation order, exact fee-function invocation, a `close_time`-immutability pre-execution verification gate, empty-cell metric handling, split tie-breaking, duplicate-observation invariants) by addition only — no horizon, bucket, threshold, or success criterion changed. **No pre-registration blocker remains**; H0005 execution itself is still a separate, explicit, not-yet-taken step.

**Hypothesis.** (Null form — expected to broadly survive.) Kalshi daily-temperature market mid-prices, sampled at fixed horizons before close, are calibrated probability estimates of YES settlement: across price buckets, realized YES frequency deviates from the bucket's mean implied probability by less than the round-trip cost band (~fees + half-spread). The research value lies in *where* it fails, if anywhere.

**Rationale.** These markets attract informed participants with free access to the same NWS forecasts; the efficient-market default must be the null hypothesis, not the alternative. Establishing market calibration (and its localized failures, if any) is the gate for every edge claim: an "edge" against a well-calibrated market is a bug in the model, and any true edge must show up as a *localized, persistent* calibration failure (by horizon, distance-to-strike, or liquidity — measured here, explained by H0006/H0009/H0010).

**Required data.** ≥60-90 days of collected market snapshots with settlement outcomes (accrues only while `collector run` operates), resolved via settlement_specs; settled observations for outcome labels; versioned `market_weather` dataset.

**Experiment design.** For each market snapshot at pre-registered sampling horizons (e.g. 48h, 24h, 12h, 2h before close): implied probability = mid/100; bucket into deciles; compare realized YES frequency per bucket. Decision rule: the null **survives** if no decile's deviation exceeds the cost band with 95% CI clear of it (per horizon); the null is **rejected** (interesting!) if ≥1 decile×horizon cell shows a deviation whose CI excludes the cost band, *and* that same cell direction replicates in a held-out later time slice. One market series family (NYC high) is primary; other series are exploratory and labelled as such (multiple-comparison rule).

**Metrics.** Reliability curve (per horizon) with cluster-bootstrap CIs (clustered by market/day — snapshots of one market are not independent); Brier score vs. the always-base-rate baseline; per-cell deviation vs. cost band.

**Required features.** F3, F6, F11 (exist/trivial/small).

**Statistical tests.** Cluster bootstrap (by event-day) for reliability CIs; replication requirement on a temporally held-out slice before any cell is called a rejection.

**Failure modes.** (1) Serial dependence across snapshots of the same market → day-level clustering mandatory. (2) Sparse extreme-price buckets → minimum-N per cell gate, cells below it reported as insufficient. (3) Mid-price fiction in wide markets → repeat key cells with executable (ask/bid) prices; a "mispricing" that vanishes at executable prices is not one. (4) Short history = one weather regime → dated conclusion, scheduled re-run.

**Difficulty.** Moderate (first market-data experiment; calibration tooling built here is reused everywhere).

**Dependencies.** Market accumulation (the hard gate); H0002 (label noise context); settlement resolution (done, Milestone 2b).

**Results.** Run 2026-07-21 — [EXP-20260721-H0005-calibration](docs/research/experiments/EXP-20260721-H0005-calibration.md), dataset version `exp-20260721-h0005`. N = 66 valid settlement days (`KXHIGHNY`, NYC, 2026-05-15→2026-07-20; 1 day excluded for missing settlement source data), clearing the pre-registered minimum of 60, split into a 33-day discovery slice and a 32-day hold-out slice (1-day embargo). Zero markets have any candle within the 15-minute staleness tolerance of the 72h/48h horizons in this pinned archive (contradicting the pre-registration's stated backfill-coverage assumption for 72h); the effective primary family is therefore 40 cells (4 horizons × 10 deciles), of which only 7 clear the 20-day-per-cell minimum-N gate. **No cell clears both screens.** Six sparse cells (1-8 discovery days, 1-7 hold-out days each) clear the Bonferroni-adjusted discovery screen alone but fall below the 20-day hold-out minimum-N gate — AMENDMENT Finding 4 step 3 therefore resolves this run to **inconclusive-for-power**, not confirmed or rejected. This supersedes an earlier, defective execution of the same pinned dataset: an execution-integrity review found that implementation rounded the cost band's half-spread term (Python banker's rounding, never specified by the frozen protocol) and decided the discovery/hold-out screens with raw binary-float comparison, letting an exact mathematical tie (the horizon=2h/decile=90-100% cell, `1/200 == 1/200`) resolve `True` by floating-point representation noise rather than by the data — that run's REJECTED verdict at that one cell did not survive exact re-derivation and has been superseded (see `EXP-20260721-H0005-calibration-SUPERSEDED.md`, `IMPLEMENTATION-NOTE-20260721-H0005-exact-cost-band.md`). Both the half-spread and the screen comparison are now computed in exact (`Fraction`/`Decimal`) arithmetic; the corrected re-execution against the identical pinned dataset (unchanged content hashes) produced the inconclusive result above, verified byte-identical across two independent runs.

**Conclusion.** **Inconclusive**, per the pre-registered decision rule (AMENDMENT Finding 4 step 3): every cell clearing the Bonferroni-adjusted discovery screen did so on too few hold-out days to independently support a replicated finding, and none of the 7 adequately-powered cells (≥20 hold-out days — concentrated in decile 0, and decile 9 at shorter horizons) cleared the discovery screen at all. This is a clean, unambiguous application of the pre-registered power gate to a dataset that is, at most horizon/decile combinations, sparse — not a near-miss between confirmed and rejected. No claim of calibration or miscalibration is supported by this run; more accumulated settlement days (concentrated in the sparse mid-probability deciles and the two currently-empty long horizons) would be needed before this design could produce a well-powered confirmed or rejected verdict. Re-run trigger: sufficient additional accumulated history, not a re-interpretation of this result.

### H0006 — Threshold anchoring: prices too extreme near the point forecast at long horizons

- **Status:** Proposed
- **Opened:** 2026-07-21
- **Closed:**
- **Related:** H0001 (this is its sharpened form), H0003, H0005

**Hypothesis.** At horizons ≥24h, for strikes within ±3°F of the latest NWS point forecast, market-implied probabilities are systematically more extreme (closer to 0/100) than the H0003 forecast-error distribution justifies — i.e., participants anchor on the point forecast and underprice its uncertainty — by a margin exceeding the cost band on average.

**Rationale.** The specific behavioral mechanism behind STRATEGY_SPEC.md's thesis: treating "forecast high 85°" as near-certainty of 85° makes "high ≥ 86°" look cheaper than a calibrated residual CDF says it is (and symmetric strikes look too dear). At long horizons true forecast σ is largest, so anchoring mispricing (if it exists) should be largest exactly where H0003's distribution is widest. If H0005 shows localized calibration failure at long horizons near strikes, this hypothesis names and tests the mechanism.

**Required data.** Everything H0003 and H0005 require (their unions of accumulated history), joined in `market_weather`.

**Experiment design.** For each snapshot at horizon ≥24h with |strike − latest forecast| ≤ 3°F: compute model probability from the frozen, walk-forward-fitted H0003 CDF (fitted strictly on data before the snapshot); edge = model − market-implied at executable prices, net of fees. Decision rule: **confirmed** if mean net edge on the pre-registered cell (long-horizon, near-strike) is positive with cluster-bootstrap 95% CI excluding zero across ≥2 non-overlapping evaluation periods, *and* the same statistic in the control cell (short-horizon, same strikes) is materially smaller — the mechanism predicts the contrast, not just an offset; **rejected** if the CI includes zero or the contrast is absent.

**Metrics.** Net edge (executable, after fees) in test vs. control cells with CIs; calibration of the combined "trade where edge>0" selection (selection-conditional reliability).

**Required features.** F1, F2, F3, F5, F6, F10, F11.

**Statistical tests.** Cluster bootstrap by event-day; test-vs-control contrast CI; replication across two time periods mandatory before "confirmed."

**Failure modes.** (1) H0003 model error masquerading as market error → H0003 must pass first; edge is claimed only net of its documented calibration error. (2) Winner's-curse selection (evaluating only cells that looked good) → the cell is pre-registered here, now, before any market data exists — this entry is the pre-registration. (3) Costs understate reality (slippage/queue) → executable prices + fee schedule, and the Phase 6 backtest (not this experiment) remains the final arbiter. (4) Regime dependence → dated conclusion + scheduled re-run, per RESEARCH.md.

**Difficulty.** Moderate (composition of H0003 + H0005 tooling; little new machinery).

**Dependencies.** H0003 confirmed; H0005 run (either outcome — its localization map guides interpretation, and its null surviving everywhere would predict rejection here); market + forecast accumulation.

**Results.** Not yet run.

**Conclusion.** Not yet concluded.

### H0007 — Preliminary-observation bound violations near settlement

- **Status:** Rejected
- **Opened:** 2026-07-21
- **Closed:** 2026-07-21
- **Related:** H0002, docs/adr/0003-weather-data-source.md (same-day preliminary CLI reports)
- **Pre-registration package:** `docs/research/experiments/PREREG-20260721-H0007-bound-violations.md` (frozen 2026-07-21, before any price inspection — elaborates this entry's terse fields to full mechanical precision: exact information set, entry conditions, outcome definition, statistical plan, execution assumptions, bias review, reproducibility manifest template; supersedes nothing below, this entry remains the canonical hypothesis record).
- **Pre-execution amendment:** `docs/research/experiments/AMENDMENT-20260721-H0007-pre-execution.md` (2026-07-21, before execution — resolves an independent audit's episode-definition/denominator/persistence/adjacency ambiguities; fee-schedule verification is **BLOCKED** — Kalshi's primary fee schedule was unreachable from this environment, see the amendment's attempt log — **H0007 must not execute until it is resolved**).
- **Fee-verification amendment:** `docs/research/experiments/AMENDMENT-20260721-H0007-fee-verification.md` (2026-07-21, same day — an exhaustive, sourced search confirming fee *applicability* to KXHIGHNY/KXLOWTNYC from live Kalshi API data (`fee_type="quadratic"`, `fee_multiplier=1`, no overrides), but the exact fee coefficient remains unverified — Kalshi's official fee-schedule PDF returned HTTP 429 on every attempt. **H0007 remains BLOCKED.**).
- **Fee-verified amendment:** `docs/research/experiments/AMENDMENT-20260721-H0007-fee-verified.md` (2026-07-21, same day — Kalshi's official fee schedule PDF, effective 2026-07-07, supplied directly by the user; cross-verified against its own 42 published worked examples with zero discrepancies; frozen as `KALSHI_WEATHER_TAKER_FEE_CONFIG` in `scripts/h0007_fees.py`. **No pre-registration blocker remains** — H0007 execution itself is still a separate, explicit, not-yet-taken step).

**Hypothesis.** After a same-day preliminary CLI report shows a running high of X°F, markets occasionally (>0 measurable instances, aggregate mispricing exceeding costs) continue to price P(final high < X) above the level H0002's upward-revision-only constraint justifies — a model-free, logic-bound mispricing in the settlement end-game.

**Rationale.** A recorded intraday high is a hard one-sided bound: the settled high can only equal or exceed it (up to H0002-quantified reporting nuances at the midnight boundary). Incorporating a published, structured NWS product faster than manual traders is precisely the kind of mechanical edge a data platform can have without any probabilistic model at all. The as-of `obs_value_known` column in `market_weather` was built for exactly this join.

**Required data.** Intraday market snapshots (5-min collector cadence) through settlement afternoons/evenings; preliminary CLI issuance times/values; ≥60 settlement days collected. H0002's revision asymmetry results (how "hard" the bound really is).

**Experiment design.** For every snapshot after a preliminary same-day high X is knowable (as-of join): flag strikes logically bounded by X; measure the market's residual probability mass on the bounded side at executable prices, net of fees, held to settlement. Decision rule: **confirmed** if bounded-side sellable mass exceeding the cost band occurs on ≥5% of settlement days *and* aggregate hypothetical P&L of harvesting it (no sizing, unit stake, executable prices) is positive with bootstrap CI excluding zero; **rejected** if occurrences are rarer or the aggregate is non-positive (e.g. the "violations" are exactly the H0002 midnight-boundary revision risk, correctly priced).

**Metrics.** Violation frequency/day with CI; mass×frequency aggregate with CI; distribution of time-to-correction for violations that close before settlement.

**Required features.** F6, F7 (both exist), F8 (small), H0002's asymmetry estimate as an input constant.

**Statistical tests.** Bootstrap CI on day-level frequency and aggregate; explicit adjustment of the "hard" bound by H0002's measured revision asymmetry (the bound is only as hard as the data says).

**Failure modes.** (1) Stale quotes in thin end-game books look like violations → require the quote to persist across ≥2 consecutive snapshots. (2) H0002 revision risk *is* the priced risk → the H0002-adjusted bound is the test, not the naive bound. (3) Collector outage on eventful afternoons → coverage reported per day.

**Difficulty.** Small-to-Moderate (event extraction over existing columns; no model).

**Dependencies.** H0002 (the bound's hardness); market accumulation with intraday cadence through settlement windows.

**Results.** Run 2026-07-21 — [EXP-20260721-H0007-bound-violations](docs/research/experiments/EXP-20260721-H0007-bound-violations.md), dataset version `exp-20260721-h0007`. N = 66 valid-coverage settlement days (tmax_f, NYC, 2026-05-14→2026-07-21; 1 day excluded for missing settlement source data), clearing the pre-registered minimum of 60. 8,356 candles satisfied the mechanical entry conditions (a logically bounded strike, unlocked, coherent non-crossed quote, pre-close) across 99 markets. **0 of those 8,356 candles showed any raw residual mass** — the bounded/near-impossible side was priced at the exchange's own minimum tick (bid 0¢/ask 1¢) in every single case, before the fee-net threshold was even applied. 0 episodes, 0 opportunity days. Opportunity-day frequency **0.0%**, Wilson 95% CI [0.0%, 5.50%] (upper bound narrowly above the 5% threshold on that one criterion alone). Aggregate hypothetical P&L **$0.00**, bootstrap 95% CI [$0.00, $0.00] (zero qualifying episodes). The tmin_f exploratory cell (mirrored mechanism, 8,350 candidate candles) shows the identical pattern.

**Conclusion.** **Rejected**, per the pre-registered decision rule — the aggregate-P&L criterion (CI upper bound ≤ $0.00) independently and unambiguously triggers rejection, regardless of the frequency criterion's marginal Wilson bound. This is a clean rejection, not a narrow miss: the *raw*, cost-free version of the effect is absent from the data at 1-minute candle resolution, not merely unprofitable after fees. Kalshi's own weather-market order book already collapses a logically bounded strike's price to the platform's minimum tradeable tick essentially instantaneously (faster than this project's finest available candle resolution can distinguish), leaving no measurable window for H0002's revision-adjusted bound to expose a tradeable gap. Per `AMENDMENT-20260721-H0007-pre-execution.md` Finding 6, this conclusion is scoped to quote-level pricing behavior only — no claim of realized profit, fill probability, or capturable alpha is made or would be supportable. Re-run trigger: none pre-registered (a market-behavior null this clean, at 1-minute resolution, is not expected to reverse on more data alone); a finer candle resolution or a different settlement mechanism would be a new hypothesis, not a re-run of this one.

### H0008 — Market reaction to forecast revisions is measurably lagged

- **Status:** Proposed
- **Opened:** 2026-07-21
- **Closed:**
- **Related:** H0006, H0007

**Hypothesis.** Following a material NWS forecast revision (|Δ point forecast| ≥ 2°F for the target day), affected strike prices adjust toward the new forecast-implied level with a half-life exceeding one collector interval (5 minutes) — i.e., there is a measurable window in which prices reflect the superseded forecast.

**Rationale.** Forecast issuances are published events with exact timestamps that this platform captures natively (issue_time). If most participants poll forecasts casually, price adjustment should be diffuse over minutes-to-hours; if bots dominate, adjustment should be near-instant and this hypothesis fails — either answer calibrates what execution speed any Phase 6+ strategy would actually require.

**Required data.** ~100 material revision events (accrues with forecast + market collection running concurrently; multi-station expansion multiplies event rate); snapshot cadence ≤5 min around issuance times.

**Experiment design.** Event study: align snapshots relative to issuance time; measure the gap between market-implied probability and (walk-forward H0003) model-implied probability at t−1 interval vs. +5/+15/+60/+180 min; fit adjustment half-life. Decision rule: **confirmed** if median half-life across events exceeds 5 minutes with bootstrap CI excluding instantaneity (i.e., the +5 min gap retains >50% of the initial gap); **rejected** if adjustment is complete within one interval. Pre-registered event definition (≥2°F, target day, first qualifying issuance of the day) — no post-hoc event cherry-picking.

**Metrics.** Gap-decay curve with CIs; median half-life; fraction of events fully adjusted within one interval.

**Required features.** F1, F2, F6, F9, F10, F11; forecast revision delta (F8).

**Statistical tests.** Event-level bootstrap (events clustered by day); pre-registered event filter.

**Failure modes.** (1) Confounded events (weather news arriving between snapshots) → cap claims at "associated with issuance," report contaminated-window fraction. (2) 5-min cadence bounds resolution → conclusions stated no finer than the cadence. (3) Few summer events at ≥2°F → event-count gate; wait rather than dilute the filter.

**Difficulty.** Moderate.

**Dependencies.** H0003 (the model gap needs a model); concurrent market+forecast accumulation; ideally multi-station.

**Results.** Not yet run.

**Conclusion.** Not yet concluded.

### H0009 — Liquidity predicts pricing-efficiency deviations (but within costs)

- **Status:** Proposed
- **Opened:** 2026-07-21
- **Closed:**
- **Related:** H0005, H0006

**Hypothesis.** (Descriptive, null-leaning.) Snapshots with wider spreads / lower depth show larger absolute deviations between market mid and the H0003 model probability — but predominantly *within* the wider cost band those same conditions impose, such that thin markets are not net-exploitable after their own costs.

**Rationale.** Thin books deviate more by arithmetic (each fill moves price further); the question that matters is whether deviation grows *faster* than the cost of trading it. The expected answer is no — pre-registering the boring outcome guards against later motivated reasoning when a thin-market "edge" appears in a backtest. The result parameterizes cost/sizing assumptions for Phase 6 regardless of outcome.

**Required data.** The H0005 dataset (no additional accumulation).

**Experiment design.** Regress |model − mid| on spread and best-depth (cluster-robust by day); separately compute net-of-executable-cost edge by liquidity tercile. Decision rule: the null **survives** if the top-liquidity-deviation tercile's net executable edge CI includes zero; **rejected** (interesting) if net edge is positive with CI excluding zero in any pre-registered tercile.

**Metrics.** Deviation-vs-spread slope with CI; net executable edge per liquidity tercile with CI.

**Required features.** F6, F10, F11.

**Statistical tests.** Cluster-robust regression; bootstrap CIs on tercile edges.

**Failure modes.** Endogeneity (deviation causes wide spreads, not vice versa) → claims stay associational; tercile assignment uses *prior-snapshot* liquidity to avoid contemporaneous selection.

**Difficulty.** Small (piggybacks on H0005/H0006 tooling).

**Dependencies.** H0003, H0005 run.

**Results.** Not yet run.

**Conclusion.** Not yet concluded.

### H0010 — Mispricing, if any, is concentrated at specific times-to-settlement

- **Status:** Proposed
- **Opened:** 2026-07-21
- **Closed:**
- **Related:** H0005, H0006, H0007

**Hypothesis.** Calibration error (H0005's per-cell deviations) and model-vs-market edge (H0006's statistic) are not uniform across the market lifecycle: they are materially larger in at least one pre-registered horizon band (>48h; 24-48h; 12-24h; 2-12h; <2h) than the lifecycle average, identifying *when* any exploitable window exists.

**Rationale.** Different participants dominate different phases (early position-takers vs. settlement-window reactors); forecast information arrives on a schedule; H0007's mechanism only exists near settlement. If deviations exist at all, lifecycle localization is the single most actionable descriptive fact for a future strategy's entry timing — and if H0005's null survives everywhere, this closes cheaply with it.

**Required data.** The H0005/H0006 datasets (no additional accumulation).

**Experiment design.** Recompute H0005 reliability deviations and H0006 edge statistics per horizon band; test each band's deviation against the lifecycle average with cluster-bootstrap CIs, Bonferroni-adjusted for the 5 pre-registered bands. Decision rule: **confirmed** if ≥1 band's excess deviation CI (adjusted) excludes zero and replicates in a held-out later slice; **rejected** otherwise.

**Metrics.** Per-band calibration deviation and net edge, with adjusted CIs; replication slice results.

**Required features.** F3 plus the H0005/H0006 feature sets.

**Statistical tests.** Cluster bootstrap; Bonferroni across 5 bands; temporal replication requirement.

**Failure modes.** Band multiplicity (handled: pre-registered, adjusted, replication-gated); sparse <2h band early on → minimum-N gate per band.

**Difficulty.** Small (a re-slicing of H0005/H0006 outputs).

**Dependencies.** H0005 and H0006 run.

**Results.** Not yet run.

**Conclusion.** Not yet concluded.

### H0011 — Daily-low settlement labels are structurally noisier (midnight-boundary effect)

- **Status:** Confirmed
- **Opened:** 2026-07-21
- **Closed:** 2026-07-21
- **Related:** H0002 (source of the exploratory observation), H0005/H0006/H0007 (consumers)
- **Pre-registration package:** `docs/research/preregistrations/PREREG-20260721-H0011-midnight-boundary.md` (frozen 2026-07-21, before any attribution classification or timing histogram was computed), amended by `docs/research/preregistrations/AMENDMENT-20260721-H0011-audit-resolution.md` (resolving the institutional audit's nine findings F1-F9 by addition — including F1's binding scope statement on Part 2), with provenance in `PROVENANCE-20260721-H0011-posthoc-vs-prospective.md`. Dataset pinned by content hash at pre-registration time (`exp-20260721-h0002`, `observation_issuances`), not at execution.

**Hypothesis.** For NYC, tmin settlement labels revise significantly more often than tmax labels (H0002 measured 24.3% vs 13.3%, exploratory), and the excess is concentrated in revisions whose final issuance follows the local-midnight boundary — i.e., the daily minimum's proximity to the day boundary, not reporting quality generally, drives the difference. Consequence if confirmed: label-noise handling in H0005/H0006/H0007 must be variable-specific, and daily-LOW (KXLOWT-family) markets carry structurally higher settlement risk than daily-HIGH markets at equal model quality.

**Rationale.** The daily maximum occurs mid-afternoon, safely inside its local day; the daily minimum frequently occurs near sunrise or just after local midnight, where the calendar-day attribution of a cooling trend is decided at the boundary. A same-day preliminary report cannot yet know whether the late-night temperature will undercut the morning low. H0002's by-variable split (2× revision rate) is consistent with this mechanism but was not a pre-registered claim; this entry pre-registers it properly before any further analysis.

**Required data.** The same issuance-level extract as H0002 (`observation_issuances` frame; dataset version `exp-20260721-h0002` suffices, or a newer version). No new collection.

**Experiment design.** For every revised station/variable-day: classify the revision by (a) variable and (b) whether the value change first appears in an issuance timestamped after local midnight of the observation date. Decision rule, pre-registered: **confirmed** if (1) the tmin-vs-tmax revision-rate difference has a two-proportion 95% CI excluding zero, AND (2) the fraction of excess tmin revisions (beyond the tmax rate) attributable to post-midnight final issuances exceeds 50% with a Wilson 95% CI lower bound above 50%; **rejected** if (1) fails; **inconclusive** if (1) holds but (2)'s CI straddles 50%.

**Metrics.** Two-proportion difference with CI; post-midnight attribution fraction with Wilson CI; revision-timing histograms by variable (descriptive).

**Required features.** F7/F8 only (same as H0002); local-time conversion of issuance timestamps.

**Statistical tests.** Two-proportion z (or Newcombe) CI for the rate difference; Wilson CI for the attribution fraction.

**Failure modes.** (1) Attribution ambiguity when multiple post-first issuances exist → classify by the first issuance at which the final value appears, pre-registered here. (2) NYC-specific timing conventions → NYC-only claim, as with H0002. (3) This entry was motivated by looking at H0002's split — the rate-difference part of the claim is partially post-hoc; mitigation: the mechanism part (post-midnight attribution) is genuinely untested, and the whole entry must replicate on ≥1 additional station once registry expansion lands before being treated as general.

**Difficulty.** Trivial (same dataset, one classification pass).

**Dependencies.** H0002 (done). Station expansion strengthens the claim but is not required for the NYC version.

**Results.** Run 2026-07-21 — [EXP-20260721-H0011-midnight-boundary](docs/research/experiments/EXP-20260721-H0011-midnight-boundary.md), dataset `exp-20260721-h0002` (`observation_issuances`, content hash `ec38d1a6…` verified by recomputation at execution, gate G1). N = 1,282 valid variable-days per variable (NYC, 2022-12-31→2026-07-20). All eligibility gates passed (G1-G5, in order; 0 unresolved attributions). **Part 1 (rate difference): confirmed** — p_tmin = 24.26% (311/1,282) vs p_tmax = 13.26% (170/1,282), D = +11.00 percentage points, Newcombe 95% CI [+8.00, +13.98], lower bound well above zero. **Part 2 (post-midnight attribution): criterion passed** — 311/311 revised tmin days reached their final value only in a post-midnight issuance (Wilson 95% CI lower bound 98.78% > 50%). Decision, per the frozen table: **Confirmed** (step 2). Mandatory adjacent disclosures (AMENDMENT F1): the tmax attribution rate is *also* 170/170 = 100%, and 1,261/1,282 days carry exactly the two-issuance {~16:30 preliminary, ~01:30 final} cadence — the issuance-cadence confound the pre-registration's own R1 risk disclosed bound exactly as predicted, so Part 2's pass carries no mechanism-discriminating information beyond that cadence. Fully deterministic execution (no randomness); reproducibility rerun byte-identical (matching MD5); naive-UTC timestamp interpretation verified against the live source (26/26 sampled rows); `config_matches_prereg` true.

**Conclusion.** **Confirmed**, per the pre-registered decision rule, with the binding AMENDMENT-F1 scope limitation attached. **What H0011 demonstrated:** (1) NYC tmin settlement labels revise materially more often than tmax labels — an ~11-percentage-point excess (roughly 2×), now formalized with a confidence interval rather than H0002's exploratory point estimates, though on the same archive that motivated the claim (a formalization, not an independent replication); (2) revised daily-minimum labels essentially always reach their final value only in a post-midnight issuance — **settlement-label formation timing**, the quantity E2 actually measures. **What H0011 did not demonstrate:** the timing of the underlying physical temperature event. The dataset carries issuance timestamps only, not occurrence timestamps, and the required adjacent disclosure shows revised tmax labels are *equally* post-midnight-formed (100%) — E2 reflects the CLI reporting cycle's cadence, not the daily minimum's proximity to the midnight boundary specifically. The physical midnight-boundary mechanism named in this entry's Rationale therefore remains unidentified; per the frozen scope statement, a mechanism-identifying study would require new occurrence-time data (e.g. parsed from raw CLI product text) and would constitute a separate, future hypothesis, not a re-interpretation of this one. Practical consequence for consumers (H0005/H0006 successors, any KXLOWT-family market work): label-noise handling must be variable-specific — daily-LOW labels carry roughly double the revision risk of daily-HIGH labels at equal model quality, and *no* label of either variable should be treated as final before the post-midnight report. NYC-only, window 2023-2026; replication on ≥1 additional station (registry expansion) is required before treating the claim as general, per this entry's own failure mode 3.

### H0012 — Settlement-time labels are stable; close-time labels are not

- **Status:** Inconclusive (retry ≥ 2026-08-19)
- **Opened:** 2026-07-21
- **Closed:**
- **Related:** H0002 (extends; does not alter its concluded result), docs/adr/0006-settlement-labels.md, EXP-20260721-H0002-cli-revisions.md (whose 18.76% preliminary-to-final rate this decomposes)

**Hypothesis.** Two claims about NYC daily-temperature settlement labels, using the reconstructed settlement timeline (value at market close vs. value at Kalshi's settlement determination vs. latest final value): (A) the close-time label is materially non-final — P(value_at_close ≠ latest_final) exceeds 5% — because markets close (~00:59 ET) before the final morning CLI report issues; and (B) the settlement-time label is stable — P(latest_final ≠ value_at_settlement), the post-settlement correction rate, is below 2% — so the settlement-time value can serve as the canonical downstream label without per-case correction modeling.

**Rationale.** H0002 measured first-issuance-to-final revision (18.76%) but that conflates intraday information flow (the 4pm preliminary is issued before the day ends — an *expected* update, not label noise) with genuine post-settlement instability. The decision-relevant quantities for market research are stage-specific: what was knowable at close (when trading stops), at settlement (when Kalshi pays), and finally (after corrections). Kalshi's exposed `settlement_ts` (~8am ET, after the ~2:15am final report) should make the settlement-time label nearly final; the close-time label should inherit most of H0002's preliminary-revision rate.

**Required data.** The reconstructed settlement labels (settlement/labels.py) over settled KXHIGHNY/KXLOWTNYC markets 2023+ joined to the 3.5-year CLI issuance history; a versioned dataset export containing the `settlement_labels` frame, version recorded at run time.

**Experiment design.** Unit of analysis: one observation per (variable, target_date) — strikes of the same event share a settlement timeline and must not be double-counted. Population: dates with a usable (resolved or bounded) label. Compute, with Wilson 95% CIs: P(value_at_close ≠ latest_final); P(latest_final ≠ value_at_settlement) [post-settlement correction rate]; P(value_at_settlement ≠ value_at_close); P(value_at_settlement ≠ first-issued preliminary); |Δ| magnitudes per category; all split by variable (tmax/tmin); revisions categorized same-day continuation (a later issuance on/for the same local day before settlement) vs later correction (issuance after settlement). Decision rule, pre-registered: **confirmed** if claim A's CI lower bound > 5% AND claim B's CI upper bound < 2%; **rejected** if A's CI upper bound < 5% OR B's CI lower bound > 2%; otherwise inconclusive. Bounded-status labels are included in primary metrics with a sensitivity check excluding them; exact-vs-bounded counts reported.

**Metrics.** The four probabilities above with CIs; magnitude distributions per category; per-variable splits; exact/bounded label counts.

**Required features.** The settlement-label reconstruction only (no F10+, no market prices).

**Statistical tests.** Wilson 95% CIs on proportions; no fitting.

**Failure modes.** (1) Bounded labels (missing settlement_ts) could misclassify late corrections → sensitivity check excluding bounded. (2) Issuance-archive gaps → labels with missing_source_data are excluded and counted, never silently dropped. (3) Payout-agreement failures (reconstruction bugs) would corrupt these metrics → the engineering validation gate (payout agreement vs Kalshi's own expiration_value/result) must pass before this analysis is credited.

**Difficulty.** Trivial (measurement over the reconstruction).

**Dependencies.** E-A reconstruction validated against Kalshi payouts.

**Results.** Run 2026-07-21 — [EXP-20260721-EA-settlement-labels](docs/research/experiments/EXP-20260721-EA-settlement-labels.md), dataset version `exp-20260721-ea-labels`, reconstruction v1. Engineering gate passed decisively: **791/791 (100%) payout agreement** with Kalshi's own `expiration_value`/`result` across every settled market with data. n = 132 variable-dates (2026-05-16→07-20, all with exact `settlement_ts`). Claim A: P(value_at_close ≠ latest_final) = 18.94% [13.17%, 26.47%] — confirmed (lower bound ≫ 5%); per-variable rates (tmax 13.6%, tmin 24.2%) match H0002's 3.5-year values almost exactly. Claim B: post-settlement corrections **0/132**, CI [0%, 2.83%] — point estimate perfect but the upper bound misses the pre-registered 2% bar at this n; zero events requires n ≥ 189.

**Conclusion.** **Inconclusive** (for sample size, per the pre-registered rule — not adjusted retroactively). Claim A is established; claim B's evidence is uniformly favorable but underpowered by ~57 variable-dates. Retry on or after **2026-08-19** (~29 more days of two-variable market accumulation). Interim practical guidance stands regardless of B's final verdict: market experiments must use `value_at_settlement` (validated 100% against actual payouts), and close-time reasoning must treat the label as unknown ~1 time in 5.

### H0013 — H0011 replicates out-of-sample at Chicago Midway (independent-station replication)

- **Status:** Confirmed
- **Opened:** 2026-07-22
- **Closed:** 2026-07-22
- **Related:** H0002 (conventions source), H0011 (the claim under replication; its own conclusion requires this — "replication on ≥1 additional station is required before treating the claim as general"), station-expansion report (`docs/research/investigations/2026-07-21-station-expansion-report.md`)
- **Pre-registration package:** `docs/research/preregistrations/PREREG-20260722-H0013-chi-replication.md` (frozen 2026-07-22, before any CHI revision or attribution statistic was computed; provenance of everything inspected pre-freeze is §0 of that document). Dataset pinned by content hash at pre-registration time (`exp-20260722-h0013-replication`, `observation_issuances`).

**Hypothesis.** H0011's two-part claim replicates on Chicago Midway (CHI), an independent station whose data played no role in generating the hypothesis: (Part 1) CHI tmin_f settlement labels revise more often than tmax_f labels — `D_chi = p_tmin − p_tmax > 0` with statistical confidence; (Part 2) among revised CHI tmin variable-days, a confident majority reach their final value only in an issuance at or after local (America/Chicago) midnight — with the H0011 amendment's binding scope limitation inherited verbatim: Part 2 is evidence about settlement-label **formation timing** only, not the timing of the underlying physical temperature event.

**Rationale.** H0002 discovered the tmin/tmax revision asymmetry exploratorily on NYC; H0011 formalized it on the *same* NYC archive — by both documents' own admission, no independent evidence for the claim exists yet, and H0011's conclusion explicitly gates any generalization on a second-station replication. CHI is the strongest available replication candidate: its CLI issuance cadence (2.009 issuances/variable-day, measured as an availability fact before this entry was frozen) is nearly identical to NYC's (2.007), so the same label-formation regime should produce the same asymmetry if the claim reflects the reporting process rather than an NYC idiosyncrasy. DEN (3.86) and LAX (2.35) have materially different cadence regimes and are deliberately excluded from this hypothesis (a cadence-heterogeneous replication is a separate, future question).

**Required data.** The pinned `observation_issuances` frame of dataset `exp-20260722-h0013-replication` (26,310 rows, all four stations, 2022-12-31 → 2026-07-21; content hash frozen in the pre-registration §10), of which the CHI subset (5,152 rows, 1,282 days per variable) is the evaluation sample. The discovery/training sample is NYC's pinned `exp-20260721-h0002` archive, used only through H0011's published, frozen results — never re-analyzed for decisions here.

**Experiment design.** Identical to H0011's frozen protocol, re-instantiated for CHI: same unit of analysis (variable-day), same revision definition (first-issued ≠ final-issued, Decimal-quantized to 0.01), same attribution rule with the boundary at 00:00:00 America/Chicago on observation_date + 1, same eligibility gates (G1 hash pin → G2 invariants on the CHI-filtered frame → G3 N≥1000 per variable → G4 R_tmin≥100 → G5 unresolved <5%), same four-step decision table on the same estimands (`D_chi` via unpaired Newcombe 95%; `p_pm,chi` via Wilson 95%). Decision rule, pre-registered: **confirmed** if `L(D_chi) > 0` and `L(p_pm,chi) > 1/2`; **rejected** if `L(D_chi) ≤ 0`, or if `L(D_chi) > 0` and `U(p_pm,chi) < 1/2`; **inconclusive** if `L(D_chi) > 0` and the Wilson interval straddles 1/2. Separately from that verdict, the pre-registered **replication assessment** against H0011's frozen published values (D_nyc = 0.109984399376, CI [0.079970196795, 0.139841273431]): *direction replicated* iff Part 1 passes; *magnitude consistent* (descriptive, not decisional) iff the CHI and NYC 95% CIs overlap. One station, one comparison — no multiplicity beyond this single pre-registered cell.

**Metrics.** `D_chi` with Newcombe 95% CI; `p_pm,chi` with Wilson 95% CI; replication-assessment fields (direction replicated; CI overlap); descriptive: per-year revision rates, tmax attribution rate, stratified gap decomposition, issuance-cadence/feature summary, NYC same-frame consistency cross-check.

**Required features.** F7 (issuance history — exists for CHI since the station expansion backfill). No new pipeline work.

**Statistical tests.** Exactly H0011's frozen machinery: Newcombe (1998) hybrid score interval (no continuity correction, two-sided 95%) for the rate difference; Wilson score interval for the attribution fraction; frozen z literal 1.959963984540054; exact Decimal arithmetic with 1e-12 quantized decision comparisons. No model fitting, no randomness, no calibration metrics (no probabilistic prediction is produced — RESEARCH.md's Brier/reliability machinery does not apply to a label-noise replication, stated here so its absence in the report is pre-explained, not discovered).

**Failure modes.** (1) CHI's revision base rates are unknown pre-freeze; if revisions are rare (R_tmin < 100), G4 blocks execution rather than producing an underpowered verdict. (2) The cadence confound H0011's amendment identified applies to CHI equally: Part 2 is expected to pass near-trivially given ~2 issuances/day, and carries no mechanism-discriminating weight — inherited scope statement makes this explicit before results are seen. (3) Shared-weather dependence: CHI and NYC are distinct stations but not meteorologically independent on any given date; this replication is out-of-sample in station, not in time, and the entry claims station-generalization only. (4) IEM archive-quality differences between stations → coverage and single-issuance-day counts reported alongside results.

**Difficulty.** Small (re-instantiation of a frozen, already-implemented protocol on a new station).

**Dependencies.** H0011 (closed), station expansion (done), CHI issuance backfill (done).

**Results.** Run 2026-07-22 — [EXP-20260722-H0013-chi-replication](docs/research/experiments/EXP-20260722-H0013-chi-replication.md), dataset `exp-20260722-h0013-replication` (hash `5ccf5b7a…` verified by recomputation at execution, gate G1; analysis commit `9edfcce`). All gates passed (G1–G5; 0 unresolved attributions; G4's genuinely unknown R_tmin came in at 330). N = 1,282 valid variable-days per variable (CHI, 2022-12-31→2026-07-21). **Part 1: pass** — p_tmin = 25.74% (330/1,282) vs p_tmax = 15.68% (201/1,282), D_chi = +10.06pp, Newcombe 95% CI [+6.94, +13.17], lower bound well above zero. **Part 2: pass** — 330/330 revised tmin days post-midnight-formed, Wilson 95% lower bound 98.85% (with the mandatory adjacent disclosures: tmax attribution 199/201 = 99.0%; stratified gap {post: +10.22pp, pre: −0.16pp} — the cadence confound binds exactly as at NYC, per the inherited scope statement). **Replication assessment (§7.3): all positive** — direction replicated; magnitude consistent (CHI CI overlaps NYC's published [+8.00, +13.98]; D gap −0.94pp); part2 consistent. NYC same-frame cross-check reproduced H0011's published revision counts exactly (311/170) at the predicted +1-day archive drift. Deterministic execution: rerun byte-identical; naive-UTC verified 26/26 sampled rows; `config_matches_prereg` true. Figures + full comparison: `docs/research/postmortems/2026-07-22-h0013-closeout.md`.

**Conclusion.** **Confirmed**, per the pre-registered decision rule (step 2), and — unlike H0011's Part 1 — this is genuine out-of-sample evidence: every decisional statistic was computed on data that played no role in generating the hypothesis. The tmin/tmax revision asymmetry generalizes beyond NYC with near-identical effect size (+11.0pp vs +10.1pp) and is best understood as a property of the CLI settlement-label formation process at matched-cadence stations, 2023–2026. Scope limits, unchanged or newly flagged: station- not era-generalization (shared calendar window); Part 2 remains label-formation timing only (tmax 99.0% post-midnight too — no mechanism identified); matched-cadence stations only (DEN/LAX regimes untested); and the registered per-year descriptive surfaced a material recency caveat — in the 2026 partial year (n=198) CHI's tmax rate rose to 25.25%, exactly equal to tmin's, so the asymmetry is absent in that slice. Two follow-up hypotheses are recommended before downstream reliance on the asymmetry's current magnitude: a 2026 recency check (both stations) and a cadence-regime replication (DEN/LAX).

### H0014 — The 2026 revision-gap collapse: sampling noise, operational artifact, or regime shift

- **Status:** Rejected (outcome B — retry ≥ 2027-01-15)
- **Opened:** 2026-07-22
- **Closed:** 2026-07-22
- **Related:** H0013 (source of the motivating observation, its closeout §6.2), H0011/H0002 (the asymmetry under recency test)
- **Pre-registration package:** `docs/research/preregistrations/PREREG-20260722-H0014-recency-stability.md` (frozen 2026-07-22, before any season-matched cell, any NYC by-variable yearly cell, or any contrast statistic was computed). Dataset: the already-pinned `exp-20260722-h0013-replication` (hash `5ccf5b7a…`) — no new build.

**Hypothesis.** The tmin/tmax revision-rate asymmetry is stable through 2026 at NYC and CHI once seasonal composition is controlled: neither station's 2026-YTD revision rates (per variable, Jan 1 – Jul 21 window) differ from its pooled 2023–2025 rates over the *same calendar window* by more than sampling variation. The alternative outcomes — operational artifact or genuine regime shift — are adjudicated by pre-registered diagnostics and a cross-station corroboration rule, not chosen after seeing results.

**Rationale.** H0013's registered per-year descriptive showed CHI's 2026 partial-year tmax rate (50/198 = 25.25%) exactly equal to tmin's, versus ~14% in each full historical year. That comparison confounds two things the design must separate: (a) a Jan–Jul window vs full-year windows (seasonal composition), and (b) a real change in the reporting process. This experiment removes (a) by construction — every decisional comparison is season-matched (Jan 1 – Jul 21 of each year) — and tests (b) with contrasts whose corroboration structure is frozen in advance. **Epistemic status, stated plainly:** CHI's full-year cells are published motivating data — a CHI shift finding would be partially post-hoc; NYC's by-variable yearly cells have never been computed by any experiment, so NYC carries the genuinely new corroborating evidence, and the decision rule gives cross-station corroboration the deciding role. This purpose is adjudication of a seen anomaly, not a search for new effects.

**Required data.** The pinned `observation_issuances` frame of `exp-20260722-h0013-replication` (26,310 rows, hash frozen in the prereg §10), NYC + CHI subsets only (DEN/LAX intentionally excluded — different cadence regimes, out of scope). No timezone conversion occurs anywhere (windows are calendar dates of `observation_date`; revision is a value comparison), so no tzdata machinery applies.

**Experiment design.** Fixed windows, frozen before execution: season-matched SM(y) = [y-01-01, y-07-21] for y ∈ {2023, 2024, 2025, 2026} (2026 YTD ≡ SM(2026) because the pinned archive ends 2026-07-21); full-year FY(y) for 2023–2025 reported in tables only. **Decisional estimands (4, the declared family):** Δ_{v,s} = p_{v,s,SM(2026)} − p_{v,s,hist} where hist = pooled SM(2023)∪SM(2024)∪SM(2025), for v ∈ {tmax_f, tmin_f}, s ∈ {NYC, CHI} — independent samples (disjoint day sets), Newcombe interval. A **shift event** for (v,s) = the Bonferroni-corrected 98.75% CI (z = 2.497705474412374, frozen; family of 4 at family-wise α = 0.05) excludes 0 under quantized comparison. Frozen **operational indicators** at any shifted station: OI-1 |Δ mean issuances/variable-day (2026 vs hist SM)| ≥ 0.15; OI-2 |Δ missing-day fraction| ≥ 0.05; OI-3 |Δ single-issuance-day fraction| ≥ 0.05 (all quantized; ≥ means NOT strictly-less). **Decision table (ordered, terminal):** step 0 any gate fails → BLOCKED; step 1 no shift events → **A** (no evidence of meaningful regime change; the H0013 fig-4 observation is attributable to sampling variation and/or seasonal composition); step 2 shift event(s) exist and any OI fires at a shifted station → **C** (evidence of operational artifact — co-occurrence, not proven causation); step 3 some variable shifts in the same direction at both stations (no OI) → **D** (evidence inconsistent with prior findings — cross-station corroborated change); step 4 otherwise → **B** (possible regime change requiring additional data; retry with full-2026 windows on or after 2027-01-15 as a new entry). Stopping rules: gates block; run once; the windows above are the only splits — no adaptive segmentation after results.

**Metrics.** Per station × window × variable: n, r, rate with Wilson 95% CI; per station × window: gap D with Newcombe 95% CI; the four Δ contrasts with both 95% (reporting) and 98.75% (decision) Newcombe CIs; sample sizes everywhere. Diagnostics per cell: mean issuances/day, single-issuance days, calendar-expected vs observed days (missing days), issuance-count distribution; monthly revision rates (pooled hist SM vs 2026, point estimates only); excluded-day counts (2022-12-31 falls in no window).

**Required features.** F7 (issuance history — exists). No new pipeline work; no new dataset.

**Statistical tests.** Wilson and Newcombe score intervals (no continuity correction), exact Decimal arithmetic with 1e-12 quantized decisions — the program's frozen machinery — plus the one new frozen literal z = 2.497705474412374 (Φ⁻¹(0.99375), derived from Python stdlib `NormalDist.inv_cdf`, round-trip verified, recorded in the prereg). No model fitting, no randomness. The station×year interaction question is rendered as the frozen cross-station corroboration criterion (step 3) — the toolkit-compatible form — not as an unregistered logistic model.

**Failure modes.** (1) 2026-YTD cells are small (n ≈ 200): decision CIs are ±8pp-scale, so only large shifts are detectable — by design, smaller drifts land in A/B, and B names the retry. (2) CHI partial post-hoc risk → disclosed; cross-station rule carries the weight. (3) Operational indicators are co-occurrence measures, not causal proof → C is worded accordingly. (4) A real shift beginning mid-2025 would contaminate the pooled baseline → per-year SM cells are reported so the record shows it; the pooled baseline stays decisional (frozen; no post-hoc re-pooling). (5) Multiplicity → Bonferroni over the declared family of 4.

**Difficulty.** Small (rate arithmetic over an already-pinned frame).

**Dependencies.** H0013 (closed; supplies the pinned dataset and the motivating observation).

**Results.** Run 2026-07-22 — [EXP-20260722-H0014-recency-stability](docs/research/experiments/EXP-20260722-H0014-recency-stability.md), dataset `exp-20260722-h0013-replication` (hash `5ccf5b7a…` re-verified at G1; analysis commit `1ad28f2`). All gates passed (16/16 SM cells n ∈ [194,200]; hist R ∈ [88,166]). The declared family of 4 season-matched contrasts (98.75% decision CIs): **CHI tmax Δ = +9.70pp [+1.72, +18.73] — SHIFT (up)**; CHI tmin −2.51pp [−10.88, +6.93]; NYC tmax +2.35pp [−4.56, +10.65]; NYC tmin +2.01pp [−6.35, +11.37] — no other shift. No operational indicator fired at CHI (cadence diff 0.012 vs threshold 0.15; missing-day fraction diff 0.5pp vs 5pp; single-issuance diff 0.2pp vs 5pp). Decision path: step 1 fails (a shift exists) → step 2 fails (no OI) → step 3 fails (NYC not corroborating) → **outcome B**. Season-matched yearly gaps: CHI +10.4/+11.6/+14.6/**+0.0**pp; NYC +4.6/+15.3/+8.1/**+9.0**pp (hist +9.4). Monthly profile: CHI tmax elevated in 5/7 months of 2026 (broad, not single-event). Deterministic: reruns byte-identical; decision-z re-derived by bisection in-run; config-vs-prereg true. Full narrative: `docs/research/postmortems/2026-07-22-h0014-closeout.md`.

**Conclusion.** **Rejected** (the registered prediction was stability/outcome A; the frozen table returned **B — possible regime change requiring additional data**). What the run established: (1) seasonal composition is real (CHI's Jan–Jul tmax baseline is 15.6% vs 13.9% full-year) but does **not** explain the 2026 CHI tmax elevation, which survives season-matching at the multiplicity-corrected level; (2) no registered operational explanation exists — cadence, completeness, and single-issuance behavior are all stable; (3) the shift is CHI-only — NYC, the fully unseen sample, is textbook-stable (2026 gap +9.0pp vs +9.4pp history), so the change is not corroborated cross-station and D was not reached. Practical guidance: downstream label-noise handling may rely on NYC's asymmetry as published; CHI's current-day asymmetry must be treated as unknown (its 2026 season-matched gap is +0.0pp [−8.5, +8.6]). H0011/H0013's pooled conclusions are unaffected per the frozen interpretation. Retry with full-2026 windows (FY(2026) vs FY(2023–2025), both stations, new pre-registered entry) on or after **2027-01-15**; suggested non-decisional preparation: an operational inspection of CHI 2026 raw CLI product text (`raw_api_payloads`) to sharpen the retry's operational indicators.

### H0015 — The late-maximum channel explains the tmax revision-rate structure

- **Status:** Inconclusive (blocked at G4; successor H0015b required)
- **Opened:** 2026-07-22
- **Closed:** 2026-07-22
- **Related:** E0001 (`docs/research/investigations/2026-07-22-e0001-chi-2026-mechanism.md`, the exploratory source — its data may not be reused decisionally), H0014 (whose outcome-B retry this complements), H0011/H0013 (the asymmetry under mechanism study)
- **Pre-registration package:** `docs/research/preregistrations/PREREG-20260722-H0015-late-max-mechanism.md` (frozen 2026-07-22, before any late-max fraction, conditional rate, or timing↔revision join was computed for any station). Inputs pinned: issuance frame `exp-20260722-h0013-replication` (`5ccf5b7a…`) + occurrence extract `exp-20260722-h0015-occurrence` (`9bf01d8f…`, built by the productionized `weather/cli_products.py` parser). Evaluation cohort: DEN + LAX (station-disjoint from all generating data — full-year 2026 does not yet exist); CHI/NYC are prespecified non-decisional reference cohorts.

**Hypothesis.** Preliminary→final tmax revisions are driven almost entirely by days whose daily maximum occurs at/after the CLI preliminary's 4 PM local cutoff: (a) P(revised | maximum before 16:00 local) is below 10% at every station-year; (b) cross-year changes in a station's tmax revision rate are explained by changes in its late-maximum day fraction — the composition-identity prediction (late-fraction × station-pooled conditional rates) tracks the observed revision rate within pre-registered tolerance; (c) conditional conversion within the late stratum is year-stable once occurrence depth (hour of maximum) is controlled.

**Rationale.** E0001 (exploratory) found: revisions are 262/265 upward across all station-years; P(revised | late max) = 34–53% vs P(revised | early max) = 2–7% in all 8 CHI/NYC station-years; and CHI's 2026 revision elevation coincides with its late-max fraction jumping to 38.2% (vs 24.6–29.6% prior) with every operational axis quiet. If the late-maximum channel is the mechanism, revision risk becomes *predictable from occurrence-time composition* — directly improving label-noise handling for KXHIGH-family markets and explaining H0014's outcome B as weather-composition, not process change.

**Required data.** Occurrence-time parsing productionized as a validated pipeline feature (E0001's parser is exploratory-only; ≥96% coverage demonstrated, ~3.5% 2026 residue must be handled); evaluation data disjoint from E0001's: full-2026 windows once complete (≥ 2027-01-15) and/or DEN/LAX as unseen stations (different cadence regimes — DEN's 3.86 issuances/day makes the preliminary-cutoff structure itself different there, which is part of the test). Dataset version pinned at pre-registration.

**Experiment design.** Frozen in the pre-registration package above: per-station cutoffs by the frozen modal-AS-OF procedure (structurally CHI 16:00, NYC 16:00, DEN 06:00, LAX 17:00 — DEN's early-morning preliminary makes the structural prediction sharp: nearly all its maxima should be late → high revision rate, while LAX's 17:00 cutoff predicts the opposite); per evaluation station the separation `p_late − p_early` (Newcombe 95%), early-floor (Wilson upper < 0.15), attributable fraction ≥ 0.50, plus four-station rank agreement of revision rates with late-max fractions; decision table 0 blocked / 1 any inversion → D / 2 both stations full-pass + rank agreement → A / 3 any positive separation → B / 4 both straddle → C.

**Metrics.** Conditional revision rates per stratum with Wilson 95% CIs; composition-identity prediction error per station-year; occurrence-depth profile stability.

**Required features.** The occurrence-time feature (new, small platform work — flagged in E0001 §5.4).

**Statistical tests.** Wilson/Newcombe intervals per program machinery; no model fitting.

**Failure modes.** (1) Reusing generating data → the design's evaluation-data restriction. (2) Parser residue biasing strata → coverage gate. (3) DEN/LAX cadence differences confounding the cutoff structure → per-station cutoff derivation frozen from their own products' AS-OF lines before any outcome is seen.

**Difficulty.** Small-to-Moderate (one parser feature + rate arithmetic).

**Dependencies.** Occurrence-time parser productionization; for the full-2026 arm, calendar (≥ 2027-01-15).

**Results.** Run 2026-07-22 — [EXP-20260722-H0015-late-max-mechanism](docs/research/experiments/EXP-20260722-H0015-late-max-mechanism.md), inputs hash-verified (issuance `5ccf5b7a…`, occurrence extract `9bf01d8f…`; analysis commit `a4ac3db`). **EXECUTION BLOCKED at gate G4 (step 0)** — no interval, criterion, or A/B/C/D verdict was computed, per the frozen table. Integrity/quality gates all passed (parse-ok 97.5–99.1%; AS-OF modal share 96–99.9%; product-vs-frame value agreement 100.0%). G4 failed at LAX: 21 late-stratum days (floor 30) and 15 total revised days (floor 50) in ~3.5 years — its daily maximum essentially never occurs at/after its own 17:00 cutoff and it almost never revises (≈1.2%). DEN passed G4 with extreme structure: ~93% late-max composition, ~91% revised days — the pre-registration's structural prediction (early 06:00 cutoff → near-universal revisions) qualitatively visible in the gate counts. Gate facts only; nothing confirmatory. Blocked reruns byte-identical; `config_matches_prereg` true.

**Conclusion.** **Inconclusive — blocked by design** (the pre-registered step-0 outcome; PREREG §8 explicitly named DEN's early stratum and LAX's late stratum as the G4 risks). The stratum-separation criteria inherited from the CHI/NYC mid-composition regime are unevaluable at the composition extremes the evaluation stations turned out to occupy (≈1.7% and ≈93% late-max), where the mechanism's testable content is the aggregate composition-implied prediction, not the within-station contrast. The published gate counts are directionally consistent with the composition mechanism at both poles, and per the frozen rules nothing sharper may be claimed. Follow-up: a successor pre-registration (H0015b) with criteria valid across the full composition range (exact-binomial handling for thin cells; composition-implied prediction error; cross-station ordering), obligated to disclose this blocked run's published gate values as post-hoc knowledge, with the full-2026 time-disjoint window (≥ 2027-01-15) as its clean arm — see `docs/research/postmortems/2026-07-22-h0015-closeout.md`.

### H0017 — Near-settlement prices misprice known revision risk (preliminary-CLI event study)

- **Status:** Rejected (outcome EFFICIENT-WITHIN-MARGIN — a decisive equivalence null)
- **Opened:** 2026-07-22
- **Closed:** 2026-07-22
- **Related:** H0002/H0011/H0013 (the revision-risk facts), H0007 (rejected bound-violation mispricing — the locked side; reused here only as a negative-control concept), H0012/E-A (payout-validated labels), 2026-07-22 roadmap P3
- **Pre-registration package:** `docs/research/preregistrations/PREREG-20260722-H0017-revision-risk-pricing.md` (frozen 2026-07-22, before any price-vs-outcome quantity was computed). Inputs pinned: market dataset `exp-20260722-h0017-market` (market_prices `f3641322…`, settlement_labels `49bc34df…`) + issuance frame `exp-20260722-h0013-replication` (`5ccf5b7a…`).

**Hypothesis.** After the preliminary CLI issues (~16:35 ET), prices of *at-risk* contracts — those whose YES requires the settlement label to move beyond the preliminary value in the physically possible direction (up for tmax, down for tmin) — are miscalibrated against realized qualifying-revision frequency: the mean post-event implied probability differs from the realized YES rate by more than the frozen margin. Directional alternative from anchoring: underpricing (traders treat the preliminary as final). The efficient-market null: calibration within ±7.5pp.

**Rationale.** The program has proven revision risk is real, directional, and material (~13–26% of days revise, upward-only for tmax); H0007 showed markets do not misprice *locked* outcomes. Whether they price the *live* revision probability is the sharpest efficiency question the archive can answer today, and the first genuine edge test: confirmed underpricing is directly actionable; a well-powered null is the honest headline that near-settlement weather pricing is efficient.

**Required data.** Only point-in-time archives, all pinned: 1-minute candles with bid/ask quotes and strike structure joined to payout-validated settlement labels (`exp-20260722-h0017-market`); archived preliminary CLI values and publication timestamps (issuance frame). Occurrence-time data is **deliberately excluded** — it derives from the final product and is not knowable at event time. Cohort: NYC only (KXHIGHNY/KXLOWTNYC; the only series with candle history — CHI/DEN/LAX excluded for coverage, named in the prereg).

**Experiment design.** Frozen in the pre-registration: event time = archived first-CLI publication timestamp per date; windows PRE [E−90m, E−10m], POST [E+15m, E+120m], placebo at E−240m; price = bid/ask midpoint of the last fully-quoted candle in-window; per variable-day one *adjacent at-risk* contract (YES region nearest beyond the preliminary) and one *adjacent locked-NO* control; primary estimand D = mean(post-event implied probability − realized YES) with a deterministic normal-approximation 95% CI; decision table: L(D)>0 → OVERPRICED; U(D)<0 → UNDERPRICED; CI within ±0.075 → EFFICIENT-WITHIN-MARGIN; else INCONCLUSIVE; gates on hashes, strike-semantics agreement ≥99%, N ≥ 80, events ≥ 5, quote coverage ≥ 50%.

**Metrics.** D with 95% CI; per-variable and per-distance splits, locked-NO control, placebo-time reaction, pre-window calibration, liquidity descriptives — all secondary/descriptive with the single pooled primary carrying the decision.

**Required features.** None new — the pinned frames carry everything.

**Statistical tests.** Deterministic normal-approximation CI on a mean of bounded calibration residuals (z frozen literal); Wilson CIs on descriptive rates; exact Decimal arithmetic; single primary cell (no multiplicity correction needed, declared).

**Failure modes.** (1) Thin quoting → missing post-window prices (excluded-and-counted, coverage gate, extended-window sensitivity). (2) Few qualifying events at n≈100–130 → only gross miscalibration (≳6pp) detectable; the margin and INCONCLUSIVE branch encode this honestly. (3) Strike-semantics error → G3 agreement gate against realized payouts. (4) Same-day tmax/tmin dependence → disclosed; near-independent physically (concordance data). (5) Two-month single-station window → conclusions dated and NYC-scoped.

**Difficulty.** Moderate.

**Dependencies.** None; runnable now on archives.

**Results.** Run 2026-07-22 — [EXP-20260722-H0017-revision-risk-pricing](docs/research/experiments/EXP-20260722-H0017-revision-risk-pricing.md), all three input hashes re-verified at G1; analysis commit `512d248`; reruns byte-identical. All gates passed: strike-semantics agreement **792/792** vs realized payouts; event-time presence 100%; N = 122 adjacent at-risk variable-days, quote coverage 96.8%, 9 qualifying revisions. **Primary: D = +2.33pp, 95% CI [−1.85, +6.50]pp** (mean implied 9.7¢ vs realized 7.4%) — within the frozen ±7.5pp margin on both sides → **EFFICIENT-WITHIN-MARGIN** (step 3). Controls coherent: locked-NO contracts at 0.5¢ mean; placebo-window reaction (9.3¢) exceeds event reaction (6.0¢) — the preliminary confirms rather than surprises, and the event-study path shows impossible buckets priced to ~0 *before* official publication; per-variable D = +0.5pp (tmax) / +4.1pp (tmin — a slight premium on the riskier variable, not neglect); sensitivities stable (+2.4 to +2.8pp); the 12h-extension artifact (stale end-of-life quotes) documented as a measurement caution. Figures + narrative: `docs/research/postmortems/2026-07-22-h0017-closeout.md`.

**Conclusion.** **Rejected** — and the rejection is the finding: near-settlement NYC weather prices incorporate the program's proven revision-risk knowledge to within economic materiality (an equivalence claim at ±7.5pp, not mere absence of evidence). Combined with H0007's locked-side null, the near-settlement zone is efficiently priced; there is no Phase-6 case from this channel on this evidence. The remaining edge candidates are the forecast-side questions (H0003 → M-01 → H0006), which concern genuinely probabilistic horizons rather than the near-settlement zone traders demonstrably handle well. Dated (2026-05→07, NYC): scheduled re-run ≥ late Sept 2026 with a ~3× cohort and possibly DEN/LAX as their candle history accrues.

### H0018 — Weather + contract features improve on the Kalshi market probability (PRELIMINARY baseline)

- **Status:** Closed 2026-07-26 — **NOT SUPPORTED** (coverage-blocked held-out test; descriptive in-sample null). See Results below.
- **Opened:** 2026-07-26
- **Related:** ADR 0014 (production cutover — the data source), ADR 0013 (provenance), `dataset/provenance.py` (canonical dataset), H0005 (null: daily-temp prices calibrated within costs — the market-benchmark ancestor)
- **Dataset version:** frozen canonical decision-grain build `EXP-20260726-H0018` (git commit + DB revision + hashes recorded in its manifest); production-only liquidity policy, though **no liquidity feature is used here**.

**Hypothesis.** A probability model using point-in-time weather forecasts, observations, contract thresholds, station/location, and time-to-resolution can improve out-of-sample probability calibration (Brier score) or predictive loss relative to the contemporaneous Kalshi market-implied probability, for settled weather markets, at a fixed 24-hour-before-close decision horizon.

**Claim reduction (pre-specified, before any modeling).** The verified-production data supports only **~168 event-days** (station×date×variable) across 72 dates (2026-05-15→07-25), of ~1,008 threshold markets, and is **heavily Denver-concentrated (≈86%)**. This is too thin and too dependent for a confirmatory held-out test. **This is registered as a preliminary baseline study**, not a confirmatory test: the primary comparison is reported with an event-day-grouped bootstrap CI, and no confirmatory "edge" claim may be made regardless of the point estimate. The strongest allowed positive verdict is `SUPPORTED BUT INCONCLUSIVE`.

**Primary outcome metric.** Test-set **Brier score** of the market+weather model minus the raw market benchmark, on identical test rows (lower is better; negative difference = improvement).

**Secondary metrics.** Log loss; calibration intercept & slope; reliability curve; expected calibration error (ECE); mean predicted vs actual positive rate; sample & event-day counts; per-station and per-variable Brier; Brier vs raw market per model.

**Market benchmark.** Contemporaneous **production candlestick close price** (in probability = cents/100) at the latest candle whose `period_end ≤ decision_time` (decision_time = close − 24h). Order-book/last-trade prices are not used (thin verified coverage). Prices clipped to [0.02, 0.98] for scoring stability (fixed, pre-registered). Raw (uncalibrated) market probability is the benchmark; a separately-calibrated market is Model 4. No fees applied (probability scoring only). No later/final price used.

**Eligible markets.** Weather-related Kalshi contract; deterministic settlement interpretation mapping to a station with weather data (CHI/DEN/LAX/NYC); valid final binary label (`kalshi_result ∈ {yes,no}`); reliable close_time; a production candle price at ≤ decision_time; a point-in-time forecast **and** observation available at ≤ decision_time. Excluded otherwise, with recorded reason. No liquidity fields required or used.

**Decision horizon.** 24 hours before `close_time`, chosen from coverage (996/1008 have a ≤24h-price) before any performance was evaluated. One primary row per market.

**Splits (chronological, by target_date).** Train = target_date ≤ 2026-06-30; Validation/calibration = 2026-07-01 → 2026-07-12; Test = 2026-07-13 → 2026-07-25. Boundaries frozen here, before test evaluation. Whole event-families (station×date) never span splits (split is by date). Grouped by (station, target_date) for bootstrap.

**Feature groups.** Contract (threshold/strike, direction, station, time-to-close, variable); forecast (value, issue time, age, forecast−threshold gap, missing flags); observation (latest value at decision, age, observation−threshold gap, missing flags). Market-implied probability is a feature only in Model 3. **Excluded:** trades, volume, OI, order-book depth, spread, imbalance, demo/NULL liquidity, any post-decision/settlement info.

**Models.** M0 constant base rate (train positive rate); M1 raw market probability; M2 weather-only logistic regression; M3 market+weather logistic regression; M4 Platt-calibrated market (calibration fit on train+val only). Logistic regression fit in numpy (IRLS), L2-regularized, standardized features. No neural nets, no hyperparameter search.

**Calibration procedure.** Platt (logistic) calibration fit on train+validation only, never on test. Isotonic not used (sample too small). Probability clip [0.02, 0.98] fixed.

**Statistical comparison.** Paired per-row Brier difference vs raw market; mean difference; **grouped bootstrap at the (station, target_date) event level**, 2,000 resamples, seed 20260726; 95% percentile CI. Related threshold markets are NOT treated as independent.

**Success threshold (preliminary).** Point-estimate Brier improvement of M3 vs M1 with a 95% grouped-bootstrap CI **entirely below 0** AND no major subgroup sign reversal → `SUPPORTED BUT INCONCLUSIVE` (the max positive verdict here; a confirmatory `CONFIRMED` is not attainable at this N and is deferred to a replication on future untouched data).

**Failure threshold.** CI spanning 0 or a non-negative point estimate → `NOT SUPPORTED`. Any leakage/integrity/timestamp/hash failure → `INVALID`.

**Decision rule.** Evaluate M0–M4 on validation; select nothing on test; report M3−M1 test Brier with grouped-bootstrap CI; apply the thresholds above once, without redefinition.

**Known limitations.** Tiny, Denver-dominated, short (<3 months) sample; single horizon; candlestick-close benchmark (not mid); weather forecast/observation sparsity; no liquidity/execution modeling → **not a tradable-edge claim** under any outcome.

**Results (H0018).** Run 2026-07-26 — [EXP-20260726-H0018](docs/research/experiments/EXP-20260726-H0018/EXP-20260726-H0018.md), deterministic (`grain_sha256` re-run-identical). **The pre-registered held-out test was NOT evaluable:** at the 24h horizon the as-of forecast join leaves only 162 markets / 14 event-days (2026-07-21→07-25) — 840 markets excluded for no forecast at the decision time — all inside the pre-registered test window, so train/validation are empty. Per the claim-reduction clause, a **descriptive in-sample** fit (biased toward the weather model) was reported: raw market Brier 0.1106 (well-calibrated, slope 1.13, ECE 0.052); weather-only 0.1327 (worse); market+weather 0.1108 ≈ market; **M3−M1 Brier +0.0002, event-grouped 95% CI [−0.0089, +0.0087] spanning 0**; subgroups sign-inconsistent (DEN −0.016 vs LAX +0.011). Leakage checks all passed; no liquidity feature used.

**Conclusion (H0018).** **NOT SUPPORTED — market benchmark not beaten.** Even the generous in-sample look shows weather adds nothing to the well-calibrated market, and the pre-registered held-out comparison was blocked by forecast-history length (the binding limitation, not a modeling failure). No tradable claim (probability scoring only). **Follow-up:** accumulate point-in-time forecast coverage, then re-run the *unchanged* pre-registered experiment once a real chronological train/test split is populated (the "inconclusive/accumulate-data" branch), before testing any richer model. *Clarification (2026-07-26): H0018 is closed and immutable — its metrics, splits, frozen dataset, and verdict must not change. Because reliable forecast-vintage collection began 2026-07-21 (after H0018's historical train/val windows) and missed vintages are unrecoverable, the "re-run" is a NEW experiment with FUTURE windows and a new identifier — see H0019 below, not a re-dated H0018.*

### H0019 — Future held-out replication of H0018 (weather vs market, prospective)

- **Status:** Registered 2026-07-26 — **accumulating prospective data** (readiness state `NOT_READY`). Do NOT run the final test until the readiness command reports `READY_FOR_FINAL_TEST`.
- **Opened:** 2026-07-26
- **Related:** H0018 (closed, immutable — this is a NEW identifier because future data cannot repair H0018's historical train/val windows; missed forecast vintages are unavailable). Registration package: `docs/research/experiments/EXP-FUTURE-H0019/registration.md`.

**Hypothesis.** At a fixed 24-hour pre-close decision horizon, does adding point-in-time weather and contract information improve **out-of-sample Brier score** relative to the contemporaneous Kalshi market-implied probability, on a genuinely prospective, chronologically-held-out test?

**Why a new experiment.** H0018's held-out test was not evaluable: reliable all-station forecast-vintage collection began 2026-07-21, after H0018's pre-registered train/val windows. Those windows cannot be repaired retroactively. H0019 fixes **future** windows anchored at the forecast-archive start, so all feature histories are collected prospectively.

**Frozen future windows (chronological, by target_date; all future at registration — today is 2026-07-26):** Train **2026-07-22 → 2026-08-04**; Validation **2026-08-05 → 2026-08-11**; Test **2026-08-12 → 2026-08-25**. Train < Val < Test with no overlap; the test interval is future and untouched. Sized for the observed ~4 complete event-groups/day.

**Independent grouping.** The inference unit is the **event-group = (station, weather variable, target_date)**. Multiple threshold markets on the same underlying outcome are NOT independent; all grouped inference (event-grouped bootstrap) and all split assignment use this unit. Because splits are by target_date and the group contains target_date, **no event-group can cross a split** (proven by test).

**Minimum readiness (operational minimums, NOT a power analysis), in event-groups:** train ≥ 40, val ≥ 15, test ≥ 25; ≥ 3 stations represented in test; ≥ 4 test event-groups per represented station; **no single station > 40% of test event-groups**; ≥ 5 positive and ≥ 5 negative labels in train and in test; window-scoped completion coverage ≥ 0.85; ≥ 25 grouped-bootstrap units. These gate execution; they are conservative operational minimums, not proof of statistical power (the sample will remain small).

**Frozen model & feature spec (identical to H0018 unless changed & documented before any H0019 test data is inspected).** Models M0 base rate, M1 raw market, M2 weather-only logistic, M3 market+weather logistic, M4 validation-calibrated market (Platt). Features: contract (threshold, direction, station, variable, time-to-close) + point-in-time forecast (value, gap, age, missing flags) + observation (value, gap, age, missing flags). **No liquidity features.** Standardized inputs; L2=1.0; probability clip [0.02, 0.98]; Platt calibration fit on train+val only; seed 20260726; 2000 grouped-bootstrap resamples; primary comparison M3−M1 Brier on test with event-grouped 95% CI. No nonlinear model (H0018 being negative is not grounds to add complexity).

**Decision rule (pre-registered).** `CONFIRMED` only if M3−M1 test Brier 95% grouped-bootstrap CI is entirely below 0 AND no major station-subgroup sign reversal AND station-concentration requirement holds; `SUPPORTED BUT INCONCLUSIVE` if the point estimate improves but the CI spans 0 or coverage/concentration is marginal; `NOT SUPPORTED` if no improvement; `INVALID` on any leakage/hash/timestamp failure. Subgroup analysis is secondary. No tradable/efficiency claim under any outcome (probability scoring only; no execution/fees/liquidity).

**Anti-peeking.** The `experiment readiness h0019` command is read-only: it counts coverage only and never trains a model, generates a test prediction, or computes a test Brier score. The final test is a separate explicit command that verifies all readiness conditions first. Each readiness invocation is logged (state only, no outcome analysis).

**Known limitations.** Small even when ready (~4 event-groups/day); 4 stations only; single horizon; candlestick-close benchmark; short seasonal span; not a tradable-edge test.

### H0020 — Forecast-revision underreaction at close minus 24 hours

- **Status:** Registered 2026-07-27 — **REGISTERED_NOT_READY** (accumulating prospective data). Do NOT run the final test until the readiness command reports readiness.
- **Opened:** 2026-07-27
- **Related:** H0019 (materially distinct: H0020 tests forecast *dynamics* conditional on the price; H0019 tests forecast *levels* against the price. H0019's registered test window 2026-08-12→08-25 is **excluded from H0020 entirely**), H0018 (closed lineage), H0008 (this formalizes its proposed mechanism). Registration package: `docs/research/experiments/EXP-FUTURE-H0020/registration.md` + `config.json`; frozen executable spec `src/kalshi_weather/experiments/h0020.py`; ledger `docs/research/ledger.jsonl`.

**Hypothesis.** At market close minus 24 hours, a model using market-implied probability plus the most recent point-in-time NWS forecast revision improves Brier score over market-implied probability alone for eligible single-sided daily high and low temperature threshold markets at CHI, DEN, LAX, and NYC. Mechanism: prices may adjust incompletely or slowly to newly arriving forecast revisions (limited attention, thin participation). Falsification: no improvement under the frozen success rule ⇒ NOT SUPPORTED, final.

**Frozen design (full detail in the registration package).** Decision at close−24h; forecast availability timestamp is **`observed_at` (ingestion), never `issue_time`** (measured delays p50 46m/p99 ~9.6h make issue-time joins forward-looking; any issue_time-as-availability use ⇒ INVALID). Primary revision `revision_24h = F_latest − F_prev` (°F, + = warming; F_latest = latest available issue, staleness ≤ 30h; F_prev separated by [60min, 30h] of issue time; missing prior ⇒ excluded with reason, never imputed; unchanged reissue ⇒ explicit 0, control stratum). Single directional feature `rev_dir = revision_24h × threshold_side_sign` (+1 = YES pays above). Benchmark M1 = market probability (clip [0.02, 0.98]); model MR = L2 logistic (L2=1.0, IRLS) on exactly [logit(market_prob), rev_dir] — no other features, no station effects, no feature selection, no extra calibration. Seed 20260909; 2000 event-grouped bootstrap resamples.

**Frozen windows (by station-local target_date).** Train 2026-07-21→08-11; **excluded gap 08-12→08-25 (H0019's test — used for nothing)**; validation 08-26→09-08; test 09-09→09-22. Grouping unit (station, variable, target_date); no group crosses a split. Deterministic extension rule: if readiness gates unmet at 09-22, extend by exactly 7 days, at most twice (absolute end 2026-10-06); still unmet ⇒ `DEFERRED_INSUFFICIENT_DATA`, no run, no replacement window.

**Readiness gates (counts and integrity only — never predictive).** Test window elapsed; ≥25 eligible test event-groups; ≥3 stations; ≥5 positive and ≥5 negative test labels; station concentration ≤0.40; ≥25 bootstrap units; frozen hashes valid; no group leakage; observed_at availability verified; exclusions (incl. missing-prior and collection-gap) reported by split; provenance recorded.

**Decision rule (pre-registered).** `SUPPORTED` only if gates passed before test execution AND mean Brier(MR)−Brier(M1) < 0 AND the 95% grouped-bootstrap CI is entirely below 0 AND no station has a positive mean difference. `NOT SUPPORTED` if the point estimate is non-negative, the CI touches/crosses 0, or any station reverses sign — final: no rerun, no changed window, no feature addition, no reinterpretation. `INVALID` on leakage/hash mismatch/pre-readiness test access/issue_time availability/split failure/non-reproducibility. A SUPPORTED result authorizes only a separate untouched replication registration — not paper or live trading. Secondary diagnostics (log loss, reliability, ECE, calibration slope/intercept, per-station, revision strata, warming/cooling, clip/L2/separation sensitivity) are exploratory and cannot alter the verdict.

**Known limitations.** ~3-week training span (archive starts 2026-07-21) including known collection gaps; 4 stations; single horizon and single primary revision definition; candlestick-close benchmark; laptop-offline gaps thin the revision-eligible sample (≈76% prior coverage in the worst observed week); not a tradable-edge test.
