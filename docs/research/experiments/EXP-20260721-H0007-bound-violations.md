# EXP-20260721-H0007-bound-violations

Executed exactly as pre-registered in
`PREREG-20260721-H0007-bound-violations.md`,
`AMENDMENT-20260721-H0007-pre-execution.md`, and
`AMENDMENT-20260721-H0007-fee-verified.md`; no metric, threshold, entry
condition, or decision rule was altered after seeing data. Machine-readable
results: `EXP-20260721-H0007-bound-violations-results.json` (same
directory), produced by `scripts/exp_h0007_bound_violations.py`.

## Hypothesis

**H0007 — Preliminary-observation bound violations near settlement.** After
a same-day preliminary CLI report shows a running high of X°F, markets
occasionally continue to price P(final high < X) above the level H0002's
upward-revision-only constraint justifies. Pre-registered decision rule:
**confirmed** if opportunity-day frequency's 95% CI lower bound > 5% *and*
aggregate hypothetical P&L's 95% CI lower bound > 0; **rejected** if
occurrences are rarer or the aggregate is non-positive.

## Reproducibility

| Field | Value |
|---|---|
| Dataset version | `exp-20260721-h0007` (`data/datasets/exp-20260721-h0007/`) |
| Frame used | `market_price_weather.parquet` |
| Content hash (market_price_weather) | `d88e6ccb5ace784f22d251c6…` (full value in `manifest.json`) |
| Content hash (settlement_labels) | `b0d907c051ed2d8b3710717b…` |
| Source DB revision | `0007` |
| Dataset-build git commit | `5eadfb3e` |
| Fee config | `KALSHI_WEATHER_TAKER_FEE_CONFIG` (multiplier 0.07, verified) |
| Random seed | 20260721 (bootstrap only; everything else deterministic) |
| Bootstrap resamples | 10,000, day-clustered, percentile method |
| Date run | 2026-07-21 |

## Methodology

Entry conditions, episode definition, persistence, candle adjacency, outcome
definition, and the decision rule are implemented exactly as specified in
the frozen documents above (see `scripts/exp_h0007_bound_violations.py`'s
per-function docstrings for the exact section/finding each block
implements). One methodology note not explicit in the frozen text: the
`market_price_weather` frame does not carry Kalshi's own `kalshi_result`
column (only `market_prices` does); the settlement outcome used for P&L
(never for entry) is derived via `settlement/labels.py`'s `implied_result`
applied to `value_at_settlement` plus the market's own strike bounds -- the
exact function ADR 0006 validated to reproduce Kalshi's own `result` field
exactly, and the same reuse-verbatim instruction Sec 4 condition 4 already
requires for the entry-side bound logic.

## Sample and coverage

- **tmax_f (primary cell)**: 66 valid-coverage settlement days
  out of 67 total (1 excluded for missing
  settlement source data: 2026-07-02) -- clears the pre-registered
  minimum of 60.
- **8,356 candles** satisfied entry conditions 1-4/6/7
  (bounded strike, unlocked, coherent quote, pre-close) across the full
  archive.
- **0 candles** (0/8,356) showed
  any raw residual mass (`sellable_price_of_bounded_side_cents >= 2`) at
  all, before the fee-net threshold.
- **0 candles** cleared the fee-net threshold.
- **0 episodes** total (maximal runs of fee-net-qualifying,
  1-minute-adjacent candles); **0** met the
  >=2-consecutive-candle persistence requirement and counted toward the
  primary metrics.

## Results (pre-registered metrics)

**Primary decision:**

| Metric | Value | 95% CI | Threshold |
|---|---|---|---|
| Opportunity-day frequency | **0.0000** (0/66) | [0.0000, 0.0550] | lower bound > 0.05 |
| Aggregate hypothetical P&L | **$0.00** (0 episodes) | [0.0, 0.0] | lower bound > 0 |

**Decision: REJECTED** (aggregate P&L CI upper bound <= 0).

**Raw finding, before any statistical machinery**: of 8,356
candles meeting the mechanical entry conditions (a bounded strike,
unlocked, coherent quote, pre-close), only **0 showed any
measurable residual mass at all** -- the bounded/near-impossible side was
priced at the exchange's own minimum tick (bid 0¢ / ask 1¢, i.e.
`sellable_price_of_bounded_side_cents == 0` in every single candidate row)
essentially uniformly. This is not a fee-net result narrowly missing the
bar; the raw, cost-free version of the effect is absent from the data at
1-minute candle resolution.

## Secondary outcomes

- **Edge size**: n=0; mean/median not
  computable (zero counting episodes).
- **Persistence**: n=0; not
  computable (zero counting episodes).
- **Time-to-correction**:
  n=0;
  not computable (zero counting episodes).
- **Failure-mode-1 transparency** (single-episode-driven days):
  0 of 0
  opportunity-days -- moot, since there are zero opportunity-days.

## Exploratory (not used for the primary decision)

- **tmin_f cell** (mirrored mechanism): 8,350 candidate
  candles, 0 fee-net qualifying, 0 counting
  episodes. Same pattern as the primary cell: raw residual mass is absent.
- **By strike type** (raw candidate/qualifying counts): see
  `by_strike_type` in the results JSON.
- **By month** (descriptive only): see `by_month_descriptive` in the
  results JSON.

## Conclusion

**REJECTED**, per the pre-registered decision rule, cleanly rather than at
the margin: across 8,356 candles where the mechanical
entry conditions were met (a logically bounded strike, unlocked, coherent
non-crossed quote), the market's quoted price for the near-impossible side
was at the exchange's own minimum tick in every single case -- zero raw
residual mass, let alone fee-net. The opportunity-day frequency point
estimate is 0 (Wilson 95% CI upper bound 0.0550, narrowly above
the pre-registered 5% threshold on that one criterion alone, but the
aggregate-P&L criterion -- CI upper bound $0.00 with zero qualifying
episodes -- independently and unambiguously satisfies the rejection rule).

Per `AMENDMENT-20260721-H0007-pre-execution.md` Finding 6, this conclusion
is stated within its permissible scope: this is evidence about *quote-level
pricing behavior* at 1-minute candle resolution, not a claim about realized
trading profit, fill probability, or latency-adjusted capturability -- no
such claim would be supportable regardless, since the result found nothing
to capture. No forecasting feature, statistical model, or alpha claim was
introduced in producing this result.

## Limitations

- **1-minute candle resolution** is the finest Kalshi provides; a
  violation that opens and closes within a single minute is invisible to
  this design (and, since Kalshi's own minimum tradeable price is 1¢, so is
  any violation cheaper than the fee to close it).
- **tmax_f only** for the primary decision, per pre-registration; the
  tmin_f exploratory cell shows the same pattern but was not subjected to
  the same statistical rigor (no bootstrap CIs computed for it, by design
  — see Sec 6's multiple-testing discussion).
- **NYC only** (both series settle against the same station), consistent
  with every prior hypothesis on this platform to date.
- **Issuance-to-availability latency is unmeasured** (Finding 6) — moot
  here, since no exploitable gap was found regardless of latency.
