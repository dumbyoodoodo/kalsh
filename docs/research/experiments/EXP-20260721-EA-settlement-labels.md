# EXP-20260721-EA-settlement-labels

E-A: settlement-time label reconstruction — engineering validation (payout
agreement) plus the pre-registered H0012 stage-difference analysis. Executed
exactly as pre-registered; no threshold altered after seeing data.
Machine-readable results: `EXP-20260721-EA-settlement-labels-results.json`;
analysis: `scripts/exp_ea_settlement_labels.py` (deterministic).

## Hypothesis

**H0012 — Settlement-time labels are stable; close-time labels are not.**
(A) P(value_at_close ≠ latest_final) > 5%; (B) P(latest_final ≠
value_at_settlement) < 2%. Pre-registered rule: confirmed iff A's CI lower
bound > 5% AND B's CI upper bound < 2%; rejected if A's upper < 5% OR B's
lower > 2%; else inconclusive. Unit: (variable, target_date) — strikes share
a timeline and are not double-counted.

## Reproducibility

| Field | Value |
|---|---|
| Dataset version | `exp-20260721-ea-labels` (frames incl. `settlement_labels`, hashes in `manifest.json`) |
| Reconstruction version | `1` (`settlement/labels.py`; recorded in manifest config) |
| Source DB revision | `0006` |
| Git commit | the commit containing this record (label module, migration, and analysis added together) |
| Data acquisition | settled KXHIGHNY/KXLOWTNYC markets retro-fetched read-only (828 markets persisted; API returns markets only for ~the most recent 2-3 months of events — older market history is unrecoverable, documented in ADR 0006); CLI issuance history from EXP-0001's 3.5-year backfill |
| Date run | 2026-07-21 |

## Coverage and validation

- 828 markets fetched (close times 2026-05-16 → 2026-07-23); **804 settled
  with exact `settlement_ts`** — no bounded labels were needed in this
  sample (the bounded path remains tested code for future gaps).
- Labels: **792 resolved**, 36 `missing_source_data` (markets for dates
  after the CLI history's end 2026-07-20, i.e. not-yet-settled events, plus
  the 2026-07-02 malformed-CLI day documented in EXP-0001). Nothing
  ambiguous; nothing silently dropped.
- **Payout agreement (engineering gate): 791/791 = 100.0%** on both checks —
  reconstructed `value_at_settlement` equals Kalshi's paid
  `expiration_value`, and the implied strike result equals Kalshi's `result`,
  for every settled market with Kalshi settlement data. Zero value
  mismatches, zero result mismatches. This simultaneously validates the
  as-of reconstruction, the issuance archive's completeness at settlement
  time, and the strike semantics (`between` inclusive; `greater`/`less`
  strict) across 791 real payouts.
- Per-event strike consistency check passed (all strikes of an event agree
  on timeline values, enforced in the analysis).

## Results (pre-registered metrics; n = 132 variable-dates, all exact)

| Claim | Result | 95% CI | Threshold | Met? |
|---|---|---|---|---|
| A: P(value_at_close ≠ latest_final) | **18.94%** (25/132) | [13.17%, 26.47%] | lower > 5% | **Yes** |
| B: P(post-settlement correction) | **0.00%** (0/132) | [0.00%, 2.83%] | upper < 2% | **No — n too small** |

Supporting pre-registered metrics: P(value_at_settlement ≠ value_at_close) =
18.94% (identical set to A — every close-vs-final difference was already
resolved by settlement time); mean |Δ| close-vs-final = 3.16°F; by variable:
tmax 13.6% (9/66), tmin 24.2% (16/66) — matching H0002's 3.5-year rates
(13.3% / 24.3%) almost exactly; post-settlement corrections zero in both
variables; sensitivity (exact-`settlement_ts`-only) identical since all
labels were exact. All revisions in this window were same-day-continuation
(final morning report), none were later corrections.

**Decision (pre-registered rule): INCONCLUSIVE** — claim A confirmed, claim
B's point estimate is perfect (0 corrections observed) but zero events at
n=132 gives a Wilson upper bound of 2.83% > 2%. Zero observed corrections
requires **n ≥ 189** variable-dates to clear the 2% bound; at current
two-variable collection (~2 dates/day) the retry threshold is reached after
**~29 more days of market accumulation — retry on or after 2026-08-19**.
The threshold is not adjusted retroactively.

## Why 18.76% (H0002) is not "universal label noise"

H0002's preliminary-to-final rate (18.76%) measures first-issuance vs final —
which mostly captures *expected intraday information flow*: the ~4pm
preliminary is issued before its own day ends, so an evening high or a
post-midnight low legitimately updates it. This experiment decomposes the
timeline: by the time Kalshi settles (~8am ET, after the final morning
report), **zero** of those revisions remained outstanding in this window.
The close-time label inherits ~19% instability (markets close at ~00:59 ET,
before the final report — traders at close genuinely don't know the answer
~1 time in 5); the settlement-time label was final 132/132 times. "Label
noise" is a property of *which stage you read*, not of the settlement system.

## Limitations

- Market window is ~2 months (API retention) and summer-only; H0002 showed
  revision behavior varies seasonally — B's retry will still be
  summer/fall-weighted. Winter coverage requires continuous collection.
- NYC only, as with all label results so far.
- 36 unlabeled markets are structurally explained (future dates + one
  malformed source day) but reduce coverage at the margins.
- The 100% payout agreement is over dates where our archive has issuances;
  it cannot rule out agreement failures on archive-gap days (none were in
  the settled window except 2026-07-02, which is excluded and counted).
