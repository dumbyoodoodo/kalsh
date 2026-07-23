# Runbook: Kalshi historical market-data collector

Read-only ingestion of Kalshi weather-market data into Postgres. No trading,
no order submission -- see `docs/adr/0002-ingestion-collector.md` and
`ARCHITECTURE.md` for design. This is Phase 2 / Milestone 2 of `TASKS.md`.

## What it does, each cycle

1. **Discover**: list series in the configured category, their events, and
   their markets (status-filtered); persist series/event metadata and a
   market snapshot for each market (`ingestion/discovery.py`).
2. **Order books**: for every discovered market, fetch and persist the
   current order book.
3. **Trades**: for every discovered market, fetch trades since the last one
   already stored for that ticker (`min_ts`), persist any new ones. A market
   with **no stored trade yet** (newly discovered, or never successfully
   collected) does not fetch its entire lifetime history -- see "Trade
   bootstrap window" below.
4. **Settled-transition capture**: re-check a bounded number of
   recently-tracked tickers that have left the open-discovery list without a
   final result yet, and persist the settled snapshot when one appears -- see
   "Settled-market transition capture" below.

Market/order-book snapshots identical to the immediately-prior stored row
for that ticker are skipped (not inserted) -- history is still append-only,
we just don't record no-op polls. Trades are deduplicated by Kalshi's own
`trade_id`. A malformed item (bad ticker, out-of-range price, unparseable
timestamp) is logged and skipped; it does not abort the cycle. A whole-cycle
failure (DB unreachable, Kalshi unreachable) is logged and the process waits
for the next interval rather than crashing.

## Prerequisites

- `.env` configured per `README.md` (`KALSHI_ENV=demo` and credentials, or
  no credentials for public-only production data).
- Postgres running: `docker compose up -d postgres`.
- Migrations applied: `uv run alembic upgrade head` (or `.venv/bin/python -m
  alembic upgrade head` -- see the `uv run` caveat in
  `docs/adr/0001-initial-architecture.md`).

## Starting

Continuous (default -- runs until stopped):

```bash
uv run kalshi-weather collector run
```

Single cycle and exit (e.g. for cron-based scheduling instead of a
long-running process):

```bash
uv run kalshi-weather collector run --once
```

Override defaults (all otherwise come from `Settings`/`.env`:
`COLLECTOR_CATEGORY`, `COLLECTOR_MARKET_STATUS`, `COLLECTOR_INTERVAL_SECONDS`):

```bash
uv run kalshi-weather collector run --category "Climate and Weather" --status open --interval 300
```

## Stopping

- **Foreground**: `Ctrl-C` (SIGINT). The current cycle finishes (nothing is
  left half-written -- each cycle's DB writes are within one session,
  committed at the end of that cycle) and the process exits cleanly.
