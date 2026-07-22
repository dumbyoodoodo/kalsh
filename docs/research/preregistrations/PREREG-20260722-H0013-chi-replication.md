# PREREG-20260722-H0013-chi-replication

**Pre-registration package for H0013 — frozen 2026-07-22, before any CHI
revision statistic, attribution classification, timing histogram, or
decision statistic has been computed.** H0013 is the independent-station
replication H0011's own conclusion requires ("replication on ≥1
additional station … before treating the claim as general"). It
re-instantiates H0011's frozen protocol for Chicago Midway (CHI),
changing only what the station change forces (station id, timezone,
dataset pin) and adding a pre-registered replication assessment against
H0011's published NYC values. Everything else — definitions, estimands,
statistical machinery, decision table, arithmetic policy, serialization —
is carried over verbatim from `PREREG-20260721-H0011-midnight-boundary.md`
as amended by `AMENDMENT-20260721-H0011-audit-resolution.md` (F1–F9),
whose resolutions are folded into this document's text from the start
rather than appended afterward.

**This document computes and reports no CHI revision rate, no CHI
attribution statistic, and no confidence interval on real CHI data.**
H0013 has not been executed.

## 0. Provenance — everything inspected before this freeze

Recorded so the record shows exactly what was and was not seen:

**Inspected pre-freeze (availability/design facts only):**

- Per-station issuance-archive coverage from the live database
  (2026-07-22): CHI = 2,576 issuance rows per variable over 1,282
  distinct days, 2022-12-31 → 2026-07-21, **2.009 issuances per
  variable-day**; NYC = 2.007; DEN = 3.859; LAX = 2.349. This is the
  basis for choosing CHI (matched cadence regime) and excluding DEN/LAX
  (different regimes; a future, separate hypothesis).
- The pinned dataset's manifest (row counts, content hash, build
  metadata) and the frame's per-station row counts/date ranges.
- H0011's published results (`EXP-20260721-H0011-midnight-boundary-results.json`),
  which are public record and are frozen below as replication references.

**Not inspected pre-freeze (and mechanically not computed):** any CHI
first-vs-final value comparison, revision count or rate for any CHI
variable or subset, any attribution classification, any timing
histogram, any per-year split, any statistic of the CHI `value` column
beyond its presence in row counts.

**Epistemic status.** Unlike H0011's Part 1 (a formalization of its own
motivating data), **every decisional statistic in H0013 is computed on
data that played no role in generating the hypothesis.** A Part 1 pass
here is genuine out-of-sample (in station) replication. The samples are
not temporally independent — CHI and NYC share dates and continental
weather regimes — so this is station-generalization evidence, not
independent-era evidence; frozen as a scope limit in §7.2.

## 1. Research question

**Two-part conjunctive claim, H0011's structure re-instantiated for CHI:**

- **Part 1 (rate difference).** For CHI, tmin_f settlement labels revise
  more often than tmax_f labels: `D_chi = p_tmin − p_tmax` is positive
  with statistical confidence.
- **Part 2 (label-formation timing).** Among revised CHI tmin
  variable-days, the proportion whose final value first appears in an
  issuance at or after local (America/Chicago) midnight of the day
  following the observation date exceeds one half, with statistical
  confidence.

**Binding scope statement (H0011 AMENDMENT F1, inherited verbatim and
in force from the moment of this freeze):** Part 2 is evidence about
settlement-label **formation timing** only — that revised daily-minimum
labels typically reach their final value in a post-midnight issuance. It
does not identify the timing of the underlying physical temperature
event, because the dataset carries issuance timestamps only. Given the
measured ~2.009 issuance/day cadence, Part 2 is **expected to pass
near-trivially and carries no mechanism-discriminating weight**; the
mandatory adjacent disclosures (§9: tmax attribution rate, stratified
gap decomposition) must be published next to any Part 2 result, exactly
as H0011's amendment required.

**Replication question (new in H0013, decisionally subordinate):** does
H0011's Part 1 effect generalize beyond NYC? Assessed per §7.3 against
H0011's frozen published values; the *direction* assessment is derived
mechanically from Part 1's own criterion, and the *magnitude*
assessment is descriptive only.

## 2. Population and unit of analysis

All frozen; none may change after any result is seen. Identical to
H0011 §2 except the rows marked **[CHI]**.

