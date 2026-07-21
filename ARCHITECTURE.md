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

### Ingestion collector

Implemented in `ingestion/` (Milestone 2, `docs/adr/0002-ingestion-collector.md`). Orchestrates the Kalshi gateway and storage layer into a repeatable collection cycle: discovery (`discovery.py`) finds weather series/events/markets and snapshots each market; the cycle (`collector.py`) then fetches each discovered market's order book and incremental trades. Runs either once (`--once`, e.g. cron) or continuously with a configurable interval and graceful shutdown (`cli.py`'s `collector run`).

Responsibilities:

- category-filtered series/event/market discovery
- immutable, deduplicated market and order-book snapshots (content-hash skip on no change)
- incremental, deduplicated trade collection (`min_ts` checkpointed per market, `trade_id` as the dedup key)
- per-item validation and error isolation (`validation.py`) -- a malformed or failing market never aborts the cycle
- per-cycle failure isolation -- a DB or network outage is logged and the loop waits for the next interval rather than crashing

Explicitly out of scope here: settlement-rule parsing (Milestone 2b), weather data (Milestone 3), and anything downstream of "collect and store" (Phases 4+).

### Weather gateway

Implemented in `weather/` (Milestone 3, `docs/adr/0003-weather-data-source.md`). A `WeatherProvider` `Protocol` (`provider.py`) abstracts the data source; the only implementation so far is `NwsProvider`, confirmed live as the source Kalshi's own settlement rules cite (NWS Climatological Report / "CLI" text product). Internally it routes between two backends — `api.weather.gov` for recent data and IEM's text-product archive for historical backfill beyond the live API's ~5-day retention — but that split is invisible to callers; a future non-NWS provider implements the same `Protocol` without changing downstream code.

Responsibilities:

- station metadata (`weather/stations.py` — a small static registry, since Kalshi cites stations by name in prose, not a resolvable code)
- forecast issue time and valid time (`get_forecast`, live-only this phase — historical forecast backfill is a documented, deferred limitation, see the ADR)
- observations, with historical backfill (`get_observations`, live + IEM-backed)
- a single shared CLI-text parser (`cli_parser.py`) used by both backends
- units and conversions (Fahrenheit, as reported)
- source/version metadata and raw payload persistence, with `raw_payload_id` threaded through individual returned records rather than read from the client after a multi-request call (see the ADR — a `get_observations`/`get_forecast` call issues several requests internally)

Explicitly out of scope here: settlement-rule parsing / market-to-station mapping (Milestone 2b) and anything downstream of "collect and store" (Phases 4+).

### Weather collector

