# EXP-20260721-H0002-cli-revisions

First experiment on the platform. Executed exactly as pre-registered in
`HYPOTHESES.md` H0002; no metric, threshold, or decision rule was altered
after seeing data. Machine-readable results:
`EXP-20260721-H0002-cli-revisions-results.json` (same directory), produced by
`scripts/exp_h0002_cli_revisions.py`.

## Hypothesis

**H0002 — CLI settlement-revision risk is material.** For NYC daily max/min
temperature, the first-issued CLI report value differs from the final settled
value on more than 5% of station/variable-days, and differs by more than 1°F
on more than 1% of days.

Pre-registered decision rule: **confirmed** if the 95% CI lower bound for
revision frequency exceeds 5% *and* the 95% CI lower bound for P(|Δ|>1°F)
exceeds 1%; **rejected** if both upper bounds fall below those thresholds;
otherwise inconclusive.

## Reproducibility

| Field | Value |
|---|---|
| Dataset version | `exp-20260721-h0002` (`DATASET_ROOT/exp-20260721-h0002/`) |
| Frame used | `observation_issuances.parquet` (5,148 rows) |
| Content hash (observation_issuances) | `ec38d1a66d7d93ee2f52a087b71c48ea…` (full value in `manifest.json`) |
| Source DB revision | `0005` |
| Git commit (dataset build + analysis code) | `2e76d320` (analysis script and issuance frame added in the commit containing this record) |
| Analysis | `scripts/exp_h0002_cli_revisions.py` — deterministic (Wilson CIs, exact binomial), no randomness, no fitting |
| Config | station `NYC`, variables `tmax_f`/`tmin_f`, thresholds 5% / 1% / 1°F, 95% CIs, denominator = all station/variable-days with ≥1 stored issuance |
| Date run | 2026-07-21 |

Data acquisition for this run: `weather backfill --station NYC --start
2023-01-01 --end 2026-07-20 --chunk-days 30` (44 chunks, 0 failed, 4,954
rows saved, 0 invalid), on top of previously collected history; snapshot cut
with `ops snapshot --version exp-20260721-h0002` (dataset_ok=True,
quality_ok=True; `quality.json`/`ops.json` sidecars in the dataset
directory).

## Methodology

One row per (station, variable, observation_date) built from the issuance-
level extract: first-issued value/time (min issuance_time), final value/time
(max issuance_time), Δ = final − first. Denominator for the primary metrics
is **all** station/variable-days with ≥1 stored issuance (single-issuance
days count as unrevised), matching the hypothesis's own "11/60 of
station/variable-days" arithmetic; single-issuance coverage is reported
separately as the pre-registered caveat. Wilson 95% CIs (the pre-registered
deterministic option); exact two-sided binomial sign test for the same-day
preliminary-high asymmetry. "Same-day" first issuance is determined in the
station's local timezone (America/New_York).

## Sample and validation

- **Window**: 2022-12-31 → 2026-07-20 (the 2022 rows are two boundary days
  from the first backfill report; retained, immaterial).
- **N = 2,564 station/variable-days** (1,282 days × 2 variables) of ~1,298
  calendar days in the window → **98.8% day coverage**; ~16 days absent from
  the source archive.
- **Issuance coverage: 2,552/2,564 (99.5%) of days have ≥2 issuances** — the
  IEM-gap failure mode pre-registered as the main coverage risk did not
  materialize. Metrics computed on the ≥2-issuance subset are within 0.1pp
  of the primary metrics (18.85% vs 18.76%), so the denominator convention
  is immaterial.
- **Exclusions (documented, none silent)**: one CLI product
  (`202607030619-KOKX-CDUS41-CLINYC`, the 2026-07-02 final report) failed
  parsing — malformed source text missing the MAXIMUM line — logged by the
  collector and skipped; 2026-07-02 is absent entirely. 12 single-issuance
  days counted as unrevised. `ops quality` at snapshot time: OK (single
  warning = that missing day).
- Station coverage: NYC only (per pre-registration; conclusion claimed for
  NYC only). Parser coverage / forecasts: not applicable — this experiment
  uses no market or forecast data.

