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

(The settlement-rule-parsing half of the original Milestone 2; complete — `docs/adr/0005-settlement-resolution.md`.)

- [x] Retrieve complete rules for target markets (already collected via Milestone 2 snapshots; the series' structured `settlement_sources` citation is now also persisted at collection time).
- [x] Define typed settlement specification (`settlement/spec.py`: `SettlementSpec` with status, confidence, parser version, and a rules hash over the exact parsed inputs).
- [x] Implement a conservative parser (`settlement/parser.py`: structured settlement-source URL first — `product=CLI`, `issuedby`, `site` — prose only for variable/date, each cross-validated against a second source; pure and deterministic).
- [x] Mark ambiguous mappings unresolved (explicit `ambiguous`/`unresolved`/`unsupported` statuses with machine-readable notes; only `resolved` specs become dataset mappings).
- [x] Create manual override file with audit fields (the Milestone 4 market-map YAML, now with optional `reason`/`author`/`added_on`; overrides take precedence and are recorded in report + manifest).
- [x] Add CLI command `settlement resolve TICKER` (plus `settlement resolve-all [--persist]` and `settlement report`).
- [x] Build a validation report comparing parsed results with raw rules (`settlement report`; live production validation: 270 markets across 50 open CLI-source series — 12/12 registry-station markets resolved at high confidence, 0 ambiguous, 0 guesses, 255 unsupported each with an actionable note).
- [x] Database: append-only versioned `settlement_specs` table (migration `0004`, additive only).

Acceptance criteria:

- [x] At least one chosen daily-temperature series is mapped correctly (KXHIGHNY and KXLOWTNYC both verified against live production markets: correct station/variable/date, high confidence).
- [x] No unresolved contract can reach a strategy (enforced by construction in `specs_to_mappings`; tested).
- [x] Dataset builder unchanged (`market_weather` now populates automatically from parsed specs; verified end-to-end in `tests/unit/test_settlement_resolver.py`).
- [x] `ruff check .`, `mypy src`, `pytest -v` all pass; Alembic `0004` round-trips.

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

### Milestone 4c — Canonical settlement-time labels (E-A; complete)

Label-validity prerequisite for all market experiments — see `docs/adr/0006-settlement-labels.md` and `docs/runbooks/settlement_labels.md`.

- [x] Settlement-timeline research against live finalized markets: Kalshi exposes exact `settlement_ts`, the paid `expiration_value`, `result`, and structured strikes; close (~00:59 ET) precedes the final CLI report (~2:15am ET) which precedes settlement (~8am ET) — the stages genuinely differ.
- [x] Typed versioned `SettlementLabel` (`settlement/labels.py`): value at close / at settlement / latest final, strict as-of (no future leakage, tested), `resolved`/`bounded`/`ambiguous`/`unsupported`/`missing_source_data` statuses, conservative close+48h bound when `settlement_ts` is absent.
- [x] Additive migration `0006`: settlement fields on `market_snapshots`; collector persists them going forward; settlement transitions append one final snapshot (hash includes `result`/`expiration_value`).
- [x] Settled NYC market history retro-fetched (API retention limits markets to ~2-3 recent months of events — documented; older history unrecoverable).
- [x] Payout-agreement validation against Kalshi's own `expiration_value`/`result`; every mismatch listed (see the E-A experiment record for rates).
- [x] Dataset integration without breaking consumers: new `settlement_labels` frame + explicit `value_at_close`/`value_at_settlement`/`latest_final_value`/`settlement_label_status`/`settlement_label_version` columns on `market_weather`; `settled_value` NOT redefined; reconstruction version recorded in manifests.
- [x] H0012 pre-registered before analysis; H0002 extended, not altered (see HYPOTHESES.md and `EXP-20260721-EA-settlement-labels`).

### Milestone 4d — Historical price ingestion (Phase 7A; complete)

Deterministic, additive, production-quality candlestick backfill —
`docs/adr/0007-price-ingestion.md` and `docs/runbooks/price_ingestion.md`.
H0007's price/quote data dependency; does not run H0007 itself.

- [x] `KalshiClient.list_candlesticks` (chunked at the API's ~4900-candle-per-request cap), typed `Candlestick`/`CandlestickPriceBlock` models reusing the existing `*_dollars`/`*_fp` normalization helpers; zero-volume periods (quote-only, no trade) parsed correctly.
- [x] Additive migration `0007`: `market_candlesticks`, unique on `(market_ticker, period_interval_seconds, period_end)` — idempotent, never overwrites; round-tripped live (`upgrade head` → `downgrade 0006` → `upgrade head`).
- [x] Repository layer: insert-if-not-exists batch save, query by market/time-range, latest-stored-candle, per-market coverage, covered-ticker set.
- [x] Resumable backfill job (`prices backfill`): oldest-`close_time`-first (rolling-retention-aware prioritization), per-market commits, dry-run, ticker/event/date filters, `--skip-covered` resume, six per-attempt outcomes (`complete`/`partial`/`no_price_data`/`expired`/`api_failure`/`unsupported` — a market is never silently dropped).
- [x] Continuous preservation: `run_price_sync_loop` as a third task in the existing `ops run` supervised loop (no new scheduler); `collector="prices"` rows in `collector_runs`; `price_retention` block in `ops health` (oldest uncaptured market, markets nearing the retention cutoff, incomplete-coverage count).
- [x] `market_prices` dataset frame: covers every captured market (not filtered to the settlement map), `data_quality_status` distinguishing zero-volume quote-only periods from trades, settlement-label columns joined per market, no future/settlement leakage (tested), `MARKET_PRICES_SCHEMA_VERSION` in manifests.
- [x] Coverage and validation (`prices coverage`): measured markets attempted/captured/complete, candle counts, coverage by event-date/variable, missing-interval detection, age-inferred (never assumed) retention-loss detection, duplicate count (structurally zero), settlement-label overlap.
- [x] Full test suite (unit: parsing, chunking, dedup, resumability, all six outcomes, repository queries, dataset joins, no-leakage, migration round-trip); 284 passed, 4 skipped; `ruff check .` and `mypy src` clean.
- [x] **Live-verified, 2026-07-21**: full backfill over all 804 discoverable settled markets — 804/804 `complete`, 0 failures of any kind, 761,475 candles saved, 0 duplicates; coverage report agrees with backfill totals exactly; 12-market stratified live-API sample matched stored counts exactly (12/12).
- [x] H0007 readiness artifact (report only, H0007 not run): `docs/research/2026-07-21-h0007-readiness.md`.

### Milestone 4e — Point-in-time price/observation alignment (Phase 7B; complete)

Closes the single blocker Milestone 4d's H0007 readiness artifact
identified — `docs/adr/0008-point-in-time-alignment.md`. Feature-engineering
only: does not run H0007, does not analyze prices, does not fit models.

- [x] Reusable as-of join engine extracted to `dataset/asof.py` (`asof_latest`, `asof_count`) out of `builder.py`'s private helpers — deterministic, vectorized (Polars `join_asof(strategy="backward")`), works for any `by` key set (multiple cities), any timestamp resolution; independently tested (`tests/unit/test_dataset_asof.py`).
- [x] Observation timeline reconstruction (`dataset/observation_timeline.py`): `compute_running_extremes` (cumulative max/min across same-day issuances, leak-free by construction) and `attach_calendar_lock` (pure calendar arithmetic — station-local day-elapsed, vectorized per timezone) — both model-free, no forecasting.
- [x] New `market_price_weather` dataset frame (candle grain, mapped markets only): `obs_value_known`/`obs_issuance_time_known`/`obs_age_seconds`, `running_tmax_f_known`/`running_tmin_f_known` + their issuance times, `tmax_locked`/`tmin_locked`, `theoretical_remaining_range_{high,low}_f` (0.0 once locked, else null — never a modeled estimate), `observation_quality_status`; registered in `build_datasets` and `pipeline.build`'s settlement-label join; `MARKET_PRICE_WEATHER_SCHEMA_VERSION` in manifests.
- [x] Leakage validation (unit tests + full-scale check): engine-level (future row never selected), frame-level (a same-day-preliminary-then-revision scenario proves locking is never a shortcut to reveal the final value early; blanket structural invariant over every row), pipeline-level (through the real settlement-label join, DB-backed). **Verified live over the full 761,475-row production archive: 0 leakage violations.**
- [x] Full test suite: 320 passed, 4 skipped; `ruff check .` and `mypy src` clean; existing test suite unaffected by the `asof.py` extraction.
- [x] Readiness verification (report only): `docs/research/2026-07-21-h0007-readiness-update.md`.

### Milestone 4b — Research operations (Phase 6 of the working plan; complete)

Makes the platform run reliably for months with minimal intervention — see `docs/runbooks/operations.md`.

- [x] Combined supervised runner (`ops run`: both collectors concurrently, one process, shared graceful shutdown); restart recovery inherent via data-derived checkpoints (no process state).
- [x] Configurable request pacing for both APIs (`KALSHI_MIN_REQUEST_INTERVAL_SECONDS`, `WEATHER_MIN_REQUEST_INTERVAL_SECONDS`; NWS provider gained a proactive throttle).
- [x] Operational metrics stored historically (`collector_runs` table, migration `0005`, additive; per-cycle duration/success/request/retry counts written best-effort so metrics can never break collection).
- [x] Data-quality monitoring (`ops quality`): schema-revision drift, natural-key duplicates, future timestamps (errors); observation gaps, forecast/market staleness, settlement-resolution failures (warnings) — machine-readable, exit-code gated.
- [x] Health report (`ops health [--json]`): collector liveness/staleness, data freshness, DB size, station/settlement coverage, completeness, quality summary.
- [x] Chunked resumable backfill (`weather backfill`): per-chunk commits, partial-failure isolation with explicit failed-chunk reporting, covered-chunk skip for resume, incremental coverage verification; runtime/storage documented.
- [x] Versioned research snapshots (`ops snapshot`): Milestone 4 dataset export + `quality.json` + `ops.json` (health, resolution report, recent runs) in one immutable directory.
- [x] Station-expansion workflow documented as data-only (`docs/runbooks/station_expansion.md`).

Acceptance criteria:

- [x] Existing collector behavior unchanged (all prior tests pass untouched); metrics/quality layers are additive.
- [x] `ruff check .`, `mypy src`, `pytest -v` all pass; migration `0005` round-trips.
- [x] Verified live: `weather backfill` extended NYC history with per-chunk commits and correct resume; `ops health`/`ops quality`/`ops snapshot` all produce correct reports against the real database (a live-only cross-transaction FK bug in the backfill sink was caught by this verification and fixed).

---

## Phase 3 — Research Framework

Establishes how research is conducted and recorded for every phase from here on. This is largely a documentation and convention phase, not a large code phase.

- [x] Add `RESEARCH.md` documenting research philosophy, reproducibility, dataset
      versioning, experiment tracking, evaluation methodology, calibration,
      statistical significance, walk-forward validation, and leakage avoidance.
- [x] Add `HYPOTHESES.md` with the hypothesis template and process.
- [x] Add `ROADMAP.md` with the long-term version-numbered roadmap.
- [x] Establish the experiment-tracking convention in practice (`RESEARCH.md`
      "Experiment tracking": records in `docs/research/experiments/` named
      `EXP-<date>-<hypothesis-id>-<slug>.md`; "dataset version" concretely means
      the Milestone 4 manifest version **plus** its per-frame content hashes).
- [x] Record the first hypothesis (or several) in `HYPOTHESES.md` for the
      daily-temperature thesis (H0001 decomposed into H0003/H0005/H0006; H0002
      and H0004-H0010 added — ten fully specified entries with pre-registered
      decision rules, statistical tests, failure modes, and dependencies).
- [x] Produce the research plan: data-platform review, prioritized research
      questions, feature planning, and the recommended experiment sequence —
      `docs/research/2026-07-21-research-plan.md`. First experiment: **H0002**
      (CLI settlement-revision risk — the only hypothesis runnable today, with
      a years-deep sample available via config-only observation backfill).
- [x] Extend `RESEARCH.md` with the standard experiment protocol
      (pre-registration, temporal separation with embargo, named baselines,
      clustered bootstrap CIs, multiple-comparison policy with temporal
      replication, dated conclusions).

Acceptance criteria:

- Every experiment from Phase 4 onward references a hypothesis ID (from
  `HYPOTHESES.md`) and a dataset version (from the Milestone 4 manifest).
- No hypothesis reaches "Confirmed" without satisfying its own pre-registered
  decision rule under the `RESEARCH.md` protocol.

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
