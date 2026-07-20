# Strategy Specification — Daily Temperature Contracts

## Status: candidate template, not a committed roadmap item

This document describes what a daily-temperature strategy would look like *if* the underlying hypothesis is validated — it is a template for Phase 6 (Backtesting) onward, not an instruction to build it now. Nothing in this document should be implemented until:

1. A corresponding entry exists in `HYPOTHESES.md` with rationale, required data, and experiment design recorded *before* results.
2. Phase 2 (Historical Data Platform) and Phase 3 (Research Framework) are in place, per `TASKS.md` and `ROADMAP.md`.
3. Phase 5 (Probability Models) has produced a calibrated model for the relevant contract, evaluated per `RESEARCH.md`.

If a hypothesis about daily-temperature contracts is rejected or produces no exploitable edge, this document should be updated to reflect that rather than left implying the strategy is validated.

## Scope

Start with one repeated daily maximum-temperature series at one weather station.

Do not initially trade:

- precipitation
- snowfall
- hurricane paths
- politics
- economics
- sports
- crypto
- multi-leg/combo markets
- markets with unclear settlement wording

## Thesis

Market participants may misestimate the distribution around an official temperature forecast, especially near threshold boundaries. The system attempts to estimate the complete conditional distribution rather than treating the published forecast as certain.

This is a hypothesis to test, not an assumption of profit. It should be tracked as an entry in `HYPOTHESES.md`, not treated as pre-validated because it's written down here.

## Baseline model

For a target day's official maximum temperature:

```text
actual_temperature = forecast + forecast_error
```

Estimate forecast error using only historical observations available before each simulated decision.

Condition the error distribution on, initially:

- station
- forecast horizon bucket
- month or season

Only add features after enough data exists.

Possible baseline implementations:

1. empirical residual CDF
2. Gaussian residual model
3. Student-t residual model

Compare all three. Favor calibration and stability over backtest return. See `RESEARCH.md` for the calibration methodology and `ROADMAP.md`/`TASKS.md` for where gradient-boosted and Bayesian extensions fit relative to these baselines.

## Contract probability

For threshold `K`:

```text
P(YES) = P(Tmax satisfies exact contract comparison with K)
```

For bucket `[L, U)`:

```text
P(YES) = P(L <= Tmax < U)
```

The exact inequality and any official rounding must come from the settlement specification.

## Executable prices

Do not use last trade as the assumed entry price.

- To buy YES immediately, use best YES ask.
- To sell YES immediately, use best YES bid.
- To buy NO immediately, use best NO ask.
- Include available depth at each level.
- A resting limit-order backtest requires a fill model and cannot assume every touch fills.

## Edge calculation

Conceptually:

```text
net_edge =
    fair_probability
    - executable_price_probability
    - fee_cost
    - expected_slippage
    - model_uncertainty_buffer
    - stale_data_buffer
```

Represent costs consistently in expected dollars or probability points.

## Entry rules — research defaults

No trade unless all are true:

- settlement spec resolved
- market open and tradable
- market and forecast data are fresh
- probability model passes diagnostics
- net edge exceeds configured minimum
- sufficient displayed depth exists
- portfolio and event limits pass
- expected value remains positive under conservative cost assumptions

Initial thresholds must be configuration, not constants.

## Sizing

Do not use full Kelly.

For initial research:

- fixed tiny notional or capped fractional Kelly
- per-market cap
- per-event cap
- per-station/day cap
- daily loss cap
- total exposure cap

A model confidence interval that crosses the executable price should generally result in no trade.

## Exit behavior

Test multiple policies independently:

- hold to settlement
- exit when edge disappears
- exit at time cutoff
- reduce when forecast changes materially

Never choose an exit policy by inspecting test-period results.

## Backtest requirements

The event-driven backtest must model:

- historical information availability
- forecast issue times
- market quote timestamps
- bid/ask
- order-book depth
- fees applicable at that time/series
- slippage
- order latency
- partial fills
- settlement
- capital tied up until exit/settlement

## Required metrics

Prediction quality:

- Brier score
- log loss
- calibration curve
- reliability by probability bucket
- sharpness
- sample count

Trading quality:

- net P&L
- return on deployed capital
- maximum drawdown
- hit rate
- average net edge at entry
- realized value versus predicted edge
- P&L by station, horizon, month, threshold distance, and liquidity
- turnover and fee share
- sensitivity to added slippage and reduced fills

## Validation gates before demo automation

- no known look-ahead leakage
- settlement mappings manually checked on a representative sample
- unit and contract tests passing
- probabilities reasonably calibrated out of sample
- performance not dependent on a few trades
- profitable or at least promising under conservative costs
- stable across rolling windows

## Validation gates before production

Define numeric gates only after collecting enough data. Production must also require:

- at least several months of uninterrupted paper/demo operation
- reconciliation between predicted and actual fills
- incident runbooks
- kill switch tested
- daily loss and exposure limits tested
- manual approval
