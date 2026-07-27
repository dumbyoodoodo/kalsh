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

## H0019 isolation
No import of `experiments`, no read of H0019/H0018 artifacts, no model-perf
calc. Pinned by test.
