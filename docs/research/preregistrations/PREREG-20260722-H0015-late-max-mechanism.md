# PREREG-20260722-H0015-late-max-mechanism

**Pre-registration package for H0015 — frozen 2026-07-22, before any
late-max fraction, conditional revision rate, stratum count, or
timing-revision join has been computed for any station.** H0015 is the
confirmatory test of E0001's exploratory mechanism: that tmax
preliminary→final revisions are driven by days whose daily maximum
occurs at/after the station's own preliminary-product cutoff.

**This document computes and reports no decisional statistic.** The only
facts cited are E0001's published exploratory record (which may not be
modified and may not serve as confirmation), the two pinned artifacts'
structural manifests, and prior H-series published results.

## 0. Provenance — what has and has not been seen

**Seen pre-freeze (published or structural):**

- E0001's exploratory record for **CHI and NYC only** (occurrence-time
  parsing over 2023–2026 YTD): late-max fractions, conditional rates,
  and their co-movement with revisions. That data *generated* this
  hypothesis; those two stations are therefore **reference cohorts**
  here, never primary evidence.
- Structural facts from building the pinned occurrence extract
  (2026-07-22, printed by `scripts/build_h0015_occurrence_extract.py`):
  per-station day counts (1,282–1,295), final-product occurrence-time
  parse coverage (CHI 99.1%, DEN 99.0%, LAX 97.5%, NYC 98.4%), and
  first-product AS-OF hour distributions (modal: CHI 16, DEN 6, LAX 17,
  NYC 16). Also two sampled DEN/LAX product texts (format
  reconnaissance for the parser).
- Prior published station facts: issuance cadence (DEN 3.86, LAX 2.35).

**Never computed, by any experiment (the unseen evidence):**

- Any DEN or LAX revision rate, revision count, late-max fraction,
  conditional revision rate, or occurrence-hour distribution.
- Any timing↔revision join at any station beyond E0001's published
  CHI/NYC record.

**Evaluation-data note (required by the task, resolved here):** the
preferred primary evaluation window — full-year 2026 — does not exist
yet (today is 2026-07-22). The largest available data disjoint from
E0001's generating set is **station-disjoint**: DEN and LAX, whose
revision and timing behavior no experiment has ever touched. They are
the primary evaluation cohort. CHI/NYC full-2026 re-evaluation is
already mandated separately (H0014's outcome-B retry, ≥ 2027-01-15) and
is not part of this experiment. E0001's outputs are frozen as published
and are not modified by anything here.

## 1. Hypotheses and predictions

**Primary hypothesis.** The station-level tmax revision rate is
primarily determined by the fraction of days whose daily maximum occurs
at/after the station's own preliminary cutoff (the "late-max fraction").

**Frozen structural prediction (made from cutoffs alone, before any
rate is computed):** DEN's preliminary cutoff is early morning (modal
AS-OF 06:00), so nearly all daily maxima should be "late" there and its
revision rate should be **high** — far above CHI/NYC's published
~14–19%. LAX's cutoff is late (17:00), so its late-max fraction and
revision rate should be **low**. If revision rates at DEN/LAX do not
follow their cutoff-implied composition, the mechanism is wrong.

**Secondary hypotheses (evaluated as specified in §7/§9):** (a) the
late/early conditional-rate separation holds at each unseen station;
(b) the relationship holds across years (descriptive stability);
(c) revision probability is substantially higher on late-max days;
(d) between-station rate differences are predominantly composition
(timing-mix), not within-stratum behavior (Kitagawa decomposition).

## 2. Population, inclusion/exclusion, datasets

