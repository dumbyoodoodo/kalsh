# Implementation Roadmap

Claude Code should work top to bottom. Each milestone must be testable and independently useful.

This roadmap is organized around a research-first development philosophy: discovering a statistically significant, repeatable edge matters more than shipping trading infrastructure quickly. See `ROADMAP.md` for the version-numbered long-term view, `RESEARCH.md` for methodology, and `HYPOTHESES.md` for the process every strategy/model idea must go through before implementation. No trading logic (paper, demo order submission, or live) is added before Phase 6 (Backtesting) has something to backtest.

---

## Phase 1 — Infrastructure (complete)

### Milestone 0 — Repository foundation

- [x] Initialize Python 3.12 project with `uv`.
- [x] Add Ruff, mypy, pytest, pytest-asyncio, Hypothesis.
- [x] Create package layout from `CLAUDE.md` (partial by design: only packages this
      milestone uses were created; see `docs/adr/0001-initial-architecture.md`).
- [x] Add typed settings with explicit environment selection.
- [x] Add `.env.example`; ensure secrets are gitignored.
- [x] Add structured logging.
- [x] Add Docker Compose with PostgreSQL.
- [x] Add SQLAlchemy and Alembic baseline.
- [x] Add CI running lint, type check, and tests.
- [x] Add `make check` or equivalent.
- [x] Add live-trading safety tests.

Acceptance criteria:

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

all pass.

### Milestone 1 — Kalshi read-only market-data gateway

- [x] Implement environment base URLs (confirmed against live API and the
      official Kalshi starter-code reference; see docs/API_VERIFICATION.md).
- [x] Implement unauthenticated production market-data requests where supported.
- [x] Implement RSA authentication module for demo/private endpoints.
- [x] Implement API response schemas.
- [x] Implement pagination.
- [x] Retrieve series, events, markets, market details, trades, and order book.
- [x] Correctly reconstruct YES and NO asks from complementary bids.
- [x] Store raw payloads and normalized snapshots.
- [x] Add CLI commands:
  - `series list`
  - `markets list`
  - `market show TICKER`
  - `orderbook show TICKER`
- [x] Add unit tests with recorded fixtures.
- [x] Add opt-in demo integration tests.

**API verification: Passed** (2026-07-20) — see
[`docs/API_VERIFICATION.md`](docs/API_VERIFICATION.md) for the full report.
Public REST endpoints, pagination, order-book reconstruction, and
authenticated portfolio reads are all verified against live data. Several
confirmed wire-format mismatches were found and fixed (signing path prefix,
`*_dollars`/`*_fp` price/quantity fields, a bad foreign key that broke
`market show`). The initial 401s on `/portfolio/*` were a demo-vs-production
API key mismatch (not a code defect) and are resolved with a correctly
demo-scoped key.

Acceptance criteria:

- Read-only collection works.
- Empty books and malformed responses are handled.
- No order endpoint exists yet.

---

## Phase 2 — Historical Data Platform

**No trading, feature, or modeling logic belongs in this phase.** The objective is collecting and versioning data correctly: Kalshi market data, order books, trades, weather forecasts, and weather observations, with point-in-time correctness and no look-ahead bias. Everything downstream (Phases 4-8) depends on getting this right.

### Milestone 2 — Kalshi historical market-data collector

(Renamed/narrowed from "Weather-market discovery and settlement mapping" — the settlement-rule-parsing half of that original milestone moved to Milestone 2b below, unstarted; this milestone is complete.)

- [x] Discover active weather markets automatically (category-filtered series/events/markets).
- [x] Snapshot collector: periodic market metadata + order-book snapshots, immutable, content-hash deduplicated.
- [x] Trade collector: incremental (`min_ts` checkpointed), deduplicated by `trade_id`, source timestamps preserved exactly.
- [x] Schema: ingestion timestamp, source timestamp, schema version, raw-payload linkage on all normalized tables.
- [x] Validation: duplicate detection, timestamp validation, payload integrity, malformed-item rejection that doesn't crash the cycle.
- [x] CLI: `collector run [--once] [--interval] [--category] [--status]`, graceful SIGINT/SIGTERM shutdown.
- [x] Comprehensive mocked unit tests (no live-API dependency).
- [x] Documentation: `ARCHITECTURE.md`, `DATA_MODEL.md`, `docs/runbooks/collector.md`, `docs/adr/0002-ingestion-collector.md`.

Acceptance criteria:

- A full collection cycle runs against mocked responses without error.
- Duplicate snapshots are skipped; malformed per-market data doesn't crash the cycle.
- `ruff check .`, `mypy src`, `pytest -v` all pass.
- Verified live against the demo API: `collector run --once` discovers weather markets and persists snapshots/order books/trades; a second `--once` run shows duplicates correctly skipped.

### Milestone 2b — Settlement mapping

(The settlement-rule-parsing half of the original Milestone 2; not started.)

- [ ] Retrieve complete rules for target markets.
- [ ] Define typed settlement specification.
- [ ] Implement a conservative parser.
- [ ] Mark ambiguous mappings unresolved.
- [ ] Create manual override file with audit fields.
- [ ] Add CLI command `settlement resolve TICKER`.
- [ ] Build a validation report comparing parsed results with raw rules.

