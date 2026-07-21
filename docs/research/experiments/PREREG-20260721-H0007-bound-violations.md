# PREREG-20260721-H0007-bound-violations

**Pre-registration package for H0007 — frozen 2026-07-21, before any price
data has been inspected.** Per `RESEARCH.md`'s "Standard experiment
protocol," steps 1-4 (pre-register, pin the dataset, separate train/test,
state the baseline) happen before any result is computed — this document
*is* that registration, elaborated to full rigor beyond the terse
`HYPOTHESES.md` template because H0007's mechanism has enough moving parts
(a logical bound, a persistence filter, a fee-net decision rule) that
"exactly what counts" must be nailed down now, not improvised during
analysis.

**This document computes, inspects, or reports no price data, no
statistics, and no results.** The only prior facts it uses are (a) H0002's
own already-published, already-concluded result (a legitimate prior input —
see §10 Bias Review for why this doesn't count as snooping on H0007's own
outcome), and (b) data-*availability* facts already published in the Phase
7A/7B readiness reports (coverage counts, schema, leakage-check pass/fail —
never anything about whether the hypothesis is true).

H0007 has **not** been executed. This document supersedes nothing in
`HYPOTHESES.md`'s H0007 entry — it elaborates the same frozen hypothesis
with the precision this pre-registration protocol requires; the entry
itself remains `Status: Proposed`.

> **Amendment notice (2026-07-21, before execution).** An independent
> institutional pre-registration audit identified ambiguities in this
> document's §5/§9 episode definition, §6 opportunity-day denominator, §4.5
> persistence scope, and §4/§9 candle-adjacency rule, and required the §8
> fee constant to be independently verified rather than assumed. These are
> resolved (fee verification remains **BLOCKED** — see below) in
> `docs/research/experiments/AMENDMENT-20260721-H0007-pre-execution.md`,
> which governs wherever it conflicts with the text below. This document's
> original wording is preserved unedited; the amendment is additive.
> **H0007 must not execute until the amendment's Finding 5 (fee schedule)
> is resolved.**
>
> **Second amendment (2026-07-21, same day, before execution).**
> `docs/research/experiments/AMENDMENT-20260721-H0007-fee-verification.md`
> documents an exhaustive, sourced search for Kalshi's official fee
> schedule. It confirms fee *applicability* (`fee_type="quadratic"`,
> `fee_multiplier=1`, no series/event overrides, for both KXHIGHNY and
> KXLOWTNYC) from live, first-party Kalshi API data, but the exact numeric
> fee coefficient remains unverified — the one document that states it was
> unreachable (HTTP 429) from every method attempted. **H0007 remains
> BLOCKED.**
>
> **Third amendment (2026-07-21, same day, before execution).**
> `docs/research/experiments/AMENDMENT-20260721-H0007-fee-verified.md`
> resolves the remaining gap: the user supplied Kalshi's official fee
> schedule PDF directly (effective 2026-07-07), which was cross-verified
> against its own 42 published worked examples with zero discrepancies and
> is now frozen as `KALSHI_WEATHER_TAKER_FEE_CONFIG` in
> `scripts/h0007_fees.py`. **No pre-registration blocker remains.** H0007
> execution itself is still a separate, explicit, not-yet-taken step.

---

## 1. Research Question

**Precise claim.** Once a same-day NWS CLI preliminary report establishes a
running daily high (`tmax_f`) or running daily low (`tmin_f`) that
logically rules out one side of a specific Kalshi strike's payoff (up to the
small downward-revision risk H0002 already measured), the market's quoted
price for the now-near-impossible side does not fully collapse toward its
logically-implied near-zero value within a persistent, executable window —
and harvesting that residual, held to settlement, nets a positive expected
value after realistic execution costs, on a non-trivial share of settlement
days.

