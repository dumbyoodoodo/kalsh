# PREREG-20260721-H0005-calibration

**Pre-registration package for H0005 — frozen 2026-07-21, before any
calibration outcome has been inspected.** This document elaborates the
existing `HYPOTHESES.md` H0005 entry (opened 2026-07-21, `Status: Proposed`)
to full mechanical precision, exactly as `PREREG-20260721-H0007-bound-
violations.md` did for H0007. It **updates only implementation** — the
data source, sampling mechanics, and execution details a calibration study
of archived candles requires that a study of prospective 5-minute snapshots
did not. It does **not** alter H0005's economic rationale, hypothesis
direction, or the substance of its success criteria. Every deviation from
the original design is catalogued, separately, in
`docs/research/experiments/DIFFERENCES-20260721-H0005-from-original-design.md`.

**This document computes, inspects, or reports no price data, no
calibration outcomes, and no statistics.** H0005 has **not** been executed.

## 0. Relationship to H0007

H0007 is closed (Rejected, 2026-07-21) and is not reopened, revisited, or
used to justify any change to H0005's design here. The only facts carried
forward from H0007 are **platform-capability facts**, exactly the same
class of legitimate prior input H0002's result was for H0007's own
pre-registration:

- The `market_price_weather` frame, the as-of join engine (`dataset/asof.py`,
  0 leakage violations over 761,475 rows), the verified settlement-label
  reconstruction (791/791 payout agreement), and the verified fee
  configuration (`KALSHI_WEATHER_TAKER_FEE_CONFIG`) all exist and are
  reusable — this is infrastructure, not a finding about market behavior.
- The 66-67-valid-day, 804-settled-market population size is a
  data-availability fact already published (Phase 7A/7B/H0007's own
  reproducibility records), not an H0005 outcome.
- **Not carried forward**: anything about *whether or where* prices are
  miscalibrated. H0007's finding (settlement-window logical bounds are
  priced efficiently) says nothing about calibration at other horizons or
  under genuine probabilistic uncertainty — see the Economic Motivation
  section for why these are independent questions.

## 1. Research Question

**Precise claim (null form, per the original entry — H0005 is expected to
broadly survive).** Kalshi NYC daily-high (`KXHIGHNY`) market prices,
observed at six pre-registered fixed times-to-close, are **calibrated
within transaction costs**: grouped into ten pre-registered probability
deciles, the mean absolute deviation between each cell's mean implied
probability and its realized YES-settlement frequency does not exceed that
cell's own mean round-trip transaction cost (fee + half-spread), across
every pre-registered (horizon × decile) cell, in a way that both clears a
family-wise significance screen *and* replicates in a temporally
subsequent, non-overlapping slice of the data.

**Operational definitions** (avoiding the vague terms the task calls out):

- **Calibration**: a forecast is calibrated if, among all instances
  assigned predicted probability *p*, the realized outcome frequency is
  *p* (the standard frequentist definition — Dawid 1982; the reliability-
  diagram criterion). Operationalized here per (horizon, decile) cell:
  `calibration_error = mean_predicted_probability − realized_yes_frequency`
  within the cell.
- **Efficient pricing** (the economic claim H0005 tests, distinct from
  pure calibration): prices may be *statistically* miscalibrated by a
  small amount and still be *economically* efficient if that miscalibration
  is smaller than what it costs to trade against it. H0005's threshold is
  therefore the **cost band**, not zero — a market can fail strict
  statistical calibration and still pass H0005.
- **Miscalibration**: a cell where `|calibration_error|`'s confidence
  interval lies entirely above that cell's own cost band — i.e., the
  deviation is large enough, with statistical confidence, to exceed what
  it costs to transact on it.