Acceptance criteria:

- At least one chosen daily-temperature series is mapped correctly.
- No unresolved contract can reach a strategy.

### Milestone 3 — Weather data ingestion

- [x] Select official provider matching settlement needs (NWS Climatological Report / "CLI" text product, confirmed live against real Kalshi rules text — `docs/adr/0003-weather-data-source.md`).
- [x] Implement station lookup and metadata (`weather/stations.py`, `WeatherProvider.get_station_metadata`).
- [x] Collect timestamped forecasts (`get_forecast`, live-only — historical forecast backfill is a documented, deferred limitation, see the ADR).
- [x] Collect official observations, with historical backfill beyond the live API's retention window (`get_observations`, routed between `api.weather.gov` and IEM).
- [x] Normalize units (Fahrenheit, `Decimal`).
- [x] Persist raw and normalized data (`weather_stations`/`weather_observations`/`weather_forecasts`, `raw_api_payloads`).
- [x] Track provider issue time, receipt time, and valid time (`issuance_time`/`issue_time`, `observed_at`, `valid_start`/`valid_end`).
- [x] Add completeness and freshness checks (per-item validation in `ingestion/validation.py`: temperature plausibility, timestamp sanity; malformed CLI reports logged and skipped, not silently dropped).

Acceptance criteria:

