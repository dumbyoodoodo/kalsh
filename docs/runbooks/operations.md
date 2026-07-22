# Runbook: Research operations

How to run, monitor, back-fill, and recover the research data platform for
months of unattended operation. Phase 6 of `TASKS.md`; complements the
per-component runbooks (`collector.md`, `weather_collector.md`,
`settlement.md`, `dataset.md`, `price_ingestion.md`).

## Starting collectors

The recommended long-running deployment is the combined runner -- three
collectors (Kalshi markets, weather, and historical prices) concurrently in
one supervised process:

```bash
uv run kalshi-weather ops run
```

Intervals come from `.env` (`COLLECTOR_INTERVAL_SECONDS`,
`WEATHER_INTERVAL_SECONDS`, `PRICE_SYNC_INTERVAL_SECONDS`, default 1800s) or
`--kalshi-interval`/`--weather-interval`/`--price-sync-interval`. Request
pacing is configurable (`KALSHI_MIN_REQUEST_INTERVAL_SECONDS`,
`WEATHER_MIN_REQUEST_INTERVAL_SECONDS`). The individual collectors
(`collector run`, `weather collect`, `prices backfill`) remain available for
running components separately — see `docs/runbooks/price_ingestion.md` for
the price collector specifically (initial backfill, retention monitoring,
coverage verification).

Under systemd (recommended for months-long operation):

```ini
[Unit]
Description=Kalshi weather research platform collectors
After=network-online.target postgresql.service

[Service]
WorkingDirectory=/path/to/kalsh
EnvironmentFile=/path/to/kalsh/.env
ExecStart=/path/to/kalsh/.venv/bin/kalshi-weather ops run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

## Stopping collectors

`Ctrl-C` (SIGINT) or SIGTERM: the current cycles finish, run records are
written, and the process exits cleanly. Nothing is left half-written -- each
cycle's writes commit together at cycle end.

## Restart recovery

No manual recovery steps exist because none are needed -- checkpoints are
derived from the data itself, not from process state:

- market/order-book snapshots are append-only with content-hash dedup: a
  re-collected unchanged snapshot is skipped, not duplicated;
- trades resume from the last stored trade timestamp per market (`min_ts`);
- weather observations resume from the last stored observation date (the most
  recent day is deliberately re-fetched for late revisions);
- settlement specs re-parse idempotently (`(market, parser_version,
  rules_hash)` unique).

Start the process again (or let systemd do it); collection resumes exactly
where the data left off.

## Collector restart policy

The collector loads code, `.env`, and key material once at start, so edits to
the working tree do **not** reach a running collector until it restarts. The
single source of truth for which changes require what action is the `RULES`
table in `src/kalshi_weather/ops/restart_policy.py`; this table summarizes it:

| Change | Action | Why |
|---|---|---|
| `docs/`, `tests/`, `notebooks/`, `scripts/` (non-service), `*.md`, `Makefile`, `.gitignore`, `.env.example`, `.env.bak-*` | **none** | not loaded by the collector process |
| `docker-compose.yml` | **none** (apply via `docker compose up -d`) | Postgres restarts are self-healed by preflight + cycle retry |
| `src/` (incl. migrations), `.env`, `secrets/`, `config/`, `pyproject.toml`, `uv.lock`, `alembic.ini` | **graceful restart** (`scripts/service/restart.sh`) | imported/read once at start; run `uv sync` / `alembic upgrade head` first where relevant |
| `scripts/service/` (or the repo moved) | **reinstall** (`scripts/service/install.sh`, idempotent) | launchd plist/entry-script layer |
| anything unclassified | **graceful restart** (conservative default) | a needless restart is cheap; stale code collecting data is not |

Detection is automatic: at every service start, `scripts/service/launch.py`
records a manifest (git commit + SHA-256 of every restart-relevant file —
hashes only, never contents) to `<KALSHI_LOG_DIR>/collector.manifest.json`.
To ask whether the running collector matches the working tree:

```bash
scripts/service/check_restart.sh            # report only; never restarts
scripts/service/check_restart.sh --restart  # act on the verdict (graceful)
```

Exit codes: `0` no restart needed, `10` restart required, `11` reinstall
required, `12` unknown (no manifest yet — restart once to establish the
baseline). The underlying command is `kalshi-weather ops restart-check`.
Nothing ever restarts automatically; the check only reports, and `--restart`
is an explicit operator action (the restart itself is graceful — current
cycles finish and commit, per "Stopping collectors" above).

## Operational metrics

Every collection cycle appends a row to `collector_runs`: start/finish time,
duration, success, request/retry counts, and the full cycle stats. Metric
writes are best-effort by design -- a metrics failure is logged and never
interrupts collection. Query directly or via `ops health` (recent success
rate, mean duration) and snapshot `ops.json` (last 50 runs).

## Monitoring health

```bash
uv run kalshi-weather ops health          # human-readable summary
uv run kalshi-weather ops health --json   # full machine-readable report
uv run kalshi-weather ops quality         # data-quality checks; exit 1 on errors
```

`ops health` shows: collector liveness (STALE = last run older than
`OPS_STALE_AFTER_INTERVALS` x its interval, or never run) for all three
collectors (`kalshi`, `weather`, `prices`), newest data timestamps per table,
database size, station/settlement coverage, per-station observation
completeness, price retention risk (oldest uncaptured market, markets
nearing the ~67-day discoverability cutoff, incomplete-coverage count — see
`docs/runbooks/price_ingestion.md`), and the quality summary.

`ops quality` checks: schema revision vs code expectation (error),
natural-key duplicates (error -- should be impossible), future timestamps
(error), observation gap days per station (warning), forecast/market
staleness (warning), settlement resolution failures (warning, with tickers).
Errors mean corruption or drift -- stop and investigate; warnings are
operational gaps to schedule fixes for. Wire `ops quality` into cron and
alert on non-zero exit.

## Monitoring forecast-collection cadence

```bash
uv run kalshi-weather ops forecast-cadence   # scheduled-floor check + schedule-agnostic gap alert
```

Forecast history has **no backfill source** (ADR 0003) -- a capture gap is
permanently unrecoverable -- so this report exists to catch under-capture
while it is still preventable. It applies two complementary invariants
(architecture review, 2026-07-21):

1. **Minimum cadence verification.** NWS guarantees a morning and an
   afternoon forecast package per office at roughly fixed local times, so
   each station-local day is checked as two windows (00-12 / 12-24 local);
   an empty interior window is a missing cycle. This verifies the
   documented scheduled floor only. `--expected-per-day` values above 2
   are clamped: the stored issue_time is NWS's irregular, clustered
   `updateTime` event stream, and finer equal partitions would assert a
   uniformity the data does not have, producing systematic false
   missing-cycle alerts rather than better monitoring.
2. **Schedule-agnostic outage detection.** The largest gap between
   successive captured issuances must stay within
   `max_issuance_gap_hours` (default 18h; strictly greater fires). This
   makes no assumption about when in the day issuances cluster, stays
   valid across seasonal schedule changes, and catches long silent
   stretches the window check structurally cannot (both windows of a day
   can be occupied while 20+ hours pass between issuances).

Per station the report also includes: issuance counts and gaps, a
local-hour issuance histogram, duplicate captures, out-of-order and
delayed arrivals, and health metrics (observed issuances/day,
completeness %, gap distribution, last capture/run, longest collector
outage from `collector_runs`). Deterministic alerts (`missing_issuance`,
`duplicate_issuance`, `abnormal_cadence`, `issuance_gap`,
`collector_outage`, `stalled_updates`) each carry the expected state, the
observed state, and a recommended action; there is no notification
transport -- wire the JSON output into whatever alerting exists.

## Performing backfills

Deep historical observation backfill (IEM archive), chunked and resumable:

```bash
uv run kalshi-weather weather backfill --station NYC \
    --start 2023-01-01 --end 2026-06-19 [--chunk-days 30]