**Expected market behavior under the hypothesis**: for a bounded strike, the
quoted bid/ask for the now-near-impossible side remains materially above
its logically-implied fair value (≈0¢, i.e., ≥2¢ given Kalshi's 1¢ minimum
tick) for a sustained period (≥2 consecutive 1-minute candles) rather than
immediately re-pricing toward that floor, on ≥5% of settlement days.

**Expected market behavior under the null**: the quoted price for the
bounded side re-prices to (or very near) its logical floor essentially as
soon as the bound is established, on <5% of days, or any residual observed
does not survive fee-net evaluation.

This is a **market-microstructure/information-processing-lag** claim, not a
weather-forecasting claim — H0007 requires no probability model at all,
only the logical implication of an already-published, already-known data
point (see §3).

## 2. Economic Motivation

**Why the inefficiency could exist:**

- **Information-integration cost, not probability-estimation cost.** The
  bound requires cross-referencing a specific NWS CLI text report (external
  to Kalshi's own UI) against a specific strike's structured threshold. This
  is a different, plausibly slower-to-correct friction than a participant
  simply misjudging a probability distribution — it requires *noticing* a
  report was just published, *mapping* it to the right station/market, and
  *acting*, each step with latency and failure probability.
- **Thin liquidity.** Weather markets are a niche Kalshi category; Phase 7A's
  coverage report already found materially sparser intraday quote/trade
  activity on illiquid strikes (particularly "between"-type strikes) than on
  liquid ones. Thin books plausibly have fewer participants continuously
  re-checking every strike's logical consistency against external data,
  especially far-OTM or late-day strikes where interest may be winding down.
- **A non-obvious inference.** The mechanism requires recognizing that a
  same-day running value constrains the *shape* of the remaining outcome
  distribution asymmetrically (a near-hard one-sided bound), not merely
  shifting its mean — a materially different and less intuitive update than
  "it's hot today, so the high will probably be high."

**Why rational traders might not remove it:**

- **Cost band.** If the residual is smaller than the fee + effective spread
  required to capture it, leaving it unexploited is the *rational* outcome,
  not a failure of rationality — which is exactly why the pre-registered
  decision rule (§9) is fee-net, not a raw-price-gap test.
- **Attention/capital allocation.** In a niche market with far more liquid
  categories on the same platform, market-making/corrective capital may be
  systematically under-allocated to weather strikes relative to their
  logical-arbitrage frequency.
- **Position wind-down near settlement.** Some participants may be reducing
  activity as a market nears close rather than opening new corrective
  positions, reducing exactly the liquidity that would otherwise close the
  gap fastest.

## 3. Information Set

Defined precisely against the actual dataset columns (`market_price_weather`,
`docs/adr/0008-point-in-time-alignment.md`), so "knowable at candle *t*" is
never a matter of interpretation.

**Allowed, as of a given candle's `period_end` (call it *t*):**

- That market's own OHLC trade prices, OHLC bid/ask quotes, volume, open
  interest, `data_quality_status`, `price_close_is_carried_forward` — for
  this candle and every earlier candle of the same `market_ticker`.
- `obs_value_known`, `obs_issuance_time_known`, `obs_age_seconds`,
  `observation_quality_status` (the market's own settlement variable's
  latest known value as of *t*).
- `running_tmax_f_known` / `running_tmax_issuance_time_known` /
  `tmax_locked`, and the tmin equivalents.
- `theoretical_remaining_range_high_f` / `_low_f`.
- Static market metadata knowable at any time: `market_ticker`,
  `event_ticker`, `floor_strike`, `cap_strike`, `strike_type`, `close_time`
  (published well in advance — not an outcome).
- H0002's own already-published, already-concluded aggregate result (the
  same-day-tmax downward-revision rate) as a **fixed input constant** — see
  §10 for why this is not a form of data snooping.

**Forbidden, absolutely:**

- `value_at_settlement`, `latest_final_value`, `kalshi_result`,
  `kalshi_expiration_value`, `settlement_label_status`,
  `settlement_label_version` — used **only** at the final evaluation step
  (§5), on an already-flagged opportunity, never to decide entry eligibility,
  timing, or sizing.
