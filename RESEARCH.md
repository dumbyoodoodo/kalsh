# Research Methodology

This document describes how research is conducted in this project: what reproducibility means concretely, how datasets and experiments are versioned and tracked, how models and strategies are evaluated, and how look-ahead bias and data leakage are avoided. It applies from Phase 3 (Research Framework) onward — see `ROADMAP.md` for phases and `HYPOTHESES.md` for the process that gates any new strategy or model idea.

## Research philosophy

- **The default outcome of a hypothesis is failure.** Most edges people imagine do not survive contact with real data, costs, and out-of-sample testing. Treat a rejected hypothesis as a normal, successful unit of research output — it removes a wrong idea from consideration and should be recorded, not deleted or quietly abandoned.
- **Evidence accumulates before complexity does.** A simple, well-validated baseline beats a complex, poorly-validated model. Model and feature complexity are added only when a documented comparison shows a simpler alternative is insufficient (see "Model comparison" below).
- **Calibration before P&L.** A backtest can show a profit by accident (small sample, favorable regime, subtle leakage) far more easily than a model can be well-calibrated by accident. Calibration is the primary signal; P&L is a secondary, noisier confirmation.
- **Negative and null results are written down.** If a hypothesis is rejected, that conclusion belongs in `HYPOTHESES.md` with enough detail that nobody re-runs the same failed experiment in six months.

## Reproducibility

A research result is reproducible only if it can be regenerated from three inputs, all recorded alongside the result:

1. **Configuration** — the exact parameters (thresholds, station, date range, model hyperparameters, cost assumptions) used, stored as versioned config, not ad hoc notebook variables.
2. **Dataset version** — the exact dataset manifest (see below) the result was computed against.
3. **Code version** — the git commit (and model/feature version, once those exist) the code ran at.

If any of the three can't be pinned down after the fact, the result is not trustworthy and should not be cited in a hypothesis conclusion. This mirrors the non-negotiable rule in `CLAUDE.md`: "every trading decision must be reproducible from stored inputs, model version, configuration, and timestamp" — research results are held to the same standard *before* anything becomes a trading decision.

## Dataset versioning

Per `TASKS.md` Milestone 4, research datasets are exported as versioned Parquet files with a manifest recording:

- date range covered
- row count
- content hash
- schema version
- the raw sources (and their `raw_api_payloads` rows) each row traces back to

A dataset version is immutable once published — if the underlying join logic or a source correction changes the output, that's a new version, not an in-place edit. Every hypothesis result in `HYPOTHESES.md` must cite the dataset version(s) used.

## Experiment tracking

Every experiment (a specific model, feature set, or strategy configuration evaluated against a specific dataset version) should record, at minimum:

- the hypothesis ID it belongs to (see `HYPOTHESES.md`)
- dataset version(s)
- configuration used
- code/model version
- the metrics defined in that hypothesis's experiment design, computed exactly as specified
- date the experiment was run

Where this is logged (a `docs/research/` directory, a lightweight local tracking tool, or something else) is an implementation decision for Phase 3/4, not fixed by this document — but whatever is chosen, an experiment result without these fields attached is not usable as evidence for or against a hypothesis.

## Evaluation methodology

### Calibration

For probability models, evaluate:

- **Brier score** — overall accuracy of probabilistic predictions.
- **Log loss** — penalizes confident wrong predictions more heavily.
- **Reliability curve / calibration bucket table** — for predictions grouped into probability buckets (e.g. 0-10%, 10-20%, ...), does the realized frequency match the predicted probability? This is the most interpretable and hardest-to-fake calibration check.
- **Sharpness** — among calibrated models, prefer the one whose predictions are more concentrated away from the base rate (more informative), but never trade sharpness for calibration.

A model is not "good" because it has a lower Brier score in one backtest window. Compare across rolling/expanding out-of-sample windows (see "Walk-forward validation") before trusting a difference.

### Statistical significance

- Report sample size next to every metric. A Brier score or hit rate from 20 trades is not evidence of anything.
- Prefer confidence intervals or bootstrap resampling over point estimates for P&L and hit-rate metrics.
- Be explicit about multiple-comparison risk: if 10 threshold/station/horizon combinations are tested and one looks good, that is expected by chance alone roughly 40% of the time at p < 0.05 per test. Correct for this (fewer, pre-registered comparisons; multiple-testing correction; or requiring the result to replicate on a held-out slice) before treating it as a finding.
- A hypothesis's hoped-for outcome and its evaluation metric must be written down *before* looking at results (see `HYPOTHESES.md`). Choosing the metric after seeing which one looks best is p-hacking.

### Walk-forward validation

Time-series data cannot be shuffled and split randomly without leaking the future into the past. Use rolling or expanding walk-forward validation instead:

1. Fit/calibrate on data up to time `T`.
2. Evaluate only on data after `T` (and before the next refit boundary).
3. Advance `T` and repeat.
4. Report the distribution of out-of-sample metrics across windows, not just the average — stability across windows matters as much as the average.

This applies to probability models (Phase 5) and to backtested strategies (Phase 6) alike.

## Avoiding look-ahead bias

Look-ahead bias means using information that would not actually have been available at decision time.

Concrete rules:

- Every forecast row is keyed by **issue time**, not valid time; a decision at time `t` may only use forecasts with issue time `<= t`. This is why `DATA_MODEL.md`'s `weather_forecasts` table says "never overwrite a forecast with a later forecast."
- Market/order-book snapshots used in a backtest must be timestamped at or before the simulated decision time, using the same append-only history the live system would have seen — not a market's final/settled state.
- Settlement outcomes are never available before `settlement_time`; a backtest may not use them earlier.
- When in doubt, ask: "could the live system, running at this exact historical instant with only the data it had ingested by then, have produced this input?" If not, it's leakage.

## Avoiding data leakage

Leakage is broader than look-ahead bias — it includes any channel by which information not legitimately available influences a result:

- **Feature leakage**: a feature computed using data derived from the label (e.g., a "forecast error" feature computed using the actual observed temperature for the same day being predicted).
- **Train/test contamination**: the same date, station, or event appearing in both a fitting window and its evaluation window (walk-forward validation avoids this by construction, ad hoc splits often don't).
- **Cross-sectional leakage**: fitting a global normalization or scaling using statistics computed over the full dataset (including future rows) rather than only past-available rows.
- **Survivorship**: only including markets/series that existed for their full lifecycle, silently dropping markets that were delisted or never resolved.

Milestone 4 ("Research dataset") includes explicit leakage tests; a new feature or join added later should extend those tests, not skip them.

## Relationship to other documents

- `HYPOTHESES.md` — the process and template every strategy/model idea goes through; this document is the methodology that process relies on.
- `STRATEGY_SPEC.md` — a candidate strategy template; any implementation of it must follow the evaluation methodology here.
- `DATA_MODEL.md` — the concrete schema (issue time, raw payload immutability) that makes the look-ahead/leakage rules above enforceable.
- `TASKS.md` / `ROADMAP.md` — where each piece of this methodology gets implemented in code (dataset manifests, calibration reports, walk-forward evaluation).
