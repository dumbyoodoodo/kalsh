# ADR 0004: Research dataset construction (point-in-time joins)

## Status

Accepted.

## Context

`TASKS.md` Milestone 4 calls for turning the collected raw/normalized data
(Kalshi markets, order books, trades; weather forecasts and observations) into
clean, versioned, reproducible research datasets with point-in-time-correct
joins and no look-ahead leakage. `RESEARCH.md` fixes the reproducibility and
leakage rules this must satisfy. No feature engineering, modeling, backtesting,
or trading logic is in scope (those are Phases 4-8).

Two facts shaped the design:

1. **`settlement_specs` does not exist yet.** Milestone 2b (settlement-rule
   parsing) is unstarted, so there is no automated link from a Kalshi market to
   the weather station/variable/date it settles against. Kalshi states that
   association only in free-text rules, and CLAUDE.md forbids inferring
   settlement facts from titles.
2. **The source datetime columns are naive UTC.** They are
   `TIMESTAMP WITHOUT TIME ZONE`; every writer stores UTC. A forecast/observation
   describes a *local* calendar day, so the day it belongs to must be derived in
   the station's timezone, not UTC.

## Decisions

1. **The market->settlement association is an explicit, versioned config file**
   (`dataset/market_map.py`, `config/dataset_market_map.example.yaml`) --- a
   documented stand-in for `settlement_specs` until Milestone 2b generates it.
   The mapping's content hash is recorded in every manifest, so a build is
   reproducible only against the exact mapping used. Markets absent from the
   mapping are **orphans** (a validation warning), exactly as unresolved markets
   will be once a real settlement resolver exists. This was chosen over parsing
   rules text now (that is Milestone 2b's job, deliberately not pulled forward)
   and over dropping the market<->weather join entirely (it is the point of the
   phase). The dataset layer swaps to real `settlement_specs` by changing only
   how the mapping list is populated.

2. **Two datasets, both point-in-time correct:**
   - `weather_panel` --- one row per (station, target_date): the *settled*
     tmax/tmin (the value from the **latest** observation issuance, since later
     CLI reports supersede earlier provisional ones), a summary of the forecast
     issuances for that day, and the forecast **residuals**. Built from the
     weather tables alone, so it is always available even with zero markets.
   - `market_weather` --- one row per market snapshot, enriched by **backward
     as-of joins** with the forecast/observation/order-book/trade facts whose
     knowledge timestamp is `<= the snapshot's timestamp`. A later forecast
     revision is invisible to an earlier snapshot *by construction* (Polars
     `join_asof`, strategy `backward`), which is the concrete mechanism enforcing
     RESEARCH.md's "a decision at time t may only use forecasts with issue_time
     <= t". The post-settlement outcome is included as a clearly-labelled
     `settled_value` target column, never as a pre-decision input.

3. **Point-in-time joins are pure Polars functions over in-memory frames**, with
   the only SQLAlchemy code in `load_source_frames`. This makes leakage
   directly unit-testable without a database (see `test_dataset_builder.py`'s
   as-of leakage tests) --- the highest-priority guarantee of the phase gets the
   most direct tests. Timestamps are normalized to naive-UTC at the load
   boundary so Postgres (tz-aware) and SQLite-test (naive) sources compare
   identically; each forecast's `target_date` is computed in the station's
   timezone at that same boundary.

4. **Forecast "high/low" per day is the max/min period point-estimate touching
   that local date** --- an interpretation-light aggregation (the warmest NWS
   forecast segment of a day approximates its forecast high). This is not
   forecast-period *classification* (that edges into feature engineering); it is
   documented as approximate. Residuals (`forecast_high_f - settled_tmax_f`,
   `forecast_low_f - settled_tmin_f`) are label-side quantities and must not be
   used as model *features* (RESEARCH.md feature-leakage rule).

5. **Versioned, immutable, self-describing exports.** A build writes
   `<DATASET_ROOT>/<version>/` containing one Parquet per dataset plus
   `manifest.json`, `validation.json`, and `stats.json`. The export refuses to
   overwrite a non-empty version directory --- a published version is immutable
   (RESEARCH.md). The manifest records git commit, source DB Alembic revision,
   sanitized (credential-free) database URL, per-frame content hashes, and the
   generation config, so a result is reproducible from what produced it. Content
   hashes are order-independent, so a rebuild that produces the same rows hashes
   identically (verified in `test_dataset_pipeline.py`).

6. **Parquet now; DuckDB is a documented future seam, not shipped.** Polars
   writes Parquet natively (no new dependency). `ExportFormat.DUCKDB` exists as
   the dispatch seam but raises `UnsupportedExportFormatError` rather than ship
   untested code with no current consumer (CLAUDE.md: no dependency/abstraction
   ahead of a real need).

## Consequences

- No schema/migration change: this layer is **read-only** over the existing
  tables. Nothing in the source database is written, so historical data is not
  at risk.
- When Milestone 2b lands, `settlement_specs` replaces the config market map by
  populating the same `list[MarketMapping]` --- `builder.py`/`pipeline.py` do not
  change. The orphan/validation machinery already models unresolved markets.
- The forecast high/low aggregation (decision 4) is a known approximation; a
  later feature phase that needs true daily-high/low forecast classification
  should refine it there, not retrofit interpretation into this layer.
- `DATASET_ROOT` is the first configured filesystem write location in the
  project (Phases 1-3 were Postgres-only); it follows the storage philosophy
  (env-configurable, created on demand, `.gitignore`d).
