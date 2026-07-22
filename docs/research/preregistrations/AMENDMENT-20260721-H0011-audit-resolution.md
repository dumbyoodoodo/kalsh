# AMENDMENT-20260721-H0011-audit-resolution

**Pre-execution amendment to `PREREG-20260721-H0011-midnight-boundary.md`,
made 2026-07-21, before H0011 execution.** No attribution classification,
timing histogram, or confidence interval has been computed on real data.
This amendment resolves the four **required** findings (F1-F4) and five
**recommended/documentation** findings (F5-F9) returned by the
independent institutional audit of the frozen pre-registration, and does
not alter H0011's hypothesis, estimands (E1, E2), statistical methods,
decision table, eligibility-gate thresholds, or execution logic — every
change below either adds a previously-missing specification or freezes a
reporting/serialization requirement, never a new or changed threshold,
formula for E1/E2, or decision branch.

The original pre-registration document is **not rewritten**. This
amendment resolves the audit's findings by addition, versioned and dated,
per the same discipline `AMENDMENT-20260721-H0005-pre-execution.md`
established for this project. `PREREG-20260721-H0011-midnight-boundary.md`
receives only a short pointer notice; its Sections 0-12 remain unedited.

## Audit record

Institutional review verdict: **APPROVE WITH REQUIRED CHANGES**. Nine
findings (F1-F9); F1-F4 required, F5-F9 recommended/documentation-only.
Resolved below in order.

---

## Finding F1 — E2 identifies label-formation timing, not physical-event timing (resolves F1)

**Audit conclusion, adopted verbatim as this amendment's ruling on the
pre-registration's own §12 question**: E2, as frozen, does not identify
the physical midnight-boundary mechanism; it primarily reflects issuance
cadence. This is the pre-registration's option (b): execution proceeds
unchanged, but any Confirmed outcome carries a binding scope constraint.
**E2's definition (§5), the attribution algorithm (§4), and the decision
rule (§7) are unchanged.**

**Frozen, new — binding scope statement.** Any report of a Confirmed
H0011 outcome (the `EXP-<date>-H0011-midnight-boundary.md` record and the
`HYPOTHESES.md` H0011 Conclusion field) must reproduce the following
sentence verbatim, adjacent to the Part 2 result, and may not report a
Confirmed outcome without it:

> This result is evidence about **settlement-label formation timing**
> only — that revised daily-minimum labels typically reach their final
> value in a post-midnight issuance. It does **not** identify the timing
> of the underlying physical temperature event (when the true daily
> minimum occurred), because this dataset carries issuance timestamps
> only, not occurrence timestamps. A study identifying the physical
> mechanism would require new occurrence-time data (e.g. parsed from raw
> CLI product text) and would constitute a separate, future hypothesis,
> not a re-interpretation of this one.

**Frozen, new — mandatory adjacent descriptive outputs.** Every report of
a Part 2 result, **regardless of outcome** (Confirmed, Rejected, or
Inconclusive), must present the following two pre-registered descriptive
outputs (already specified in PREREG §9, unchanged) immediately adjacent
to the Part 2 result — same table or same paragraph, never in a separate
section a reader could miss:

- the **tmax attribution rate** (§9's tmax-mirrored application of the §4
  procedure) — the cadence baseline; and
- the **stratified gap decomposition** (§9) — how much of the raw
  tmin−tmax rate gap sits in the post-midnight stratum.

This is a reporting-placement requirement on two analyses the
pre-registration already specifies; no new analysis is introduced and no
existing one is altered.

**Manifest structure (minimal, required for self-containment).** The
`TEMPLATE-H0011-manifest.json` `primary_results.part_2_post_midnight_attribution`
object gains one new key, `required_adjacent_fields`, an array naming the
two descriptive-output field paths above (`descriptive_results.tmax_attribution_rate`,
`descriptive_results.stratified_gap_decomposition`) — a cross-reference,
computing nothing new. The `config` block gains one new frozen string
key, `part_2_scope_statement`, holding the sentence above verbatim, so
the manifest is self-contained without depending on this amendment
document at read time.

## Finding F2 — Paired Newcombe robustness check removed (resolves F2)

