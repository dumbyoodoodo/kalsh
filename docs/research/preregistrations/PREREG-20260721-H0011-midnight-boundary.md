# PREREG-20260721-H0011-midnight-boundary

**Pre-registration package for H0011 — frozen 2026-07-21, before any
attribution classification, timing histogram, or decision statistic has
been computed.** This document elaborates the existing `HYPOTHESES.md`
H0011 entry (opened 2026-07-21, `Status: Proposed`) to full mechanical
precision, exactly as `PREREG-20260721-H0005-calibration.md` did for
H0005. It does not alter H0011's hypothesis direction, thresholds,
confidence levels, or statistical machinery — every place it makes a
specific choice the entry left open is catalogued in
`PROVENANCE-20260721-H0011-posthoc-vs-prospective.md` (same directory),
and the two places it must *resolve an ambiguity* in the entry's own
decision-rule text are flagged inline for the institutional audit
(§6.3, §7.1).

**This document computes and reports no attribution statistic, no
timing histogram, and no confidence interval on real data.** H0011 has
not been executed. The only real-data facts cited are those already
published by `EXP-20260721-H0002-cli-revisions.md` and the pinned
dataset's own manifest.

Directory note: earlier pre-registrations live flat in
`docs/research/experiments/`; from H0011 onward they live in
`docs/research/preregistrations/` per the updated convention. Execution
artifacts (`EXP-*`) continue to live in `docs/research/experiments/`.

> **Amendment notice (2026-07-21, before execution).** An independent
> institutional pre-registration audit returned **APPROVE WITH REQUIRED
> CHANGES**, with nine findings (F1-F9). All nine are resolved by
> addition in `AMENDMENT-20260721-H0011-audit-resolution.md`, which
> governs wherever it adds precision to the text below — including a
> binding scope statement on E2/Part 2 (F1), removal of the
> non-decisional paired-Newcombe robustness check (F2), a frozen
> float→Decimal construction path and quantization rounding mode plus a
> round-trip invariant (F3), and a frozen manifest serialization format
> (F4). This document's original wording (Sections 0-12) is preserved
> unedited; the amendment is additive. No hypothesis, estimand,
> statistical method, decision-table branch, or eligibility-gate
> threshold changed. H0011 has still not been executed.

## 0. Relationship to H0002

H0002 is closed (Confirmed, 2026-07-21) and is not reopened or altered.
Facts carried forward, all already published in
`EXP-20260721-H0002-cli-revisions.md`:

- The issuance-level extract exists (`observation_issuances`, dataset
  `exp-20260721-h0002`, 5,148 rows, NYC, 2022-12-31 → 2026-07-20), with
  99.5% of variable-days carrying ≥2 issuances.
- H0002's by-variable **exploratory** split: tmin_f revised 24.3% of
  1,282 days vs tmax_f 13.3% of 1,282 days. This split *motivated*
  H0011 and is therefore **not independent evidence for it** — see §1
  and the provenance note. No other H0002 statistic is load-bearing
  here.
- H0002's operational conventions, reused verbatim for comparability
  (§2-§3): revision = first-issued value ≠ final-issued value;
  denominator = all station/variable-days with ≥1 stored issuance
  (single-issuance days count as unrevised and are reported as a
  coverage caveat); "final" = value at the maximum stored
  `issuance_time`; local-time classification uses America/New_York.

## 1. Research question

**Two-part claim (conjunctive), exactly as the frozen H0011 entry
states it:**

- **Part 1 (rate difference).** For NYC, tmin_f settlement labels
  revise more often than tmax_f labels: the difference in
  variable-day revision proportions, `D = p_tmin − p_tmax`, is
  positive with statistical confidence.
- **Part 2 (mechanism attribution).** The tmin excess is driven by the
  local-midnight boundary: among revised tmin variable-days, the
  proportion whose final value first appears in an issuance timestamped
  at or after local midnight of the day following the observation date
  exceeds one half, with statistical confidence.

**Epistemic status of each part, stated plainly (per the entry's own
failure mode 3):**

- Part 1 is **partially post-hoc**: its direction and approximate
  magnitude were observed in H0002's exploratory by-variable split, on
  the *same* 3.5-year archive this experiment will reuse. Its role here
  is formalization (an explicit CI on the difference, which H0002 never
  computed), not independent discovery. A Part-1 pass on this dataset
  must never be described as replication.
