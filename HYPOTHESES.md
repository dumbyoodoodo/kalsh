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

- **Status:** Proposed
- **Opened:** 2026-07-21
- **Closed:**
- **Related:** H0001, STRATEGY_SPEC.md (baseline model), ROADMAP.md 0.5

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

**Results.** Not yet run.

**Conclusion.** Not yet concluded.

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

- **Status:** Proposed
- **Opened:** 2026-07-21
- **Closed:**
- **Related:** H0006, RESEARCH.md (calibration methodology)

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

**Results.** Not yet run.

**Conclusion.** Not yet concluded.

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

- **Status:** Proposed
- **Opened:** 2026-07-21
- **Closed:**
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

**Results.** Not yet run.

**Conclusion.** Not yet concluded.

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

- **Status:** Proposed
- **Opened:** 2026-07-21
- **Closed:**
- **Related:** H0002 (source of the exploratory observation), H0005/H0006/H0007 (consumers)

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

**Results.** Not yet run.

**Conclusion.** Not yet concluded.

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