**Frozen decision: remove, not fully specify.** Per the audit's own
framing ("no preference... ambiguity is the only unacceptable option")
and the instruction to choose whichever option preserves reproducibility
with minimal protocol complexity: fully specifying Newcombe's paired-
difference method to implementation precision requires freezing a
correlation-estimator variant and a zero-cell convention that PREREG
§6.3/§9 never committed to, and that machinery is non-decisional. Removal
is strictly simpler and cannot itself introduce a divergence between
independent implementations, since there is nothing left to diverge on.

**Frozen, superseding PREREG §6.3's closing sentences** ("To let the
audit and the record assess this: the within-date 2×2 concordance table
... is reported descriptively, and a paired Newcombe interval ... is
reported as a non-decisional robustness check (§9)."):

> The paired-interval robustness check is removed from this protocol. The
> within-date tmin/tmax concordance 2×2 table (§9, unchanged) is retained
> as the raw material any future, separately-specified paired analysis
> would need; no paired interval is computed or reported by this
> execution. The decisional interval for E1 remains PREREG §6.1's
> unpaired Newcombe hybrid score interval, unchanged, and the conservative-
> direction argument for that choice under the plausible positive
> correlation of tmin/tmax revision indicators stands as originally
> stated in §6.3.

**Frozen, superseding PREREG §9's "Robustness" bullet**: the "Robustness
(pre-registered, non-decisional)" subsection and its single bullet are
removed in their entirety. §9's "Descriptive" subsection is unchanged.

**Manifest**: the `robustness_results` top-level key (which held only
`paired_newcombe_ci95`) is removed from `TEMPLATE-H0011-manifest.json`.

## Finding F3 — Float→Decimal construction, quantization rounding mode, and round-trip invariant frozen (resolves F3)

**Frozen, extending PREREG §2's value-comparison line** ("All value
equality/inequality tests ... compare Decimal values quantized to 0.01
... Never float equality"):

> **Construction path**: a raw Float64 value `v` is converted to `Decimal`
> exclusively via `Decimal(str(v))` — never `Decimal(v)` directly on the
> float object, which would import the value's full binary-exact (and
> visually misleading) expansion rather than its shortest round-tripping
> decimal representation. **Quantization**: the resulting Decimal is then
> quantized to `Decimal("0.01")` using `ROUND_HALF_EVEN`, for consistency
> with this protocol's own comparison-precision convention (PREREG
> §6.2's 1e-12 quantization, also `ROUND_HALF_EVEN`) — not this
> platform's separate, unrelated money-rounding convention
> (`round_half_up`, reserved for currency/fee computation per
> `AMENDMENT-20260721-H0005-pre-execution.md` Finding 5), which does not
> apply to temperature observations. No other construction path or
> rounding mode may be substituted anywhere in this protocol.
>
> **Round-trip invariant (new, extends G2)**: for every row, the
> constructed-and-quantized Decimal value, converted back to `float`,
> must lie within `1e-6` of the frame's original raw Float64 value. This
> tolerance is roughly four orders of magnitude looser than Float64's
> actual observed representation noise at these magnitudes (~1e-13, per
> the audit) and roughly three orders of magnitude tighter than half the
> quantization step (0.005) — wide enough to never trigger on ordinary
> floating-point noise, narrow enough that it can never mask a genuine
> near-tie value. A violation is treated exactly as G2's existing null/
> uniqueness violations are treated: it aborts the run before any
> classification or interval is computed. This is an additional
> **invariant enumerated under G2** (G2 is already, and remains, an
> invariant-assertion gate — see PREREG §2/§8); it introduces no new gate
> number, changes no eligibility-gate threshold (G3/G4/G5 are untouched),
> and changes no execution-blocking behavior beyond what G2 already does
> on any other invariant failure. Practically, this invariant is expected
> to hold trivially for every row given the source column's
> `numeric(6,2)` precision — freezing it turns that expectation into a
> checked fact rather than an unstated assumption, per the H0005
> post-mortem's lesson on data-domain assumptions.

## Finding F4 — Manifest serialization frozen for byte-identical reproducibility (resolves F4)

**Frozen, new PREREG §10 row** ("Serialization"):