| Item | Frozen value |
|---|---|
| Variable | `tmax_f` only |
| Evaluation stations (decisional) | **DEN, LAX** |
| Reference cohorts (non-decisional, prespecified) | CHI, NYC |
| Window | Full archive per station: 2022-12-31 → 2026-07-21 (every observation_date present in both pinned artifacts). One window; per-year splits are descriptive only. |
| Input A (revisions) | `observation_issuances.parquet`, dataset `exp-20260722-h0013-replication`, hash `5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775` (already immutable). Revision = first-issued ≠ final-issued value, Decimal-quantized 0.01 (H0002 convention, unchanged). |
| Input B (timing) | `cli_occurrences.parquet`, extract `exp-20260722-h0015-occurrence`, hash `9bf01d8fc708c17215434ad6c0a7767137f00504f360a164d57133580e4e1d77`, 5,142 rows, built at commit recorded in its manifest by the productionized parser (`kalshi_weather.weather.cli_products`, unit-tested). |
| Analysis population (per station) | Days present in both inputs **with a parsed final-product occurrence time** ("parse-ok"). Days without a parsed occurrence time are excluded from strata and counted (a registered diagnostic + sensitivity re-run includes them as early). |
| Cutoff (per station) | **Frozen procedure:** the modal `first_asof_hour` among non-null preliminary AS-OF hours across the station's archive. (Structurally known to yield CHI 16, DEN 6, LAX 17, NYC 16; the procedure, not the numbers, is what is frozen — G3 gates the modal share.) |
| Late day | parsed final max occurrence hour ≥ cutoff, quantized comparison ("at-or-after", consistent with prior boundary conventions) |
| Timezone | none — occurrence hours and cutoffs are the products' own local wall-clock values; no conversion exists in this design |
| Exclusions | none beyond the population definition; no weather filtering, no outlier removal |

## 3. Estimands

Per evaluation station s (analysis population):

- `L_s` — late-max fraction; `1 − L_s` — early-max fraction.
- `p_late,s`, `p_early,s` — revision rate conditional on timing stratum.
- `R_s` — overall revision rate (same population; the composition
  identity `R_s = L_s·p_late,s + (1−L_s)·p_early,s` holds exactly and
  is reported as a table, not tested).
- `sep_s = p_late,s − p_early,s` — the within-station stratum effect
  (disjoint day sets → independent-samples Newcombe interval applies).
- `AF_s = (R_s − p_early,s) / R_s` — attributable fraction: the share
  of the revision rate above the early-stratum baseline (point
  estimate; declared as such, no CI).
- **Rank agreement** — across all four stations (evaluation +
  reference, same population definition): whether ordering by `L_s`
  equals ordering by `R_s`. Exact match required; any quantized tie in
  either ordering counts as non-agreement (fail-safe).
- **Kitagawa decomposition** (descriptive, §9): for each station pair,
  `R_a − R_b` split into a composition component
  `(L_a − L_b)·mean(p_late) − (L_a − L_b)·mean(p_early)` and a
  within-stratum rate component `mean(L)·Δp_late + mean(1−L)·Δp_early`
  (two-stratum Kitagawa with cross-station means as weights).

Overall effect vs within-stratum effect, stated: `R_s` differences are
the *overall* effect; `sep_s` and the rate component are the
*within-stratum* effect; the mechanism claims the overall effect is
mostly composition.

## 4. Statistical procedures

The program's frozen machinery, unchanged: Wilson score intervals
(rates) and Newcombe (1998) hybrid score intervals (differences), no
continuity correction, two-sided 95%, z = `Decimal("1.959963984540054")`;
exact arithmetic (integer counts, Decimal precision-50 bounds, 1e-12
ROUND_HALF_EVEN quantized strict comparisons, fixed-point 12-digit
serialization). No randomness; no model fitting.

**Multiplicity, declared:** the decisional family is the two evaluation
stations' separation CIs at 95%. Outcome A requires **both** stations
to pass (conjunctive — family-wise false-support ≤ per-test level);
outcome B is deliberately sensitive (either station); outcome D
requires a full CI inversion, a qualitatively strong signal. No further
correction is applied, and this reasoning is the declaration.

## 5. Decision criteria (per evaluation station)

| Criterion | Frozen definition |
|---|---|
| sep state | Newcombe 95% CI on `sep_s`: **strong** if L > 0.10; **positive** if L > 0; **straddle** if L ≤ 0 ≤ U; **inverted** if U < 0 (all quantized) |
| early floor | Wilson 95% upper bound of `p_early,s` < 0.15 |
| attribution | `AF_s` ≥ 0.50 (quantized not-strictly-less) |
| full_pass_s | strong sep AND early floor AND attribution |

## 6. Diagnostics (registered)