- [x] Re-running ingestion is idempotent (dedup by `(station_id, variable, issuance_time)` / `(station_id, variable, issue_time, valid_start)`; verified by `tests/unit/test_weather_collector.py` and `tests/unit/test_repositories.py`).
- [x] Historical forecasts are never overwritten (every issuance is a new row; historical *backfill* of forecasts predating the collector's first run is explicitly not implemented — a stated limitation, not silent data loss).
- [x] Missing/late data is visible (invalid/malformed items counted in `WeatherCycleStats.invalid_items` and logged; per-station and per-cycle failures logged and counted rather than crashing).
- [x] `ruff check .`, `mypy src`, `pytest -v` all pass.

### Milestone 4 — Research dataset

- [x] Join market, settlement, forecast, and observation data without leakage (`dataset/builder.py`; as-of joins via Polars `join_asof`). Settlement association uses an explicit market→station config map as a documented stand-in for `settlement_specs` until Milestone 2b (`dataset/market_map.py`).
- [x] Create point-in-time snapshots (`market_weather`: each market snapshot enriched with only the forecast/observation/order-book/trade facts knowable as of its timestamp).
- [x] Calculate forecast residuals (`weather_panel`: `residual_high_f`/`residual_low_f` vs the settled tmax/tmin; label-side, documented).
- [x] Export versioned Parquet datasets (`dataset/export.py`; `<DATASET_ROOT>/<version>/`, immutable, DuckDB left as a documented future seam).
- [x] Add dataset manifest with date range, row count, hashes, and schema version (`dataset/manifest.py`; also git commit + source DB Alembic revision + credential-free DB URL + per-frame content hashes + generation config).
- [x] Add leakage tests (`tests/unit/test_dataset_builder.py` as-of leakage tests; validation `impossible_timestamps`/`settlement_mismatches`/`duplicate_join_*`).
- [x] CLI: `dataset build|validate|stats|export`, and a validation report + summary statistics per build.
- [x] Documentation: `ARCHITECTURE.md`, `DATA_MODEL.md`, `docs/runbooks/dataset.md`, `docs/adr/0004-research-dataset.md`.

Acceptance criteria:

- [x] Any row can be traced to raw sources (source tables' `raw_payload_id` carried through; manifest pins git commit + source DB revision + market-map hash).
- [x] Dataset can be rebuilt deterministically (order-independent content hashes; verified by `tests/unit/test_dataset_pipeline.py::test_pipeline_build_is_reproducible`).
- [x] No migration (read-only over existing tables); `ruff check .`, `mypy src`, `pytest -v` all pass.
- [x] Verified live against the Postgres DB: `dataset build` produces `weather_panel`/`market_weather` Parquet + manifest/validation/stats; validation reports only expected warnings.

---

## Phase 3 — Research Framework

Establishes how research is conducted and recorded for every phase from here on. This is largely a documentation and convention phase, not a large code phase.

- [x] Add `RESEARCH.md` documenting research philosophy, reproducibility, dataset
      versioning, experiment tracking, evaluation methodology, calibration,
      statistical significance, walk-forward validation, and leakage avoidance.
- [x] Add `HYPOTHESES.md` with the hypothesis template and process.
- [x] Add `ROADMAP.md` with the long-term version-numbered roadmap.
- [ ] Establish the experiment-tracking convention in practice (where results
      get logged, what "dataset version" means concretely, given the Parquet
      manifests from Milestone 4).
- [ ] Record the first hypothesis (or several) in `HYPOTHESES.md` for the
      daily-temperature thesis in `STRATEGY_SPEC.md`, before any Phase 4/5 code
      is written against it.

Acceptance criteria:

- Every experiment from Phase 4 onward references a hypothesis ID (from
  `HYPOTHESES.md`) and a dataset version (from the Milestone 4 manifest).

---

## Phase 4 — Feature Engineering

Document the feature pipeline before building it broadly; only implement features a live hypothesis actually needs.

Candidate features (examples, not commitments):

- forecast error
- ensemble spread
- humidity
- wind
- pressure
- seasonal effects
- liquidity
- spread
- order-book imbalance
- time until settlement

- [ ] Implement `features/forecast_error.py` for the features the first
      recorded hypothesis (Phase 3) actually requires.
- [ ] Implement `features/builder.py` to assemble a point-in-time feature
      snapshot from the Phase 2 research dataset, with no leakage.
- [ ] Version feature definitions alongside dataset versions (see `RESEARCH.md`).

Acceptance criteria:

- A feature snapshot is reproducible from dataset version + feature version.
- No feature uses data unavailable at decision time.

---

## Phase 5 — Probability Models

Progression, evaluated primarily on calibration rather than P&L alone (see `RESEARCH.md`):

1. Historical baseline (empirical residual CDF)
2. Gaussian residual model
3. Student-t residual model
4. Gradient-boosted trees
5. Bayesian approaches

Do not skip ahead in this list without first showing the simpler model is insufficient — see the "prefer simple models" rule in `CLAUDE.md`.

### Milestone 5 — Baseline probability model

- [ ] Implement empirical residual model.
- [ ] Implement Gaussian baseline.
- [ ] Implement Student-t baseline.
- [ ] Convert distributions to exact contract probabilities.
- [ ] Add rolling/expanding time-series validation.
- [ ] Generate calibration, Brier score, and log-loss reports.
- [ ] Add model registry and version metadata.

Acceptance criteria:

- Probability predictions are valid and reproducible.
- Baselines are compared out of sample.

### Milestone 5a — Advanced models (only if justified)

- [ ] Gradient-boosted trees, only after a `HYPOTHESES.md` entry shows the
      Milestone 5 baselines are miscalibrated or insufficiently sharp in a
      way additional structure could plausibly fix.
- [ ] Bayesian approaches, only after the same bar is met.

Acceptance criteria:

- Each advanced model is compared against the best Milestone 5 baseline on
  calibration, not just backtest return.

---

## Phase 6 — Backtesting

Backtesting comes before paper trading. No strategy is considered successful without passing a conservative, cost-aware backtest first.

### Milestone 6 — Cost and signal engine

- [ ] Implement executable-price calculations.
- [ ] Implement current fee model from configurable series metadata/schedule.
- [ ] Implement slippage and uncertainty buffers.
- [ ] Generate auditable signals with reason codes.
- [ ] Add fixed-size and capped fractional-Kelly sizing.
- [ ] Unit test all boundaries.

Acceptance criteria:

- No positive signal based on last-trade price alone.
- Every rejected trade has a reason.

### Milestone 7 — Event-driven backtester

- [ ] Replay quotes, forecasts, signals, orders, fills, and settlement.
- [ ] Support market orders and conservative limit-fill models.
- [ ] Model latency, depth, partial fills, fees, and capital usage.
- [ ] Produce prediction and trading reports.
- [ ] Run cost and fill sensitivity analysis.
- [ ] Add walk-forward evaluation.

Acceptance criteria:

- No look-ahead.
- Results can be reproduced from config and dataset version.
- Report prominently shows sample size and uncertainty.
- The corresponding `HYPOTHESES.md` entry is updated with the result
  (confirmed, rejected, or inconclusive) before moving to Phase 7 for that
  strategy.

---

## Phase 7 — Paper Trading

Only after: validated data (Phase 2), a calibrated probability model (Phase 5), and a strategy that has passed a conservative backtest (Phase 6) for the specific hypothesis being paper-traded.

### Milestone 8 — Paper trading

- [ ] Implement paper broker using live quotes.
- [ ] Add order state machine.
- [ ] Add position and P&L reconciliation.
- [ ] Add stale-data and disconnect handling.
- [ ] Add monitoring endpoints and kill switch.
- [ ] Run continuously with no real orders.

### Milestone 9 — Kalshi demo execution

- [ ] Implement authenticated order submission only for demo.
- [ ] Add client order IDs and idempotency.
- [ ] Add cancel/replace.
- [ ] Subscribe to fills and order updates.
- [ ] Reconcile REST and WebSocket state.
- [ ] Add hard risk limits.
- [ ] Chaos-test reconnects and duplicate messages.

Demo uses simulated funds only (see README environment policy); this milestone still carries no real-money risk.

---

## Phase 8 — Live Trading

### Milestone 10 — Production review

Do not implement automatically.

Create a written review covering:

- validation history
- model calibration
- backtest assumptions
- paper/demo results
- risk limits
- operational failure modes
- legal/compliance/account constraints
- explicit human approval

Production remains disabled unless the owner intentionally authorizes it. Live trading must remain disabled by default at every point before this milestone is explicitly, manually completed.