> Every computed proportion, difference, and confidence-interval bound
> reported in `primary_results` (`p_tmax`, `p_tmin`, `d_hat`, the two
> `ci95_newcombe`/`ci95_wilson` bound pairs, `p_pm`) is serialized as a
> **JSON string, not a JSON number**: the underlying `Decimal` value,
> quantized to exactly `1e-12` via `ROUND_HALF_EVEN` (PREREG §6.2's
> existing comparison-precision quantization — the same quantized value
> used to decide §7's comparisons, never a separately-rounded display
> value), rendered in fixed-point notation with **exactly 12 digits**
> after the decimal point, no scientific/exponential notation, a leading
> `-` only if the value is negative (never a leading `+`), and no other
> leading/trailing characters or whitespace (e.g. `"0.108000000000"`,
> `"-0.005000000000"`). Integer counts (`n_tmax`, `n_tmin`, `r_tmax`,
> `r_tmin`, `x_pm`, `n_unresolved`) remain plain JSON integers — only
> non-integer decision-relevant quantities use the string convention.
> Two independent implementations following this specification produce
> byte-identical `primary_results` JSON.

**Manifest**: every affected field's `<TO_BE_FILLED: ...>` placeholder
text in `TEMPLATE-H0011-manifest.json` is updated to state this format
explicitly (see the updated template; no field is renamed, added, or
removed by this change beyond the placeholder wording itself).

---

## Recommended improvements and documentation findings (F5-F9)

None of the below is required for execution to proceed; all are adopted
in this amendment because they cost nothing scientifically and remove
residual ambiguity, per the audit's own framing of them as recommended/
documentation-only.

### F5 — Local-hour histogram population and binning (resolves F5)