| Item | Frozen value |
|---|---|
| Station **[CHI]** | `CHI` (Chicago Midway). The pinned frame contains four stations; a **frozen filter step** selects `station_id == "CHI"` after G1 and before G2 (§8). No other station's values are read by any decisional computation. |
| Variables | Exactly `tmax_f` and `tmin_f` (asserted at execution on the filtered frame) |
| Source frame **[CHI]** | `observation_issuances.parquet` from dataset **`exp-20260722-h0013-replication`** — already exported; content hash frozen in §10. No new dataset build; no execution-time dataset discretion exists. |
| Date range | Every `observation_date` present in the pinned frame's CHI subset (2022-12-31 → 2026-07-21 per the frozen manifest; recorded, not re-filtered) |
| Unit of analysis | The **variable-day**: one row per (station_id, variable, observation_date), built from that day's issuance set |
| Valid variable-day | ≥1 stored issuance (H0002's denominator convention). Single-issuance days count as unrevised in Part 1's denominators and are reported as coverage; they can never appear in Part 2. |
| Issuance stages | Within a variable-day, sort by `issuance_time` ascending: **first** = minimum; **final** = maximum; others **intermediate**. |
| Canonical label | The final-stage value. Label formation only; `value_at_settlement`/market data are not used anywhere. |
| Duplicates | Frame must be unique on (station_id, variable, issuance_time); asserted, violation aborts (never silently deduplicated). |
| Missing data | Absent days are absent — counted in coverage, excluded from denominators, never imputed. Null value/issuance_time/observation_date asserted absent; violation aborts. |
| Value comparison | `Decimal` quantized to **0.01** (source column numeric(6,2)); construction via `Decimal(str(v))` only; ROUND_HALF_EVEN; round-trip invariant \|float(quantized) − raw\| < 1e-6 checked under G2, violation aborts (H0011 AMENDMENT F3, inherited). Never float equality. |
| Timezone **[CHI]** | `America/Chicago` (IANA), resolved via `zoneinfo` backed by the **pinned pip `tzdata` package whose version is recorded in the manifest** (execution blocked if unavailable). `issuance_time` is timezone-naive, interpreted as **UTC**; §11 requires an execution-time cross-check of ≥20 sampled CHI rows against the source database's timezone-aware column before any classification. |
| DST | US DST transitions occur at 02:00 local (America/Chicago transitions on the same dates as America/New_York), never at 00:00, so the local-midnight boundary exists exactly once on every date; §11 requires explicit transition-date tests. |

## 3. Revision definitions

Identical to H0011 §3, verbatim: a variable-day is **revised** iff its
first-stage value ≠ final-stage value (quantized comparison); only
first-vs-final counts (flip-flop days are descriptive, never
reclassified); Part 1 uses the any-revision indicator per variable-day;
no correction/continuation distinction is attempted; all first≠final
changes count equally.

## 4. Post-midnight attribution (deterministic, no discretion)

Identical to H0011 §4 with the boundary timezone changed: for a revised
variable-day with observation date *T*, the **first appearance** of the
final value (earliest issuance whose quantized value equals the final
value — the frozen tie-break for non-monotone sequences) is classified
**post-midnight-attributed** iff its local (**America/Chicago**) time is
at or after 00:00:00 on *T + 1 day* (an issuance at exactly 00:00:00
belongs to the next day). Unresolved classifications (unconvertible
timestamp, or a null surviving G2/F3's assertions — H0011 AMENDMENT F9's
closed trigger list, inherited) are individually enumerated, **remain in
Part 2's denominator, and count as NOT post-midnight-attributed** (the
conservative direction); the unresolved rate is gate G5.

## 5. Primary estimands

- **E1 (rate difference).** `D_chi = p_tmin − p_tmax`; `p_v = R_v / N_v`
  over valid CHI variable-days. Positive D means tmin revises more —
  the hypothesized (and previously NYC-observed) direction.
- **E2 (post-midnight formation fraction).** `p_pm,chi = X_pm / R_tmin`
  over revised CHI tmin variable-days (unresolved in denominator,
  excluded from numerator).

## 6. Statistical methods

Identical to H0011 §6 as amended, verbatim — restated in full so this
document stands alone:

- **E1**: Newcombe (1998, "method 10") hybrid score interval for a
  difference of two independent proportions, no continuity correction,
  two-sided 95%, with per-sample Wilson bounds (l₁,u₁), (l₂,u₂):
  `L(D) = (p̂₁−p̂₂) − sqrt((p̂₁−l₁)² + (u₂−p̂₂)²)`,
  `U(D) = (p̂₁−p̂₂) + sqrt((u₁−p̂₁)² + (p̂₂−l₂)²)`; sample 1 = tmin,
  sample 2 = tmax.
