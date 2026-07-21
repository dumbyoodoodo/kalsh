# AMENDMENT-20260721-H0005-pre-execution

**Pre-execution amendment to `PREREG-20260721-H0005-calibration.md`, made
2026-07-21, before H0005 execution.** No H0005 calibration outcome has been
inspected. No price data has been examined. This amendment resolves the
nine findings (F1-F9) returned by the independent institutional
pre-registration audit and does not alter H0005's research question,
hypothesis direction, horizons, probability buckets, statistical
thresholds, success criteria, economic rationale, or experimental scope —
every value below either restates an existing frozen value verbatim or
adds a previously-missing specification, never a new or changed threshold.

The original pre-registration document is **not rewritten**. This
amendment resolves ambiguity by addition, versioned and dated, per the
same discipline `AMENDMENT-20260721-H0007-pre-execution.md` established
for this project and `RESEARCH.md` already applies to research plans and
settlement labels.

## Audit record

Institutional review recommendation: **APPROVE WITH REQUIRED CHANGES**.
Nine findings (F1-F9), resolved below in order.

---

## Finding 1 — Research-question / decision-rule consistency (resolves F1)

**Problem**: §1's closing sentence could be read as stating that the
calibration (null-survival) claim itself requires cells to "clear a
family-wise significance screen and replicate" — the opposite of §12,
where clearing both screens is what **rejects** H0005.

**Frozen correction, superseding §1's closing sentence:**

> Kalshi NYC daily-high (`KXHIGHNY`) market prices, observed at six
> pre-registered fixed times-to-close and grouped into ten pre-registered
> probability deciles, are **calibrated within transaction costs**: this
> claim **survives** unless at least one pre-registered (horizon × decile)
> cell shows a deviation between mean implied probability and realized
> YES-settlement frequency that **both** clears a family-wise significance
> screen in an initial slice of the data **and** replicates — same sign,
> still exceeding its own cost band — in a temporally subsequent,
> non-overlapping slice. Clearing both screens for even one cell
> **rejects** the calibration claim for that cell; failing to clear both
> for every cell **confirms** it.

This is the identical claim §1 already intended (matching §12's decision
rule, which is unchanged) — only the sentence connecting "clearing
screens" to "confirmed" vs. "rejected" is corrected. No threshold, cell
definition, or success criterion changes.

## Finding 2 — Bootstrap sampling pools (resolves F2)

**Frozen, superseding the implicit reading of §10's "Held-out replication
split":**

> The discovery-slice bootstrap resamples **exclusively** from
> discovery-slice settlement days — never from hold-out-slice days, and
> never from a pooled set later filtered. The hold-out-slice bootstrap
> resamples **exclusively** from hold-out-slice settlement days — never
> from discovery-slice days. The two day-pools are constructed once, at
> the start of execution, from the frozen chronological split (§10, as
> resolved by Finding 8 below), and are never combined, re-pooled, or
> otherwise mixed at any point in either resampling procedure.

## Finding 3 — Deterministic RNG consumption order (resolves F3)

**Frozen, exhaustive specification** (new; §10 named a seed but not a
consumption order):

> Exactly **one** `random.Random(20260721)` instance is instantiated once
> per execution. It is the **only** source of randomness in the entire
> run. It is consumed, in this exact fixed order, and no other order:
>
> 1. **Discovery-slice bootstrap**, for all 60 primary-family
>    (`KXHIGHNY`, mid-price) cells, iterated with horizon as the **outer**
>    loop in ascending order `[2h, 6h, 12h, 24h, 48h, 72h]` and decile as
>    the **inner** loop in ascending order `[0,10), [10,20), ...,
>    [90,100]` — 60 bootstrap calls, each drawing 10,000 resamples (§10).
> 2. **Hold-out-slice bootstrap**, for the same 60 cells, in the identical
>    nested order (horizon outer ascending, decile inner ascending) — 60
>    further bootstrap calls.
>
> Hold-out-slice bootstrapping is performed **unconditionally for all 60
> cells**, regardless of whether a given cell cleared the discovery
> screen — this is itself a resolution, not a new design choice: computing
> every cell's hold-out CI unconditionally (rather than only for
> discovery-passers) removes a second, previously-implicit branch point
> that would otherwise make the RNG consumption order data-dependent. The
> decision rule (§12, as ordered by Finding 4 below) reads which cells
> cleared both screens from this complete, unconditionally-computed table.
>
> **Nothing else consumes the RNG.** For the avoidance of doubt, per
> §9/§10's own "point estimates only" / "descriptive" language for
> secondary and exploratory results: the `KXLOWTNYC` exploratory cell, the
> by-month descriptive breakdown, the four secondary metrics (Brier score,
> log loss, ECE, calibration slope/intercept), and the ask/bid executable-
> price robustness check (§8) receive **no bootstrap CI and consume no
> randomness** — matching `AMENDMENT-20260721-H0007-pre-execution.md`'s
> identical treatment of its own tmin_f exploratory cell.
>
> Two independent implementations of this specification, given the same
> pinned dataset, must produce byte-identical bootstrap draws and
> therefore byte-identical CIs.