- **Background/service**: send `SIGTERM` (`kill <pid>`, or your process
  manager's normal stop signal). Handled identically to SIGINT.
- There is no separate "kill switch" beyond normal process signals in this
  milestone -- this is a read-only collector with no positions or orders to
  protect.

## Running as a long-lived process

For "run continuously for months," use a process supervisor (systemd,
supervisord, a container restart policy, etc.) rather than a bare shell
process, so it restarts after a host reboot or crash. A minimal systemd unit:

```ini
[Unit]
Description=Kalshi weather data collector
After=network-online.target postgresql.service

[Service]
WorkingDirectory=/path/to/kalsh
EnvironmentFile=/path/to/kalsh/.env
ExecStart=/path/to/kalsh/.venv/bin/kalshi-weather collector run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

`Restart=on-failure` is a safety net for a crash outside the code's own
per-cycle error handling (e.g. an unhandled signal); the collector itself
already tolerates transient DB/API failures without exiting.

## Verifying it's healthy

- Logs are structured JSON (`structlog`) at `INFO` by default
  (`LOG_LEVEL` in `.env`); each cycle logs a `collector.cycle_complete` event
  with counts (`markets_discovered`, `*_saved`, `*_duplicate`, `errors`,
  `invalid_items`).
- A cycle with `errors > 0` most commonly means Kalshi returned something
  unexpected for a specific market -- check the preceding
  `collector.orderbook.failed` / `collector.trades.failed` log lines for the
  ticker and exception.
- Query recent snapshots directly:
  ```sql
  select market_ticker, observed_at from market_snapshots order by observed_at desc limit 10;
  select market_ticker, captured_at from orderbook_snapshots order by captured_at desc limit 10;
  ```
  If `observed_at`/`captured_at` stop advancing, the process has likely
  stopped or is stuck on a cycle failure loop -- check logs.

## Trade bootstrap window

Trade collection is incremental: each cycle only requests trades newer than
the latest one already stored for that ticker (`min_ts`, checkpointed via
`get_latest_trade_timestamp`). That checkpoint doesn't exist yet the first
time a ticker is collected.

**Why full-history bootstrap is intentionally avoided**: some Climate &
Weather markets (long-running series can accumulate very large trade
volumes) have well over 100,000 trades. Requesting `min_ts=None` on such a
market asks for its entire lifetime history, which `paginate()`'s
`max_pages=1000` safety guard then aborts (by design -- it exists precisely
to stop a runaway fetch). Because `list_trades()` only returns once
pagination fully completes, that abort happens *before any trades are
returned*, so zero trades get persisted, no checkpoint is ever established,
and every following cycle repeats the exact same full-history fetch --
forever. This was an observed operational deadlock, not a hypothetical.

**How bootstrap differs from incremental collection**: when no checkpoint
exists for a ticker, the collector fetches only trades from the last
`INITIAL_TRADE_BOOTSTRAP_LOOKBACK_DAYS` days (`min_ts = now - lookback`)
instead of the market's full history. Those trades are persisted normally,
which establishes the checkpoint -- every cycle after that is the same
incremental `min_ts` fetch as any other market. A `collector.trades.bootstrap`
log line is emitted each time this path is taken, and `CycleStats.
trades_bootstrapped` counts how many tickers hit it in a given cycle. Trade
history older than the bootstrap window, for a market with no prior
checkpoint, is not collected by this path -- a known, documented gap (not
silent data loss), matching the existing precedent for `weather_backfill_days`.

**Changing the window**: set `INITIAL_TRADE_BOOTSTRAP_LOOKBACK_DAYS` in
`.env` (default: 30, mirroring `WEATHER_BACKFILL_DAYS`). No code change or
migration is required.

## Settled-market transition capture

Discovery is open-status-filtered, so a market leaves the discovery list the
moment Kalshi closes it -- *before* its final, result-bearing snapshot (the
row carrying `result` ∈ {`yes`,`no`}, `expiration_value`, `settlement_ts`)
exists. Everything downstream that keys off "the latest snapshot has a
result" -- price-sync/candlestick candidacy, settlement-label generation
(`settlement/labels.py`), retention monitoring -- would otherwise never see
that market settle. This starved settlement capture for every market settling
after 2026-07-21 until it was fixed (see
`docs/adr/0009-settled-market-transition-capture.md`).

Each Kalshi cycle, after the per-market pass, `capture_settled_transitions`
(`ingestion/settlement_sync.py`) re-checks a **bounded** set of tickers that
are:

- **recently observed** -- their latest snapshot is within
  `COLLECTOR_SETTLE_CHECK_DAYS` (default 7),
- **absent from the current open discovery** -- gone from the open list, and
- **not yet settled** -- their latest snapshot has no `result`.

The pending queue is **derived purely from stored data** (that three-part
predicate over `market_snapshots`), never from process state or a separate
table -- so there is nothing to desync and no migration. It is bounded by
`COLLECTOR_SETTLE_CHECK_LIMIT` (default 25) tickers per cycle, one `get_market`
call each, oldest-`close_time` first; the recency window means a market that
vanishes without ever settling (delisted/expired) ages out instead of being
retried forever, and the full historical settled archive is never re-fetched.

The pass is **idempotent and self-draining**: captured markets go through the
same `persist_market_snapshot` helper open discovery uses (so the two paths
cannot drift), which routes to `save_market_snapshot`'s content-hash
deduplication. Capturing a settled snapshot removes the ticker from the queue
on the next cycle (its latest snapshot now bears a result); re-checking an
unchanged market is a content-hash no-op. **Per-ticker failures are isolated**
-- a 404 for a delisted market or a transient API error is logged
(`collector.settle_capture.failed`) and counted, never raised, matching the
collector's whole-cycle error-isolation contract. Captured snapshots preserve
**raw-payload provenance** exactly as any other snapshot.

`collector.cycle_complete` reports `settle_checks`, `settled_captured`, and
`settle_errors`. Steady-state `settled_captured=0` with `settle_checks>0` is
normal (recent no-result tickers that have not settled yet); `settled_captured>0`
is the queue draining a backlog. Tune with `COLLECTOR_SETTLE_CHECK_LIMIT` /
`COLLECTOR_SETTLE_CHECK_DAYS` in `.env` -- no code change or migration.

**Relation to candle sync and settlement labels**: this pass only records the
settled *snapshot*. Once a market has a result-bearing snapshot it becomes a
price-sync candidate normally (`docs/runbooks/price_ingestion.md`), and its
settlement label can form (`docs/runbooks/settlement_labels.md`). Markets with
zero lifetime volume (quote-only, no trades) legitimately have no candles --
that is not a capture failure.

## Known limitations (see docs/API_VERIFICATION.md and the ADR)

- Order-book/trade quantities are rounded to the nearest whole contract from
  Kalshi's fixed-point (`*_fp`) wire fields; a genuinely fractional contract
  count (observed once in live testing) would be misrepresented.
- Sub-cent ("deci_cent") prices round to the nearest cent.
- No settlement-rule mapping happens here (Milestone 2b) -- discovered
  markets are not yet tied to a station/variable/threshold.