**Frozen, extending PREREG §9's histogram bullet**: "Local-hour histogram
of the final value's first-appearance issuance, by variable" covers
**revised variable-days only** (an unrevised day's "first appearance of
the final value" is definitionally its only issuance and would inflate
the histogram with non-events). Binning: 24 bins, one per local
wall-clock hour `0`-`23` (the hour component of the classified
first-appearance issuance's `America/New_York` local time), computed
separately per variable.

### F6 — Gate-failure reporting sentinel (resolves F6)

**Frozen, extending PREREG §8**: gates are evaluated strictly in the
frozen order G1→G2→G3→G4→G5. On the first failing gate, evaluation stops;
every gate field at or after the failing gate that would otherwise
require inputs from later stages is recorded in the manifest as the
literal string `"not_evaluated"`, never a computed value, `null`, or a
placeholder number. `TEMPLATE-H0011-manifest.json`'s `eligibility_gates`
block documents this sentinel inline.

### F7 — Null convention for descriptive ratios (resolves F7)

**Frozen, extending PREREG §9**: any descriptive ratio whose denominator
is zero (e.g. `tmax_attribution_rate` if `r_tmax = 0`) is recorded as
JSON `null`, exactly as AMENDMENT-20260721-H0005-pre-execution.md Finding
7 froze for H0005's empty-cell calibration error — never fabricated as
`0` or omitted. This case is not expected to occur (H0002 published
~170 tmax revisions on this same archive) but is frozen for completeness.

### F8 — Entry-internal wording reconciled (resolves F8)

**Documentation only, added to
`PROVENANCE-20260721-H0011-posthoc-vs-prospective.md`** (not the
pre-registration, since it concerns the *original* `HYPOTHESES.md` entry's
wording, not this protocol's own text): the frozen `HYPOTHESES.md` H0011
entry uses two descriptions of the attribution event across its
Hypothesis prose ("whose final issuance follows the local-midnight
boundary") and its Experiment design / failure-mode-1 text ("the value
change first appears... classify by the first issuance at which the
final value appears"). These agree for the modal two-issuance day but can
diverge on a 3-plus-issuance day where a value settles before midnight
and is merely re-confirmed by a later post-midnight issuance. PREREG §4's
"earliest appearance of the final value" reading is the mechanism-
faithful one and is confirmed as the frozen procedure; this note records
the discrepancy in the entry's own text for the audit trail. No change to
PREREG §4 (already correct) or to the frozen `HYPOTHESES.md` entry.

### F9 — Unresolved-attribution catch-all tightened (resolves F9)

**Frozen, extending PREREG §4.7**: the exhaustive trigger list for
"unresolved attribution" is exactly `{an issuance timestamp that fails
UTC/zoneinfo conversion, a null value or timestamp surviving §2's/F3's
assertions}` — no broader "any other mechanical failure" category exists.
The expected count of unresolved cases is **zero** (both triggers are
already supposed to be structurally excluded by G2/F3); a nonzero count
at execution is itself flagged as a reportable data anomaly in the
results, not merely fed into G5's rate gate silently.

---

## Change log — every audit finding mapped to its resolution

| Finding | Severity | Resolution | Where |
|---|---|---|---|
| F1 | Required | Binding scope statement (verbatim, frozen) + mandatory adjacent tmax-rate/gap-decomposition reporting for every Part 2 result, any outcome; manifest cross-reference field and frozen scope-statement string added | This amendment §F1; `TEMPLATE-H0011-manifest.json` `config.part_2_scope_statement`, `primary_results.part_2_post_midnight_attribution.required_adjacent_fields` |
| F2 | Required | Paired Newcombe robustness check removed entirely (not specified) | This amendment §F2; PREREG §6.3/§9 superseded by addition; `robustness_results` removed from manifest template |
| F3 | Required | Float→Decimal construction path (`Decimal(str(v))`) and quantization rounding mode (`ROUND_HALF_EVEN`) frozen; round-trip invariant (1e-6 tolerance) added, enumerated under existing gate G2 | This amendment §F3; PREREG §2 superseded by addition |
| F4 | Required | Manifest serialization frozen: fixed-point Decimal strings, exactly 12 digits after the decimal point, 1e-12 quantization, no scientific notation | This amendment §F4; PREREG §10 superseded by addition; `TEMPLATE-H0011-manifest.json` placeholder text updated |
| F5 | Recommended | Histogram scope (revised-only) and binning (24 local-hour bins) frozen | This amendment §F5 |
| F6 | Recommended | `"not_evaluated"` sentinel frozen for gate fields after a first gate failure | This amendment §F6 |
| F7 | Recommended | Zero-denominator descriptive ratios recorded as `null`, never `0` | This amendment §F7 |
| F8 | Documentation | Entry-internal wording discrepancy recorded in the provenance note; PREREG §4's reading confirmed correct, unchanged | This amendment §F8; `PROVENANCE-20260721-H0011-posthoc-vs-prospective.md` |
| F9 | Documentation | Unresolved-attribution trigger list closed to exactly two named cases; nonzero count flagged as an anomaly | This amendment §F9 |

## Validation

Mechanical checks performed before declaring this amendment complete:

- [x] F1's scope statement and adjacency requirement do not alter E2's
      definition (PREREG §5), the attribution algorithm (§4), or any
      decision-table branch (§7) — confirmed by diffing this amendment's
      content against those sections, which are only ever referenced,
      never restated with different substance.
- [x] F2's removal does not touch E1's decisional interval (PREREG
      §6.1) or the unpaired-vs-paired choice already approved by the
      audit (R6, PREREG §12) — only the non-decisional robustness output
      is removed.
- [x] F3's invariant is enumerated under the existing gate G2, not a new
      gate; G3/G4/G5's thresholds (N≥1000, R≥100, unresolved<5%) are
      untouched.
- [x] F4 changes only how already-computed values are serialized, never
      how they are computed or compared (§7's decisions are made in
      Decimal before this serialization step, per PREREG §6.2, unchanged).
- [x] F5-F9 add no new estimand, threshold, or decision branch.
- [x] No horizon-equivalent, bucket-equivalent, confidence level,
      z-constant, gate threshold, or decision-table branch was changed —
      confirmed by diffing this amendment's content against
      `PREREG-20260721-H0011-midnight-boundary.md`'s existing frozen
      values, which are only ever restated verbatim or extended by
      addition above, never altered.
- [x] H0011 has not been executed; no attribution classification, timing
      histogram, or confidence interval exists to have been inspected.

---

**Pointer added to `PREREG-20260721-H0011-midnight-boundary.md`, without
altering its existing Sections 0-12 — see that document's own top-level
notice.**