- **E2**: Wilson score interval, no continuity correction, two-sided 95%.
- **z** frozen as the exact literal `Decimal("1.959963984540054")`.
- **Exact arithmetic:** integer counts; Decimal interval bounds at
  context precision 50; **every strict decision comparison** decided
  only after quantizing both operands to 1e-12 (ROUND_HALF_EVEN);
  quantized equality never satisfies a strict inequality; no float ever
  reaches a decision comparison.
- **Serialization (AMENDMENT F4, inherited):** every non-integer
  decision-relevant quantity serialized as a fixed-point string,
  quantized to 1e-12, exactly 12 digits after the point, no scientific
  notation.
- **No randomness anywhere** (`random_seed: not applicable`).
- **Pairing:** the decisional interval is unpaired, exactly as H0011's
  audit approved; no paired robustness check exists (AMENDMENT F2,
  inherited). Serial dependence across dates is acknowledged and
  untreated, inheriting H0002/H0011's caveat.

## 7. Decision rule

### 7.1 Decision table (exhaustive; evaluated in order; each row terminal)

Identical to H0011 §7.1:

| Step | Condition (comparisons per §6) | Outcome |
|---|---|---|
| 0 | Any §8 gate fails | **EXECUTION BLOCKED** — not a scientific outcome; gate report published; stop |
| 1 | `L(D_chi) ≤ 0` (incl. quantized equality) | **Rejected** (no confident positive rate difference at CHI) |
| 2 | `L(D_chi) > 0` and `L(p_pm,chi) > 1/2` | **Confirmed** |
| 3 | `L(D_chi) > 0` and `U(p_pm,chi) < 1/2` | **Rejected** (formation-timing conjunct refuted) |
| 4 | `L(D_chi) > 0` and the interval straddles 1/2 | **Inconclusive** |

Boundary behavior: quantized equality always resolves to the branch
less favorable to confirmation.

### 7.2 What the outcomes mean (scope limits)

A **Confirmed** H0013 claims, for CHI and this archive's window only:
the tmin/tmax revision asymmetry exists at a second, independent
station, and revised tmin labels there are predominantly
post-midnight-formed **in the label-formation-timing sense of AMENDMENT
F1 only**. Combined with H0011 it supports the claim that the asymmetry
is a property of the CLI reporting process at matched-cadence stations,
not an NYC idiosyncrasy. It does **not** establish: any physical
midnight-boundary mechanism; generalization to different cadence
regimes (DEN/LAX); temporal-era independence (shared window with NYC);
or any market/trading conclusion. Rejected/Inconclusive carry the
symmetric limits.

### 7.3 Replication assessment (pre-registered; subordinate to §7.1)

