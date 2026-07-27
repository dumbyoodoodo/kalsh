# ADR 0017: Historical production market-data replay adapter

## Status

Accepted (2026-07-26). Extends the execution simulator (ADR 0015) and fee model
(ADR 0016). Read-only; orthogonal to and isolated from H0019.

## Context

The simulator could replay synthetic fixtures but not the archived production
market data already stored in PostgreSQL. We need a deterministic, read-only
adapter that turns explicitly-selected historical rows into replay events,
preserving point-in-time semantics, provenance, ordering, and missingness -- and
that never writes to the archive, submits orders, or consumes H0019.

## Source-schema audit (Phase 1, traced through collector + persistence code)

- **orderbook_snapshots**: full-depth independent snapshots (`get_orderbook` is
  called with no depth limit), deduped when unchanged from the immediately-prior
  book for a ticker. `captured_at = utc_now()` at save -- the collector clock at
  fetch, and the ONLY timestamp available for a book (Kalshi's orderbook response
  has no server time). Levels are resting bids per side: `yes_levels_json` = YES
  bids, `no_levels_json` = NO bids; a NO bid at p is a YES ask at `100 - p`
  (kalshi/orderbook.py). `content_hash` dedups; `environment` is provenance.
- **trades**: PK `trade_id` (Kalshi's immutable id); `executed_at` exchange time;
  `count` quantity; `taker_side` (yes/no) reliable when present; `environment`.
  No per-row ingestion time -- `raw_payload_id -> raw_api_payloads.received_at`.
- **market_snapshots**: `status` (active/closed/determined/finalized), `result`
  in {yes,no} (empty string = unsettled), `settlement_ts` determination time,
  `expiration_time` venue finality, `observed_at` ingestion; `environment`.
- **environment** (demo|production|unknown|NULL-pre-provenance) is stamped from
  the client base URL, never guessed (ADR 0013/0014).

## Decision

`execution/history.py` (loaders) + `execution/history_replay.py` (export/import
+ run). Key choices:

1. **Bounded, read-only.** `HistoryQuery` requires an explicit time range and
   ticker list (no implicit all-history). Queries filter by both in SQL; the CLI
   opens the connection with `default_transaction_read_only=on` so a write is
   impossible at the DB level.
2. **Environment policy** (Phase 2): production only by default; demo rejected;
   NULL/unknown rejected unless a data type is explicitly allowed. Missing rows
   are missing, never zero. Exclusions are counted.
3. **Book normalization**: canonical YES-referenced `OrderBook` -- `yes_bids` =
   yes_levels, `yes_asks` = {(100 - p, q) for no_levels}. No depth is invented.
4. **Timestamp modes** (Phase 4, recorded in the manifest):
   - *exchange-time*: idealized public history -- books by `captured_at`, trades
     by `executed_at`, settlement by `settlement_ts` (then `expiration_time`).
   - *collector-available*: what the archive knew in real time -- settlement
     becomes visible at the finalizing snapshot's `observed_at` (ingestion), and
     a trade lacking ingestion provenance is excluded. Books coincide across
     modes because `captured_at` is the only (and ingestion-equal) book time.
5. **Dedup/conflict**: exact-duplicate books/trades dropped; two books at the
   same `(ticker, captured_at)` with different content are an irreconcilable
   conflict -- both rejected and reported. Trades dedup on `trade_id`.
6. **Ordering** (Phase 6): one deterministic engine. Event stream ranks
   market-status (1) < order intent (3) < settlement (4); books/trades are
   looked up per intent (latest book <= submission; only strictly-subsequent
   trades) so nothing future-dated affects an earlier fill. Every tie has a
   stable secondary key.
7. **Market-state windows**: when a ticker has status events, an order outside
   its OPEN window is rejected `market_not_open`. Without status data the window
   is unknown (not enforced) -- documented, not silently assumed open.
8. **Immutable export** (Phase 12): Parquet event tables + JSON manifests, with
   content hashes over canonical (sorted) JSON so determinism is independent of
   Parquet byte layout; refuses to overwrite.
9. **Fills**: the existing conservative models -- marketable consumes only
   captured visible depth at/through the limit (partial allowed), respects
   `max_book_age`, never falls back to candle prices; passive uses only
   subsequent qualifying volume, never a price touch alone.

## Consequences

- Historical intervals replay deterministically with explicit provenance and a
  data-quality report; weak coverage is surfaced, never hidden.
- Historical fills do NOT reconstruct real queue position; passive fills are a
  conservative proxy. This is execution-engine validation, not strategy
  backtesting, and no order is ever submitted.
- Follow-ups: trade-direction-aware fills, per-market historical fee schedules,
  and larger-interval coverage reporting.
