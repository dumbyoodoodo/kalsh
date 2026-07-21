# Runbook: Research dataset construction

Turns the collected source tables into versioned, point-in-time-correct
research datasets (Parquet + manifest). Read-only over the database; no
trading, feature, or model logic. See `docs/adr/0004-research-dataset.md` for
design and `ARCHITECTURE.md` for where it sits. This is Phase 2 / Milestone 4
of `TASKS.md`.

## What it produces

A build writes `<DATASET_ROOT>/<version>/`:

```
weather_panel.parquet     one row per (station, target_date): settled tmax/tmin,
                          forecast summary, forecast residuals
market_weather.parquet    one row per market snapshot: as-of forecast/observation/
                          order-book/trade facts + settled outcome (label)
manifest.json             git commit, source DB revision, content hashes, config
validation.json           the validation report
stats.json                summary statistics
```

- **Point-in-time correct**: `market_weather` uses backward as-of joins, so a
  market snapshot at time *t* only ever sees forecasts/observations whose
  issue/issuance time is `<= t`. A later revision cannot leak backwards.
- **Immutable versions**: a build refuses to overwrite an existing non-empty
  version directory. Re-run with a new `--version` instead.
- **Reproducible**: same source data + same market map + same code (git commit)
  produce identical per-frame content hashes, regardless of build time.

## Prerequisites

- `.env` configured (`DATABASE_URL`); Postgres running (`docker compose up -d
  postgres`); migrations applied (`alembic upgrade head`).
- Collected data present (run the collectors first --- `docs/runbooks/collector.md`
  and `docs/runbooks/weather_collector.md`).
- Optional: a market->settlement mapping file for the `market_weather` dataset
  (see below). Without it, `market_weather` is empty and every market is an
  "orphan" in the validation report --- expected until Milestone 2b.

## The market map (settlement stand-in)

Until Milestone 2b builds `settlement_specs`, the dataset layer needs to be
told which station/variable/date each Kalshi market settles against. Copy the
example and point `DATASET_MARKET_MAP_PATH` at your copy:

```bash
cp config/dataset_market_map.example.yaml config/dataset_market_map.yaml
# edit in verified mappings, then set DATASET_MARKET_MAP_PATH in .env
```

Its content hash is recorded in every manifest, so changing the mapping
changes the dataset version's provenance.

## Commands

Build everything and export a timestamped version:

```bash
uv run kalshi-weather dataset build
```

Options: `--which all|weather_panel|market_weather`, `--start YYYY-MM-DD`,
`--end YYYY-MM-DD` (inclusive target-date bounds), `--version LABEL`,
`--output-root PATH`, `--no-export` (build + validate + stats without writing).

Inspect without writing:

```bash
uv run kalshi-weather dataset stats       # summary statistics as JSON
uv run kalshi-weather dataset validate    # validation report; exits non-zero on errors
```

Export explicitly (choose format/version/root):

```bash
uv run kalshi-weather dataset export --version 2026-07-20 --format parquet
```

DuckDB export is a documented future option; `--format duckdb` currently
exits with a clear "not yet supported" message.

## Reading the validation report

`ok` is true only when no **error**-severity finding fired. Errors mean genuine
corruption and should block use of the dataset:

- `impossible_timestamps` --- a forecast with `valid_start > valid_end`, an
  observation issued before the date it describes, or a market snapshot dated in
  the future.
- `settlement_mismatches` --- a settled daily high below the daily low, or a
  settled temperature outside plausible bounds.
- `duplicate_join_*` --- a dataset has rows sharing its grain (a join bug).

**Warnings** are expected gaps, not failures:

- `missing_weather_observations` / `missing_forecasts` --- a day with one side
  but not the other (common: historical observations backfilled for days that
  predate any collected forecast).
- `orphaned_market_records` --- markets with no settlement mapping.
- `orphaned_weather_records` --- weather for a station not in the registry.

## Reproducing a dataset

Every dataset directory is self-describing. To reproduce version *V*:

1. Read `manifest.json`: check out its `git_commit`, confirm the source DB is at
   its `source_db_revision`, and use the same market map (`market_map_hash`).
2. Re-run `dataset build --version <new>` with the same `--start`/`--end`/`--which`
   from the manifest `config`.
3. Compare `content_hashes` in the new manifest against the old --- equal hashes
   prove byte-equivalent data (hashes are row-order-independent).

## Known limitations

- `market_weather` requires the manual market map until Milestone 2b.
- Forecast "high/low" per day is the max/min forecast period touching that local
  date --- an approximation (see the ADR), not true daily-high/low classification.
- DuckDB export is not implemented (Parquet only).