- Any row (any market, any variable) with `period_end` or `issuance_time`
  later than *t*.
- Re-fetching raw `weather_observations` directly instead of the pre-joined
  `*_known` as-of columns (which would bypass the leakage-tested join).
- Per-instance knowledge of H0002's raw per-day revision records for the
  specific target date being evaluated — only H0002's aggregate, cross-day,
  already-published rate may be used, never a lookup of "did *this* day
  revise."
- Forecast data (`WeatherForecast`/`market_weather`'s forecast columns) —
  H0007 is a hard-logical-bound claim from an *observed* running value, not
  a forecast-based claim; mixing the two would blur this into H0006/H0008's
  territory.
- Any manual, discretionary, or "looks obviously mispriced" judgment not
  encoded in the mechanical entry conditions (§4).

## 4. Entry Conditions

Fully mechanical — every condition below is a deterministic function of
already-materialized columns. No condition requires human judgment.

A candle is **entry-eligible** iff **all** of the following hold:

1. `variable == "tmax_f"` — the **primary cell** (see §6/§10: `tmin_f` is a
   declared secondary, exploratory cell, evaluated identically but never
   used for the primary confirm/reject decision, because H0002 only
   established the same-day-tmax asymmetry specifically — extending the
   "hard floor" assumption to `tmin_f` without its own validated asymmetry
   result would be an unregistered extrapolation).
2. `running_tmax_f_known` is not null.
3. `tmax_locked == False` — the observation window has **not** yet elapsed.
   This restricts the primary cell to the pre-lock "end-game" window
   H0007's own rationale describes ("mispricing in the settlement
   end-game"). The post-lock, pre-confirming-issuance window (the ~1.1%
   reporting-lag gap measured in Phase 7B) is a plausible future extension,
   **explicitly not part of this pre-registration's primary cell**.
4. The strike is **logically bounded** by `running_tmax_f_known`, using
   exactly the strike semantics already implemented and empirically
   validated in `settlement/labels.py` (ADR 0006) — reused verbatim, never
   reimplemented:
   - `strike_type == "less"` (payoff: YES iff `final < cap_strike`):
     bounded iff `running_tmax_f_known >= cap_strike`. Bounded side = `YES`
     (near-impossible).
   - `strike_type == "greater"` (payoff: YES iff `final > floor_strike`):
     bounded iff `running_tmax_f_known > floor_strike`. Bounded side = `NO`
     (near-impossible; YES is near-certain).
   - `strike_type == "between"` (payoff: YES iff `floor_strike <= final <=
     cap_strike`): bounded **only on the upper edge** — iff
     `running_tmax_f_known > cap_strike`. Bounded side = `YES`
     (near-impossible). The lower edge (`running_tmax_f_known <
     floor_strike`) is **explicitly excluded** — it says nothing about
     whether the range will still be entered from below, so it is not a
     hard bound and must never be flagged.
5. **Persistence** (operationalizes H0007's own pre-registered failure mode
   #1): condition 4 must **also** hold for the immediately preceding candle
   of the same `market_ticker`. A single-candle flicker is never eligible.
6. A **coherent, non-crossed quote** exists at both this candle and the
   preceding one: `yes_bid_close_cents` and `yes_ask_close_cents` both
   non-null and `yes_bid_close_cents <= yes_ask_close_cents`. (A zero-volume
   candle, `data_quality_status == "zero_volume"`, is **not** excluded on
   that basis alone — Phase 7A established that quotes persist through
   zero-volume periods; only a genuinely missing or crossed quote excludes a
   candle.)
7. `period_end` is strictly before the market's own `close_time` (a guard;
   trivially true by construction of how candlesticks are generated).
8. `settlement_label_status` plays **no role** in entry eligibility — every
   candle meeting 1-7 is flagged regardless of it. `settlement_label_status`
   is consulted only afterward (§5) to determine whether a valid outcome
   label exists for evaluation; a market/day lacking one is excluded from
   *evaluation* and reported as a coverage statistic, never silently folded
   into "no opportunity."

