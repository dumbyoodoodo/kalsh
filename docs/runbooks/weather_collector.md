# Runbook: Weather data collector

Read-only ingestion of NWS weather data (the confirmed settlement source for
Kalshi's daily-temperature markets) into Postgres. No trading logic -- see
`docs/adr/0003-weather-data-source.md` and `ARCHITECTURE.md` for design. This
is Phase 2 / Milestone 3 of `TASKS.md`.

## What it does, each cycle

For every registered station (`weather/stations.py` -- currently just `NYC`):

1. **Station metadata**: resolve and upsert the station's NWS office/timezone
   (`get_station_metadata`).
2. **Observations**: fetch daily max/min temperature since the last stored
   observation for this station (or `WEATHER_BACKFILL_DAYS` back, on first
   run), routed automatically between the live API (recent) and IEM's
   archive (older than ~5 days) -- transparent to the collector. The most
   recently seen day is always re-fetched too, since a more authoritative
   report issuance may have since been published for it.
3. **Forecast**: fetch the current forecast periods for the station
   (live-only; historical forecast backfill is not implemented -- see the
   ADR).

Every observation report issuance and every forecast issuance is stored as
its own row, keyed by `(station_id, variable, issuance_time)` /
`(station_id, variable, issue_time, valid_start)` respectively -- neither
table is ever overwritten, matching `CLAUDE.md`'s append-only rule. A
malformed item (unparseable CLI report, implausible temperature,
out-of-range timestamp) is logged and skipped; it does not abort the
station. A whole-station failure (e.g. the provider raised) is logged and
counted; the cycle continues with the next station. A whole-cycle failure
(DB unreachable, NWS unreachable) is logged and the process waits for the
next interval rather than crashing.

## Prerequisites

- `.env` configured per `README.md`. No credentials are required for NWS/IEM
  -- only `WEATHER_USER_AGENT`, which NWS asks API consumers to set to
  something descriptive (a default is provided in `.env.example`).
- Postgres running: `docker compose up -d postgres`.
- Migrations applied: `uv run alembic upgrade head`.

## Starting

Continuous (default -- runs until stopped):

```bash
uv run kalshi-weather weather collect
```

Single cycle and exit (e.g. for cron-based scheduling):

```bash
uv run kalshi-weather weather collect --once
```

Override defaults (all otherwise come from `Settings`/`.env`:
`WEATHER_BACKFILL_DAYS`, `WEATHER_INTERVAL_SECONDS`):

```bash
uv run kalshi-weather weather collect --station NYC --backfill-days 60 --interval 1800
```

List the station registry:

```bash
uv run kalshi-weather weather stations list
```

## Stopping

- **Foreground**: `Ctrl-C` (SIGINT). The current cycle finishes and the
  process exits cleanly.
- **Background/service**: send `SIGTERM`. Handled identically to SIGINT.
- Same as the Kalshi collector, there is no separate "kill switch" beyond
  normal process signals -- this is a read-only collector with no positions
  or orders to protect.

## Running as a long-lived process

Same pattern as `docs/runbooks/collector.md`'s systemd example -- swap the
`ExecStart` command for `weather collect`:

```ini
[Unit]
Description=Kalshi weather data collector (NWS)
After=network-online.target postgresql.service

[Service]
WorkingDirectory=/path/to/kalsh
EnvironmentFile=/path/to/kalsh/.env
ExecStart=/path/to/kalsh/.venv/bin/kalshi-weather weather collect
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

## Verifying it's healthy

- Logs are structured JSON (`structlog`); each cycle logs a
  `weather_collector.cycle_complete` event with counts
  (`stations_processed`, `observations_saved`, `observations_duplicate`,
  `forecasts_saved`, `forecasts_duplicate`, `invalid_items`, `errors`).
- An `errors > 0` cycle means a station's provider call raised -- check the
  preceding `weather_collector.station_failed` log line.
- A steady stream of `invalid_items` for one station most likely means a CLI
  report format changed -- check the `weather_collector.observation_invalid`
  / `weather_collector.forecast_invalid` log lines and, if it's a parser
  issue, `weather.cli_parse_failed` from `weather/provider.py`.
- Query recent data directly:
  ```sql
  select station_id, variable, observation_date, issuance_time
    from weather_observations order by issuance_time desc limit 10;
  select station_id, variable, issue_time, valid_start
    from weather_forecasts order by issue_time desc limit 10;
  ```
  If `issuance_time`/`issue_time` stop advancing, the process has likely
  stopped or is stuck on a cycle failure loop -- check logs.

## Known limitations (see docs/adr/0003-weather-data-source.md)

- Historical forecast backfill is not implemented -- forecasts are only
  collected going forward from whenever the collector first runs for a
  station. Historical *observations* are backfilled (via IEM), which is the
  settlement-critical path.
- The live/historical observation cutoff (`LIVE_RETENTION_DAYS = 5` in
  `weather/provider.py`) is a conservative approximation of NWS's actual
  retention window, not an exact published boundary.
- Only daily max/min temperature (`tmax_f`/`tmin_f`) is parsed from CLI
  reports today; other CLI fields (precipitation, snowfall, etc.) are not
  extracted.
