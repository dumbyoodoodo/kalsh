# Architecture

## Design principles

1. **Settlement correctness before modeling.**
2. **Raw data is immutable.**
3. **Normalized data is versioned.**
4. **Decision inputs are reproducible.**
5. **Research and execution use the same domain logic.**
6. **Live trading is an adapter, not a special code path.**
7. **Failures should stop trading, not weaken checks.**

## Development phases

This architecture is built incrementally, gated phase by phase — see `ROADMAP.md` for version numbers and `TASKS.md` for the task-level checklist. A component below should not be implemented before its phase is reached, even if it's convenient to build early.

| Phase | Focus | Trading logic present? |
|---|---|---|
| 1. Infrastructure (complete) | Kalshi gateway, storage, CLI | No |
| 2. Historical Data Platform | Weather gateway, settlement resolver, ingestion, point-in-time datasets | No |
| 3. Research Framework | `RESEARCH.md` / `HYPOTHESES.md` conventions, dataset versioning, experiment tracking | No |
| 4. Feature Engineering | Feature builder | No |
| 5. Probability Models | Baseline → Gaussian → Student-t → GBM → Bayesian, calibration | No |
| 6. Backtesting | Strategy engine, cost/signal engine, event-driven backtester | Simulated only, no broker calls |
| 7. Paper Trading | `PaperBroker`, `KalshiDemoBroker` | Simulated funds only |
| 8. Live Trading | `KalshiLiveBroker`, production review | Real capital, disabled by default |

Backtesting (Phase 6) comes before paper trading (Phase 7): no strategy is considered a candidate for paper trading, let alone live trading, without first passing a conservative, cost-aware backtest. See `RESEARCH.md` for what "passing" means (calibration, sample size, out-of-sample stability) — a positive backtest P&L alone is not sufficient.

## Main data flow

```text
Kalshi REST/WebSocket ─┐
                      ├─> Raw append-only store ─> Normalizers ─> Canonical DB
Weather APIs/files ───┘                                      │
                                                             v
Settlement resolver ─> Feature builder ─> Probability model ─> Fair value
                                                             │
                                      Market snapshot ───────┤
                                                             v
                                                  Signal + risk checks
                                                             │
                             Historical replay / Paper / Demo / Live broker
                                                             │
                                                             v
                                                    Orders, fills, P&L
```

Everything left of "Signal + risk checks" is Phases 2-6 (research); everything from "Historical replay" onward is Phases 6-8 (execution), reusing the same signal/domain logic per design principle 5. "Historical replay" (the backtester) is reached before "Paper" or "Demo" in practice, even though the diagram lists them side by side as broker implementations.

## Components

### Kalshi gateway

Responsibilities:

- environment-specific base URLs
- RSA request signing for authenticated endpoints
- REST pagination
- WebSocket authentication and reconnects
- schema validation
- rate-limit handling
- raw response capture
- idempotent client order IDs

The public market-data client and authenticated trading client should be separable.

### Weather gateway

Initial provider should be an official source suitable for the target settlement rules. Keep the provider interface abstract because forecast grids, stations, observations, and ensemble data may come from different sources.

Responsibilities:

- station metadata
- forecast issue time and valid time
- observations
- units and conversions
- source/version metadata
- raw payload persistence

### Settlement resolver

This is a critical control.

Input:

- market ticker
- event metadata
- complete rules text
- selected market subtitle/strike information

Output:

- canonical station identifier
- source
- variable
- event date
- observation window
- timezone
- threshold or bucket
- inclusivity (`>`, `>=`, `<`, `<=`)
- units
- precision/rounding
- resolution deadline
- parser version
- confidence status

No strategy may trade a market whose settlement mapping is unresolved or ambiguous.

### Probability model

Version 1 should be simple and interpretable:

- forecast central estimate
- historical forecast error conditioned on station, horizon, season, and possibly forecast value
- parametric or empirical residual distribution
- probability integral across the exact contract threshold/bucket
- optional calibration layer learned only on prior data

Examples:

```text
P(Tmax > K)
P(L <= Tmax < U)
```

Model outputs must include point probability, uncertainty range, model version, data timestamp, and diagnostics.

Each model added beyond the empirical baseline (Gaussian, Student-t, gradient-boosted trees, Bayesian) must be justified by a hypothesis in `HYPOTHESES.md` and compared against simpler baselines primarily on calibration, not backtest P&L. See `RESEARCH.md` for evaluation methodology.

### Strategy engine

The strategy does not predict price direction. It compares executable market prices to fair probabilities.

Inputs:

- best executable bid/ask
- depth
- fair probability
- confidence interval
- fees
- expected slippage
- stale-data age
- risk state

Output:

- no action
- buy YES
- buy NO
- reduce/exit
- proposed limit price and size
- reason codes

A strategy is a hypothesis made executable. It should not exist in code before it exists as an entry in `HYPOTHESES.md` with a rationale and experiment design; see `STRATEGY_SPEC.md` for the strategy template it should follow once validated.

### Broker interface

```python
class Broker(Protocol):
    async def get_positions(self) -> list[Position]: ...
    async def get_open_orders(self) -> list[Order]: ...
    async def submit_order(self, order: NewOrder) -> OrderAck: ...
    async def cancel_order(self, order_id: str) -> None: ...
```

Implementations:

- `ReplayBroker`
- `PaperBroker`
- `KalshiDemoBroker`
- `KalshiLiveBroker`

The live broker must fail closed. None of these exist yet (Phase 1 is data-gateway only, and this protocol first gets implemented in Phase 6's `ReplayBroker`); this section documents the eventual shape so later phases build toward a consistent interface.

## Storage strategy

- PostgreSQL: normalized durable operational data
- object/file storage locally at first: compressed raw JSON payloads
- DuckDB/Parquet: research snapshots and backtests
- Alembic: schema migration

## Reliability behavior

- stale market data => no new orders
- stale forecast data => no new orders
- unresolved settlement => no new orders
- model load failure => no new orders
- database unavailable => no new orders
- Kalshi disconnect => cancel or freeze according to configured policy
- breached position/loss limit => cancel open orders and disable strategy