Implemented in `ingestion/weather_collector.py` (Milestone 3), mirroring the shape of the Kalshi ingestion collector above. For each registered station: refresh station metadata, collect observations (bounded initial backfill via `WEATHER_BACKFILL_DAYS`, then incremental — the most recently seen day is always re-fetched too, since a more authoritative issuance may have since been published for it), and collect the current forecast. Runs either once (`--once`) or continuously with a configurable interval and graceful shutdown (`cli.py`'s `weather collect`).

Responsibilities:

- per-station error isolation — one station's failure is logged and counted, collection continues with the next
- per-item validation (`validation.py`'s `validate_temperature_f`, plus `validate_timestamp` with a weather-specific floor) — a malformed observation or forecast is logged and skipped, not stored, and does not abort the station
- dedup via the repository layer's insert-if-not-exists by natural key (never overwrite a stored observation or forecast)

### Settlement resolver

This is a critical control. Implemented in `settlement/` (Milestone 2b, `docs/adr/0005-settlement-resolution.md`): a deterministic, versioned parser (`parser.py`) derives a typed `SettlementSpec` (`spec.py`) for every collected market, and resolver implementations (`resolver.py`) turn resolved specs into the `list[MarketMapping]` contract the dataset builder has consumed since Milestone 4 — swapping the config file for the parser required no dataset-builder changes.

Input (all from collected data; nothing re-fetched):

- the series' structured `settlement_sources` URL — the primary signal (`product=CLI`, `issuedby` = station location code, `site` = WFO office, all cross-checked against the station registry)
- complete rules text (variable and target date — the only facts that live in prose, each cross-validated: date against the event ticker's date segment, variable against the series/market title)
- market/event metadata (tickers, close time)

Output: a `SettlementSpec` with status (`resolved`/`ambiguous`/`unresolved`/`unsupported`), confidence (`high`/`medium`), canonical station, source, variable, target date, observation window, units, rounding, parser version, rules hash, and machine-readable notes for every non-resolved outcome. Specs are persisted append-only in `settlement_specs`, versioned by `(market_ticker, parser_version, rules_hash)`.

Deliberately not parsed here: thresholds/strike structure (payoff, not settlement-source identity — Kalshi exposes structured strike fields for when a later phase needs them).

The config market-map file survives as the manual override layer (audit fields: `reason`/`author`/`added_on`), taking precedence over parsed output and recorded in the resolution report and dataset manifest.

**Settlement-time labels** (`settlement/labels.py`, E-A, `docs/adr/0006-settlement-labels.md`): for every settled market, a typed versioned label distinguishing the value at market close, the value at Kalshi's exact `settlement_ts`, and the latest (possibly post-settlement-corrected) final value — validated against Kalshi's own exposed `expiration_value`/`result`. Strict as-of selection; statuses gate downstream use; exported as a `settlement_labels` dataset frame and as explicit stage columns on `market_weather`. Market experiments must use `value_at_settlement` as the outcome label.

No strategy may trade a market whose settlement mapping is unresolved or ambiguous — enforced by construction: only `resolved` specs ever become mappings.

### Research dataset

Implemented in `dataset/` (Milestone 4, `docs/adr/0004-research-dataset.md`). A **read-only** layer over the collected source tables that produces versioned, point-in-time-correct research datasets --- the join surface Phases 4+ consume. No feature/model/trading logic lives here.

Four datasets are built (pure Polars functions over frames read from the DB):

- `weather_panel` --- per (station, target_date): settled tmax/tmin (latest observation issuance), forecast summary, and forecast residuals.
- `market_weather` --- per market snapshot: forecast/observation/order-book/trade facts joined **as-of** the snapshot's timestamp (backward `join_asof`, so a later revision can never leak into an earlier row --- the concrete enforcement of `RESEARCH.md`'s look-ahead rule), plus the post-settlement outcome as a labelled target column.
- `market_prices` --- per stored candlestick (Phase 7A): OHLC trade/quote prices, volume, open interest, plus static market metadata; covers every captured market, not only mapped ones.
- `market_price_weather` --- per stored candlestick, restricted to mapped markets (Phase 7B, `docs/adr/0008-point-in-time-alignment.md`): the observation progression (latest known value, running high/low, calendar-based lock state, theoretical remaining range) knowable **as of that candle's own timestamp** --- the point-in-time join H0007's readiness assessment identified as missing, now closed.

The as-of join primitive itself lives in `dataset/asof.py` (`asof_latest`, `asof_count`) --- a reusable, independently-tested engine every point-in-time frame builds from, rather than each frame reimplementing `join_asof` calls. `dataset/observation_timeline.py` reconstructs the observation-side state (`compute_running_extremes`: cumulative max/min across same-day issuances; `attach_calendar_lock`: pure calendar arithmetic, vectorized per station timezone) that `market_price_weather` joins against.

Responsibilities:

- point-in-time (as-of) joins with no look-ahead leakage; forecast local `target_date` derived in the station timezone
- the market->settlement association via an explicit versioned config map (`dataset/market_map.py`) --- a stand-in for `settlement_specs` until Milestone 2b; unmapped markets are reported as orphans, not guessed
- a reproducibility manifest per build (git commit, source DB Alembic revision, credential-free DB URL, per-frame content hashes, generation config) and immutable versioned Parquet export under the configurable `DATASET_ROOT`
- validation (missing/duplicate/impossible/mismatch/orphan checks) and summary statistics emitted alongside each build

Explicitly out of scope here: settlement-rule parsing (Milestone 2b) and anything downstream of "join and version" (Phases 4+) --- `market_price_weather` deliberately stops at attaching known facts; no forecasting feature, model input, or statistical estimate lives in this layer.

### Research operations

Implemented in `ops/` and `ingestion/backfill.py` (Phase 6, `docs/runbooks/operations.md`). Makes the platform run unattended for months:

- **Combined runner** (`ops run`): both collectors concurrently in one supervised process; graceful shutdown; restart recovery is inherent because every checkpoint is derived from stored data (trade `min_ts`, latest observation date, content-hash dedup), never from process state.
- **Operational metrics**: every cycle appends a `collector_runs` row (duration, success, request/retry counts, full stats) — written best-effort so a metrics failure can never break collection. Client and provider carry request/retry counters; request pacing is env-configurable for both APIs.
- **Data-quality monitoring** (`ops quality`): store-level checks — schema-revision drift, natural-key duplicates, future timestamps (errors); observation gaps, forecast/market staleness, settlement-resolution failures (warnings) — as a machine-readable report, exit-code gated for cron.
- **Health report** (`ops health`): collector liveness/staleness, data freshness, DB size, station/settlement coverage, completeness, quality summary.
- **Chunked resumable backfill** (`weather backfill`): per-chunk commits, partial-failure isolation with explicit failed-chunk reporting, covered-chunk skipping for cheap resume.
- **Research snapshots** (`ops snapshot`): the Milestone 4 versioned dataset export plus `quality.json` and `ops.json` sidecars — a daily, immutable, fully self-describing research artifact.

### Price ingestion

Implemented in `ingestion/price_backfill.py` and `ops/price_coverage.py`
(Phase 7A, `docs/adr/0007-price-ingestion.md`). Recovers and preserves
historical Kalshi candlestick data (1-minute OHLC trade prices, OHLC bid/ask
quotes, volume, open interest) for every settled weather market, independent
of the live snapshot collector — whose intraday depth is effectively zero,
since it has only run in short bursts to date.

- **Backfill** (`prices backfill`): resumable, oldest-`close_time`-first over
  every discoverable settled market; one market per commit; six per-attempt
  outcomes (`complete`/`partial`/`no_price_data`/`expired`/`api_failure`/
  `unsupported`) — a market is never silently dropped. `--skip-covered`
  (default) makes reruns free once complete.
- **Continuous sync**: a third task in the existing `ops run` supervised
  loop (`run_price_sync_loop`), recorded under `collector="prices"` — no new
  scheduler, reusing the same restart-recovery-via-stored-data pattern as the
  other two collectors.
- **Coverage and retention monitoring** (`prices coverage`, and a
  `price_retention` block in `ops health`): measured (never estimated)
  coverage by market/event-date/variable, missing-interval detection within a
  market's own observed span, and age-inferred (never assumed) retention-loss
  detection against Kalshi's rolling ~67-day discoverability window — an
  empirical finding from this phase, stored as a configurable setting rather
  than a hardcoded guarantee.
- `market_candlesticks` is additive (migration `0007`), unique on
  `(market_ticker, period_interval_seconds, period_end)` so reruns can never
  overwrite; `data_quality_status` on the derived dataset frame distinguishes
  a quote-only zero-volume period from an executed trade.

Always targets Kalshi's production environment regardless of
`settings.kalshi_env` — every weather market this project has ever collected
came from production's public category, unauthenticated (see the ADR).
Explicitly out of scope here: individual trade-level ticks, and anything
downstream of "collect and preserve" (feature engineering, models, strategy).

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