- Part 2 carries the **primary new inferential weight**: no attribution
  classification, midnight-boundary count, or timing histogram has ever
  been computed on this archive, by H0002 or any other experiment.
- External validity (any claim beyond NYC) additionally requires
  replication on ≥1 more station once the registry expands — frozen in
  the entry, restated here, and out of scope for this run.

## 2. Population and unit of analysis

All frozen; none may change after any result is seen.

| Item | Frozen value |
|---|---|
| Station | `NYC` only (the registry's only station; NYC-only claim) |
| Variables | Exactly `tmax_f` and `tmin_f` (the archive's only variables; asserted at execution) |
| Source frame | `observation_issuances.parquet` from dataset **`exp-20260721-h0002`** — already pinned; content hash frozen in §10. No new dataset build; no execution-time dataset discretion exists. |
| Date range | Every `observation_date` present in the pinned frame (2022-12-31 → 2026-07-20 per the frozen manifest; recorded, not re-filtered) |
| Unit of analysis | The **variable-day**: one row per (station_id, variable, observation_date), built from that day's issuance set |
| Valid variable-day | ≥1 stored issuance (mirrors H0002's denominator convention exactly). Single-issuance days are **counted as unrevised** in Part 1's denominators and reported separately as coverage; they can never appear in Part 2 (a revision requires ≥2 issuances). |
| Issuance stages | Within a variable-day, sort issuances by `issuance_time` ascending: **first** = minimum `issuance_time`; **final** = maximum `issuance_time`; all others are **intermediate**. |
| Canonical label | The final-stage value (H0002's "final settled value" convention). This experiment is about label formation, not market settlement; `value_at_settlement`/market data are not used anywhere. |
| Duplicates | The frame must be unique on (station_id, variable, issuance_time) — the source database's own dedup constraint. Asserted before any processing; a violation aborts the run (never silently deduplicated). |
| Missing data | A variable-day absent from the archive is absent (H0002 documented ~1.2% day gaps and one malformed 2026-07-02 product); absent days are counted in coverage reporting and excluded from all denominators — never imputed. Rows with a null value or null `issuance_time` are structurally impossible (database NOT NULL); asserted anyway, with any violation aborting. |
| Value comparison | All value equality/inequality tests (revision detection, final-value first-appearance) compare `Decimal` values quantized to **0.01** (the source column's `numeric(6,2)` precision). Never float equality. |
| Timezone | `America/New_York` (IANA), resolved via Python `zoneinfo` backed by a **pinned `tzdata` package whose version is recorded in the manifest** (execution is blocked if the zone resolves only against an unpinned system database). The pinned frame's `issuance_time` is timezone-naive and is interpreted as **UTC** (the platform-wide internal convention); §11 requires an execution-time cross-check of a sample of rows against the source database's timezone-aware column before any classification. |
| DST | Handled by the IANA database. Note for the record: US DST transitions occur at 02:00 local, never at 00:00, so the local-midnight boundary (§4) exists exactly once on every date; §11 still requires explicit tests on transition dates. |

## 3. Revision definitions

- **Revision (the Part 1 event):** a variable-day is *revised* iff its
  first-stage value ≠ final-stage value (Decimal-quantized comparison,
  §2). This is H0002's exact definition, reused for comparability.
- **Which transitions count:** only first-vs-final. A day whose value
  changes at an intermediate issuance but returns to the first value by
  the final issuance (first == final) is **not revised** — mirroring
  H0002. Such "flip-flop" days are counted descriptively (§9), never
  reclassified.
- **Multiple revisions on one variable-day:** Part 1 uses an
  **any-revision indicator** per variable-day (0/1), exactly as H0002
  did. Per-day revision counts (number of value changes across the
  issuance sequence) are reported descriptively only (§9) and feed no
  decision.
- **Corrections vs observation-stage changes:** this design does not
  (and, from this frame, cannot) distinguish a late correction from an
  ordinary final report — the frame carries issuance times and values
  only. No correction/continuation classification is attempted here;
  that distinction belongs to H0012's settlement-timeline
  reconstruction, not this experiment. All first≠final changes count
  equally as revisions.

## 4. Post-midnight attribution (deterministic, no discretion)

Applied to every revised **tmin_f** variable-day (and, descriptively
only, to every revised tmax_f variable-day — §9). For a revised
variable-day *d* with observation date *T*:

1. **Sort** *d*'s issuances by `issuance_time` ascending. (Uniqueness
   already asserted, §2; there are no timestamp ties by constraint.)
2. **Final value** `v_final` := the value at the maximum
   `issuance_time`, Decimal-quantized (§2).
3. **First appearance** := the *earliest* issuance in the ascending
   sequence whose quantized value equals `v_final`. This is the entry's
   own frozen tie-break ("classify by the first issuance at which the
   final value appears" — H0011 failure mode 1). If the final value
   appears, disappears, and reappears across the sequence, the first
   appearance still governs (literal frozen text); such non-monotone
   cases are counted descriptively (§9).
4. **Boundary** := 00:00:00 local (`America/New_York`) on calendar date
   *T + 1 day* — "local midnight of the observation date" in the
   entry's wording, i.e. the instant the observation date ends.
5. **Classification**: the revision is **post-midnight-attributed** iff
   `local(first appearance issuance_time) ≥ boundary` (at-or-after: an
   issuance at exactly 00:00:00 belongs to the next day, since the
   observation date is over at that instant). Otherwise it is
   **pre-midnight**. Issuances landing exactly on the boundary are
   counted descriptively (§9; expected ≈ 0).
6. **Multiple candidate issuances**: impossible by construction — step
   3 selects a unique earliest issuance (timestamps are unique). No
   manual classification exists anywhere in this procedure.
7. **Unresolved attribution**: a revised tmin variable-day whose
   classification cannot be mechanically completed (an unconvertible
   timestamp, a null value surviving the §2 assertions, or any other
   mechanical failure). Unresolved days are individually enumerated in
   the results, **remain in Part 2's denominator, and count as NOT
   post-midnight-attributed** — the conservative direction (they can
   only make confirmation harder). The unresolved rate is also an
   eligibility gate (§8).

## 5. Primary estimands

- **E1 (rate difference).** `D = p_tmin − p_tmax`, where for each
  variable `v`: `p_v = R_v / N_v`; `N_v` = number of valid variable-days
  (§2); `R_v` = number of revised variable-days (§3). Units:
  proportion (dimensionless), reported also in percentage points.
  **Sign convention: positive D means tmin revises more often** — the
  hypothesized direction.
- **E2 (post-midnight attribution fraction).** `p_pm = X_pm / R_tmin`,
  where `X_pm` = number of revised tmin variable-days classified
  post-midnight-attributed (§4; unresolved days excluded from the
  numerator, included in the denominator). Units: proportion.
  Direction: larger means more midnight-attributable.

## 6. Statistical methods

All closed-form and deterministic. **This experiment contains no
randomness whatsoever** — no bootstrap, no resampling, no seed
(`random_seed: not applicable` in the manifest). The entry's frozen
machinery (two-proportion/Newcombe CI; Wilson CI) is used and nothing
else.

### 6.1 Interval methods

- **E1**: **Newcombe hybrid score interval** for a difference of two
  independent proportions (Newcombe 1998, "method 10"), **without**
  continuity correction, two-sided, **95%**. With per-sample Wilson
  bounds (l₁,u₁) for p̂_tmin and (l₂,u₂) for p̂_tmax:
  `L(D) = (p̂₁−p̂₂) − sqrt((p̂₁−l₁)² + (u₂−p̂₂)²)`,
  `U(D) = (p̂₁−p̂₂) + sqrt((u₁−p̂₁)² + (p̂₂−l₂)²)`.
- **E2**: **Wilson score interval**, **without** continuity correction,
  two-sided, **95%**:
  `center = (p̂ + z²/2n) / (1 + z²/n)`,
  `halfwidth = z·sqrt(p̂(1−p̂)/n + z²/4n²) / (1 + z²/n)`.
- **z** is frozen as the exact literal `Decimal("1.959963984540054")`
  (the two-sided 95% normal quantile to 15 significant digits). No
  library quantile function may be substituted.

### 6.2 Exact-arithmetic policy (H0005 lesson, applied from the start)

- Counts are Python integers; point proportions are exact `Fraction`s.
- CI bounds are computed in `Decimal` at a frozen context precision of
  **50 significant digits** (`Decimal.sqrt` is correctly rounded under
  the context, hence deterministic).
- **Every strict decision comparison** (`> 0`, `> 1/2`, `< 1/2`, and
  the gate comparisons of §8) is decided only after quantizing **both**
  operands to **1e-12** (`ROUND_HALF_EVEN`); quantized equality means
  the strict inequality is **not** satisfied. No binary-float value may
  ever reach a decision comparison. Reported JSON values may be floats;
  decisions are made before, and independently of, serialization.

### 6.3 Sidedness, pairing, and dependence

- Both intervals are **two-sided 95%**, with one-sided *decision
  criteria* read off their bounds (§7) — exactly the entry's own
  construction ("CI excluding zero"; "CI lower bound above 50%").
- **Pairing**: tmin and tmax variable-days share calendar dates, so the
  two samples in E1 are not independent. The entry froze an
  *unpaired* two-proportion interval ("two-proportion z (or Newcombe)
  CI"), and that is what decides Part 1. Under non-negative
  cross-variable correlation of revision indicators (the plausible
  direction — shared weather regimes), the unpaired interval is
  conservative for the difference. To let the audit and the record
  assess this: the within-date 2×2 concordance table (both revised /
  only tmin / only tmax / neither, over dates with both variables
  present) is reported descriptively, and a **paired** Newcombe
  interval (his paired-difference score method, computed from the
  discordant counts) is reported as a non-decisional robustness check
  (§9). *Flagged for audit: the paired interval is arguably the more
  correct primary; this pre-registration keeps the entry's frozen
  unpaired choice as decisional and discloses the alternative rather
  than silently substituting it.*
- **Serial dependence** across consecutive dates (weather persistence)
  is acknowledged and untreated, mirroring H0002's own frozen Wilson
  convention on the same archive; conclusions inherit that caveat. The
  entry contemplates no clustering machinery for this design and none
  is added.

## 7. Decision rule

### 7.1 Decision table (exhaustive; evaluated in this order; each row terminal)

| Step | Condition (all comparisons per §6.2) | Outcome |
|---|---|---|
| 0 | Any §8 eligibility gate fails | **EXECUTION BLOCKED** — not a scientific outcome; no interval is computed or inspected; the gate report is published and the run stops. |
| 1 | `L(D) ≤ 0` (including exact quantized equality, and including the sub-case `U(D) < 0`) | **Rejected** (Part 1 fails — no confident positive rate difference; the sub-case actually observed is recorded: zero-straddling vs. confidently negative) |
| 2 | `L(D) > 0` **and** `L(p_pm) > 1/2` | **Confirmed** |
| 3 | `L(D) > 0` **and** `U(p_pm) < 1/2` | **Rejected** (mechanism refuted: the rate difference exists but the attribution fraction is confidently below one half, so the conjunctive claim fails) |
| 4 | `L(D) > 0` **and** `L(p_pm) ≤ 1/2 ≤ U(p_pm)` | **Inconclusive** (mechanism unresolved — the entry's own "straddles 50%" branch) |

Boundary behavior, frozen explicitly: exact quantized equality of
`L(D)` with 0, or of `L(p_pm)` or `U(p_pm)` with 1/2, always resolves
to the *less favorable* branch for confirmation (equality is never
"exceeds"; an interval touching 1/2 at either end straddles it).

*Flagged for audit:* step 3 completes a branch the H0011 entry's text
does not cover (the entry names only "confirmed if (1) and (2);
rejected if (1) fails; inconclusive if (1) holds but (2) straddles").
An interval lying *entirely below* 1/2 decisively refutes the mechanism
conjunct, and a decisively-refuted conjunct rejects a conjunctive
hypothesis — the same by-addition completion discipline
`AMENDMENT-20260721-H0005-pre-execution.md` Finding 4 applied to
H0005's evaluation order. No threshold or direction is altered.

### 7.2 What the outcomes mean (scope limits)

- **Confirmed** claims, for NYC and this archive's window only: tmin
  labels revise more often than tmax, and a confident majority of
  revised tmin days settled to their final value only in a
  post-midnight issuance. It does *not* claim causation by any specific
  meteorological mechanism, and (per §1) Part 1's pass is a
  formalization, not a replication.
- **Rejected/Inconclusive** carry the symmetric scope limits. No
  outcome licenses any market or trading conclusion.

## 8. Power and eligibility gates

All gates are verified mechanically at execution start, **before any
confidence interval is computed**; gate inputs are counts and
classifications only. Any failure → step 0 of §7.1 (blocked; stop).

| Gate | Frozen requirement | Grounding (published facts only) |
|---|---|---|
| G1 | The loaded frame's content hash equals the frozen hash (§10) exactly | pins the analysis to `exp-20260721-h0002` byte-for-byte |
| G2 | Frame unique on (station_id, variable, issuance_time); no null value/issuance_time/observation_date; station set == {NYC}; variable set == {tmax_f, tmin_f} | source DB constraints; asserted, not trusted |
| G3 | `N_tmax ≥ 1000` and `N_tmin ≥ 1000` valid variable-days | H0002 published 1,282 each; the gate exists to catch extract regressions, not because doubt exists |
| G4 | `R_tmin ≥ 100` revised tmin variable-days | H0002's published tmin rate (24.3% of 1,282 ⇒ ≈ 311 expected). At R = 100, a Wilson lower bound above 1/2 requires p̂ ≳ 0.60 — the criterion remains decidable, not vacuous |
| G5 | Unresolved-attribution rate among revised tmin variable-days **< 5%** (strict; quantized comparison) | a data-quality bar set before any attribution input has been inspected; unresolved days additionally remain in the denominator as non-attributed (§4.7) |

Power statement from published numbers only: with `N ≈ 1,282` per
variable and H0002's published rates, E1's Newcombe interval half-width
is on the order of ±3 percentage points against an ≈ 11-point observed
gap — Part 1 is overwhelmingly powered (unsurprising, and
epistemically discounted, since it is the same data that produced the
motivating split). With `R_tmin ≈ 311`, E2's Wilson lower bound clears
1/2 for any attribution fraction ≳ 0.56 — Part 2 is decidable across
essentially its whole plausible range except a narrow band just above
one half, which is exactly the entry's pre-registered Inconclusive
region.

## 9. Robustness and descriptive analyses

Everything below is **non-decisional**: computed after the §7 outcome
is already final, never feeding back into it. Primary = §5/§7 only.

**Robustness (pre-registered, non-decisional):**
- Paired Newcombe interval for D from the within-date discordant
  counts (§6.3), reported next to the primary unpaired interval.

**Descriptive (entry-permitted "revision-timing histograms" plus
counts the audit needs; no CIs on any of these):**
- The same §4 attribution procedure applied to revised **tmax** days:
  the tmax post-midnight attribution rate. This is the discriminating-
  power disclosure for the §12 risk R1 — if tmax revisions are also
  near-universally post-midnight-attributed, the audit and readers see
  it immediately.
- Stratified gap decomposition, point estimates only:
  `(r_pm,tmin − r_pm,tmax)` and `(r_pre,tmin − r_pre,tmax)` where
  `r_pm,v` = post-midnight-attributed revisions per valid variable-day
  of variable v — i.e., how much of the raw tmin−tmax gap sits in the
  post-midnight stratum. (The identifiable, descriptive rendering of
  the entry's "excess" language — see §12 risk R2.)
- Local-hour histogram of the final value's first-appearance issuance,
  by variable.
- Issuance-count distribution per variable-day; single-issuance-day
  count (Part 1 coverage caveat, mirroring H0002).
- Flip-flop counts: (a) unrevised days whose intermediate values
  deviated; (b) revised days where `v_final` first appeared and later
  deviated before the terminal run (§4.3's non-monotone case).
- Exact-boundary count: issuances landing at exactly 00:00:00 local.
- Within-date tmin/tmax revision concordance 2×2 (§6.3).
- Unresolved-attribution enumeration (§4.7): count and per-day listing.

No exploratory category is registered for this experiment. Anything
not listed above is out of scope and would require an amendment before
being computed.

## 10. Reproducibility

| Field | Frozen value |
|---|---|
| Dataset version | `exp-20260721-h0002` (already pinned — frozen *now*, not at execution) |
| Frame | `observation_issuances.parquet`, 5,148 rows |
| Content hash (observation_issuances) | `ec38d1a66d7d93eeccdd6ca6df34d14d6e5214477a50c903e976a24f5f327ca9` |
| Dataset-build git commit | `2e76d32065b46b3e470bcf7bf088d393c5d6fc1f` |
| Source DB revision (at dataset build) | `0005` |
| Analysis git commit | recorded at execution (the commit containing the implemented script) |
| Software versions | Python/polars/etc. recorded at execution, as in prior EXP records; floors per `pyproject.toml` |
| Timezone database | pip `tzdata` package pinned in the environment; exact version recorded in the manifest; execution blocked if unavailable (§2) |
| Randomness | none — fully deterministic; `random_seed: not applicable` |
| Sorting order | input frame sorted by (variable, observation_date, issuance_time) ascending before processing; iteration order: tmax_f then tmin_f, dates ascending. Results are order-independent (closed-form); the ordering is frozen so serialized output is byte-stable |
| Evaluation order | G1→G5 gates → E1 interval → E2 interval → §7.1 decision table top-to-bottom — each step's inputs computed exactly once |
| Output schema | `TEMPLATE-H0011-manifest.json` (same directory) — every key present, none added/renamed after results are seen; plus an `EXP-<date>-H0011-midnight-boundary.md` human-readable report in `docs/research/experiments/` |
| Determinism verification | the script is run twice against the pinned frame; the two results JSONs must be byte-identical before the result may be reported (H0007/H0005 convention) — trivially expected here (no RNG) but still mandatory |

## 11. Implementation-integrity checklist

To be completed **before** execution (none of these steps compute a
real-data attribution or decision statistic). Incorporates the H0005
post-mortem's process lessons directly:

- [ ] **Hand-derive one synthetic example per decision branch** (steps
      1-4 of §7.1) from this document's formulas alone — *not* from the
      implementation — and confirm the implementation reproduces each.
      Suggested synthetic inputs: Wilson at (x=60, n=100) → lower bound
      just above 1/2 (Confirmed branch when paired with a passing E1);
      (x=55, n=100) → interval straddling 1/2 (Inconclusive branch);
      (x=30, n=100) → interval entirely below 1/2 (Rejected-at-step-3
      branch); an E1 configuration with equal counts → `L(D) < 0`
      (Rejected-at-step-1). The implementer must derive the exact
      bounds independently (Decimal, §6.2) — the branch expectations
      above are the check, the derived numbers are the evidence.
- [ ] **Exact-tie tests**: feed the decision comparator two operands
      equal after 1e-12 quantization (including a constructed
      `L(p_pm)` exactly at 0.5) and confirm every strict comparison
      resolves to the unfavorable branch. No float comparison path may
      exist.
- [ ] **Zero-event cells**: `X_pm = 0` with `R_tmin` at the gate
      minimum (Wilson at x=0 — interval well-defined, entirely below
      1/2 → step 3); a variable with zero revisions in a synthetic
      frame (E1 with r=0 — Wilson at zero numerator well-defined).
- [ ] **Timezone boundary tests**: synthetic issuances at 23:59:59,
      00:00:00, and 00:00:01 local around the boundary (the 00:00:00
      case must classify post-midnight, §4.5); observation dates
      spanning the US DST transitions (2026-03-08 and 2025-11-02):
      conversion must use the pinned IANA data and produce the
      documented classification.
- [ ] **Duplicate-observation test**: a frame with a duplicated
      (station, variable, issuance_time) row must abort loudly at G2 —
      never silently deduplicate.
- [ ] **Multiple-candidate test**: a synthetic revised day whose final
      value appears at more than one issuance (including a
      non-monotone appear-deviate-reappear sequence) must attribute by
      the *earliest* appearance (§4.3) and count the non-monotone case
      descriptively.
- [ ] **Unresolved-attribution test**: a synthetic mechanically-
      unclassifiable case must land in the denominator as
      non-attributed, be enumerated, and drive the G5 gate arithmetic.
- [ ] **Hand-calculated cross-check**: at least one full Wilson and one
      full Newcombe interval computed by hand (or an independent tool)
      to ≥10 significant digits and compared against the
      implementation's Decimal output.
- [ ] **Formulas reviewed against this document's text** — §6.1's
      formulas as written here, not against any existing code's
      behavior (the H0005 lesson: re-derive from the frozen text, do
      not confirm the code against itself).
- [ ] **Naive-UTC verification**: cross-check ≥20 sampled frame rows'
      `issuance_time` against the source database's timezone-aware
      column to confirm the naive values are UTC (§2) — recorded in the
      manifest (`issuance_time_utc_verified`), blocking if it fails.
- [ ] Code review of the finished script against §3-§7 before first
      execution, per the standard workflow.

## 12. Known risks (disclosed for the audit; none resolved by fiat)

### R1 — Part 2's attribution may reflect issuance cadence rather than the intended mechanism

Nothing in this subsection changes E2's definition (§5), the
attribution algorithm (§4), or the decision rule (§7) — it separates
four distinct facets of one disclosed risk so the audit can rule on
each precisely.

**Statistical validity — not in question.** E2 is a well-defined
binomial proportion over a fully deterministic classification, and the
Wilson interval and decision comparisons of §6 are valid for it exactly
as frozen. Whatever the audit concludes below, the *statistics* are
sound for the estimand as defined; the question is what the estimand
measures.

**Causal identifiability — the substantive concern.** The entry's
mechanism claim is about *why* tmin labels revise more: the daily
minimum's proximity to the local-midnight boundary ("the calendar-day
attribution of a cooling trend is decided at the boundary"). E2,
however, measures when the final value **first appeared in an
issuance** — not when, or why, the underlying temperature event
occurred. A post-midnight first appearance is consistent with the
intended mechanism, but it is equally consistent with a mundane
alternative: the reporting cycle simply publishes its final report
after midnight for every variable-day, regardless of variable or
cause. E2 therefore may not separate "midnight-boundary physics" from
"reporting-cycle timing," and a Confirmed Part 2 under E2 would not,
by itself, distinguish the two.

**Archive cadence — the empirical fact that makes the confound likely
to bind.** The pinned archive averages ≈ 2.008 issuances per
variable-day (5,148 rows / 2,564 variable-days): the typical day has
exactly a same-day preliminary (~16:30 local) and a next-morning final
(~01:30 local). On a two-issuance day, *any* first≠final revision
necessarily first appears in the post-midnight final issuance — for
tmax exactly as for tmin. The attribution fraction may therefore be
near 1 for both variables, letting Part 2's `> 1/2` criterion pass
near-trivially while carrying little mechanism-specific information.
The §9 tmax attribution rate and stratified gap decomposition are the
pre-registered instruments that quantify exactly this in the published
record, whichever way the run resolves.

**Measurement limitations — why no sharper attribution is available
here.** The frame carries no time-of-occurrence field (schema: station,
variable, value, observation_date, issuance_time, raw_payload_id), so
no attribution finer than issuance timestamps is mechanically possible
from this dataset. Occurrence times do exist in raw CLI product text,
but extracting them would require new parsing work and an amendment —
out of scope for this pre-registration, which registers only what the
pinned frame supports.

#### Question for Institutional Audit

Reviewers are asked to determine, **before execution**: does E2, as
frozen, identify the intended midnight-boundary mechanism, or does it
primarily reflect issuance cadence? Concretely, the audit should select
one of: (a) Part 2 as frozen carries the inferential weight the entry
intends, and execution may proceed unchanged; (b) execution may
proceed, but any Confirmed outcome must be reported with an explicit
cadence-confound caveat bounding the mechanism claim; or (c) an
amendment introducing a sharper attribution measure (e.g., parsed
occurrence times) is required before execution. This pre-registration
deliberately does not resolve the question unilaterally.

### Other known risks (R2-R6)

- **R2 — The entry's "excess" wording is resolved, not implemented
  literally.** The entry's criterion (2) speaks of "the fraction of
  excess tmin revisions (beyond the tmax rate)" — an event-level
  quantity that does not exist (no individual revision is identifiably
  "excess"). The only reading compatible with the entry's own frozen
  Wilson-CI machinery is a plain proportion over an actual event set;
  §5's E2 (all revised tmin days as denominator) is that reading. At
  H0002's published rates it is *stricter* than an excess-coverage
  reading (it demands post-midnight attributions exceed half of *all*
  tmin revisions, ≈ 12.2 points of rate, versus half of the ≈ 11-point
  excess ≈ 5.5 points), so this resolution cannot manufacture a
  confirmation the looser reading would deny. The identifiable
  rendering of the excess idea is reported descriptively (§9). Flagged
  for audit.
- **R3 — A decision branch is completed by addition** (§7.1 step 3);
  flagged there.
- **R4 — Part 1 re-tests its own motivating data** (§1); the provenance
  note carries the full statement.
- **R5 — Naive-UTC interpretation** of the frame's timestamps is a
  platform convention, verified (not assumed) at execution (§11).
- **R6 — Pairing choice** (§6.3): the decisional interval is the
  entry's unpaired Newcombe; the paired variant is disclosed as
  robustness. An auditor preferring the paired interval as primary
  should require an amendment before execution, not after.

---

**No attribution statistic, timing histogram, or decision interval has
been computed. H0011 has not been executed.**