Per station: parse-ok fraction; missing-occurrence day count;
value-agreement rate (parsed final-product max value == frame
final-issued value, rounded to integer — the timing-extraction quality
check); AS-OF modal share; cadence; per-year cell tables. **Sensitivity
analyses (non-decisional, registered):** (i) cutoff ± 1 hour; (ii)
missing-occurrence days included as early-stratum; (iii) per-year
stratum rates; (iv) reference-cohort separations by the identical
procedure.

## 7. Decision table (exhaustive; evaluated in order; each row terminal)

| Step | Condition | Outcome |
|---|---|---|
| 0 | Any §8 gate fails | **BLOCKED** |
| 1 | Any evaluation station's sep state is **inverted** | **D — evidence inconsistent with the proposed mechanism** |
| 2 | full_pass_DEN and full_pass_LAX **and** four-station rank agreement | **A — composition mechanism strongly supported** |
| 3 | At least one evaluation station has sep state positive-or-strong | **B — composition mechanism partially explains the asymmetry** |
| 4 | Otherwise (both straddle) | **C — composition mechanism insufficient** |

Boundary behavior: quantized equality of a CI bound with its threshold
resolves to the less-favorable state for support (touching 0.10 is not
strong; touching 0 is not positive; an AF exactly at 0.50 passes, per
the not-strictly-less definition; a rank tie is non-agreement).

### 7.2 Interpretation criteria (frozen wording)

- **A:** at both unseen stations, late-max days carry decisively higher
  revision risk with the early stratum near-floor and the majority of
  each station's revision rate attributable to the late channel, and
  the four stations' revision rates order exactly as their late-max
  compositions — the asymmetry's station-level structure is
  composition-driven. Downstream label-noise handling should condition
  on occurrence timing (or its observable proxies) rather than on
  station identity.
- **B:** the stratum effect replicates at ≥1 unseen station but the
  full quantitative package (margin, floor, attribution, or cross-
  station ordering) does not — composition is a real but partial
  driver; the report must name which criterion failed and the follow-up
  it implies.
- **C:** no confident stratum effect at either unseen station — the
  E0001 decomposition does not generalize; the mechanism may be
  CHI/NYC-specific or confounded; downstream work must not condition on
  occurrence timing.
- **D:** the effect reverses at an unseen station — the mechanism as
  stated is contradicted; E0001's exploratory record stands as
  published but its interpretation must be reopened by a new
  hypothesis, not an edit.
- Under every outcome: E0001's outputs are unmodified; H0011/H0013/
  H0014's frozen results are untouched; no market/trading conclusion is
  licensed.

## 8. Power and eligibility gates

Gates verified mechanically before any interval; counts only; failure →
step 0.

| Gate | Frozen requirement |
|---|---|
| G1a | Input A's recomputed canonical hash == its §2 pin |
| G1b | Input B's recomputed canonical hash == its §2 pin |
| G2 | Input A: unique (station,variable,issuance_time), no nulls, station set == {CHI,DEN,LAX,NYC}, F3 value round-trip. Input B: unique (station_id, observation_date), station set == {CHI,DEN,LAX,NYC}. |
| G3 | Per **evaluation** station: parse-ok fraction ≥ 0.90; AS-OF modal share ≥ 0.60; value-agreement rate ≥ 0.98 on joined parse-ok days |
| G4 | Per **evaluation** station: total revised days ≥ 50; **both** stratum sizes ≥ 30 |

Power, from structural facts only: n ≈ 1,250–1,282 parse-ok days per
evaluation station. Stratum sizes are genuinely unknown (that is the
point); G4's 30-day floor keeps the Wilson/Newcombe machinery
decidable — at n = 30, a zero-event stratum still yields an upper bound
≈ 0.114, so the early-floor criterion is decidable at the gate minimum.
DEN's early stratum (maxima before 06:00) and LAX's late stratum
(maxima after 17:00) are the two cells most at risk of G4 failure; a
blocked run names them rather than deciding from vapor.

## 9. Descriptive analyses (non-decisional, after the verdict)