```

- Each chunk commits independently; a mid-run crash loses at most one chunk.
- A failed chunk is reported and skipped; the run continues and exits
  non-zero listing exactly which chunks failed. **Re-run the identical
  command to retry**: completed chunks are skipped (`--skip-covered`,
  default), failed ones are re-fetched. `--no-skip-covered` forces a full
  re-fetch (still duplicate-safe).
- Progress and per-chunk coverage (days-with-data / days-in-chunk) are
  logged as it runs.

**Expected runtime**: IEM serves ~1 listing request/day plus ~1 text
request/issuance (~2/day) → ~1,100 requests per station-year; at 0.1s pacing
plus latency, plan roughly **5-15 minutes per station-year**. **Storage**:
~1,500 observation rows plus raw payloads per station-year — a few MiB;
years x stations remain trivial for Postgres.

## Research snapshots

```bash
uv run kalshi-weather ops snapshot        # version defaults to snapshot-YYYYMMDD
```

Cuts an immutable `<DATASET_ROOT>/<version>/` containing the full Milestone 4
dataset export (Parquet + manifest + validation + stats) **plus**
`quality.json` (data-quality report) and `ops.json` (health report,
settlement resolution report, last 50 collector runs). The manifest carries
git commit, source DB revision, and content hashes -- any experiment citing a
snapshot version is reproducible and auditable. Run daily from cron after
the collectors' morning cycles; a second snapshot the same day needs an
explicit `--version` (versions are immutable).

## Validating datasets

`dataset validate` (build-time validation) and `ops quality` (store-level
checks) are complementary; both exit non-zero on error-severity findings.
Snapshots embed both reports.

## Recovering from failures

| Symptom | Diagnosis | Action |
|---|---|---|
| `ops health` shows a collector STALE | process died or wedged | restart it (`ops run`); no data repair needed (see Restart recovery) |
| cycle logs show repeated `cycle_failed` | DB or API unreachable | fix connectivity; the loop is already retrying every interval |
| `ops quality` schema error | migrations not applied (or old code against new DB) | `alembic upgrade head`, or deploy matching code |
| backfill exits non-zero | some chunks failed (network/IEM outage) | re-run the identical command; only failed chunks re-fetch |
| observation gap days persist | source had no CLI report those days, or backfill range never covered them | backfill the specific range; if IEM has nothing, the gap is real and stays visible (never papered over) |
| snapshot fails: version exists | one snapshot per version, immutable | pass a new `--version` |