## 5. Outcome Definition

**`p_bound`** (the H0002-adjusted violation probability): the *unconditional*
empirical rate of any downward revision from a same-day-issued preliminary
tmax reading, per `EXP-20260721-H0002-cli-revisions`: **1/1278 ≈ 0.0783%**
(deliberately the unconditional rate, not the 1/170 rate conditional on a
revision having occurred — using the conditional rate here would overstate
`p_bound` by roughly 7.5x). Fair value of the bounded side ≈
`round(p_bound × 100)` ≈ 0¢, below Kalshi's 1¢ minimum tick.

**Sellable price of the bounded side**, per candle:
`yes_bid_close_cents` if bounded side is `YES`; `100 − yes_ask_close_cents`
if bounded side is `NO` (using the standard complementary-binary-contract
identity `NO price = 100 − YES price`, a structural fact, not an
assumption).

**Primary outcome** (binary, per entry-eligible candle):
`has_residual_mass = (sellable_price_of_bounded_side_cents − 1) > 0`, i.e.
the bounded side can still be sold for ≥2¢ when its logical fair value is
≈0¢.

**Secondary outcomes**, per opportunity **episode** (a maximal run of
consecutive entry-eligible candles for one `market_ticker` — the natural
unit; a single unchanging mispricing spanning many 1-minute candles must not
be double-counted as many independent "opportunities"):

- `residual_mass_cents` at first-eligible-candle-of-episode (continuous
  magnitude — the "edge size" metric).
- Net-of-fees hypothetical unit-stake P&L: buy the *favored* (likely-winning)
  side at its executable ask at the episode's first eligible candle, hold to
  settlement, realize the standard binary payoff minus the pre-registered
  fee (§8) — `value_at_settlement`/`kalshi_result` used **only** here, at
  final evaluation.
- Time-to-correction: for an episode that ends (returns to
  `has_residual_mass == False`) before settlement, elapsed minutes between
  the episode's first and last eligible candle.

## 6. Metrics

Every statistic below is pre-registered; **none may be added after results
are seen**.

