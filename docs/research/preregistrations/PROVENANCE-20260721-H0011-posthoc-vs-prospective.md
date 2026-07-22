# PROVENANCE-20260721-H0011-posthoc-vs-prospective

Companion to `PREREG-20260721-H0011-midnight-boundary.md` (frozen
2026-07-21). Purpose: state exactly which parts of H0011 are inherited
from H0002's exploratory evidence (and therefore carry no independent
confirmatory weight on this dataset) and which parts are genuinely
prospective, plus every place the pre-registration had to make a choice
the frozen `HYPOTHESES.md` entry left open. Nothing here modifies the
entry, H0002's artifacts, or the pre-registration itself.

## Post-hoc (inherited from H0002; not independent evidence here)

- **The direction and approximate size of the rate difference.**
  H0002's *exploratory, explicitly non-pre-registered* by-variable
  split (tmin 24.3% vs tmax 13.3%, n = 1,282 variable-days each,
  2022-12-31 → 2026-07-20) is what generated H0011. Part 1 of this
  pre-registration re-tests that split **on the same pinned archive**
  (`exp-20260721-h0002`, hash frozen in the prereg). A Part-1 pass is
  therefore a formalization — an explicit Newcombe CI on a difference
  H0002 published only as two point estimates — and must never be
  reported as replication or independent confirmation. Independent
  confirmation of Part 1 requires a *new* time window or a second
  station (registry expansion), both out of scope for this run.
- **The operational conventions.** Revision definition (first ≠ final),
  denominator convention (≥1 issuance; single-issuance days unrevised),
  final-value definition (max issuance_time), and the America/New_York
  local-time convention are all H0002's, reused verbatim for
  comparability — inherited machinery, not new choices.

## Genuinely prospective (never computed on this archive by anyone)

- **The post-midnight attribution classification** (PREREG §4): which
  revised days' final values first appear at-or-after the local
  midnight ending the observation date. No experiment has computed
  this, for either variable.
- **The attribution fraction `p_pm` and its Wilson interval** (Part 2)
  — the claim carrying the primary new inferential weight, per the
  entry's own failure-mode 3.
- **All timing structure**: first-appearance local-hour histograms,
  the tmax attribution rate, the stratified gap decomposition, the
  concordance table, flip-flop and exact-boundary counts.
- **The conjunctive decision** itself (Part 1 AND Part 2 under the
  frozen decision table).

## Elaboration choices made by the pre-registration (flagged for audit)

These resolve ambiguity in the entry's text; none changes a threshold,
direction, confidence level, or method family the entry froze.

1. **"Excess" wording → plain proportion (PREREG §5 E2, §12 R2).** The
   entry's "fraction of excess tmin revisions (beyond the tmax rate)
   attributable to post-midnight" is not event-level identifiable; the
   entry's own frozen Wilson-CI machinery requires a concrete binomial
   proportion. E2 is frozen as `X_pm / R_tmin` (all revised tmin days
   as denominator) — at H0002's published rates a *stricter* criterion
   than an excess-coverage reading, so the resolution is conservative
   toward confirmation. The identifiable rendering of "excess" (the
   stratified gap decomposition) is reported descriptively.
2. **Uncovered decision branch completed (PREREG §7.1 step 3).** The
   entry's rule is silent on "(1) holds and (2)'s interval lies
   entirely below 50%". Frozen as **Rejected** (a decisively refuted
   conjunct rejects a conjunction), by the same by-addition discipline
   as `AMENDMENT-20260721-H0005-pre-execution.md` Finding 4.
3. **Unpaired interval kept decisional (PREREG §6.3, §12 R6).** The
   entry froze a two-sample ("two-proportion z (or Newcombe)")
   interval; the paired variant — arguably more correct given shared
   calendar dates — is reported as non-decisional robustness. An
   auditor preferring paired-as-primary should require an amendment
   *before* execution.
4. **Attribution boundary details (PREREG §4).** At-or-after 00:00:00
   local counts as post-midnight; earliest appearance of the final
   value governs non-monotone sequences (the entry's own failure-mode-1
   tie-break, applied literally); unresolved cases stay in the
   denominator as non-attributed; a 5% unresolved-rate eligibility gate
   (G5) added as a data-quality bar, set before any attribution input
   was inspected.
5. **Exact-arithmetic and gate machinery (PREREG §6.2, §8).** Frozen
   z-literal, Decimal-50 bounds, 1e-12 quantized strict comparisons,
   and minimum-sample gates — all process hardening imported from the
   H0005 post-mortem, none of it statistical redesign.
6. **Entry-internal wording reconciled (AMENDMENT-20260721-H0011-audit-
   resolution.md Finding 8).** The original `HYPOTHESES.md` H0011 entry
   describes the attribution event two ways across its own text: the
   Hypothesis line says a revision "follows the local-midnight boundary"
   via its **final issuance**; the Experiment design / failure-mode-1
   text says to classify by the **first issuance at which the final
   value appears**. These agree on the modal two-issuance day but can
   diverge on a 3-plus-issuance day where a value settles pre-midnight
   and is only re-confirmed later. PREREG §4 adopted the first-appearance
   reading (the more mechanism-faithful one, and the entry's own
   explicit failure-mode-1 tie-break) as the frozen procedure — no
   change to §4 or to the entry's text; this is a record of a
   discrepancy in the entry's own prose that the audit surfaced,
   resolved in favor of the already-frozen reading.

## Standing scientific-validity disclosure

The single most consequential known risk is **PREREG §12 R1**: the
archive's ≈ 2 issuances/variable-day cadence means Part 2's
issuance-timestamp attribution may classify nearly *all* revisions
(both variables) as post-midnight, draining the mechanism test of
discriminating power while still passing its frozen criterion. The
pre-registration discloses this rather than redesigning around it
(redesign is outside this task's mandate); the tmax attribution rate
and gap decomposition are pre-registered descriptives so the published
record exposes the issue quantitatively either way. The institutional
audit should rule on whether Part 2 as frozen carries the intended
inferential weight before execution proceeds.
