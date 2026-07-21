# DIFFERENCES-20260721-H0005-from-original-design

Itemized comparison between the original `HYPOTHESES.md` H0005 entry
(opened 2026-07-21, still `Status: Proposed` before this update; text
below is quoted from that entry as it stood before
`PREREG-20260721-H0005-calibration.md` was written) and the frozen
pre-registration package. Per this task's instruction, every change is
either **(a) an implementation clarification** — the original left a
detail unspecified or only gave "e.g." examples, and this document simply
freezes an exact choice — or **(b) a data-source update** — the original
assumed infrastructure that turned out not to be the infrastructure the
platform actually built. **No entry below changes economic rationale,
hypothesis direction, or the substance of success criteria.**

## Category A: Data source update (mandated by how the platform was built)

| Original assumption | What actually exists | Resolution |
|---|---|---|
| "≥60-90 days of collected market snapshots... accrues only while `collector run` operates" (prospective, 5-minute cadence, `market_snapshots`) | The live 5-minute snapshot collector has near-zero historical depth (confirmed during Phase 7B/7C: `market_snapshots` held 828 rows, all from a single day's pass) — it was never the right instrument for this study | Use the **archived candlestick record** (`market_price_weather`, 761,475 one-minute candles across 804 settled markets, full historical depth via backfill, not accrual) instead. This is a strict resolution/coverage upgrade (1-minute vs. 5-minute; full history vs. today-only), not a substitution of a different kind of evidence — both are quote/price observations of the same markets. |
| "versioned `market_weather` dataset" | `market_weather` is snapshot-grain and inherits the same near-zero historical depth | Use `market_price_weather` (candle-grain), which already carries the settlement-label columns `market_weather` does, via the identical join pattern (`dataset/pipeline.py`) | 
| Implicit reliance on `best_yes_bid_cents`/`best_yes_ask_cents` (order-book-derived, snapshot-grain) | Not populated at useful historical depth for the same reason | Use `yes_bid_close_cents`/`yes_ask_close_cents` (candle-grain quote OHLC, Phase 7A) — the same underlying concept (best bid/ask), sourced from the archive instead of the live snapshot collector |

## Category B: Previously unspecified, now frozen (the original used "e.g.", left a formula qualitative, or didn't state a number)

| Topic | Original text | Now frozen as |
|---|---|---|
| Horizons | "fixed horizons before close (e.g. 48h, 24h, 12h, 2h)" — explicitly only examples | **{72h, 48h, 24h, 12h, 6h, 2h}** — the original four retained exactly; two added, justified by archive depth (72h) and by wanting resolution near the original's shortest horizon (6h) — see `PREREG-20260721-H0005-calibration.md` §6 for the full justification, itself grounded in facts published *before* this freeze, never in this experiment's own price data |
| Moneyness/decile boundaries | "bucket into deciles" — no exact edges stated | **[0,10), [10,20), ..., [90,100]** of mid-implied probability, equal-width, frozen |
| Cost band | "~fees + half-spread" — approximate | Exact formula: `contract_fee_cents(round(mid_price_cents), 1, KALSHI_WEATHER_TAKER_FEE_CONFIG) + (yes_ask_close_cents - yes_bid_close_cents)/2`, reusing the already-verified fee function verbatim |
| Cluster level | "clustered by market/day" — two candidate units named, not chosen between | **Settlement day** (`target_date`) — the more conservative of the two, matching the identical ambiguity's resolution in H0007's own audit (Finding 3-adjacent reasoning) |
| Bootstrap specifics | "cluster-bootstrap CIs" — no B, no seed, no percentile method stated | B = 10,000, percentile method, seed = 20260721 — reusing the exact convention `AMENDMENT-20260721-H0007-pre-execution.md` established as a project-wide standard (seed = protocol freeze date) |
| Multiple-comparison correction | Not addressed in the original entry at all | Bonferroni (`0.05/60`) across the full 60-cell grid, **plus** the already-specified replication requirement (both, per `RESEARCH.md`'s own permitted options, matching H0010's precedent of correcting a pre-registered grid) |
| Replication ("held-out later time slice") | Named but not defined — no split rule, no embargo, no pass/fail threshold for the hold-out slice itself | Chronological halves of the valid-day population, 1-day embargo, hold-out slice requires the same sign and a standard-95%-CI-supported deviation exceeding cost |
| Minimum-N-per-cell gate | Failure mode #2 names the *risk* ("sparse extreme-price buckets") without a number | **20 distinct settlement days** minimum per cell to support "rejected"; below that, `insufficient_n` |
| Executable-price robustness check | Failure mode #3 names the check without specifying which prices, or that it is descriptive-only, non-primary | Ask-implied and bid-implied, computed only for cells that already cleared both primary screens, explicitly non-primary and unable to independently support "rejected" |
| Primary vs. secondary price choice | "market mid-prices" stated as the analysis target, but the task instructing this update required an explicit, justified choice among {bid, ask, mid, trade} | Mid confirmed as primary (this *is* what the original already said — now justified explicitly); trade price explicitly excluded, reusing the platform-wide quote-over-trade convention H0007 already established |
| Full metrics list | Only "reliability curve," "Brier score," and "per-cell deviation vs. cost band" named | Adds, as secondary/diagnostic only (never primary): log loss, Expected Calibration Error, calibration slope/intercept — standard companions to a reliability-curve study, none of which changes what "confirmed"/"rejected" means |
| Halted-market treatment | Not addressed in the original | Documented as **not representable** on this platform (`market_price_weather` carries no halt-status column) — stated as a limitation, not papered over |
| Information-set boundary against H0006/H0007-adjacent columns | Not addressed (those columns didn't exist at H0005's original writing) | `market_price_weather`'s observation/running-extreme/lock columns are explicitly marked out-of-scope for H0005 (§3), preventing accidental scope creep into H0006/H0007 territory through a column that happens to be sitting in the same frame |

## Category C: Explicitly NOT changed

- The null-form framing and its direction (H0005 is expected to broadly
  survive; "confirmed" = the null survives = the market is calibrated
  within costs).
- The core test statistic: implied-probability-decile vs. realized
  frequency, evaluated against a cost-band threshold, not a
  zero-deviation threshold.
- `KXHIGHNY` as the sole primary series; `KXLOWTNYC` as exploratory-only.
- Brier score as a required metric.
- The three original failure-mode mitigations (day clustering, minimum-N
  gate, executable-price check) — all three retained, now with exact
  parameters.
- `HYPOTHESES.md`'s own Hypothesis/Rationale/Required data/Experiment
  design/Metrics/Statistical tests/Failure modes/Difficulty/Dependencies
  text — **untouched**; only a single new pointer line was added (see the
  entry itself), and `Status`/`Closed`/`Results`/`Conclusion` remain
  exactly as before this update (still `Proposed`, not yet run).

## What this document is not

This is not an amendment resolving an audit finding (contrast
`AMENDMENT-20260721-H0007-pre-execution.md`, which responded to an
independent institutional review). No such review of H0005 has occurred.
This document exists solely because the task that produced it explicitly
required "every deviation from the original preregistration" to be
catalogued before execution — an ordinary, expected step whenever a
hypothesis's implementation is updated to reflect infrastructure that did
not exist (or was not yet known to be inadequate) at the time it was first
registered.