- **Opportunity-day frequency**: fraction of settlement days with ≥1
  entry-eligible candle (primary cell) on which ≥1 episode has
  `has_residual_mass == True` net of fees (§9's cost-aware version).
- **Persistence**: distribution (median, IQR) of episode duration in
  minutes, across all flagged episodes.
- **Edge size**: distribution (mean, median, IQR) of `residual_mass_cents`
  across flagged episodes; day-level aggregate = sum of
  `residual_mass_cents` at first-eligible-candle-of-episode across all
  episodes on that day (H0007's own "mass×frequency aggregate").
- **Net-of-fees hypothetical P&L**: per-episode as defined in §5, summed
  across all episodes, with a clustered bootstrap 95% CI.
- **Confidence intervals**: 95% throughout. Proportions use the Wilson score
  interval (the deterministic method already used for H0002, reused for
  consistency). Continuous aggregates (P&L, edge size) use a **percentile
  bootstrap, clustered at the settlement-day level** — per `RESEARCH.md`'s
  explicit rule that snapshots of one market-day are one observation's
  worth of independence, not many.
- **Bootstrap methodology**: resample **days** (not candles, not episodes)
  with replacement, B = 10,000 resamples, 2.5th/97.5th percentile CI, fixed
  seed 20260721 (§11).
- **Multiple-testing correction**: `tmax_f` is the sole primary cell — no
  scan across strike-type sub-cells or thresholds supports the primary
  decision. `tmin_f` and any strike-type breakdown are exploratory and
  reported separately, never substituted into the primary decision if the
  primary cell disappoints. No Bonferroni factor is needed for the primary
  decision because exactly one comparison is declared; this is stated
  explicitly to foreclose later "just look at between-strikes" cherry-picking.

## 7. Statistical Plan

- **Estimators**: empirical proportions with Wilson CIs (opportunity-day
  frequency); mean/median with clustered bootstrap CIs (P&L, edge size,
  persistence, time-to-correction).
- **Confidence intervals**: 95%, as specified in §6.
- **Hypothesis tests**: the confirm/reject decision is a **threshold test on
  CI bounds** (§9), matching this project's established H0002-style
  convention, not a traditional null-hypothesis p-value test.
- **Minimum detectable effect**: at the pre-registered N ≈ 66-67 settlement
  days, one-sided α = 0.05, 80% power, against the 5%-of-days null, a
  normal-approximation one-sample-proportion power calculation
  (`n = [z_α·√(p₀(1−p₀)) + z_β·√(p₁(1−p₁))]² / (p₁−p₀)²`, solved for `p₁`
  at `n = 66`) gives **MDE ≈ 13 percentage points** — i.e., the study is
  well-powered to detect a true opportunity-day frequency at or above
  roughly **18%**, but is **not** well-powered to reliably distinguish a
  true rate of, say, 7-10% from the 5% null; such a result would likely land
  in "inconclusive," not a confident reject. This is an approximate,
  normal-approximation estimate for design purposes; an exact/simulation
  power check should be run (using only N and these pre-registered
  thresholds, zero price data) before executing.
- **Missing data**: a settlement day with no valid coverage (e.g.
  `settlement_label_status == "missing_source_data"` for the whole day, or
  no candle archive for that day) is **excluded from the denominator
  entirely** and reported as its own coverage statistic
  (`days_missing_data`) — never folded into "0% opportunity."
- **Zero-volume candles**: not excluded outright (quotes persist through
  zero-volume periods, per Phase 7A) — only a genuinely missing/crossed
  quote excludes a candle (entry condition 6).
- **Illiquid markets**: no market is excluded a priori for being illiquid
  (that would be a selection/survivorship risk — see §10); illiquidity
  manifests naturally as fewer eligible episodes, and day-level (not
  candle-level) aggregation already prevents over-weighting a sparse
  market's many small candles. A descriptive, exploratory-only split by
  sparse- vs. normal-coverage markets (reusing `ops/price_coverage.py`'s
  existing `markets_with_missing_intervals` definition) is reported for
  transparency, never used as a filter.

## 8. Execution Assumptions

- **Fee schedule**: use Kalshi's **official, currently-published**
  per-contract trading fee formula, applied per fill. The exact numeric
  constant is **not hardcoded in this document** — it must be verified
  against official documentation or a live-verified reference at execution
  time (per this project's own established precedent,
  `docs/API_VERIFICATION.md`, for grounding external assumptions rather than
  guessing) and recorded, with its source and retrieval date, in the
  experiment manifest (`TEMPLATE-H0007-manifest.json`'s `config.fee_schedule`
  block). Fee schedules can change; freezing a possibly-stale number now
  would itself be a form of unverified assumption this project's rules
  prohibit.
- **Slippage**: none beyond the quoted bid/ask spread itself — unit stake,
  one contract, no market-impact model. If position sizing beyond one
  contract is ever explored, that requires its own, separate
  pre-registration.
- **Executable price definition**: a buy fills at the current ask
  (`yes_ask_close_cents` for YES, `100 − yes_bid_close_cents` for NO); a
  sell fills at the current bid (`yes_bid_close_cents` for YES,
  `100 − yes_ask_close_cents` for NO) — always the candle's own `_close_cents`
  quote field (the last quote observed at or before `period_end`), never
  open/high/low (which would let the analysis pick a favorable-looking price
  within the candle window).
- **Quote vs. trade priority**: **quotes are primary**. `price_close_cents`
  (the trade price) is descriptive/exploratory context only — it may be
  stale (`price_close_is_carried_forward == True`) and must never be used as
  an executable price. H0007's mechanism concerns the market's *standing
  offer*, not whether a trade happened to clear at an old price.

**No parameter in this section may be tuned after observing data.**

## 9. Failure Criteria

Reproducing `HYPOTHESES.md`'s H0007 entry verbatim, operationalized:

> "confirmed if bounded-side sellable mass exceeding the cost band occurs on
> ≥5% of settlement days *and* aggregate hypothetical P&L of harvesting it
> (no sizing, unit stake, executable prices) is positive with bootstrap CI
> excluding zero; rejected if occurrences are rarer or the aggregate is
> non-positive."

**"Exceeding the cost band," operationalized**: the per-candle
cost-aware flag used for the primary decision is
`residual_mass_cents > fee_cents_for_one_contract_at_this_price` (the fee
constant per §8) — a strictly narrower, fee-net version of the raw
`has_residual_mass` flag in §5 (the raw flag remains useful for descriptive
"how often is there *any* gap" reporting, but only the fee-net version
feeds the primary decision).

- **Confirmed**: opportunity-day frequency's 95% CI **lower** bound > 5%
  **and** aggregate hypothetical P&L's 95% CI **lower** bound > 0.
- **Rejected**: opportunity-day frequency's 95% CI **upper** bound ≤ 5%
  **or** aggregate hypothetical P&L's 95% CI **upper** bound ≤ 0.
- **Inconclusive**: neither confirmed nor rejected criteria are met (e.g.
  the frequency CI straddles 5%, or the P&L CI straddles zero); **or**
  fewer than the pre-registered 60 settlement days have valid coverage at
  execution time, regardless of point estimates — per `RESEARCH.md`:
  "Inconclusive-for-sample-size states the sample it was short of and the
  date to retry — it is not a soft 'confirmed.'"

**Failure modes** (already pre-registered in `HYPOTHESES.md`, tied to this
design's concrete mitigations):

1. *Stale quotes in thin end-game books* → mitigated by the ≥2-consecutive-
   candle persistence requirement (§4.5). If a day's flag is still driven by
   exactly one episode, that must be reported as a transparency statistic
   (fraction of opportunity-days driven by a single episode), never
   filtered out silently.
2. *H0002 revision risk is the priced risk* → mitigated by using the
   H0002-adjusted `p_bound` (§5), not a naive zero-probability assumption —
   built directly into the fee-net residual-mass definition.
3. *Collector outage on eventful afternoons* → mitigated by excluding
   uncovered days from the denominator and reporting coverage separately
   (§7), never silently treating a gap as "no opportunity."

## 10. Bias Review

- **Look-ahead bias**: structurally prevented by `dataset/asof.py`'s
  backward-only `join_asof`, verified with **0 leakage violations** over the
  full 761,475-row production archive (Phase 7B). Entry conditions use only
  `*_known` columns; outcome columns are used strictly at final evaluation
  (§5), never for entry — to be confirmed by code review of the analysis
  script against this document before it runs.
- **Survivorship bias**: the entry population is every market in
  `market_price_weather` meeting §4's mechanical conditions, over the full
  discoverable-and-backfilled archive (804/804 settled markets verified,
  0 markets lost to Kalshi's retention window per Phase 7A). No market is
  excluded for its outcome, volume, or delisting status — illiquid markets
  are explicitly retained (§7).
- **Selection bias**: entry conditions are fixed and mechanical, declared
  before any price inspection. The only exclusion (days with
  `settlement_label_status == "missing_source_data"`, 1 of 67 known days per
  the Phase 7A coverage report) is a data-availability exclusion applied
  identically regardless of outcome, reported as a coverage statistic, not a
  cherry-pick.
- **Multiple testing**: addressed in §6 — one declared primary cell
  (`tmax_f`), everything else exploratory.
- **Data snooping**: this document is frozen before any H0007-specific price
  behavior has been inspected. The only prior facts used are (a) H0002's own
  independently pre-registered, already-concluded result (a legitimate
  input constant, computed and closed under its *own* pre-registration
  before H0007 existed) and (b) Phase 7A/7B's *data-availability* facts
  (coverage counts, 0-leakage-violation confirmation) — never anything
  about whether the hypothesis holds. Note explicitly: Phase 7B's readiness
  update (`docs/research/2026-07-21-h0007-readiness-update.md`) reported, as
  a byproduct of validating the join mechanism, that "9 distinct
  (station, target_date) pairs show a known-so-far-below-eventual-settlement
  pattern" in the archive. That number is **not used anywhere** in this
  pre-registration's thresholds, entry conditions, or decision rule — it is
  flagged here for transparency precisely because it exists in a prior
  document, so a future reader can verify it played no role in this design.
- **Calendar effects**: not a pre-registered primary comparison (would
  require its own family-wise correction). A purely descriptive by-month
  breakdown may be reported as exploratory only, following H0002's own
  precedent, and must never be used to select a favorable period after the
  fact.

## 11. Reproducibility

- **Manifest**: dataset version = the Milestone 4 export version **and**
  both `market_price_weather` and `settlement_labels` frames' content
  hashes (the version label alone is insufficient — `RESEARCH.md`).
- **Versioning**: `MARKET_PRICE_WEATHER_SCHEMA_VERSION` (currently `"1"`),
  `RECONSTRUCTION_VERSION` (settlement labels), source DB Alembic revision
  (currently `0007`), and the git commit of both the dataset-build and
  analysis code — all three of `RESEARCH.md`'s reproducibility inputs
  (configuration, dataset version, code version).
- **Deterministic execution**: entry-condition flagging, mass computation,
  and Wilson CIs are deterministic arithmetic over stored data — no
  randomness. The **only** randomized component is the clustered bootstrap
  CI for continuous aggregates (§6).
- **Random seed**: fixed at **20260721** (this protocol's freeze date) for
  the B = 10,000-resample bootstrap — the first bootstrap-based experiment
  on this platform (H0002 needed none), so this establishes the
  seed-equals-freeze-date convention going forward; recorded in the
  manifest so the CI is regenerable byte-for-byte.
- **Artifact generation**: the eventual analysis script
  (`scripts/exp_h0007_bound_violations.py`, not yet written — matching
  `scripts/exp_h0002_cli_revisions.py`'s precedent) must produce
  `docs/research/experiments/EXP-<execution-date>-H0007-bound-violations.md`
  and a `-results.json` sidecar (schema: `TEMPLATE-H0007-manifest.json`),
  run via `uv run` so dependency versions are pinned by `pyproject.toml`.

## 12. Deliverables (this task)

| Deliverable | Location |
|---|---|
| H0007 pre-registration document | this file |
| Analysis plan | §§4-8 above |
| Success/failure criteria | §9 above |
| Required dataset versions | §11 above (schema versions/revision numbers; no specific export version pinned yet — that happens at execution time, per `RESEARCH.md`'s "pin the dataset" step) |
| Required software versions | below |
| Experiment manifest template | `docs/research/experiments/TEMPLATE-H0007-manifest.json` |

**Required software versions** (as of this freeze, git commit
`6c37eb46355bdfd736546bf10145c8c226ce9fde`):

| Package | Installed | Floor (`pyproject.toml`) |
|---|---|---|
| Python | 3.12.13 | >=3.12 |
| polars | 1.42.1 | >=1.0 |
| pydantic | 2.13.4 | >=2.7 |
| SQLAlchemy | 2.0.51 | >=2.0 |
| typer | 0.27.0 | >=0.12 |
| httpx | 0.28.1 | >=0.27 |
| structlog | 26.1.0 | >=24.1 |

Execute via `uv run` against this exact lockfile-pinned environment (or a
newer one within these floors, recorded in the manifest) — never an ad hoc
interpreter with unpinned dependencies.

---

**No statistics have been calculated. No price data has been inspected
beyond already-published data-availability facts (Phase 7A/7B). H0007 has
not been executed.**
