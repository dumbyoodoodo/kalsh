# PREREG-20260722-H0014-recency-stability

**Pre-registration package for H0014 — frozen 2026-07-22, before any
season-matched cell, any NYC by-variable yearly cell, any contrast
statistic, or any per-cell diagnostic has been computed.** H0014
adjudicates the 2026 observation surfaced by H0013's registered per-year
descriptive (CHI 2026-partial-year tmax rate = tmin rate): ordinary
sampling variation, operational artifact, or genuine regime shift. Its
purpose is adjudication of a seen anomaly under frozen rules — **not** a
search for new effects.

**This document computes and reports no new statistic.** The only
real-data facts cited are already published by
`EXP-20260722-H0013-chi-replication-results.json` (and its H0011/H0002
predecessors) or by the pinned dataset's manifest.

## 0. Provenance — what has and has not been seen

**Seen (published record; the motivating data):**

- CHI **full-year** per-variable revision cells (H0013 registered
  descriptive): tmin 96/362 ('23), 92/359 ('24), 92/362 ('25), 50/198
  ('26 YTD); tmax 50/362, 50/359, 51/362, 50/198. The 2026 tmax
  elevation and exact tmin equality (50/198 both) is the anomaly under
  adjudication.
- Pooled station rates (H0011/H0013), archive cadence (~2.01
  issuances/variable-day both stations), coverage (~98.7–99% of days),
  single-issuance day counts (6–7 per variable per station), and the
  pinned dataset's manifest facts.

**Never computed by any experiment (the genuinely unseen evidence):**

- NYC by-variable **yearly** cells (any window). H0002 published only
  pooled-across-variables yearly rates.
- **Season-matched** (Jan 1 – Jul 21) cells for any station or year.
- Any 2026-vs-history contrast, at either station.
- Any per-cell diagnostic (cadence, missing days, single-issuance
  fraction by window; monthly rates).

**Epistemic consequence, stated plainly:** a CHI shift finding is
partially post-hoc (its full-year cells motivated this hypothesis; the
SM cells are new but correlated with what was seen). NYC carries the
genuinely new corroborating evidence. The §7 decision structure
therefore gives **cross-station corroboration** the deciding role
between "possible" (B) and "inconsistent with prior findings" (D).

## 1. Research question and prediction

**Question.** Once seasonal composition is controlled (identical
Jan 1 – Jul 21 calendar windows), do the 2026 per-variable revision
rates at NYC and CHI differ from their own pooled 2023–2025 rates by
more than sampling variation — and if so, does the difference co-occur
with measurable operational changes, and does it corroborate across
stations?

**Prediction (frozen, directional-null).** The hypothesis as registered
predicts **stability**: all four contrasts' decision CIs include zero
(outcome A), with the H0013 fig-4 observation explained by the seasonal
mismatch of a Jan–Jul window against full-year baselines plus sampling
noise in an n≈198 cell. The design does not assume this: outcomes B, C,
and D have equal standing under the frozen table, and the CHI full-year
arithmetic makes a CHI tmax shift event a live possibility the
prediction may well lose to.

## 2. Population and unit of analysis

| Item | Frozen value |
|---|---|
| Stations | Exactly `{NYC, CHI}`. DEN and LAX are **intentionally excluded** (different cadence regimes; out of scope; their rows are dropped by the frozen filter and never read). |
| Variables | Exactly `tmax_f` and `tmin_f` (asserted on the filtered frame) |
| Source frame | `observation_issuances.parquet` from the **already-pinned** dataset `exp-20260722-h0013-replication` — the same artifact H0013 executed against; content hash frozen in §10. No new dataset build; no execution-time dataset discretion. |
| Unit of analysis | The variable-day (station_id, variable, observation_date); ≥1 stored issuance = valid; single-issuance days count as unrevised (H0002 convention) |
| Revision | first-issued value ≠ final-issued value, Decimal-quantized to 0.01; construction `Decimal(str(v))`; ROUND_HALF_EVEN; round-trip invariant < 1e-6 asserted under G2 (H0011 F3, inherited) |
| Timezone | **None.** Windows are calendar dates of `observation_date`; revision is a value comparison. No local-time conversion exists in this design, so the tzdata-pinning gate of H0011/H0013 does not apply (recorded here so its absence is a decision, not an omission). |
| Duplicates / nulls | Frame unique on (station_id, variable, issuance_time); no nulls; violation aborts (G2) |
| Missing days | Absent variable-days are absent — reported as missing-day counts per window (a registered diagnostic and operational indicator), never imputed |

## 3. Time segmentation (fixed; no adaptive splitting)

All windows are frozen now; no window may be added, merged, shifted, or
subdivided after any result is seen.

| Window | Definition | Role |
|---|---|---|
| SM(2023) | 2023-01-01 → 2023-07-21 inclusive | decisional (baseline pool) + tables |
| SM(2024) | 2024-01-01 → 2024-07-21 | decisional (baseline pool) + tables |
| SM(2025) | 2025-01-01 → 2025-07-21 | decisional (baseline pool) + tables |
| SM(2026) ≡ 2026 YTD | 2026-01-01 → 2026-07-21 | decisional (comparison cell) + tables |
| hist | SM(2023) ∪ SM(2024) ∪ SM(2025), pooled | decisional baseline |
| FY(2023..2025) | full calendar years | **tables/figures only** (the task's yearly comparison), never decisional |

Rationale, frozen: the H0013 observation compared a Jan–Jul window
against full-year baselines — a seasonal-composition confound. Every
decisional comparison here is season-matched by construction. Days
outside all windows (exactly 2022-12-31, one variable-day per variable
per station = 4) are excluded and counted in the results.

## 4. Primary estimands (the declared family of 4)

For v ∈ {tmax_f, tmin_f} and s ∈ {NYC, CHI}:

- `p_{v,s,2026}` = revised fraction over SM(2026) valid variable-days;
  `p_{v,s,hist}` = revised fraction over pooled hist valid variable-days.
- **Δ_{v,s} = p_{v,s,2026} − p_{v,s,hist}** — a difference of two
  proportions over **disjoint day sets** (independent samples), the
  Newcombe interval's intended setting. Sign convention: positive =
  2026 revises more.

Supporting (reported with CIs, never decisional): per-cell rates; the
gap `D_{s,w} = p_tmin − p_tmax` per station per window.

## 5. Statistical methods

The program's frozen machinery, unchanged: Wilson score interval and
Newcombe (1998) hybrid score interval, no continuity correction,
two-sided; exact arithmetic (integer counts; Decimal precision-50
bounds; all strict decision comparisons after 1e-12 ROUND_HALF_EVEN
quantization of both operands; quantized equality never satisfies a
strict inequality; serialization fixed-point 12 digits — H0011 F4). No
randomness anywhere.

**Confidence levels (both frozen):**

- **Reporting level:** 95%, z = `Decimal("1.959963984540054")` — every
  table/figure CI.
- **Decision level:** 98.75% per contrast — Bonferroni over the declared
  family of 4 contrasts at family-wise α = 0.05 —
  z = `Decimal("2.497705474412374")`, the two-sided Φ⁻¹(0.99375),
  derived 2026-07-22 from Python stdlib `statistics.NormalDist.inv_cdf`
  (round-trip `cdf(z) = 0.99375` verified; §11 requires an independent
  re-derivation at implementation time). Only the four Δ contrasts are
  ever evaluated at this level; only they can fire a shift event.

**Shift event for (v,s):** the 98.75% Newcombe CI on Δ_{v,s} excludes 0
(quantized: `L > 0` or `U < 0`). Direction = sign of the point estimate.

**Dependence caveats (disclosed, untreated, inherited):** serial
dependence across consecutive days; cross-variable dependence within a
date; cross-station dependence within a date (shared continental
weather). All push the independent-samples intervals toward
anti-conservatism to an unknown degree; the same caveat attached to
H0002/H0011/H0013 and their consumers, and conclusions inherit it.

## 6. Diagnostics and operational indicators

**Registered diagnostics** (computed for every station × window cell,
descriptive):

- mean issuances per variable-day (cadence); issuance-count
  distribution
- single-issuance-day count and fraction
- calendar-expected days vs observed days (**missing-day count and
  fraction**) — archive/observation completeness
- monthly revision rates by variable, pooled hist SM vs 2026 (point
  estimates only) — seasonal composition within the matched windows
- excluded-day count (§3)

**Frozen operational indicators** — evaluated only at stations with ≥1
shift event, each on 2026-vs-hist (SM windows), each comparison
quantized, "≥ T" meaning NOT strictly-less than T:

| Indicator | Quantity | Threshold |
|---|---|---|
| OI-1 cadence | \|mean issuances/variable-day, 2026 − hist\| | ≥ 0.15 |
| OI-2 completeness | \|missing-day fraction, 2026 − hist\| | ≥ 0.05 |
| OI-3 single-issuance | \|single-issuance-day fraction, 2026 − hist\| | ≥ 0.05 |

These are **co-occurrence** measures: an indicator firing alongside a
shift classifies the outcome as operational-artifact evidence (C); it
does not prove causation, and the report must say so. No indicator may
be added, removed, or re-thresholded after results are seen; an
explanation not backed by a registered diagnostic may be *suggested* in
the report's limitations but can never enter the decision.

## 7. Decision rule

### 7.1 Decision table (exhaustive; evaluated in order; each row terminal)

Let S_s = the set of variables with a shift event at station s.

| Step | Condition (quantized comparisons throughout) | Outcome |
|---|---|---|
| 0 | Any §8 gate fails | **BLOCKED** — not a scientific outcome; gate report published; stop |
| 1 | S_NYC = ∅ and S_CHI = ∅ | **A — No evidence of a meaningful regime change** |
| 2 | (S_NYC ∪ S_CHI ≠ ∅) and any OI fires at a station with S_s ≠ ∅ | **C — Evidence of operational artifact** |
| 3 | ∃ v with a same-direction shift event at **both** stations (no OI fired at shifted stations) | **D — Evidence inconsistent with prior findings** |
| 4 | otherwise (uncorroborated or direction-discordant shifts; no OI) | **B — Possible regime change requiring additional data** |

Boundary behavior: quantized equality of a CI bound with 0, or of an OI
quantity with its threshold, resolves per the definitions above (a
touching CI is **not** a shift; an OI exactly at threshold **fires**).

### 7.2 Interpretation criteria (frozen wording per outcome)

- **A.** The 2026 behavior at both stations is consistent with the
  2023–2025 season-matched baseline at the decision level; H0013's
  fig-4 observation is attributable to seasonal composition (Jan–Jul vs
  full-year) and/or sampling variation in an n≈198 cell. The
  H0011/H0013 asymmetry stands as published, with the ±8pp-scale
  detectability limit of §8 named. Downstream consumers may rely on the
  pooled asymmetry, re-checking after full-2026 data exists.
- **B.** At least one station shows a decision-level shift without
  cross-station corroboration or operational co-occurrence. The
  asymmetry's current magnitude is unreliable at the shifted
  station(s); downstream work must not assume it there. Retry with
  full-2026 windows (FY(2026) vs FY(2023–2025), a new pre-registered
  entry) on or after **2027-01-15**.
- **C.** The shift co-occurs with a measured collection/reporting
  change (the specific OI(s) are named in the report). The result is
  evidence about the archive/reporting pipeline, not about label
  physics; the follow-up is an operational investigation (raw-payload
  inspection for the affected window) before any physics-flavored
  hypothesis.
- **D.** A same-direction shift replicates across the two stations with
  no registered operational co-occurrence — evidence the label-formation
  process itself changed in 2026. H0011/H0013's conclusions retain
  their 2023–2026 pooled validity but their forward-looking use is
  suspended pending a full-2026 re-run; downstream consumers must treat
  variable-specific label noise as time-varying.
- Under **every** outcome: the pooled pre-registered H0011/H0013
  results are unaffected (this experiment cannot and does not reopen
  them); and no outcome licenses any market or trading conclusion.

## 8. Power and eligibility gates

Gates verified mechanically before any interval is computed; inputs are
counts only; any failure → step 0.

| Gate | Frozen requirement |
|---|---|
| G1 | Loaded **full frame's** recomputed canonical content hash equals the §10 pin exactly (before the station filter) — same hash, same artifact, as H0013's G1 |
| G2 | On the {NYC, CHI}-filtered frame: natural key unique; no nulls; station set == {CHI, NYC}; variable set == {tmax_f, tmin_f}; every value passes the round-trip invariant |
| G3 | Every SM cell (2 stations × 2 variables × 4 years = 16 cells) has **n ≥ 150** valid variable-days |
| G4 | Pooled hist SM revised count **R ≥ 30** for each (station, variable) — the contrast-decidability floor |

Power, from published facts only: SM windows span 202 calendar days
(203 in 2024), and archive coverage is ~98.7–99%, so n ≈ 197–201 per
cell and n_hist ≈ 590–600. At rates in the published 13–26% range, the
98.75% Newcombe CI on Δ (n ≈ 198 vs ≈ 595) has half-width ≈ 7–9
percentage points. A shift of the size the CHI full-year arithmetic
suggests (~11pp) is decidable; drifts ≪ 8pp are not, and land in A —
with §7.2's explicit statement that A is bounded by this detectability
limit. G4 keeps every decisional contrast's baseline arm at R ≥ 30.

## 9. Descriptive analyses

All non-decisional, computed after the §7 outcome is final: the full
station × window × variable tables (n, r, rate, Wilson 95% CI — SM and
FY windows); per-window gaps D with Newcombe 95% CIs; the §6
diagnostics; the four Δ contrasts restated at the 95% reporting level.
**Figures** (post-verdict, from the frozen results JSON + pinned frame;
registered quantities only): (1) per-station SM yearly rates with
Wilson CIs by variable; (2) per-station SM yearly gap D with Newcombe
CIs; (3) forest plot of the four Δ contrasts at both CI levels with the
zero line; (4) monthly revision-rate profiles, pooled hist vs 2026, per
station. Nothing not listed here may be computed without an amendment.

**Calibration note (pre-explained):** no probabilistic prediction is
produced; Brier/reliability machinery does not apply. The CI figures
are the calibration-adjacent deliverable.

## 10. Reproducibility

| Field | Frozen value |
|---|---|
| Dataset version | `exp-20260722-h0013-replication` (reused; already immutable) |
| Frame | `observation_issuances.parquet`, 26,310 rows (NYC+CHI subsets: 5,150 + 5,152 rows) |
| Content hash | `5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775` |
| Dataset-build git commit | `9285a0145db39d5829b93b648a1e19e5a32c7237` |
| Source DB revision (at build) | `0007` |
| Motivating-record reference | `EXP-20260722-H0013-chi-replication-results.json` (CHI full-year cells; frozen literals re-typed in the analysis module and drift-checked) |
| z literals | reporting `1.959963984540054`; decision `2.497705474412374` (§5) |
| Analysis git commit | recorded at execution |
| Software versions | recorded at execution |
| Randomness | none — fully deterministic |
| Sorting/iteration order | frame filtered to {CHI, NYC}, sorted (station_id, variable, observation_date, issuance_time); stations iterated CHI then NYC, variables tmax_f then tmin_f, windows 2023→2026 then hist; output byte-stable |
| Evaluation order | G1 → filter → G2 → G3 → G4 → the 4 Δ decision CIs → shift events → OIs (only if shifts) → §7.1 table → §9 descriptives |
| Output schema | `TEMPLATE-H0014-manifest.json` (same directory); no key added/removed/renamed after results are seen; plus `EXP-20260722-H0014-recency-stability.md` in `docs/research/experiments/` |
| Determinism verification | two raw executions must be byte-identical before the result is reported |
| Point-in-time audit | the frame is an issuance-time archive; no forecast, market, or settlement-outcome data enters anywhere; window assignment uses `observation_date` only. No leakage channel exists; the naive-UTC property of `issuance_time` is not load-bearing here (no time-of-day computation), noted for completeness. |

## 11. Implementation-integrity checklist

To be completed before execution; none computes a real-data cell:

- [ ] **Window assignment tests**: boundary dates (Jan 1, Jul 21, Jul 22,
      Dec 31), the 2024 leap day, 2022-12-31 exclusion, and calendar
      day-count expectations (202/203).
- [ ] **Independent re-derivation of the decision z**: recompute
      Φ⁻¹(0.99375) by a second method (bisection on `NormalDist.cdf`)
      and compare to the frozen literal to ≥12 significant digits.
- [ ] **Contrast tests**: Newcombe with unequal n (200 vs 600) against
      an independent arrangement of the same formula; a hand-checked
      spot value; equal-rates configuration gives a CI containing 0.
- [ ] **Shift-event tests**: CI excluding 0 on each side (direction
      sign); touching-0 (quantized) is NOT a shift at either bound.
- [ ] **Decision-table tests**: every branch A/B/C/D + BLOCKED reachable
      via synthetic configurations, including: shifts at both stations
      same variable+direction → D; both stations different variables →
      B; both stations same variable opposite directions → B; one
      station only → B; any shift + OI at the shifted station → C
      (including OI exactly at threshold → fires); OI at a NON-shifted
      station only → not C.
- [ ] **OI threshold tests**: exactly-at-threshold fires; just-under
      does not (quantized).
- [ ] **Gate tests**: G1 tamper detection; G2 on the two-station filter
      (DEN/LAX rows dropped; duplicate outside {NYC,CHI} ignored);
      G3/G4 short-circuits with `not_evaluated` sentinels.
- [ ] **Determinism**: run-twice byte-identity on synthetic data.
- [ ] Code review of the finished script against §3–§7 before first
      execution.

## 12. Known risks

- **R1 — Partial post-hoc at CHI** (§0): the decision structure's
  cross-station rule, not CHI alone, separates B from D. Disclosed.
- **R2 — Low power in the 2026 arm**: only ~8pp-scale shifts are
  decidable; A explicitly carries that bound (§7.2, §8).
- **R3 — Baseline contamination**: a change beginning mid-2025 would
  dilute hist; per-year SM cells are published so the record shows it;
  the pooled baseline stays decisional (no post-hoc re-pooling).
- **R4 — OIs measure co-occurrence, not cause** (§6); C is worded
  accordingly.
- **R5 — Cross-station weather dependence**: NYC/CHI share continental
  regimes; corroboration (step 3) is strong but not independent
  evidence in the strict sense. Disclosed.
- **R6 — Seasonal residue**: season-matching equalizes calendar windows
  but not weather realizations across years; the monthly diagnostic
  profiles this, decides nothing.

---

**No season-matched cell, NYC yearly cell, contrast, or per-cell
diagnostic has been computed. H0014 has not been executed.**
