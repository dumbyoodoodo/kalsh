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

**Results.** Not yet run — Phase 2 (Historical Data Platform) has not started.

**Conclusion.** Not yet concluded.
