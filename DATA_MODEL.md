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
- `raw_payload_id`
- `observed_at`

### `events`

- `event_ticker` PK
- `series_ticker`
- `title`
- `status`
- `open_time`
- `close_time`
- `settlement_time`
- `raw_payload_id`
- `observed_at`

### `markets`

- `market_ticker` PK
- `event_ticker`
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
- `raw_payload_id`
- `observed_at`

Use a separate history table or temporal model rather than overwriting market snapshots.

### `orderbook_snapshots`

- `id`
- `market_ticker`
- `sequence`
- `captured_at`
- `yes_levels_json`
- `no_levels_json`
- `best_yes_bid_cents`
- `best_yes_ask_cents`
- `spread_cents`
- `raw_payload_id`

Important binary relation:

```text
best YES ask = 100 - best NO bid
best NO ask  = 100 - best YES bid
```

Handle empty sides explicitly.

### `trades`

- `trade_id`
- `market_ticker`
- `executed_at`
- `price_cents`
- `count`
- `taker_side`
- `raw_payload_id`

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