## Finding 4 — Evaluation order for Confirmed/Rejected/Inconclusive (resolves F4)

**Frozen, superseding §12's unordered branch list — evaluated in this
exact sequence, each step terminal (a later step is reached only if the
prior step does not resolve the outcome):**

> 1. **Total-sample check.** If `settlement_days_available < 60`:
>    outcome = **Inconclusive** (reason: insufficient total sample). Stop.
> 2. **Rejection check.** Using the complete 60-cell table (Finding 3): if
>    **at least one** cell clears both the Bonferroni-adjusted discovery
>    screen and the hold-out replication screen (same sign, hold-out CI
>    lower bound on `|calibration_error|` exceeding that slice's cost
>    band): outcome = **Rejected**, listing every clearing cell. Stop.
> 3. **Power check.** If no cell cleared both screens (step 2 found none):
>    check whether **at least one** cell cleared the discovery screen
>    alone but was marked `insufficient_n` (fewer than 20 day-clusters,
>    §10) in the hold-out slice specifically. If so: outcome =
>    **Inconclusive** (reason: inconclusive-for-power), naming exactly
>    which cells. Stop.
> 4. **Otherwise**: outcome = **Confirmed**.
>
> This ordering resolves the vacuous-truth ambiguity directly: if **zero**
> cells ever clear the discovery screen, step 3's condition ("at least one
> cell that passed discovery but was insufficient-N in hold-out") is
> false by construction — there is no such cell — so execution falls
> through to step 4, **Confirmed**. A clean discovery pass with no
> flagged cells is unambiguously Confirmed, never Inconclusive.

## Finding 5 — Exact fee-function invocation (resolves F5)

