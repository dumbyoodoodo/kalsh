# Runbook: paper-trading / execution simulator

**SIMULATION ONLY -- NO EXCHANGE ORDERS ARE SUBMITTED.** See ADR 0015.

## What it is (and is not)
An offline, deterministic evaluator of *hypothetical* orders against synthetic
fixtures or explicitly-supplied historical production data. It is NOT demo
execution and NOT real execution; it has no order-submission path and never
connects to an account. Demo execution (a future, separate, authenticated
client) is NOT equivalent to realistic production fills.

## Architecture
`execution/`: `models` (orders, state machine, YES/NO Position), `market`
(OrderBook/Trade fixtures), `policy` (ExecutionPolicy, FeeModel, RiskConfig),
`fills` (Modes A/B/C), `risk` (pre-trade), `ledger` (append-only + replay),
`engine` (order->risk->fill->ledger, settlement), `metrics`, `replay` (runner +
immutable artifacts), `loader` (JSON config).

## Order state machine
created -> accepted -> {resting|partially_filled|filled|canceled|expired}; or
created -> rejected. Terminal: filled/canceled/expired/rejected. Invalid
transitions raise.

## Fill / fee / risk assumptions
- Marketable fills consume only visible depth at/through the limit (partial when
  short). Passive fills need realized subsequent volume beyond a queue-ahead
  assumption; queue position cannot be perfectly reconstructed from public data.
- Risk rejects (with a reason) before acceptance: order qty, position/market,
  gross exposure, total/per-event possible loss, open orders, markets with
  exposure, cash, book age, spread, price bounds, duplicate id.

## Fee models (versioned; see ADR 0016)
Three models behind one interface (`config policy.fee_model.type`):
- `zero` (default) — charges nothing.
- `configurable` — labelled PLACEHOLDER (`FeeModel` alias); NOT a real schedule.
- `kalshi_event_contract` — AUTHORITATIVE, grounded ONLY in official Kalshi docs:
  - General trade fee `round up(0.07 x C x P x (1-P))`, S&P500/NASDAQ-100
    (`INX*`/`NASDAQ100*`) `round up(0.035 x ...)` — CFTC-filed Kalshi Fee
    Schedule (eff. 2022-09-12).
  - Per-fill centicent trade fee + balance-precision rounding + **per-order
    $0.01 rebate accumulator** — Kalshi Predictions API "Fee Rounding" docs.
  - Fee intermediates in integer **centicents** ($0.0001); ledger in integer
    cents. No floats.
- **Limitations:** the current July-2026 consolidated PDF was unreachable here
  (HTTP 429), so the 0.07 coefficient's July-2026 currency and current per-market
  **maker** coefficients are unconfirmed. Maker fee is **off by default**;
  supply an official coefficient to enable it. Simulated fees are an estimate;
  the exchange reports realized fees after execution, and schedules change — a
  run pins the fee-model version.
- Inspect a single fill's fee offline:
  `paper calculate-fee --price-cents 40 --quantity 10 --liquidity taker
  --fee-model kalshi [--ticker INXD-26]`.

## Ledger invariants & reconciliation
Portfolio = replay(ledger). Every balance change is an entry; each entry's cash
delta is recomputed and asserted equal on apply. `cash+reserved >= 0`,
`reserved >= 0`. Duplicate settlement is idempotent. `paper validate` and
`paper replay-ledger` confirm reconciliation.

## Settlement
Given an authoritative result (explicit input, never read before its availability
time): pay yes_qty*100 (YES) or no_qty*100 (NO), close positions, record realized
P&L, preserve fees, mark settled, reject further orders, idempotent.

## Replay ordering
(timestamp, kind_rank, tiebreak) with book<trade<intent<settlement<mark; only
past books and strictly-subsequent trades inform a fill.

## Known gaps
Placeholder fees; conservative passive proxy; historical-data fill adapter and a
generic prediction interface are follow-ups.

## Reproduce
`paper simulate --config <json> --out-dir <new-dir>` (refuses to overwrite);
`paper inspect|validate|replay-ledger <dir>`. Same inputs -> byte-identical run.

## Historical production replay (ADR 0017)
Read-only adapter from archived PostgreSQL market data into replay events.
- **Source tables**: `orderbook_snapshots` (full-depth snapshots, deduped;
  `captured_at` = collector clock), `trades` (`executed_at`, `count`,
  `taker_side`), `market_snapshots` (status/result/settlement_ts/observed_at).
  `environment` provenance is production/demo/unknown/NULL.
- **Environment filtering**: production only by default; demo rejected; NULL
  rejected unless explicitly allowed per data type. Missing != zero.
- **Book normalization**: `yes_bids` = yes_levels; `yes_asks` = {(100-p, q) for
  no_levels}. No fabricated depth.
- **Timestamp modes**: `exchange-time` (settlement at determination time) vs
  `collector-available` (settlement visible at ingestion `observed_at`; trades
  without ingestion provenance excluded). Books coincide (captured_at is the
  only book time). Mode recorded in the manifest.
- **Trade direction**: `taker_side` kept when reliable, else unknown -- never
  guessed. **Passive fills**: subsequent qualifying volume only, never a price
  touch; lower-confidence when trades are sparse. **Missing data**: surfaced in
  the quality report, never hidden; configurable minimum coverage.
- **Deterministic ordering**: single engine; market-status < intent < settlement;
  latest book <= submission, strictly-subsequent trades only.
- **Read-only DB**: connection is `default_transaction_read_only=on`; bounded
  time+ticker filters required; the adapter never writes orders/fills back.
- **Reproduce**: `paper export-replay-data --start --end --tickers <file>
  --mode <mode> --output <dir>` (immutable; content-hashed) then
  `paper simulate-history --data <export> --orders <file> --config <exec>
  --output <dir>`. Same inputs -> identical hashes.
- **Execution-engine validation != strategy backtesting**: a historical replay
  checks how orders would have executed; it is not an edge claim, and (like the
  rest of the simulator) submits nothing.

## H0019 isolation
No import of `experiments`, no read of H0019/H0018 artifacts, no model-perf
calc -- including the historical adapter (market-data tables only). Pinned by
tests; the smoke market is a production market unrelated to H0019 selection.