Frozen NYC reference values (H0011's published record, verbatim):
`N = 1282` per variable; `R_tmin = 311`, `R_tmax = 170`;
`D_nyc = 0.109984399376`, 95% CI `[0.079970196795, 0.139841273431]`;
`p_pm,nyc = 1.000000000000`, CI `[0.987798751679, 1.000000000000]`.

| Field | Frozen definition |
|---|---|
| `direction_replicated` | `true` iff Part 1's criterion passes (`L(D_chi) > 0`). Mechanical restatement of step 1 vs steps 2–4; not a second test. |
| `magnitude_consistent` | **Descriptive, non-decisional.** `true` iff the CHI Newcombe 95% CI and the frozen NYC CI overlap (closed intervals; comparisons per §6's quantization). CI overlap is a conservative consistency heuristic, not a formal equivalence test — frozen as such. |
| `d_gap_vs_nyc` | Point difference `D_chi − D_nyc` (descriptive). |
| `part2_consistent` | **Descriptive.** `true` iff CHI's `criterion_2_state` equals H0011's published state (`pass`). Given the cadence expectation (§1), agreement here is expected and carries little weight — frozen as such. |

No replication field can change the §7.1 outcome. The generalization
recommendation in the report is: *supported* if §7.1 = Confirmed and
`direction_replicated`; *directionally supported with magnitude caveat*
if Confirmed but not `magnitude_consistent`; *not supported* if
Rejected at step 1. (Steps 3/4 leave Part 1's replication standing but
the conjunctive claim unreplicated; the report must say exactly that.)

## 8. Power and eligibility gates

Verified mechanically at execution start, before any interval is
computed; any failure → §7.1 step 0. Structure identical to H0011 §8;
G1/G2 adapted to the multi-station pinned frame.

| Gate | Frozen requirement |
|---|---|
| G1 | The **loaded full frame's** recomputed canonical content hash (`kalshi_weather.dataset.manifest.frame_content_hash`, the platform's own algorithm) equals the §10 frozen hash exactly. Evaluated **before** the CHI filter — pins the whole artifact byte-for-byte. |
| G2 | On the **CHI-filtered** frame: unique (station_id, variable, issuance_time); no null value/issuance_time/observation_date/station_id/variable; station set == {CHI}; variable set == {tmax_f, tmin_f}; every value passes the F3 round-trip invariant. |
| G3 | `N_tmax ≥ 1000` and `N_tmin ≥ 1000` valid CHI variable-days (manifest row counts imply ≈1,282 each; the gate catches filter/extract regressions) |
| G4 | `R_tmin ≥ 100` revised CHI tmin variable-days. **This is a genuine unknown** — no CHI revision count has ever been computed. If CHI revises like NYC (~24%), R ≈ 311; the gate blocks an underpowered Part 2 rather than deciding one. |
| G5 | Unresolved-attribution rate among revised CHI tmin days **< 5%** (strict; quantized) |

Power, from availability facts and NYC-published rates only: at
N = 1,282 per variable, if CHI's rates resemble NYC's, E1's Newcombe
half-width is ≈ ±3.1 percentage points — ample against an ≈ 11-point
effect, and still decisive for any true gap ≳ 4 points. At R_tmin ≈ 311,
E2's Wilson lower bound clears 1/2 for any fraction ≳ 0.56. If CHI's
revision rates are much lower than NYC's, G4 (not an underpowered CI)
is the designed failure mode.

## 9. Robustness and descriptive analyses

All **non-decisional**, computed only after the §7.1 outcome is final.
Anything not listed is out of scope and would require an amendment.

Inherited from H0011 §9 (identical definitions, CHI population):

- **Mandatory adjacent disclosures (AMENDMENT F1):** tmax attribution
  rate; stratified gap decomposition (post- vs pre-midnight stratum
  share of the raw gap, per valid variable-day). Zero-denominator
  ratios are null, never 0 (F7).
- First-appearance local-hour histogram (revised days only, 24 bins,
  per variable — F5).
- Issuance-count distribution per variable-day; single-issuance-day
  counts.
- Flip-flop counts (unrevised-with-deviation; revised-non-monotone).
- Exact-boundary count (expected ≈ 0).
- Within-date tmin/tmax concordance 2×2.
- Unresolved-attribution enumeration.

New in H0013 (registered now, descriptive only):

- **Per-year revision rates** by variable (calendar year of
  observation_date; point estimates only) — the stability disclosure
  mirroring H0002's by-year reporting.
- **Feature summary** per variable: N days, issuance-count mean,
  single-issuance days, min/max observation_date — the dataset-
  characterization block for the report.
- **NYC same-frame consistency cross-check:** Part 1 counts
  (N, R per variable) recomputed from the pinned frame's **NYC** subset
  by the same code path, reported next to H0011's published counts.
  Expected: +1 day per variable (the archive advanced from 2026-07-20
  to 2026-07-21 between pins) and otherwise near-identity. Any larger
  divergence is a data-integrity red flag to investigate — but it
  cannot alter H0013's outcome, which depends only on CHI rows.
- **Figures** (generated from the frozen results JSON + pinned frame
  after the verdict, by a separate non-decisional script):
  (1) revision rates with 95% CIs, CHI vs frozen NYC, by variable, plus
  D with CIs; (2) first-appearance local-hour histograms; (3) issuance-
  count distributions CHI vs NYC; (4) per-year CHI revision rates.
  Figures visualize registered quantities only; no new statistic may be
  introduced by a figure. **Note on "calibration plots":** no
  probabilistic prediction exists in this design, so no Brier/
  reliability analysis applies (RESEARCH.md's calibration machinery is
  for probability models); the CI-with-observed-rate figures above are
  the calibration-adjacent deliverable, and the report must state this
  rather than manufacture a vacuous calibration artifact.

## 10. Reproducibility

| Field | Frozen value |
|---|---|
| Dataset version | `exp-20260722-h0013-replication` (already exported; frozen now, not at execution) |
| Frame | `observation_issuances.parquet`, 26,310 rows (all stations; CHI subset 5,152) |
| Content hash (observation_issuances) | `5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775` |
| Dataset-build git commit | `9285a0145db39d5829b93b648a1e19e5a32c7237` |
| Source DB revision (at dataset build) | `0007` (from `alembic current` at build time; the manifest's own `alembic_revision` field is null — a known manifest-writer gap, recorded here so the pin is not lost) |
| Discovery-sample reference | dataset `exp-20260721-h0002` (NYC), used only via H0011's published results; its hash remains `ec38d1a66d7d93eeccdd6ca6df34d14d6e5214477a50c903e976a24f5f327ca9` |
| Analysis git commit | recorded at execution |
| Software versions | recorded at execution; floors per `pyproject.toml` |
| Timezone database | pip `tzdata` pinned; version recorded; execution blocked if unavailable |
| Randomness | none — fully deterministic |
| Sorting order | frame filtered to CHI, then sorted (variable, observation_date, issuance_time) ascending; iteration tmax_f then tmin_f, dates ascending; output byte-stable |
| Evaluation order | G1 → CHI filter → G2 → G3 → G4 → G5 → E1 → E2 → §7.1 table top-to-bottom → §7.3 replication fields → §9 descriptives |
| Output schema | `TEMPLATE-H0013-manifest.json` (same directory); no key added/removed/renamed after results are seen; plus `EXP-20260722-H0013-chi-replication.md` in `docs/research/experiments/` |
| Determinism verification | script run twice against the pinned frame; the two raw results JSONs must be byte-identical before the result may be reported |

## 11. Implementation-integrity checklist

To be completed before execution (none computes a real-data CHI
statistic). Inherits H0011 §11 in full; the timezone items are
re-derived for America/Chicago:

- [ ] Hand-derive one synthetic example per §7.1 branch from this
      document's formulas alone and confirm the implementation
      reproduces each (Wilson x=60/n=100 → lower bound > 1/2;
      x=55/n=100 → straddle; x=30/n=100 → below; equal-count E1 →
      `L(D) < 0`).
- [ ] Exact-tie tests: operands equal after 1e-12 quantization resolve
      every strict comparison to the unfavorable branch.
- [ ] Zero-event cells: X_pm = 0; a zero-revision variable.
- [ ] Timezone boundary tests **in America/Chicago**: synthetic
      issuances at 23:59:59 / 00:00:00 / 00:00:01 local (00:00:00 →
      post-midnight); observation dates spanning US DST transitions
      (2026-03-08, 2025-11-02); and a UTC-offset check distinct from
      NYC's (CST = UTC−6 / CDT = UTC−5): a naive-UTC issuance at
      05:30 on date T+1 is 23:30 CST on T (pre-midnight) in winter but
      00:30 CDT on T+1 (post-midnight) in summer — the replication's
      one genuinely new conversion regime must have its own tests.
- [ ] Duplicate-row abort at G2; multiple-candidate earliest-appearance
      tie-break; unresolved-attribution denominator handling (as H0011).
- [ ] Replication-assessment tests: CI-overlap true/false/touching
      cases against the frozen NYC literals (touching → overlap per
      closed-interval convention, quantized).
- [ ] Multi-station frame handling: G1 hashes the full frame; the CHI
      filter drops other stations; G2's station set on the filtered
      frame is exactly {CHI}; a frame whose CHI subset is empty fails
      G3, never silently passes.
- [ ] Hand-calculated Wilson and Newcombe cross-checks to ≥10
      significant digits.
- [ ] Formulas reviewed against this document's §6 text, not against
      the H0011 implementation's behavior (re-derive, then compare).
- [ ] Naive-UTC verification: ≥20 sampled **CHI** frame rows
      cross-checked against the source database's timezone-aware
      column; recorded in the manifest; blocking if it fails.
- [ ] Code review of the finished script against §3–§7 before first
      execution.

## 12. Known risks

- **R1 — Cadence confound (inherited, already adjudicated).** H0011's
  audit resolved this with the F1 scope statement, in force here from
  the start (§1). Part 2 is expected to pass for cadence reasons and is
  reported with the adjacent disclosures; it adds label-formation
  facts, not mechanism evidence.
- **R2 — Station non-independence in time.** CHI and NYC share the
  calendar window and continental weather; this is station-, not
  era-generalization (§0, §7.2).
- **R3 — Unknown CHI base rates.** The genuine out-of-sample virtue is
  also the power risk; G4 converts "too few revisions" into a blocked
  run instead of a weak verdict.
- **R4 — Selection of CHI over DEN/LAX.** Chosen for cadence match
  *before* freeze (§0), which limits the replication's breadth: a pass
  generalizes the claim to matched-cadence stations only. Testing the
  other regimes is future work, pre-registered separately.
- **R5 — Naive-UTC interpretation** verified at execution (§11), not
  assumed; CHI's different UTC offset makes this check newly
  substantive rather than inherited.
- **R6 — Archive-quality heterogeneity across stations** (IEM backfill
  differences): coverage and single-issuance counts are reported;
  absent days remain absent.

---

**No CHI revision rate, attribution statistic, timing histogram, or
decision interval has been computed. H0013 has not been executed.**