**Frozen, superseding §9's informal `fee_cents_for_one_contract_at_
round(mid_price_cents)` notation:**

> The fee term of the cost band is computed as exactly:
>
> ```
> contract_fee_cents(
>     price_cents=round_half_up(mid_price_cents),
>     contracts=1,
>     config=KALSHI_WEATHER_TAKER_FEE_CONFIG,
> )
> ```
>
> `contracts=1` (never a sized position — the cost band represents
> per-contract friction, matching H0007's own unit-stake convention,
> reused verbatim, not re-derived). `round_half_up` resolves the one
> remaining implicit default: `mid_price_cents` is a plain average of two
> integers and may land exactly on a half-cent (e.g. bid 79 + ask 82 → mid
> 80.5); such values round **up** to the next whole cent (80.5 → 81),
> consistent with this platform's existing fee-rounding convention
> (`docs/research/experiments/AMENDMENT-20260721-H0007-fee-verified.md`
> §2.4 / §3: every rounding rule already established on this platform
> rounds up, never to nearest-even or down). No other rounding rule may be
> substituted.

## Finding 6 — `close_time` immutability (resolves F6)

Per this task's explicit instruction not to investigate, immutability is
**not asserted**. The second option the audit offered is taken instead:

**Frozen addition to the Execution Checklist (§16), inserted as a new
first step, before dataset build:**

> - [ ] **Verify `close_time` immutability.** Before pinning the dataset
>       version, confirm — by inspecting `MarketSnapshot` history for a
>       sample of `KXHIGHNY`/`KXLOWTNYC` markets, or by consulting official
>       Kalshi documentation — whether a market's `close_time` can change
>       after that market first appears in the collected archive. If
>       immutable: record the evidence in the execution manifest
>       (`config.close_time_immutability_verified: true`, with a citation)
>       and proceed. If **not** immutable, or if this cannot be confirmed:
>       do not proceed with the `_market_metadata`-reduced `close_time`
>       field as the horizon anchor without first determining, and
>       recording in the manifest, which markets (if any) in the pinned
>       dataset had a `close_time` revision — this is a **blocking**
>       pre-execution item, not a caveat to note after the fact.

This adds a checklist gate; it does not change §6's horizon definitions,
§5's dataset definition, or any threshold.

## Finding 7 — Empty-cell behavior (resolves F7)

**Frozen, extending §9's metric definitions to the `N = 0` case (not
previously specified beyond decision-rule eligibility):**

> - **`calibration_error`**: for a cell with zero valid observations,
>   `calibration_error` is **undefined** (recorded as `null`, never
>   fabricated as `0.0` or any other placeholder value).
> - **ECE contribution**: `ECE_h = sum_d (n_d / N_h) * |calibration_error_
>   {h,d}|` — a cell with `n_d = 0` has weight `n_d / N_h = 0` and is
>   **excluded from the summation entirely** (implemented as skipping
>   empty cells in the loop, not as evaluating `0 * undefined`, which
>   would risk `NaN` propagation depending on implementation). This is
>   mathematically equivalent to the natural zero-weight convention and
>   keeps `ECE_h` well-defined even when some deciles are empty at some
>   horizons.
> - **Regression participation**: the per-horizon calibration
>   slope/intercept logistic regression (§9) operates on individual
>   observations pooled across all deciles at that horizon, not on
>   cell-level aggregates — an empty decile bucket structurally
>   contributes zero rows to that horizon's regression; no special case is
>   required beyond this statement.
> - This is purely a completeness addition to already-frozen formulas;
>   `ECE`, calibration slope/intercept, and `calibration_error` remain
>   exactly as defined in §9.

## Finding 8 — Chronological split tie-breaking (resolves F8)

**Frozen, extending §10's "Held-out replication split":**

> If the post-embargo valid-day population has an **odd** count, the
> **discovery slice** receives the extra (chronologically earliest) day —
> i.e., discovery gets `ceil(n/2)` days and hold-out gets `floor(n/2)`
> days, where `n` is the day count remaining after the 1-day embargo is
> removed. Rationale: the discovery slice carries the stricter,
> Bonferroni-adjusted screening burden (§10) and benefits more from the
> additional day than the hold-out slice, whose role is a fixed-threshold
> (standard 95%) confirmatory check that does not itself need to be
> maximally powered to serve its purpose. This is a tie-breaking rule
> only; it does not change the definition of the split, the embargo, or
> which slice is "discovery" vs. "hold-out."

## Finding 9 — Duplicate-observation invariant checks (resolves F9)

**Frozen, new pre-analysis validation step (inserted into the
Implementation Checklist, §15, and the Execution Checklist, §16, as a
required first step of the analysis script itself, run before any bucket
assignment or bootstrap call):**

> Before computing any cell statistic, the analysis script must assert
> both of the following, aborting the run with an explicit error (never
> silently proceeding or de-duplicating) if either fails:
>
> 1. **One row per (market, horizon).** After as-of candle selection
>    (§6), a `group_by(["market_ticker", "horizon_hours"]).count()` over
>    the analysis table must show every group count equal to exactly 1.
> 2. **Underlying candle uniqueness.** The candle selected for each
>    (market, horizon) pair is drawn from `market_price_weather`, whose
>    rows are already guaranteed unique on `(market_ticker,
>    period_interval_seconds, period_end)` by the platform's own database
>    constraint (`docs/adr/0007-price-ingestion.md`); the script asserts
>    this constraint's natural key remains unique in the loaded frame
>    before selection, as a defense against a corrupted or hand-edited
>    dataset export rather than an expected failure mode.
>
> Both checks are structural validations of an already-expected invariant
> (per §5's design), not new filtering logic — they exist so a violation
> is caught loudly, at the start of the run, rather than silently
> distorting a downstream cell statistic.

## Validation

Mechanical checks performed before declaring this amendment complete:

- [x] F1's corrected sentence states the identical claim §12 already
      encodes — no threshold or direction changed, confirmed by comparison.
- [x] F2's pool-separation rule is stated for both slices, symmetrically.
- [x] F3's RNG order is exhaustive: every statistic in §9/§10 that could
      plausibly consume randomness is explicitly accounted for (bootstrapped
      or explicitly declared non-random), not just the ones named in F1-F9.
- [x] F4's four-step order is evaluated for the specific zero-cells edge
      case that motivated the finding, and resolves to Confirmed as intended.
- [x] F5's invocation matches `contract_fee_cents`'s actual signature
      (`scripts/h0007_fees.py`) exactly — `price_cents`, `contracts`,
      `config`, no other parameters exist to omit.
- [x] F6 adds a checklist gate without asserting an unverified fact.
- [x] F7's three sub-definitions cover every place §9's formulas reference
      cell population size.
- [x] F8's tie-break is a single, stated rule with a rationale, not left
      to implementer discretion.
- [x] F9's two checks are assertions (fail loudly), not silent corrections.
- [x] No horizon, decile boundary, price definition, statistical
      threshold (Bonferroni family/alpha, confidence level, minimum-N,
      minimum-day floor), or success-criterion substance was changed —
      confirmed by diffing this amendment's content against
      `PREREG-20260721-H0005-calibration.md`'s existing frozen values,
      which are only ever restated verbatim above, never altered.
- [x] H0005 has not been executed; no price data was inspected; no
      calibration outcome exists to have been inspected.

---

**Pointer added to `PREREG-20260721-H0005-calibration.md` and to
`HYPOTHESES.md`, without altering their existing text — see those files'
own top-level notes.**