Full per-station tables (both cohorts): strata counts/rates with Wilson
95% CIs; `sep` with Newcombe 95% CIs; `L`, `R`, `AF`; the composition-
identity table; per-year versions of all of the above; the pairwise
Kitagawa decomposition table (all 6 station pairs); the §6 diagnostics
and sensitivity analyses; occurrence-hour histograms per station.
Figures (post-verdict, registered): (1) per-station conditional rates
with CIs (late vs early, all four stations); (2) L vs R scatter with
the four stations labeled (the composition-ordering picture); (3)
Kitagawa decomposition bars for evaluation-vs-reference pairs; (4)
occurrence-hour distributions by station. Nothing beyond this list may
be computed without an amendment.

## 10. Reproducibility

| Field | Frozen value |
|---|---|
| Inputs | the two §2 pins (hashes above; both immutable on disk) |
| Parser | `kalshi_weather.weather.cli_products` (unit-tested; commit recorded at execution) |
| Analysis git commit / software versions | recorded at execution |
| Randomness | none — fully deterministic |
| Iteration order | stations CHI, DEN, LAX, NYC; dates ascending; output byte-stable |
| Evaluation order | G1a → G1b → G2 → G3 → G4 → per-station estimands (evaluation stations first) → §5 criteria → §7 table → §9 descriptives |
| Output schema | `TEMPLATE-H0015-manifest.json`; no key added/removed/renamed after results are seen; report `EXP-20260722-H0015-late-max-mechanism.md` |
| Determinism | two raw executions byte-identical before reporting |
| Point-in-time audit | both inputs are issuance-time archives; occurrence times come from the final product (post-hoc facts used only to explain revisions, not to predict anything); no forecast/market/settlement data enters; no leakage channel exists for this design's claims |

## 11. Implementation-integrity checklist

- [ ] Parser unit tests green (all four stations' formats, edges,
      corrections, missing data) — done pre-freeze (10 tests).
- [ ] Cutoff-procedure tests: modal selection, modal-share gate,
      at-cutoff occurrence classifies late (quantized).
- [ ] Stratum/join tests: parse-fail exclusion + sensitivity inclusion;
      value-agreement computation; day present in one input only.
- [ ] Newcombe/Wilson spot checks (reuse the H0011-published-interval
      reproduction test).
- [ ] AF and Kitagawa identities on synthetic counts (components sum to
      the raw difference exactly).
- [ ] Decision-table tests: every branch reachable, including
      inversion→D precedence, rank-tie→non-agreement, AF-exactly-0.50
      passes, sep-touching thresholds resolve unfavorably.
- [ ] Gate tests: hash tamper (both inputs), G3/G4 short-circuits with
      sentinels.
- [ ] Determinism: run-twice byte-identity on synthetic inputs.
- [ ] Code review against §2–§7 before first execution.

## 12. Known risks

- **R1 — Station-disjoint but time-overlapping evaluation** (§0): DEN/
  LAX share the calendar window with the generating data; weather
  regimes correlate across the continent. Station-disjointness is the
  strongest isolation available today; the full-2026 time-disjoint test
  arrives with the H0014 retry.
- **R2 — Cutoff heterogeneity**: DEN's 06:00 cutoff makes its "late"
  stratum nearly the whole population and its early stratum small; LAX
  mirrors this. G4 converts thin strata into a blocked run; the §1
  structural prediction makes the heterogeneity itself the test.
- **R3 — Conditional-rate non-transfer**: conversion within the late
  stratum plausibly depends on cutoff depth (a 06:00 cutoff leaves more
  room for change than 16:00), so no criterion here demands
  cross-station equality of `p_late` — only ordering, separation,
  floor, and attribution.
- **R4 — Parser residue** (0.9–2.5% of days): excluded-and-counted,
  with the as-early sensitivity bounding its influence.
- **R5 — Serial dependence** across days: inherited program-wide caveat;
  intervals are approximate under weather persistence.
- **R6 — Reference contamination**: CHI/NYC appear only in the rank-
  agreement criterion (step 2's third conjunct) — a deliberate, limited
  reuse of generating-cohort data, disclosed here; steps 1/3/4 depend
  on evaluation stations alone.

---

**No late-max fraction, conditional rate, stratum count, or
timing↔revision join has been computed for any station. H0015 has not
been executed.**