## Results (pre-registered metrics)

**Primary metrics and decision:**

| Metric | Value | 95% CI | Threshold | Met? |
|---|---|---|---|---|
| Revision frequency P(first ≠ final) | **18.76%** (481/2,564) | [17.30%, 20.32%] | lower bound > 5% | **Yes** |
| P(\|Δ\| > 1°F) | **12.25%** (314/2,564) | [11.03%, 13.57%] | lower bound > 1% | **Yes** |

**Decision: CONFIRMED** (both pre-registered conditions met, with wide margin).

**|Δ| distribution on revised days** (bucket = ⌈|Δ|⌉ °F; mean |Δ| when
revised = 3.32°F, max = 18°F):

```
 ≤1°F  167  ██████████████████████████████████
 ≤2°F   90  ██████████████████
 ≤3°F   70  ██████████████
 ≤4°F   38  ████████
 ≤5°F   30  ██████
 ≤6°F   27  █████
 ≤7°F   21  ████
 ≤8°F    5  █
 ≤9°F    8  ██
≤10°F    3  █
>10°F   22  ████     (11-18°F tail)
```

**Sign asymmetry, same-day preliminary tmax (pre-registered):** of 1,278
tmax days whose first issuance was same-day, 170 were revised — **169 upward,
1 downward** (99.4% upward; exact two-sided sign test p ≈ 2.3×10⁻⁴⁹). The
physical prior (a same-day running maximum can only rise) holds almost
exactly; the single downward case is a boundary/correction artifact. The
"preliminary high is a floor" bound is violated at a rate of ~0.6% of
revised same-day cases (~0.08% of all tmax days).

**By variable:**

| Variable | Days | Revision freq | Note |
|---|---|---|---|
| tmax_f | 1,282 | **13.3%** | afternoon max sits safely inside the local day |
| tmin_f | 1,282 | **24.3%** | daily minimum often lands near the local-midnight boundary |

**By year (stability check, pre-registered failure-mode mitigation):**
2023: 17.1% [14.5, 20.0] · 2024: 18.5% [15.8, 21.4] · 2025: 19.2%
[16.5, 22.2] · 2026 (partial): 21.7% [17.9, 26.0]. Overlapping CIs; no
regime break; coverage ≥99.2% every year.

**By month (pooled, descriptive):** highest in December (26%) and
March-April (22-23%); lowest in August (11%). Seasonal variation is present
but the phenomenon is material in every month.

## Conclusions

- **Confirmed finding:** CLI settlement-revision risk is material for NYC —
  roughly one in five station/variable-days settles at a value different
  from its first-issued report, and one in eight moves by more than 1°F.
  Every downstream hypothesis that treats the settled CLI value as its label
  (H0005, H0006, H0007) inherits this quantified noise, and near-settlement
  reasoning that treats a preliminary report as final is wrong ~19% of the
  time (mean miss 3.3°F when wrong).
- **Confirmed finding (asymmetry):** the same-day preliminary high is, to
  ~0.6% violation, a hard floor on the settled high — directly load-bearing
  for H0007's bound-violation design, which should use the measured 169/170
  asymmetry rather than assuming a perfectly hard bound.
- **Exploratory (clearly labeled, generated a follow-up):** tmin labels are
  ~2× noisier than tmax (24.3% vs 13.3%) — plausibly a local-midnight
  boundary effect. Recorded as follow-up hypothesis H0011; **not** a
  pre-registered claim of this experiment.

## Limitations

- NYC only; per pre-registration the conclusion is claimed for NYC until
  replicated on other stations (a data-only registry addition away).
- "First-issued" means first *stored* issuance; missing archive days (1.2%)
  and any IEM-side gaps bias the frequency estimate downward if anything —
  the confirmation direction is robust to this.
- The window (3.55 years) predates any NWS practice change that may occur
  later; per `RESEARCH.md`, weather-physics conclusions carry their window:
  **2023-2026, re-examine if NWS reporting practice changes.**
- One malformed source product excluded (documented above); no indication
  the exclusion is correlated with revision behavior.
