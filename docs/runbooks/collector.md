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
   already stored for that ticker (`min_ts`), persist any new ones.

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

## Known limitations (see docs/API_VERIFICATION.md and the ADR)

- Order-book/trade quantities are rounded to the nearest whole contract from
  Kalshi's fixed-point (`*_fp`) wire fields; a genuinely fractional contract
  count (observed once in live testing) would be misrepresented.
- Sub-cent ("deci_cent") prices round to the nearest cent.
- No settlement-rule mapping happens here (Milestone 2b) -- discovered
  markets are not yet tied to a station/variable/threshold.
