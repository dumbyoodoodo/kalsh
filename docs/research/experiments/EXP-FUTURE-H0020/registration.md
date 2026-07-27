# EXP-FUTURE-H0020 — registration (frozen 2026-07-27)

**Status: REGISTERED_NOT_READY.** Do not run the final test until the
readiness command reports readiness. Do not compute validation or test
performance before then. `config.json` in this directory is the frozen
machine-readable spec; `src/kalshi_weather/experiments/h0020.py` is its
frozen executable half (revision rule, windows, extension rule, gates).
This document is the narrative registration; on any perceived conflict,
`config.json` governs and the conflict must be recorded in HYPOTHESES.md
**before** any test access.

## Hypothesis (frozen)

At market close minus 24 hours, a model using market-implied probability
plus the most recent point-in-time NWS forecast revision improves Brier
score over market-implied probability alone for eligible single-sided
daily high and low temperature threshold markets at CHI, DEN, LAX, and
NYC.

**Mechanism.** Weather-market prices may adjust incompletely or slowly to
newly arriving forecast revisions because of limited attention and thin
participation. The information tested is the forecast **change**
conditional on the price — orthogonal to H0018/H0019, which test forecast
**levels** against the price.

**Falsification.** If the revision-augmented model does not improve
untouched test Brier under the frozen success rule, H0020 is NOT
SUPPORTED and final.

## Why the availability timestamp is `observed_at` (binding)

Measured ingestion delays for the NWS forecast archive are p50 46 min,
p90 107 min, p99 ≈ 9.6 h. A join keyed on NWS's claimed `issue_time`
would therefore hand the model forecasts up to hours before this system
could actually have known them. All availability comparisons use the
ingestion timestamp `observed_at`; `issue_time` orders versions among
already-available rows only. **Any use of `issue_time` as availability
renders the experiment INVALID.**

## Revision definition (frozen — one primary definition, no alternatives)

`revision_24h = F_latest − F_prev` in °F, positive = warming, matched on
(source=nws, station_id, variable, station-local target date, matching
high/low forecast period):

- **F_latest**: greatest `issue_time` among rows with `observed_at` ≤
  decision time, and `observed_at` ≥ decision − 30 h (else
  `latest_forecast_stale`).
- **F_prev**: greatest `issue_time` among rows with `observed_at` ≤
  decision time and `issue_time` ∈ [issue_time(F_latest) − 30 h,
  issue_time(F_latest) − 60 min] (else `no_eligible_prior_forecast`).
- Ties: greatest (`issue_time`, `observed_at`, `id`) — corrected
  same-issue rows resolve to the latest `observed_at`; earlier rows are
  retained for provenance. Missing prior ⇒ exclusion, never an imputed
  zero. A stored unchanged reissue ⇒ revision = 0, retained as the
  control stratum.
- **Directional feature**: `rev_dir = revision_24h × threshold_side_sign`
  (+1 when YES pays above the threshold, −1 when YES pays below).

Secondary revision definitions (trailing-6 h, revision counts,
dispersion) are future hypotheses, not part of H0020.

## Eligibility (frozen)

Include: single-sided daily temperature threshold markets in the
KXHIGHT*/KXLOWT* families at CHI/DEN/LAX/NYC with deterministic
settlement lineage, point-in-time market evidence at ≤ decision time, and
a valid frozen revision construction. Exclude — each with its
deterministic reason code from `config.json` — range/between contracts,
composites, non-temperature contracts, stations without a forecast
archive, unresolved settlement mappings, rows without an eligible prior,
stale forecasts, and rows without market evidence at decision.

## Model & benchmark (frozen)

- **M1 (benchmark)**: latest eligible market-implied probability at
  decision time, clipped to [0.02, 0.98].
- **MR**: L2 logistic regression (numpy IRLS,
  `experiments/baseline.fit_logistic`, L2 = 1.0) on exactly two features:
  `logit(market_probability)` and `rev_dir`. No static forecast-level
  features, no station effects, no feature selection, no calibration
  stage beyond the fitted intercept and coefficients.
- Seed 20260909; clip [0.02, 0.98]; missing data excluded with recorded
  reason, never imputed.

MR−M1 isolates the revision information conditional on the price: if the
market fully impounds revisions, `rev_dir` adds nothing and the primary
comparison shows no improvement.

