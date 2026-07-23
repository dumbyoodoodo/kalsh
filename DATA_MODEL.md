# Data Model

All timestamps are timezone-aware UTC. Monetary values use integer cents or exact decimal types.

## Core tables

### `raw_api_payloads`

- `id`
- `source` (`kalshi_rest`, `kalshi_ws`, `weather`)
- `endpoint_or_channel`
- `request_key`
- `received_at`
- `http_status`
- `content_hash`
- `payload_json`
- `schema_version`

Append-only. Deduplicate by source/request/content hash where appropriate.

### `series`

- `series_ticker` PK
- `category`
- `title`
- `frequency`
- `settlement_source`
- `source_updated_at` — Kalshi's own `last_updated_ts` for this row, distinct from `observed_at`
- `schema_version`
- `raw_payload_id`
- `observed_at`

Upserted by `series_ticker`, not append-only (a series is a stable identity, not a point-in-time observation).

### `events`

- `event_ticker` PK
- `series_ticker`
- `category` — present on live `/events` responses
- `title`
- `sub_title` — present on live `/events` responses
- `status` — part of the original design; not present on live `/events` responses as of Milestone 2 (kept nullable for forward compatibility, e.g. once Milestone 2b settlement mapping needs it)
- `open_time`, `close_time`, `settlement_time` — same status as above: nullable, not currently populated from source
- `source_updated_at`
- `schema_version`
- `raw_payload_id`
- `observed_at`

Upserted by `event_ticker`, same rationale as `series`.

### `market_snapshots`

(Implemented as `market_snapshots` — the table name in code differs from the original `markets` sketch above to make its point-in-time, append-only nature explicit rather than implying one row per market.)

- `id` PK (surrogate; `market_ticker` is not unique)
- `market_ticker`
- `event_ticker` — not a foreign key; markets are commonly discovered before their parent event is separately persisted
- `market_type`
- `title`
- `subtitle`
- `status`
- `yes_bid_cents`
- `yes_ask_cents`
- `last_price_cents`
- `volume`
- `open_interest`
- `close_time`
- `rules_primary`
- `rules_secondary`
- `source_updated_at` — Kalshi's own `updated_time` for this market
- `result`, `expiration_value`, `settlement_ts`, `floor_strike`, `cap_strike`, `strike_type` — settlement fields (migration `0006`, `docs/adr/0006-settlement-labels.md`): the payout side, the underlying value Kalshi paid on, the exact determination time, and the structured strike definition; populated once a market settles
- `expiration_time` — the venue's own finality marker (migration `0008`, `docs/adr/0011-settled-metadata-revision.md`), observed ~7 days after `close_time`. NULL on every row written before `0008`
- `schema_version`
- `content_hash` — hash of the fields above plus `result`/`expiration_value` (so a settlement transition appends one final snapshot); excludes id/observed_at/raw_payload_id and the immutable strike fields (see `docs/adr/0002-ingestion-collector.md`)
- `raw_payload_id`
- `observed_at`

Append-only: a market's history is the full sequence of rows for its ticker, never overwritten. "Never overwritten" and "duplicate detection" are reconciled by content-hash-based skip-on-no-change, not by allowing every poll to insert a redundant identical row.

#### Result-bearing is not metadata-final

**A snapshot carrying `result` is not necessarily final.** Kalshi keeps revising `volume`, `open_interest`, and occasionally `result` itself *after* publishing `status="finalized"` with a `settlement_ts`, up to the market's own `expiration_time` (~7 days after `close_time`). Measured on 2026-07-23: three hours after close, one event reported `volume=0` for ten of eleven markets that had each traded 104–1,165 contracts, and one market's `result` contradicted its own strike arithmetic before the venue corrected it (`docs/adr/0011-settled-metadata-revision.md`).

Consequences for analysis:

