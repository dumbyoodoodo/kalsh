# ADR 0002: Kalshi historical market-data collector

## Status

Accepted.

## Context

`TASKS.md` Phase 2 / Milestone 2 calls for a robust, immutable historical Kalshi market-data ingestion system: market discovery, periodic snapshots (metadata, order books), trade collection, and validation, designed to run continuously for months. Weather-data ingestion (Milestone 3) and settlement-rule parsing (moved to Milestone 2b, see below) are explicitly out of scope for this work.

## Decisions

1. **"Never overwrite history" + "detect duplicate snapshots" are reconciled by content-hash skip-on-no-change, not blind insert.** A poller that inserts a new row every cycle regardless of whether anything changed would technically be "append-only," but would not be "detecting duplicates" in any useful sense, and would bloat storage indefinitely for inactive markets. `save_market_snapshot`/`save_orderbook_snapshot` (`storage/repositories.py`) compute a content hash of the fields that matter and compare against the immediately-prior row for that ticker; an identical snapshot is skipped (the prior row remains, untouched) rather than inserted. Nothing is ever overwritten either way — the difference is purely whether a redundant row gets appended.

2. **Trade collection is incremental via `min_ts`, not a full re-fetch every cycle.** `list_trades()` gained an optional `min_ts` parameter; the collector looks up the latest stored trade timestamp per ticker (`get_latest_trade_timestamp`) and only requests newer trades. Without this, a months-long-running collector would re-fetch a market's entire trade history on every single poll — a correctness/practicality problem for "run continuously for months," not a premature performance optimization. Trades are additionally deduplicated by Kalshi's own `trade_id` (a `save_trade` call for an already-stored id is a no-op skip), so an overlapping `min_ts` window can't produce duplicate rows even without the incremental fetch.

3. **Market snapshots are populated directly from `list_markets()`, not a separate `get_market()` call per ticker.** Kalshi's markets-list response already includes bid/ask/volume/open-interest — the same data a per-ticker detail call would return — so discovery persists a snapshot for every market it finds in one pass, and only order books and trades need a dedicated per-ticker request. Fewer requests, same data, no extra code path.

4. **Per-item and per-cycle failure isolation.** A malformed series/event/market (bad ticker, out-of-range price, unparseable timestamp — `ingestion/validation.py`) is logged and skipped; it does not abort discovery. A failing order-book or trade fetch for one market is caught and logged individually; collection continues with the next market. A whole-cycle failure (DB unreachable, Kalshi unreachable) is caught at the top of the loop in `run_collector_loop` and logged; the process waits for the next interval rather than exiting. This is what "run continuously for months without losing historical information" requires in practice — a collector that crashes on the first transient failure defeats the purpose regardless of how correct its happy path is.

5. **`raw_payload_id` is now actually wired up.** Every normalized-record save call previously hardcoded `raw_payload_id=None` (flagged as a known gap in `docs/API_VERIFICATION.md`). `KalshiClient` now exposes `last_raw_payload_id`, set immediately after each request's raw-payload sink runs; callers read it right after each `await client.xxx(...)` call. This is a stateful, single-attribute approach rather than threading a `(payload, raw_payload_id)` tuple through every client method's return type — a much smaller change, safe because a `KalshiClient` instance's requests within one collection cycle are sequential `await`s, never concurrent/overlapping on the same instance.

6. **`EventRecord` gained `category`/`sub_title` and dropped the assumption that `open_time`/`close_time`/`settlement_time`/`status` are populated from source.** Confirmed live (`docs/API_VERIFICATION.md`) that Kalshi's `/events` responses carry `category`/`sub_title` but not the other four fields as of this writing. Those columns are kept (nullable) rather than removed, since Milestone 2b (settlement mapping) may need them once it's built — removing and re-adding later would be more churn than leaving unused nullable columns now.

7. **Migrations are additive from here on.** Migration 0001 was committed and pushed to GitHub in Milestone 1's session; unlike that session (where editing 0001 in place was justified because nothing had ever shipped), this phase adds a new `0002_ingestion.py` rather than modifying 0001. This is the practice going forward: once a migration has shipped, it's history, not a draft.

8. **Milestone 2 split into 2 and 2b.** The user's Phase 2 request (market discovery, snapshot/trade collection, validation, docs) is narrower than `TASKS.md`'s original "Milestone 2: Weather-market discovery and settlement mapping," which bundled discovery together with settlement-rule parsing. Rather than force-fitting the old milestone definition or renumbering everything downstream, Milestone 2 was renamed to just the collector work (now complete) and the settlement-parsing items moved to a new Milestone 2b (still pending) — matching the existing "5a" naming precedent already in `TASKS.md`.

## Consequences

- Anyone adding a new normalized table to the collector should follow the same pattern: `schema_version`, a `source_updated_at` where Kalshi provides one, and content-hash dedup if the table represents a repeated poll of the same entity (as opposed to a genuinely one-per-event row like `trades`).
- `KalshiClient.last_raw_payload_id` must be read immediately after the corresponding `await` — a future refactor that introduces concurrent requests on a shared client instance would break this assumption and needs to revisit decision 5.
- Fractional trade/order-book quantities (`count_fp` observed as `"0.91"` once during Milestone 1 verification) round to the nearest whole contract; this is a known, documented limitation (`docs/runbooks/collector.md`), not silent data loss — flagged for follow-up if it's shown to matter once real trading volume is analyzed.