## Frozen windows (chronological, by station-local target_date)

| Window | Dates |
|---|---|
| Train | 2026-07-21 → 2026-08-11 |
| **Excluded gap (H0019's test)** | **2026-08-12 → 2026-08-25 — used for nothing** |
| Validation | 2026-08-26 → 2026-09-08 |
| Test (initial) | 2026-09-09 → 2026-09-22 |

Grouping unit: **event_group = (station, variable, target_date)**; splits
are by target_date, so no group can cross a split (workbench
`grouped_chronological_split` re-asserts this). The H0019 test window is
excluded from H0020 entirely — never trained on, validated on, or tested
on — so the two experiments' outcomes cannot couple through shared data.

## Deterministic test-extension rule (frozen before any test access)

If readiness gates are unmet at 2026-09-22: extend the test window by
exactly 7 calendar days and re-evaluate **readiness counts only**; at
most two extensions; absolute final end **2026-10-06**. If gates remain
unmet then: status `DEFERRED_INSUFFICIENT_DATA`, the final test is not
run, the hypothesis is unchanged, and no replacement window may be
created post hoc.

## Readiness gates (execution gate — counts and integrity only)

Full registered test window elapsed; ≥ 25 eligible test event-groups;
≥ 3 stations; ≥ 5 positive and ≥ 5 negative test labels; station
concentration ≤ 0.40; ≥ 25 bootstrap units; frozen hashes valid; no
train/validation/test group leakage; availability uses `observed_at`;
exclusion counts and reasons available and reported by split (including
missing-prior and collection-gap exclusions); repository and dataset
provenance recorded. Readiness must not compute predictive performance.

## Primary metric and uncertainty (frozen)

Exactly one primary comparison: **Brier(MR) − Brier(M1)** on the single
untouched test window (lower is better), event-grouped bootstrap
(2,000 resamples, 95% CI, seed 20260909).

## Decision rule (frozen)

- **SUPPORTED** only if: readiness gates passed before test execution,
  mean Brier(MR)−Brier(M1) < 0, the 95% grouped-bootstrap CI lies
  entirely below 0, and no station has a positive mean Brier difference.
- **NOT SUPPORTED** if: the point estimate is non-negative, or the CI
  touches/crosses zero, or any station reverses the sign criterion.
  Final: no rerun, no changed window, no feature addition, no threshold
  adjustment, no reinterpretation as partial support.
- **INVALID** if: leakage, hash mismatch, test access before readiness,
  `issue_time` used as availability, split-integrity failure, or the
  registered design cannot be reproduced.
- A SUPPORTED result authorizes **only** a separate untouched replication
  registration — not paper trading, not live trading.

## Secondary diagnostics (exploratory, non-claim-bearing)

Log loss; reliability table; ECE; calibration slope/intercept;
per-station Brier difference; zero-vs-nonzero revision strata;
warming-vs-cooling descriptive split; sensitivity to clipping, L2, and
minimum revision separation. None can alter the primary verdict. None
are computed before the final run.

## Commands and artifacts (frozen behavior)

- Readiness: `uv run kalshi-weather experiment readiness h0020`
- Final run (single-shot): `uv run kalshi-weather experiment run h0020
  --out docs/research/experiments/EXP-FUTURE-H0020/`
- h0020 readiness/runner support is to be implemented (counts-only
  readiness first) before the validation window closes; a single-run
  guard must refuse a second test execution.
- Outputs land in this directory with an immutable manifest: git SHA,
  DB revision (0011), dataset manifest hash, dependency lock hash
  (`uv.lock` sha256), config hash, registration hash, ledger sequence and
  record hash, captured stdout/stderr, and sha256 of every output
  artifact.
- Ledger: `docs/research/ledger.jsonl` — H0020 is the first native
  contemporaneous record, following two narrowly marked historical
  backfills (H0018, H0019) that exist so test-window overlap checks are
  mechanical.

## Anti-peeking

No validation or test performance may be computed before readiness; the
registered spec (this file + `config.json` + `h0020.py`) is hashed into
the ledger record; any post-registration change to frozen elements other
than a documented reproducibility fix (recorded in HYPOTHESES.md before
test access) is INVALID. A NOT SUPPORTED outcome is a complete, final,
successful research result.
