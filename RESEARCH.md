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

**Where this is logged (convention established 2026-07-21):** experiments live in `docs/research/experiments/`, one markdown file per experiment run, named `EXP-<YYYYMMDD>-<hypothesis-id>-<slug>.md` (e.g. `EXP-20260901-H0002-cli-revisions.md`). Each record contains, in order: hypothesis ID; dataset version(s) — meaning the Milestone 4 export's `manifest.json` `version` **and** its per-frame `content_hashes` (the version label alone is not sufficient; the hashes are what make a rebuild verifiable); the exact configuration (copied inline, not referenced); the git commit; the pre-registered metrics computed exactly as the hypothesis specified; and the date run. The corresponding `HYPOTHESES.md` entry's Results section links to the record. Research plans (what to test and in what order) are separate dated documents in `docs/research/` (e.g. `2026-07-21-research-plan.md`) — a plan is superseded by writing a new dated plan, never by rewriting an old one after results are known. An experiment result without these fields attached is not usable as evidence for or against a hypothesis.

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
- Settlement outcomes are never available before `settlement_time`; a backtest may not use them earlier. Symmetrically, the outcome *label* itself must be the settlement-time value: market experiments score against `value_at_settlement` (the value Kalshi actually paid on, `docs/adr/0006-settlement-labels.md`), never the latest-final CLI value, which can include corrections published after settlement that no market participant ever saw.
- When in doubt, ask: "could the live system, running at this exact historical instant with only the data it had ingested by then, have produced this input?" If not, it's leakage.

## Avoiding data leakage

Leakage is broader than look-ahead bias — it includes any channel by which information not legitimately available influences a result:

- **Feature leakage**: a feature computed using data derived from the label (e.g., a "forecast error" feature computed using the actual observed temperature for the same day being predicted).
- **Train/test contamination**: the same date, station, or event appearing in both a fitting window and its evaluation window (walk-forward validation avoids this by construction, ad hoc splits often don't).
- **Cross-sectional leakage**: fitting a global normalization or scaling using statistics computed over the full dataset (including future rows) rather than only past-available rows.
- **Survivorship**: only including markets/series that existed for their full lifecycle, silently dropping markets that were delisted or never resolved.

Milestone 4 ("Research dataset") includes explicit leakage tests; a new feature or join added later should extend those tests, not skip them.

## Standard experiment protocol

Every experiment, regardless of hypothesis, follows the same sequence. Steps 1-4 happen **before** any result is computed; this ordering is the p-hacking defense, not bureaucracy.

1. **Pre-register.** The `HYPOTHESES.md` entry is complete through Metrics/Statistical tests/Failure modes, including the numeric confirm/reject decision rule, *before* the experiment code runs against real data. The entry is the registration — evaluation cells, event filters, horizon bands, and comparison counts named there are the only ones that can support a "confirmed."
2. **Pin the dataset.** Build (or reuse) a versioned Milestone 4 export; record its manifest version + content hashes in the experiment record. If the experiment needs data the datasets don't carry, that's a platform gap to raise, not an excuse for an ad hoc query whose provenance can't be pinned.
3. **Separate train from test temporally, always.** Random shuffles/splits are prohibited on this data. Fitting uses only data strictly before the evaluation window (walk-forward, expanding or rolling, per the section above), with a one-day embargo between fit and evaluation windows to keep same-day information (a forecast and its own outcome; overlapping market snapshots) from straddling the boundary.
4. **State the baseline.** Every model-flavored claim is relative: to climatology (H0003), to the unconditioned model (H0004), to the market itself (H0006). An experiment without a named baseline measures nothing.
5. **Run once, per the design.** Exploratory analysis is fine and encouraged — in a scratch notebook, labelled exploratory, and *its findings become a new hypothesis entry*, never a silent edit to the current one's decision rule.
6. **Report uncertainty, not points.** Every headline metric carries a 95% CI — bootstrap by default, **clustered at the event-day level** for anything involving market snapshots (snapshots of one market-day are one observation's worth of independence, not fifty). Sample sizes (N days, N events, N markets — not N rows) appear next to every number.
7. **Multiple comparisons are declared and paid for.** Scanning cells (stations × horizons × buckets) requires either pre-registering the one primary cell (others labelled exploratory), a family-wise correction (Bonferroni over the declared family), or both. Additionally, any "confirmed" that emerged from a family of comparisons must replicate on a temporally held-out later slice before the status changes — replication is the correction that actually matters.
8. **Record and conclude.** Write the experiment record (convention above), update the hypothesis's Results/Conclusion against the pre-registered rule verbatim, and set Status. Inconclusive-for-sample-size states the sample it was short of and the date to retry — it is not a soft "confirmed."
9. **Date every conclusion.** Market-behavior conclusions decay; a confirmed market hypothesis (H0005-H0010 family) carries a re-run date (default: +2 accumulation months). Weather-physics conclusions (H0002-H0004 family) are more durable but still name the data window they cover.

## Relationship to other documents

- `HYPOTHESES.md` — the process and template every strategy/model idea goes through; this document is the methodology that process relies on.
- `STRATEGY_SPEC.md` — a candidate strategy template; any implementation of it must follow the evaluation methodology here.
- `DATA_MODEL.md` — the concrete schema (issue time, raw payload immutability) that makes the look-ahead/leakage rules above enforceable.
- `TASKS.md` / `ROADMAP.md` — where each piece of this methodology gets implemented in code (dataset manifests, calibration reports, walk-forward evaluation).

## Pre-freeze registration gate (mandatory from 2026-07-27)

Before any new hypothesis's config/registration is frozen and hashed into
the research ledger, ALL of the following must pass (2026-07-27 lineage
audit; ADR record in the audit report):

1. `uv run kalshi-weather research leakage-audit` — static latest-state
   scan + production temporal invariants must return PASS (or every
   warning individually dispositioned in the registration).
2. **Availability-contract declaration** — the registration must state
   `publication` (issue_time/issuance_time; market-efficiency claims) or
   `ingestion` (observed_at; tradability claims). Mixing fields across
   contracts is a leakage-lint ERROR (rule R003).
3. **Close-time stability guard** — whenever decision_time derives from
   close_time, run the close-time revision guard over the design's scope
   (generic: `research/close_time_guard.py`; H0019's instance:
   `uv run kalshi-weather experiment preflight h0019-close-time`).
   REVIEW_REQUIRED means a human scientific decision BEFORE freezing —
   the guard never selects a close_time.
4. **Grouped partition isolation** — splits built via the workbench's
   group-atomic splitters, leakage assertion re-run on the final frames.
5. **Provenance completeness** — every input row type traceable to raw
   payload / collector lineage / manifest / commit / config hash
   (leakage-lint R009).
6. Only after 1–5: freeze config + registration, hash them, append the
   ledger record.

For already-frozen experiments the gate applies at the **final-run
preflight** instead (e.g. H0019's close-time preflight before its single
test execution) — frozen specifications are never edited retroactively.