- `volume`, `open_interest`, and `result` are **venue-mutable until `expiration_time`**. Treat a snapshot observed before that point as provisional metadata.
- For normal analysis, **the latest snapshot for a ticker after its finality time is authoritative.** A `market_metadata_verifications` row with a terminal outcome confirms that point was reached and re-checked.
- Earlier snapshots remain **valid provenance and genuine revision-history observations** — they record what the venue published at that moment, which is itself research signal (compare the settlement-label revision work in H0011/H0012). They are never rewritten or deleted.
- Anything derived from provisional volume is provisional too — notably `data_quality_status = "zero_volume"` in the dataset builder, which was systematically wrong for same-day settlements before this capture existed.
- **Frozen datasets are unchanged** by this mechanism. A dataset rebuilt after revisions have been captured will contain additional snapshot rows; that is a deliberate rebuild, never a silent mutation.

### `market_metadata_verifications`

Append-only record of post-finality re-checks (migration `0008`).

- `id` PK
- `market_ticker`
- `verified_at` — when the check ran
- `finality_at` — the finality time it was gated on (the market's `expiration_time`, or the documented `close_time + 7d` fallback for pre-`0008` rows), so an audit can tell which rule admitted it
- `outcome` — `unchanged` | `changed` | `market_removed` | `retention_expired`
- `snapshot_appended` — whether this check appended a `market_snapshots` row
- `changed_fields` — which venue-mutable fields moved
- `raw_payload_id`, `schema_version`

This table exists because a verification that finds *nothing changed* correctly appends no snapshot, so "verified unchanged" would otherwise be indistinguishable from "never checked" and the market would be re-fetched forever. Keeping it separate leaves `market_snapshots` meaning exactly one thing — observed market state — rather than overloading it with "we looked".

### `orderbook_snapshots`

- `id`
- `market_ticker`
- `captured_at`
- `yes_levels_json`
- `no_levels_json`
- `best_yes_bid_cents`
- `best_yes_ask_cents`
- `best_no_bid_cents`
- `best_no_ask_cents`
- `spread_cents`
- `schema_version`
- `content_hash` — hash of `yes_levels_json`/`no_levels_json`; a book identical to the immediately-prior snapshot for this ticker is skipped rather than inserted, same rationale as `market_snapshots`
- `raw_payload_id`

No `sequence` column: Kalshi's REST order-book endpoint (unlike its WebSocket `orderbook_delta` channel, not implemented as of Milestone 2 — see `docs/API_VERIFICATION.md`) does not provide one; ordering is by `captured_at`/`id`.

Important binary relation:

```text
best YES ask = 100 - best NO bid
best NO ask  = 100 - best YES bid
```

Handle empty sides explicitly.

### `trades`

- `trade_id` PK — Kalshi's own id; used directly for duplicate detection (a repeat fetch of an already-stored trade is silently skipped, not re-inserted or erred on)
- `market_ticker`
- `executed_at` — preserved exactly as received from Kalshi's `created_time`, never rounded or rewritten
- `price_cents`
- `count`
- `taker_side`
- `schema_version`
- `raw_payload_id`

Kalshi's live wire format represents price/count as decimal-dollar-string (`yes_price_dollars`/`no_price_dollars`) and fixed-point-string (`count_fp`) fields rather than the plain integers above; these are normalized at parse time (see `kalshi/models.py`). A genuinely fractional `count_fp` (observed once during Milestone 1 API verification) rounds to the nearest whole contract — a known, documented limitation, not silent data loss.

### `settlement_specs`

(Implemented in Milestone 2b, `docs/adr/0005-settlement-resolution.md`; the
implemented schema differs from the original sketch — thresholds/inclusivity
are deliberately out of scope, being payoff structure rather than
settlement-source identification, and Kalshi exposes structured strike fields
for when a later phase needs them.)

- `id` PK (surrogate)
- `market_ticker`, `series_ticker`, `event_ticker`
- `status` — `resolved` | `ambiguous` | `unresolved` | `unsupported`; only `resolved` rows ever become dataset mappings
- `confidence` — `high` (every cross-check agreed) | `medium` (a secondary signal was missing, noted) | `none`
- `city`
- `station_id` — deliberately **not** a foreign key: an `unsupported` spec legitimately references a station absent from `weather_stations` (that absence is the finding)
- `variable` — `tmax_f` / `tmin_f`
- `target_date`
- `settlement_source` — e.g. `"NWS Climatological Report (Daily)"`
- `source_url`, `wfo_site`, `source_location_code` — the structured Kalshi settlement citation preserved verbatim (`site=`/`issuedby=` query params)
- `unit` (`F`), `observation_window` (`local_calendar_day`), `rounding_rule` (`integer_f`)
- `market_close_time`
- `notes_json` — machine-readable reasons for every non-resolved outcome
- `parser_version`, `rules_hash` — reproducibility: which parser saw which exact inputs
- `schema_version`
- `observed_at`

Append-only and versioned: unique on `(market_ticker, parser_version, rules_hash)`. Re-parsing unchanged rules with an unchanged parser is a detectable no-op; a parser upgrade or rules change appends a new row beside the old — resolution history is never rewritten. The latest row per market (max `id`) is the current resolution.

### `market_candlesticks`

(Implemented in Phase 7A, `docs/adr/0007-price-ingestion.md`. Not in the original sketch — added once historical price preservation had a concrete consumer, H0007.)

- `id` PK (surrogate)
- `market_ticker`, `series_ticker`
- `period_interval_seconds` — candle width in seconds (`60` for the 1-minute resolution collected so far; Kalshi also confirmed `3600`/`86400` live)
- `period_start`, `period_end`
- `price_open_cents`, `price_high_cents`, `price_low_cents`, `price_close_cents`, `price_mean_cents` — traded price OHLC
- `price_close_is_carried_forward` — true when the period had no trade and `price_close_cents` was filled from the API's own carry-forward value rather than an executed close; an explicit stored fact, never silent inference
- `yes_bid_open_cents`, `yes_bid_high_cents`, `yes_bid_low_cents`, `yes_bid_close_cents` — quoted bid OHLC (persists through zero-volume periods)
- `yes_ask_open_cents`, `yes_ask_high_cents`, `yes_ask_low_cents`, `yes_ask_close_cents` — quoted ask OHLC
- `volume`, `open_interest`
- `schema_version`, `raw_payload_id`, `observed_at`

Append-only and idempotent: unique on `(market_ticker, period_interval_seconds, period_end)` — a repeat fetch of an already-stored candle is silently skipped, never overwritten. If Kalshi ever revises a historical candle, the current policy is append-and-flag (a future migration), not overwrite; no revision has been observed to date.

### `collector_runs`

(Implemented in Milestone 4b, research operations — `docs/runbooks/operations.md`. Not in the original sketch: added when operational metrics gained a historical store.)

- `id` PK (surrogate)
- `collector` — `"kalshi"` | `"weather"` | `"prices"` (Phase 7A)
- `started_at`, `finished_at`, `duration_seconds`
- `success`
- `requests_attempted`, `retries` — from the client/provider instance counters for that cycle
- `stats_json` — the full cycle stats (`markets_discovered`, `*_saved`, `*_duplicate`, `errors`, ...)
- `error` — `"ExceptionType: message"` for failed cycles
- `schema_version`

Append-only: one row per collection cycle, never updated. Written **best-effort** by the collector loops — a metrics write failing must never break collection itself. `ops health` derives collector liveness/staleness from the newest row per collector.

### `weather_stations`

(Implemented in Milestone 3, `docs/adr/0003-weather-data-source.md`. Not in the original sketch above — added because station identity/metadata needed its own table once a real provider existed.)

- `station_id` PK — our internal id, matching `weather/stations.py`'s static registry (e.g. `"NYC"`)
- `provider`
- `source_location_code` — the provider's own code for this station (e.g. NWS CLI product location code, currently identical to `station_id`)
- `office` — NWS WFO office code (e.g. `"OKX"`), confirmed live via `/points`
- `latitude`, `longitude`
- `name`
- `timezone` — IANA name, used to interpret provider-local timestamps
- `schema_version`
- `raw_payload_id`
- `observed_at`

Upserted by `station_id`, same rationale as `series`/`events` — station identity is a stable entity, not a point-in-time observation.

### `weather_forecasts`

(Implemented as sketched, with a few additions below.)

- `id` PK (surrogate)
- `station_id` — references `weather_stations.station_id`
- `provider`
- `variable` — `"temperature"` to start
- `point_estimate`
- `unit`
- `issue_time` — when this forecast was issued (NWS `updateTime`)
- `valid_start`, `valid_end` — the period this point estimate covers
- `schema_version`
- `raw_payload_id` — set per-record at creation time, not read from a single "last request" attribute after the call that produced it (`get_forecast` makes several requests internally; see the ADR)
- `observed_at`

Not implemented from the original sketch: `model_name`, `horizon_hours`, `distribution_json` — no consumer needs them yet; add when a model actually requires them, per `CLAUDE.md`'s storage philosophy. Deduplicated/never-overwritten by `(station_id, variable, issue_time, valid_start)` — a later forecast issuance for the same validity window is stored as a new row, never merged into or replacing an earlier one; issue time is essential for preventing look-ahead bias. Historical forecast backfill (pre-dating whenever the collector first ran for a station) is not implemented this phase — a documented limitation, not a silent gap (see the ADR).

### `weather_observations`

(Implemented as sketched, with a few additions below.)

- `id` PK (surrogate)
- `station_id` — references `weather_stations.station_id`
- `provider`
- `variable` — `"tmax_f"` / `"tmin_f"` to start
- `value`
- `unit`
- `observation_date` — the calendar day this value describes, read from the CLI report's own header line, not inferred from issuance time (an early-morning issuance can report on the previous day; see `weather/cli_parser.py` and the ADR)
- `issuance_time` — when *this version* of the report was issued; CLI reports are reissued multiple times per day, and this is what dedup/recency is keyed on, not `observation_date`
- `source_product_id` — the provider's id for the specific report this value came from
- `schema_version`
- `raw_payload_id` — same per-record capture rationale as `weather_forecasts`
- `observed_at`

Not implemented from the original sketch: `quality_flag` — no consumer needs it yet. Deduplicated/never-overwritten by `(station_id, variable, issuance_time)` — every report reissuance is a distinct, valuable row (a later, more authoritative issuance for the same date is not a duplicate of an earlier provisional one). Historical backfill beyond `api.weather.gov`'s live retention window is implemented via IEM's text-product archive (see the ADR); observations, unlike forecasts, do get a historical-backfill path.

### `model_predictions`

- `id`
- `market_ticker`
- `prediction_time`
- `model_name`
- `model_version`
- `probability`
- `probability_low`
- `probability_high`
- `feature_snapshot_id`
- `diagnostics_json`

### `signals`

- `id`
- `market_ticker`
- `generated_at`
- `side`
- `market_price_cents`
- `fair_probability`
- `gross_edge`
- `estimated_fee`
- `estimated_slippage`
- `uncertainty_buffer`
- `net_edge`
- `proposed_size`
- `decision`
- `reason_codes`
- `strategy_version`

### `orders`, `fills`, `positions`, `daily_risk`

Store broker environment, client order ID, exchange order ID, status transitions, quantities, exact prices, fees, and timestamps.

## Data quality checks

- market ticker uniqueness
- chronological forecast issue/valid times
- prices in valid cent range
- probabilities in `[0,1]`
- order-book complement identities
- station and units resolved
- no prediction uses data received after prediction time
- no duplicate trade/fill ingestion

Implemented as of Milestone 2 (`ingestion/validation.py`,
`storage/repositories.py`; see `docs/adr/0002-ingestion-collector.md`):

- source timestamps must be timezone-aware, not absurdly old (before 2018),
  and not further than 5 minutes in the future (clock-skew tolerance)
- market-level prices validated to `[0, 100]` cents; a bad ticker string or
  out-of-range price is logged and the item skipped, not persisted
- market/order-book snapshots identical to the immediately-prior stored row
  for that ticker are skipped (content-hash comparison), not re-inserted
- trades are deduplicated by Kalshi's own `trade_id`, skipped rather than
  overwritten if already stored

## Research datasets (derived, Milestone 4)

These are **not database tables**. They are versioned Parquet files produced by
a read-only join over the tables above (`dataset/`, `docs/adr/0004-research-dataset.md`),
written under `DATASET_ROOT/<version>/` alongside a `manifest.json` (git commit,
source DB Alembic revision, credential-free DB URL, per-frame content hashes,
generation config), a `validation.json`, and a `stats.json`. A published version
is immutable; a change in join logic or source data is a new version.

### `weather_panel.parquet`

Grain: one row per `(station_id, target_date)`.

- `station_id`, `target_date`
- `n_forecast_issuances`, `first_forecast_issue_time`, `last_forecast_issue_time`
- `final_forecast_high_f`, `final_forecast_low_f` — from the latest forecast issuance for the day (max/min period point-estimate touching that local date; an approximation, see the ADR)
- `settled_tmax_f`, `settled_tmin_f` — the value from the **latest** observation issuance for the day (later CLI reports supersede earlier provisional ones)
- `settlement_issuance_time`, `n_observation_issuances`
- `residual_high_f` = `final_forecast_high_f - settled_tmax_f`, `residual_low_f` = `final_forecast_low_f - settled_tmin_f` — **label-side** quantities (use the realized value); never use as model features (`RESEARCH.md` feature-leakage rule)

### `settlement_labels.parquet`

Grain: one row per market (E-A, `docs/adr/0006-settlement-labels.md`). The canonical settlement timeline: `value_at_close` (as-of market close — usually the same-day preliminary, since close precedes the final morning report), `value_at_settlement` (as-of Kalshi's `settlement_ts`; the **only valid outcome label for market experiments**), `latest_final_value` (may include post-settlement corrections), Kalshi's own `kalshi_result`/`kalshi_expiration_value`, payout-agreement flags, `settlement_label_status` (`resolved`/`bounded`/`ambiguous`/`unsupported`/`missing_source_data`), `settlement_label_version`, and per-label notes. Derived deterministically; never allows a future issuance into an earlier stage.

### `market_weather.parquet`

Grain: one row per market snapshot (only for markets present in the settlement
mapping). All weather/order-book/trade columns are joined **as-of** the
snapshot's `observed_at` — the latest source row with knowledge timestamp
`<= observed_at`, so nothing from the future leaks in.

- market snapshot fields (`market_ticker`, `event_ticker`, `status`, `yes_bid_cents`, `yes_ask_cents`, `last_price_cents`, `volume`, `open_interest`, `rules_primary`, `observed_at`, `raw_payload_id`)
- settlement target (`station_id`, `variable`, `target_date`) — from the market map
- as-of forecast (`forecast_high_f`, `forecast_low_f`, `forecast_issue_time`, `forecast_age_seconds`, `n_forecast_issuances_known`)
- as-of known observation of the market's variable (`obs_value_known`, `obs_issuance_time_known`)
- as-of order book (`best_yes_bid_cents`, `best_yes_ask_cents`, `spread_cents`) and trades (`last_trade_price_cents`, `n_trades_known`)
- `settled_value`, `settlement_issuance_time` — latest-final semantics (**unchanged since Milestone 4**; may include post-settlement corrections)
- `value_at_close`, `value_at_settlement`, `latest_final_value`, `settlement_label_status`, `settlement_label_version` — the stage-distinguished settlement labels (E-A); **market experiments must use `value_at_settlement`**, not `settled_value`

Any row traces to raw sources via the source tables' `raw_payload_id` columns;
the dataset is rebuildable deterministically from the manifest's git commit,
source DB revision, market map, and generation config.

### `market_prices.parquet`

(Implemented in Phase 7A, `docs/adr/0007-price-ingestion.md`.) Grain: one row
per stored candle (`market_candlesticks`), covering **every captured market**
— unlike `market_weather`, not filtered to only markets present in the
settlement map; settlement-derived columns are simply `null` for a market
whose spec doesn't resolve, matching how `settlement_labels` already
represents unsupported markets.

- candle fields (`market_ticker`, `period_interval_seconds`, `period_start`, `period_end`, price/quote OHLC, `price_close_is_carried_forward`, `volume`, `open_interest`, `raw_payload_id`)
- market metadata reduced to one row per ticker (`event_ticker`, `close_time`, `floor_strike`, `cap_strike`, `strike_type`) — settlement fields only populate on a market's later snapshots once it settles, so the reduction picks the latest non-null value per field rather than the latest row wholesale
- `data_quality_status` (`"ok"` / `"zero_volume"`) — computed from `volume == 0`, so a quote-only period can never be mistaken for an executed trade downstream
- the same settlement-label columns as `market_weather` (`station_id`, `variable`, `target_date`, `settlement_time`, `value_at_close`, `value_at_settlement`, `latest_final_value`, `kalshi_result`, `kalshi_expiration_value`, `settlement_label_status`), joined per market — **market experiments must use `value_at_settlement`**, same rule as `market_weather`

`MARKET_PRICES_SCHEMA_VERSION` is recorded in every manifest alongside
`RECONSTRUCTION_VERSION`, gating comparability across rebuilds the same way.

### `market_price_weather.parquet`

(Implemented in Phase 7B, `docs/adr/0008-point-in-time-alignment.md`.) Grain:
one row per stored candle, same as `market_prices`, but restricted to
markets present in the settlement map (`dataset/market_map.py`) — same
convention as `market_weather`, since weather progression is meaningless for
a market with no known station/variable/target_date.

- every `market_prices` column (candle OHLC/quotes, `data_quality_status`, market metadata, strike)
- `obs_value_known`, `obs_issuance_time_known`, `obs_age_seconds` — the latest known value for the market's OWN settlement variable as of this candle, and how old it is (same as-of semantics `market_weather` already defines, joined here against the full historical candle archive rather than the ~zero-depth live snapshot history)
- `running_tmax_f_known`, `running_tmax_issuance_time_known` / `running_tmin_f_known`, `running_tmin_issuance_time_known` — the cumulative running high/low known so far for the market's target date (attached regardless of which single variable the market itself settles on)
- `tmax_locked`, `tmin_locked` — whether that variable's observation window (a station-local calendar day) has fully elapsed as of this candle; pure calendar arithmetic, independent of whether the confirming issuance has actually arrived
- `theoretical_remaining_range_high_f`, `theoretical_remaining_range_low_f` — `0.0` once locked (no further data can move the running value), else `null` ("unbounded" — never a modeled or forecast estimate)
- `observation_quality_status` (`"known"` / `"unknown"`) — whether the market's own variable has any as-of observation yet
- the same settlement-label columns as `market_weather` (`value_at_close`, `value_at_settlement`, `latest_final_value`, `settlement_label_status`, `settlement_label_version`) — **market experiments must use `value_at_settlement`**, same rule as every other frame

`MARKET_PRICE_WEATHER_SCHEMA_VERSION` is recorded in every manifest. Leakage
is enforced structurally by `dataset/asof.py`'s backward-only join (a future
row cannot be selected) and verified explicitly — see the ADR's "Leakage
validation" section, including a 0-violation check over the full 761,475-row
production archive.
