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
- `schema_version`
- `content_hash` — hash of the fields above (excluding id/observed_at/raw_payload_id); a new snapshot identical to the immediately-prior row for this ticker is skipped rather than inserted (see `docs/adr/0002-ingestion-collector.md`)
- `raw_payload_id`
- `observed_at`

Append-only: a market's history is the full sequence of rows for its ticker, never overwritten. "Never overwritten" and "duplicate detection" are reconciled by content-hash-based skip-on-no-change, not by allowing every poll to insert a redundant identical row.

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

- `id`
- `market_ticker`
- `station_id`
- `source_name`
- `weather_variable`
- `event_start`
- `event_end`
- `timezone`
- `lower_bound`
- `upper_bound`
- `lower_inclusive`
- `upper_inclusive`
- `unit`
- `rounding_rule`
- `parser_version`
- `resolution_status`
- `resolution_notes`
- `created_at`

### `weather_forecasts`

- `id`
- `station_id`
- `provider`
- `model_name`
- `issue_time`
- `valid_start`
- `valid_end`
- `horizon_hours`
- `variable`
- `point_estimate`
- `distribution_json`
- `unit`
- `raw_payload_id`

Never overwrite a forecast with a later forecast; issue time is essential for preventing look-ahead bias.

### `weather_observations`

- `id`
- `station_id`
- `observed_at`
- `variable`
- `value`
- `unit`
- `quality_flag`
- `raw_payload_id`

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