- **Uncertainty**, in this document, always means *sampling* uncertainty
  (confidence intervals on the estimates above) — H0005 makes no claim
  about and does not estimate any latent "true" weather-outcome
  probability distribution (that is H0003/H0004's territory).
- **Edge**: a persistent, replicated calibration failure exceeding cost —
  i.e., exactly the condition under which H0005 (the null) is rejected.
  H0005 itself never computes a trading P&L; it only establishes *whether*
  the precondition for an edge claim exists.

## 2. Economic Motivation

**Why the null is still market efficiency.** Kalshi daily-temperature
markets are open to any participant with free access to the same NWS
forecast products this platform itself uses; nothing about the contract
structure creates a structural reason a rational, informed participant
population would leave a persistent, cost-exceeding calibration failure
unexploited over a 66-67-day window spanning an entire summer weather
regime. The efficient-market default is therefore the correct null, exactly
as the original entry states — H0005 is not designed to find an edge; it is
designed to be the gate every edge claim must clear first.

**Why calibration failures could nonetheless exist** (candidate mechanisms,
none assumed true — each is a distinct, separately-testable hypothesis this
project already has entries for, which H0005 is explicitly designed to
*localize evidence for*, not adjudicate directly):

- **Anchoring** (H0006): participants anchor on the point forecast and
  underprice residual uncertainty, especially at long horizons where the
  true forecast-error distribution is widest — predicts calibration
  failure concentrated in far-decile cells (near 0/100) at long horizons.
- **Horizon-dependent uncertainty / inattention** (H0008, H0010):
  information arrives on a forecast-issuance schedule; if attention is
  episodic rather than continuous, calibration could degrade between
  issuance events and sharpen right after them — predicts failure
  clustered by horizon independent of decile.
- **Tail-probability misestimation**: prediction markets are documented in
  the wider literature to sometimes overprice long-shot outcomes
  ("favorite-longshot bias") — predicts failure concentrated in the
  extreme deciles (0-10%, 90-100%) specifically.
- **Liquidity segmentation** (H0009): thin books may deviate more, but the
  economically relevant question is whether they deviate *faster than
  their own wider cost band grows* — H0005's cost-band-relative framing
  (not a raw-price test) is specifically designed so a liquidity effect
  alone cannot manufacture a false rejection here.
- **Risk-transfer / hedging demand**: if some participants use these
  contracts to hedge non-weather exposure (e.g. energy trading), prices
  could be systematically shifted for reasons unrelated to temperature
  belief — H0005 cannot distinguish this mechanism from anchoring or
  inattention; a confirmed rejection here motivates, but does not itself
  identify, a mechanism (H0006/H0008/H0010 exist for that).

None of these mechanisms is asserted; they motivate *why a careful
calibration study is worth running* even though the efficient-market null
is the default expectation.

## 3. Information Set

Defined precisely against `market_price_weather`'s actual columns
(`docs/adr/0008-point-in-time-alignment.md`), mirroring
`PREREG-20260721-H0007-bound-violations.md` §3's discipline.

**Allowed, as of a sampled horizon instant *t* for a given market:**

- That market's own OHLC trade prices, OHLC bid/ask quotes, volume, open
  interest, and `data_quality_status`, from the single as-of-selected
  candle (§5) and, for descriptive purposes only, every earlier candle of
  the same `market_ticker`.
- Static market metadata knowable at any time: `market_ticker`,
  `event_ticker`, `floor_strike`, `cap_strike`, `strike_type`, `close_time`.
- The verified fee configuration (`KALSHI_WEATHER_TAKER_FEE_CONFIG`,
  `scripts/h0007_fees.py`) — a platform constant, not price data.
- The pre-registered horizon set, decile boundaries, and cost-band formula
  themselves (design parameters fixed in this document, not data).

**Forbidden, absolutely:**

- `value_at_settlement`, `latest_final_value`, `settlement_label_status`,
  `settlement_label_version` — used **only** at the final outcome-frequency
  computation (§9), after bucket assignment (horizon + decile) is already
  fixed from pre-outcome information, never to influence which cell an
  observation falls into, which horizon is sampled, or which candle is
  selected.
- Any candle (any market) with `period_end` later than the sampled instant
  *t* — enforced structurally by the same backward as-of selection
  `dataset/asof.py` already uses (§5).
- `obs_value_known`, `running_tmax_f_known`/`running_tmin_f_known`,
  `tmax_locked`/`tmin_locked`, `theoretical_remaining_range_*_f` — these
  columns exist in the frame and are not forbidden as *leakage* (they are
  themselves point-in-time correct), but they are **out of scope for
  H0005**: using them to explain or filter calibration cells would be
  H0006/H0007-style analysis, not a calibration measurement, and must not
  be introduced here without its own pre-registration.
- `price_close_cents` (trade price) as an executable price — per the
  platform-wide convention already established in
  `AMENDMENT-20260721-H0007-pre-execution.md` §8 ("quotes are primary...
  trade price may be stale and must never be used as an executable
  price"), reused verbatim here, not re-derived.
- Any manual, discretionary, or "this cell looks interesting" judgment not
  encoded in the mechanical bucket/horizon definitions below (§6-§8).

## 4. Preservation of the Original Hypothesis

Per this task's own instruction, the following are **retained exactly**
from the original `HYPOTHESES.md` H0005 entry and are not reconsidered
here:

- Null-form framing: H0005 is expected to broadly survive; the research
  value is in *where*, if anywhere, it fails.
- The core statistic: implied-probability deciles compared against
  realized settlement frequency, evaluated against a **cost band**
  threshold (fees + half-spread), not a zero-deviation threshold.
- Cluster-bootstrap confidence intervals, clustered by day (not by raw
  observation) — "snapshots of one market are not independent."
- A replication requirement on a **held-out, temporally later slice**
  before any cell supports "rejected."
- One primary series (`KXHIGHNY`, NYC daily high); other series
  (`KXLOWTNYC`) are exploratory only, per the original's own explicit
  multiple-comparison guard.
- Brier score vs. an always-base-rate baseline as a required metric.
- The three original failure-mode mitigations (day-level clustering;
  minimum-N gate for sparse buckets; executable-price robustness check for
  mid-price fiction) — all three are frozen with exact numbers/formulas
  below where the original left them qualitative.

Every place this document makes a *specific choice* the original left
open (an exact horizon list, exact decile boundaries, an exact minimum-N,
an exact bootstrap seed, an exact replication-split rule) is a
**previously-unspecified implementation detail**, not a change in
direction — see `DIFFERENCES-20260721-H0005-from-original-design.md` for
the itemized list.

## 5. Dataset Definition

**Source frame**: `market_price_weather` (built via
`dataset build --which market_price_weather`), joined to `settlement_labels`
in the same build, exactly as `PREREG-20260721-H0007-bound-violations.md`
used. **No dataset build has been run for this pre-registration** — per
`RESEARCH.md`'s "pin the dataset" step, the specific dataset version and
content hashes are recorded at execution time, not now (matching H0007's
own precedent).

**Required schema/version fields** (to be recorded verbatim in the
execution manifest, current values as of this freeze, git commit
`b30f92e3c8db17e2a246fb1be9d1f6328161a1e9`):

| Field | Value at freeze |
|---|---|
| `MARKET_PRICE_WEATHER_SCHEMA_VERSION` | `"1"` |
| `RECONSTRUCTION_VERSION` (settlement labels) | `"1"` |
| Source DB Alembic revision | `0007` |
| Fee config | `KALSHI_WEATHER_TAKER_FEE_CONFIG`, verified 2026-07-21 |

**Market inclusion**: every `KXHIGHNY-*` market (primary) and
`KXLOWTNYC-*` market (exploratory) present in `market_price_weather` with
`settlement_label_status == "resolved"` and non-null `value_at_settlement`.

**Market exclusion**:

- Markets/days with `settlement_label_status == "missing_source_data"` —
  excluded from the day-level population entirely and reported as a
  coverage statistic, identical treatment to H0007 §4 condition 8 /
  AMENDMENT Finding 2 (a day is excluded only if **every** primary-series
  market for that date lacks a resolved label).
- No market is excluded a priori for illiquidity, sparse candle coverage,
  or any outcome-related property — the original entry's own survivorship
  guard, reused verbatim from H0007's Bias Review §10.

**Halted markets**: this platform's `market_price_weather` frame carries
no distinct "halted" trading-status column (unlike a traditional equities
feed) — `MarketSnapshot.status` values observed to date are
`initialized`/`active`/`finalized`, with no halt state, and that column is
not even present in `market_price_weather` (it is candle-grain, not
snapshot-grain). **No halt-detection mechanism exists on this platform as
built**, stated plainly rather than assumed away: the only proxies that
would incidentally catch a de facto halted period are the coherent-quote
requirement (§8) and the staleness tolerance (§6), both of which already
exclude a period with no valid quote regardless of cause.

**Sparse candles**: per Phase 7A's coverage finding, some markets
(particularly "between"-strike, illiquid contracts) have far fewer stored
candles than their observed span implies, because Kalshi's own API emits
no candle for many idle minutes. This is **not** an exclusion criterion
(illiquid markets are retained, per the survivorship guard above); it is
handled by the staleness-tolerance rule in §6, which independently, per
observation, excludes a horizon sample too far from its target instant —
the natural consequence being that sparse markets contribute fewer valid
observations, not that they are dropped outright.

**Missing data (general)**: any (market, horizon) pair for which no candle
exists within the staleness tolerance (§6) of the target instant is
excluded from that specific cell and counted in a coverage report
(`observations_excluded_stale`), never imputed, forward-filled, or
silently treated as "at the cost band."

## 6. Time-to-Close Buckets

**Frozen horizon set**: **{72h, 48h, 24h, 12h, 6h, 2h}** before each
market's own `close_time`.

**Justification**: 48h, 24h, 12h, and 2h are the four horizons the
original `HYPOTHESES.md` entry itself named as examples — retained
exactly. Two horizons are added, each justified by data availability
established *before* this freeze (never by inspecting this experiment's
own price data): 72h is added because the candle archive's own backfill
window (`CANDLE_LOOKBACK_DAYS = 5` = 120 hours before each market's close,
`docs/adr/0007-price-ingestion.md`) comfortably covers it for every
market, giving a genuine "far from settlement" anchor point the original's
longest example (48h) could not by itself distinguish from "not far
enough." 6h is added to give resolution between the original's 12h and 2h
examples, motivated by H0007's own **closed, prior, platform-capability**
finding that the immediate settlement end-game is priced efficiently —
motivating a look at whether that efficiency extends earlier in the final
day or is confined to the last hour(s). Neither addition was chosen by
examining H0005's own price data, which has not been touched.

**As-of candle selection**: for a market with `close_time = C`, the
observation at horizon *h* uses the single candle with the **latest**
`period_end <= (C − h)` — a strict backward as-of selection, identical in
spirit to `dataset/asof.py`'s engine. **Staleness tolerance**: if the
nearest such candle's `period_end` is more than **15 minutes** before
`(C − h)`, the observation is excluded (§5's "missing data" rule) rather
than treated as current. Fifteen minutes is chosen because it is small
relative to every horizon gap (the closest pair, 6h/2h, differs by 4
hours) while being generous enough not to discard markets whose candle
archive has brief legitimate gaps (Phase 7A found sparse markets can go
tens of minutes between emitted candles).

**No adaptive horizon selection.** These six values are frozen; no horizon
may be added, removed, or adjusted after seeing any result.

## 7. Moneyness (Probability Decile) Definition

**Frozen**: ten equal-width bins of the primary (mid-price) implied
probability, in percentage points: **[0,10), [10,20), [20,30), [30,40),
[40,50), [50,60), [60,70), [70,80), [80,90), [90,100]** (the final bin
closed on both ends, since Kalshi's own price ceiling is 99¢/99%,
naturally landing inside it).

This operationalizes "moneyness" as the original entry's own "deciles"
language already specified, rather than as strike-distance-in-degrees
(a fundamentally different, not-comparable-across-strikes metric that the
original never proposed and this document does not introduce). A market
with mid-implied probability 62.4% falls in the [60,70) bucket regardless
of how many degrees its strike sits from the current forecast — bucket
assignment uses price alone, computed before any outcome is known.

**No adaptive bucketing.** These ten boundaries are frozen; no bucket may
be split, merged, or re-centered after seeing any result.

## 8. Price Definition

**Primary: midpoint.** `mid_price_cents = (yes_bid_close_cents +
yes_ask_close_cents) / 2` at the as-of-selected candle (§6). **Justification**:
the original entry's own design already specifies "market mid-prices" as
primary — retained exactly, not reconsidered. Economically, the midpoint
is the standard reliability-diagram convention because it is unbiased with
respect to the bid-ask bounce in a way neither the bid nor the ask alone
is (the ask is mechanically inflated by half the spread relative to the
market's central belief; the bid is mechanically deflated by the same
amount) — using either alone as the *primary* calibration target would
manufacture an apparent miscalibration that is really just the spread,
exactly the "mid-price fiction" concern the original entry's own failure
mode #3 already flags as a *secondary*-analysis concern, not a reason to
abandon mid as primary.

**Secondary (robustness, not primary)**: the original entry's failure mode
#3 ("repeat key cells with executable (ask/bid) prices") is retained
exactly. For any cell that clears both discovery-slice and hold-out-slice
screens (§12) using mid, the same cell is recomputed twice more,
descriptively:

- **Ask-implied**: `yes_ask_close_cents / 100` (the cost to buy YES).
- **Bid-implied**: `yes_bid_close_cents / 100` (the value of selling YES,
  equivalently `1 − (100 − yes_bid_close_cents)/100` for buying NO).

A mid-price finding that vanishes at both executable prices is reported
explicitly as "not executable" and does **not** independently support
"rejected" — only mid-price cells drive the primary decision (§12); the
executable check is a required robustness disclosure, never a second
chance at significance.

**Explicitly excluded from both primary and secondary analysis**: trade
price (`price_close_cents`), per §3's forbidden-information list.

## 9. Calibration Metrics

Every statistic below is pre-registered; none may be added after results
are seen.

**Primary** (drives the confirm/reject decision, §12):

- **Per-cell calibration error**: for each (horizon, decile) cell,
  `calibration_error = mean_predicted_probability − realized_yes_frequency`
  (signed; sign records over- vs. under-pricing direction for the
  replication check). The decision statistic is `|calibration_error|`.
- **Per-cell cost band**: `cost_band = mean(fee_cents_for_one_contract_at_
  round(mid_price_cents) + half_spread_cents)` across the cell's
  observations, where `half_spread_cents = (yes_ask_close_cents −
  yes_bid_close_cents) / 2` at the same candle and the fee term uses
  `KALSHI_WEATHER_TAKER_FEE_CONFIG` via `contract_fee_cents` (reused
  verbatim from `scripts/h0007_fees.py`, not reimplemented).

**Secondary** (descriptive/diagnostic, reported for every cell and every
horizon, never substituted into the primary decision):

- **Reliability curve**: mean predicted probability vs. realized frequency
  per decile, one curve per horizon (the standard visualization
  companion to the primary statistic above).
- **Brier score**: `mean((predicted_probability − outcome)^2)` per
  horizon, primary series, vs. an always-base-rate baseline (the
  primary series' own overall realized YES frequency, held fixed) —
  exactly as the original entry specifies.
- **Log loss**: `-mean(outcome*ln(p) + (1-outcome)*ln(1-p))` per horizon,
  vs. the same base-rate baseline, clipped at `p in [0.005, 0.995]` to
  avoid infinite loss from a boundary price.
- **Expected Calibration Error (ECE)**: the decile-weighted average of
  `|calibration_error|` per horizon:
  `ECE_h = sum_d (n_d / N_h) * |calibration_error_{h,d}|`.
- **Calibration slope and intercept**: per horizon, the coefficients of a
  logistic regression of the binary outcome on `logit(mid_price_cents/100)`
  — slope 1 and intercept 0 indicate perfect calibration; slope < 1 is the
  classic "overconfidence" (extreme prices too extreme) signature the
  anchoring mechanism (§2) would predict.
- **Bucket error** (per-cell N and coverage): the count of valid
  observations contributing to each cell, reported alongside every metric
  above — never a calibration statistic itself, but required so a reader
  can judge cell reliability without re-deriving it.

## 10. Statistical Plan

- **Estimators**: cell-level `calibration_error`, ECE, and calibration
  slope/intercept are point estimates from the pooled cell/horizon data;
  Brier score and log loss likewise.
- **Confidence intervals**: 95% throughout, via a **day-clustered
  percentile bootstrap** — resample **settlement days** (`target_date`)
  with replacement, not raw observations, not episodes (there are no
  episodes in this design) — reused verbatim from
  `AMENDMENT-20260721-H0007-pre-execution.md`/`PREREG-H0007`'s own
  established convention, for the same reason: multiple sibling strikes on
  one settlement day share a single realized-weather outcome and are not
  independent draws.
- **Bootstrap methodology**: B = **10,000** resamples, 2.5th/97.5th
  percentile CI, matching H0007's frozen convention exactly.
- **Random seed**: fixed at **20260721** — this protocol's freeze date,
  continuing the seed-equals-freeze-date convention
  `AMENDMENT-20260721-H0007-pre-execution.md` §11 established as a
  project-wide convention going forward, not a fresh, unrelated choice.
- **Cluster level**: settlement day (`target_date`) — the original
  entry's own "clustered by market/day" language, resolved to the more
  conservative of the two (day, not individual market_ticker), matching
  H0007's own resolution of the identical ambiguity class.
- **Multiple-comparison correction and family definition**: the primary
  family is **all 60 (horizon × decile) cells** for the primary series
  (`KXHIGHNY`) and primary price (mid) — 6 horizons × 10 deciles. A
  Bonferroni-corrected significance level (`0.05 / 60`, i.e. a
  99.917%-equivalent two-sided CI) is applied when screening cells in the
  discovery slice (§12). This is **in addition to**, not instead of, the
  temporal-replication requirement already in the original design —
  `RESEARCH.md`'s protocol explicitly permits "a family-wise correction...
  or both," and this document chooses both, the more conservative option,
  consistent with H0010's own precedent of Bonferroni-correcting a
  pre-registered grid. `KXLOWTNYC` (exploratory series) and the two
  executable-price robustness passes (§8) are **outside this family** —
  reported descriptively, never used to support "rejected."
- **Minimum detectable effect**: using the same day-count fact H0007's own
  pre-registration cited (N ≈ 66-67 valid settlement days, a
  data-availability fact, not a price observation) as an upper bound on
  cluster count, and noting each of the 60 primary cells will receive only
  a fraction of that many day-clusters — an exact per-cell power
  calculation cannot be stated now (it depends on how many strikes fall in
  each decile, itself a fact about price data this document has not
  inspected). This is flagged explicitly as a **known power risk carried
  into execution**: the minimum-N-per-cell gate below exists precisely so
  an underpowered cell is reported as insufficient rather than
  misinterpreted as "calibrated."
- **Minimum N per cell**: a cell must have observations from **at least 20
  distinct settlement days** to be eligible to support a "rejected"
  finding; cells below this threshold are reported with their point
  estimates and explicitly marked `insufficient_n`, never silently
  dropped, and can never independently trigger "rejected" regardless of
  their point estimate.
- **Treatment of dependence / repeated observations**: a single market
  contributes at most one observation per (horizon, decile) cell (it can
  appear in different deciles across different horizons, since its
  implied probability moves over time, but never twice in the same
  cell). Sibling strikes of the same event on the same day are treated as
  separate observations within the day-level cluster (the clustering, not
  a filter, is what handles their shared-outcome dependence).
- **Missing data policy**: identical to §5's dataset-level policy —
  excluded and counted, never imputed.

**Held-out replication split** (operationalizing the original entry's
"held-out later time slice," previously unspecified as an exact rule):
the full valid-day population is split chronologically into two
contiguous, non-overlapping halves — the earlier half is the **discovery
slice**, the later half the **hold-out slice** — with a **one-day embargo**
between them (the boundary day itself excluded from both), per
`RESEARCH.md`'s walk-forward embargo rule. A cell must clear the
Bonferroni-adjusted screen in the discovery slice, **and** show the same
sign of `calibration_error` with a standard-95%-CI lower bound (on
`|calibration_error|`) exceeding that slice's own cost band in the
hold-out slice, before it may support "rejected."

## 11. Execution Costs

- **Fee schedule**: `KALSHI_WEATHER_TAKER_FEE_CONFIG`
  (`scripts/h0007_fees.py`), verified 2026-07-21 against Kalshi's official
  fee schedule (effective 2026-07-07) — reused verbatim, not re-verified
  or re-derived. No new fee assumption is introduced by H0005.
- **Entry assumption**: none — H0005 does not simulate a trade or a
  position; the "cost band" is a pure pricing-friction yardstick against
  which a *measured* deviation is compared, not a P&L simulation. This is
  a deliberate, important difference from H0007 (which did simulate a
  hypothetical unit-stake trade) — H0005 only asks "is the deviation
  bigger than it would cost to trade," never "would trading it have been
  profitable."
- **Exit assumption**: not applicable, for the same reason.
- **Spread treatment**: `half_spread_cents` computed directly from the
  candle's own `yes_bid_close_cents`/`yes_ask_close_cents` at the sampled
  instant — no modeled slippage beyond the observed spread itself, same
  convention as H0007 §8.
- **Quote executability**: a candle's quote is only used if
  `yes_bid_close_cents` and `yes_ask_close_cents` are both non-null and
  non-crossed (`bid <= ask`) — identical to H0007 §4 condition 6, reused
  verbatim. A crossed or missing quote excludes that specific observation
  (§5).

**No cost assumption in this section may be changed after any result is
seen.**

## 12. Success Criteria

Stated in terms of **H0005 itself** (the null-form hypothesis) — "confirmed"
means the null (calibration within costs) survives, matching the original
entry's own framing exactly.

- **Confirmed** (the null survives): no primary-family cell (§10) clears
  both the Bonferroni-adjusted discovery-slice screen and the hold-out
  replication screen. Kalshi NYC daily-high prices are calibrated within
  transaction costs, at the resolution this design can detect.
- **Rejected** (a calibration failure is found): **at least one**
  primary-family cell clears **both** screens in §10 — Bonferroni-adjusted
  significance in the discovery slice, same-sign replication with a
  standard-95%-CI-supported deviation exceeding cost in the hold-out
  slice.
- **Inconclusive**: either (a) fewer than **60** valid settlement days are
  available in total at execution time (mirroring the original entry's own
  "≥60-90 days" required-data floor, and H0007's identically-numbered
  gate) — the whole study is inconclusive-for-sample-size, regardless of
  any point estimate; or (b) every primary-family cell that shows a
  discovery-slice screen pass falls below the minimum-N-per-cell gate
  (§10) in the hold-out slice, such that no cell could have replicated
  even if the true effect were real — reported as
  "inconclusive-for-power," naming exactly which cells were underpowered.

Every outcome branch is tied to the numeric thresholds above; none may be
adjusted after any result is computed.

## 13. Bias Review

- **Look-ahead bias**: structurally prevented the same way as H0007— the
  as-of candle selection (§6) can only select `period_end <= t`; outcome
  columns are read only after bucket assignment is fixed (§3). To be
  confirmed by code review of the analysis script against this document
  before it runs, exactly as H0007's own bias review committed to.
- **Survivorship bias**: no market is excluded for its outcome, liquidity,
  or delisting status (§5) — the full discoverable-and-backfilled archive
  (804/804 settled markets, 0 lost to retention, per Phase 7A) is the
  population.
- **Selection bias**: horizons (§6), deciles (§7), price definition (§8),
  and metrics (§9) are all fixed before any price inspection. The one
  data-driven exclusion (missing-settlement-label days, §5) is applied
  identically regardless of outcome and reported as a coverage statistic.
- **Data snooping**: this document uses only (a) the original H0005
  entry's own already-frozen design choices (§4), (b) H0007's already-
  closed, published *capability* facts (§0) — never its market-behavior
  finding — and (c) generic data-availability facts (day counts) already
  public from Phase 7A/7B. No H0005-specific price behavior has been
  examined.
- **Multiple testing**: addressed explicitly in §10 — a declared 60-cell
  family, Bonferroni-corrected, **plus** mandatory temporal replication
  (both layers, not either/or).
- **Calendar effects**: not a pre-registered primary comparison. A
  descriptive by-month breakdown may be reported as exploratory only,
  following H0002/H0007's own precedent, and must never be used to select
  a favorable period.
- **Cross-sectional dependence**: sibling strikes of the same event share
  the same realized settlement outcome on the same day — this is exactly
  what day-level clustering (§10) is designed to absorb; it is not treated
  as independent replication within a day.
- **Horizon overlap**: the six horizons (§6) are measured on the *same*
  underlying markets and are therefore correlated with each other (a
  market miscalibrated at 24h is not independent of itself at 12h) — this
  is a within-day, cross-horizon version of the same dependence
  cross-sectional clustering handles, and is explicitly *not* treated as
  six independent experiments; the 60-cell Bonferroni family already
  spans all six horizons jointly for exactly this reason (correcting
  across the full grid at once, not per-horizon separately, which would
  understate the true multiplicity).

## 14. Reproducibility

- **Manifest**: dataset version and both `market_price_weather` and
  `settlement_labels` frames' content hashes (the version label alone is
  insufficient — `RESEARCH.md`), recorded at execution time (§5).
- **Versioning**: `MARKET_PRICE_WEATHER_SCHEMA_VERSION` (`"1"` at freeze),
  `RECONSTRUCTION_VERSION` (`"1"` at freeze), source DB Alembic revision
  (`0007` at freeze), fee-config verification date (2026-07-21), and the
  git commit of both the dataset-build and analysis code.
- **Deterministic execution**: bucket assignment, as-of candle selection,
  cost-band computation, and all point estimates are deterministic
  arithmetic over stored data. The **only** randomized component is the
  day-clustered bootstrap CI (§10), seeded at 20260721.
- **Software versions** (as of this freeze, git commit
  `b30f92e3c8db17e2a246fb1be9d1f6328161a1e9`):

  | Package | Installed | Floor (`pyproject.toml`) |
  |---|---|---|
  | Python | 3.12.13 | >=3.12 |
  | polars | 1.42.1 | >=1.0 |
  | pydantic | 2.13.4 | >=2.7 |
  | SQLAlchemy | 2.0.51 | >=2.0 |
  | typer | 0.27.0 | >=0.12 |
  | httpx | 0.28.1 | >=0.27 |
  | structlog | 26.1.0 | >=24.1 |

- **Artifact generation**: the eventual analysis script
  (`scripts/exp_h0005_calibration.py`, not yet written, matching
  `scripts/exp_h0007_bound_violations.py`'s precedent) must produce
  `docs/research/experiments/EXP-<execution-date>-H0005-calibration.md`
  and a `-results.json` sidecar (schema:
  `TEMPLATE-H0005-manifest.json`), run via `uv run`/the project's pinned
  environment, and must reproduce byte-identically on re-run against the
  same pinned dataset version — the same reproducibility bar H0007 met and
  verified.
- **Directory layout**: all artifacts live under
  `docs/research/experiments/`, following the existing flat, prefixed
  naming convention (`PREREG-`/`AMENDMENT-`/`TEMPLATE-`/`EXP-`/
  `DIFFERENCES-`).

## 15. Implementation Checklist

To be completed **before** execution (none of these steps compute a
calibration result):

- [ ] Write `scripts/exp_h0005_calibration.py` implementing exactly:
      as-of candle selection with the 15-minute staleness tolerance (§6);
      decile bucketing on mid-price (§7); the primary/secondary price
      definitions (§8); the primary and secondary metrics (§9); the
      day-clustered bootstrap with seed 20260721 (§10); the Bonferroni-
      corrected discovery/hold-out replication split (§10); the exact
      success-criteria branching (§12).
- [ ] Reuse, not reimplement: `contract_fee_cents` and
      `KALSHI_WEATHER_TAKER_FEE_CONFIG` from `scripts/h0007_fees.py`;
      `implied_result` from `settlement/labels.py` for outcome derivation.
- [ ] Add focused unit tests for the as-of candle selection (staleness
      tolerance boundary), decile assignment (boundary prices), the
      cost-band formula, and the discovery/hold-out date-split logic —
      mirroring `tests/unit/test_h0007_fees.py`'s hand-calculated-example
      style.
- [ ] Code review the finished script against this document's §3
      (Information Set) before first execution, per the Bias Review's own
      commitment.

## 16. Execution Checklist

To be completed **at** execution time, in order:

- [ ] `dataset build --which market_price_weather --version
      exp-<date>-h0005` (mirrors `exp-20260721-h0007`'s precedent) — pins
      the dataset version and content hashes.
- [ ] Record the resulting manifest's `content_hashes`,
      `source_db_revision`, and `git_commit` into the results JSON (§14).
- [ ] Run `scripts/exp_h0005_calibration.py` once against the pinned
      dataset.
- [ ] Re-run it a second time against the same dataset; diff the two
      output JSON files byte-for-byte (H0007's own verification step) —
      any difference blocks the run from being reported as a valid result
      until the non-determinism is found and fixed.
- [ ] Apply the success-criteria branching (§12) exactly as written; do
      not adjust any threshold based on how "close" a cell came.
- [ ] Write the `EXP-<date>-H0005-calibration.md` record and
      `-results.json` sidecar.
- [ ] Update `HYPOTHESES.md`'s H0005 entry: `Status`, `Closed`, `Results`,
      `Conclusion` only — no other field.

## 17. Reproducibility Checklist

To be completed **after** execution, before treating the result as usable
evidence:

- [ ] Confirm the two same-dataset re-runs (§16) were byte-identical.
- [ ] Confirm every content hash, schema version, and git commit recorded
      in the results JSON matches the manifest actually produced by the
      dataset build.
- [ ] Confirm the random seed recorded is exactly 20260721.
- [ ] Confirm the reported cell family size is exactly 60 (6 horizons x 10
      deciles, primary series, mid price) and that the Bonferroni
      threshold applied is `0.05/60`.
- [ ] Confirm no field in the results JSON's `config` block differs from
      this document's frozen values (§6-§11) — any difference means the
      protocol was not executed as pre-registered and the result must not
      be reported as H0005's outcome.
- [ ] Confirm `HYPOTHESES.md`'s H0005 entry's Hypothesis/Rationale/
      Required data/Experiment design/Metrics/Statistical tests/Failure
      modes/Difficulty/Dependencies text is byte-identical to its
      pre-execution state — only Status/Closed/Results/Conclusion may
      differ.

---

**No statistics have been calculated. No calibration outcome has been
inspected. H0005 has not been executed.**
