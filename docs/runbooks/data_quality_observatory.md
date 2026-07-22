# Runbook: Data quality observatory

Continuous, deterministic certification that data already collected across
all five streams (forecasts, observations, Kalshi markets, trades, candles)
remain scientifically usable. Implemented in `kalshi_weather/observatory/`
(`severity.py`, `continuity.py`, `integrity.py`, `drift.py`, `parsing.py`,
`pit_consistency.py`, `report.py`).

**This package computes no research statistic and performs no statistical
inference.** It never fits a model, never estimates an effect, and owns no
hypothesis's decision thresholds. It exists solely to certify that the
platform's stored data are fit for future scientific work to depend on --
the same posture as `ops/quality.py` and `ops/forecast_cadence.py`, which it
reuses rather than duplicates (see "What's reused vs. new" below).

**Detection only.** No check here ever writes to the database, deletes a
row, or retries a collection. A finding is a report, not an action.

## Running it

```bash
uv run kalshi-weather ops observatory          # human-readable summary (WARNING/CRITICAL only)
uv run kalshi-weather ops observatory --json   # full machine-readable report, including INFO
```

Exit code is non-zero iff the report's overall status is CRITICAL. Wire the
no-arg form into cron/alerting the same way as `ops quality`
(`docs/runbooks/operations.md`); use `--json` for programmatic consumption
or historical archiving of report snapshots.

## Severity scale

Every finding (`observatory.severity.Finding`) carries exactly one of three
levels (`observatory.severity.Severity`):

| Severity | Meaning |
|---|---|
| `info` | Normal/expected state, recorded for trend visibility only. |
| `warning` | A condition worth an operator's attention; not yet data loss. |
| `critical` | Corruption, an integrity violation, or an unrecoverable gap. |

The report's overall `status` is the highest severity across all findings
(`Severity.highest`/`overall_severity`). `ObservatoryReport.by_severity()`
and `.by_domain()` filter the flat finding list; `.to_dict()` is the JSON
shape used by `--json` and by tests asserting deterministic reruns.

## Domains

Findings are tagged with one of six domains
(`observatory.severity.DOMAINS`): `forecast`, `observation`, `market`,
`trade`, `candle` (the five collection streams), and `platform`
(cross-cutting checks that don't belong to one stream -- schema drift,
collector-cycle liveness, point-in-time integrity).

## What's reused vs. new

| Source | What it contributes | Adapter |
|---|---|---|
| `ops/quality.py` (`run_quality_checks`) | schema drift, observation/forecast/market staleness, natural-key duplicates (observations, settlement specs), future-dated rows, settlement resolution failures | `drift.adapt_quality_finding` |
| `ops/forecast_cadence.py` (`run_forecast_cadence`) | forecast issuance cadence: missing/duplicate/out-of-order/delayed issuances, abnormal cadence, collector outage, stalled updates | `drift.adapt_cadence_alert` |
| `ops/health.py` (`build_health_report`) | collector liveness, per-station observation completeness (`dataset_completeness`) | `pit_consistency.adapt_completeness` |

Each existing report is computed **exactly once** per observatory run
(`report.build_observatory_report` passes its own `run_quality_checks`
result into `build_health_report` rather than letting it recompute one) and
folded into the unified `Finding` type. None of the three modules above are
modified by this package.

Every other check below is new to this package -- none of the following
existed before this task; there is no other rule elsewhere in the codebase
duplicating them.

## New checks

### Archive continuity (`continuity.py`)

Two distinct, schedule-agnostic questions per stream:

1. **Has the data itself stopped arriving?**
   (`find_continuity_gaps`, check `archive_continuity_<stream>`) -- gaps
   exceeding a per-stream threshold between consecutive stored timestamps,
   *including* the trailing gap from the newest timestamp to now (a stream
   that stopped entirely has no "next" timestamp to compare against
   otherwise). An empty stream in the window produces no gaps -- "never
   collected yet" is a distinct condition from "collection stopped."
   Default thresholds (`DEFAULT_MAX_GAP_HOURS`, hours): forecast 30,
   observation 48 (a station's CLI cycle is daily), market 2, trade 48
   (trading activity can legitimately pause), candle 2. Severity: WARNING
   if any gap, else INFO.

2. **Has the collector process stopped running, independent of whether
   upstream has anything new to report?** (`find_missed_run_cycles`, check
   `missed_collection_cycles_<collector>`) -- gaps between consecutive
   `collector_runs` rows (any status) exceeding the collector's configured
   interval by `DEFAULT_MISSED_CYCLE_MULTIPLIER` (3x), including the
   trailing gap to now. A collector can run successfully every cycle while
   upstream has nothing new -- that is stream silence (check 1), not a
   missed cycle (check 2); conflating them would misattribute an upstream
   lull as a collector outage. Severity: **CRITICAL** if any missed window
   (a dead collector process is always worth waking someone up for), else
   INFO.

Both are pure functions over in-memory timestamp/run sequences
(`find_continuity_gaps`, `find_missed_run_cycles`); thin async loaders
(`load_stream_timestamps`, `load_run_records`, `newest_timestamp`) adapt the
database and normalize every timestamp to naive-UTC via
`domain.time.to_naive_utc` before it reaches the pure functions -- the
platform's datetime columns are a genuine historical mix of
`TIMESTAMP WITH TIME ZONE` (migrations 0001/0003) and
`TIMESTAMP WITHOUT TIME ZONE` (migrations 0004/0005/0007), and comparing an
aware value against a naive `now` raises `TypeError` -- exactly the failure
this normalization step exists to prevent.

### Duplicate records + timestamp monotonicity (`integrity.py`)

`ops/quality.py` already checks natural-key duplicates for
`weather_observations` and `settlement_specs`; this module covers what that
check does not:

- **Duplicate natural keys** for `market_snapshots`, `market_candlesticks`,
  and `weather_forecasts` (checks `duplicate_market_snapshots`,
  `duplicate_candlesticks`, `duplicate_forecasts`) -- each already has a
  DB-level unique constraint that should make a duplicate impossible; a
  nonzero count is corruption. Severity: CRITICAL if count > 0, else INFO.
- **Timestamp monotonicity** (`find_monotonicity_violations`, checks
  `timestamp_monotonicity_<market|candle|forecast>`) -- are rows being
  *inserted* in roughly chronological order? A different question from "is
  a timestamp future-dated" (`ops/quality.py`'s check): a collector clock
  issue or a backfill-replay bug can insert a row whose own timestamp
  regresses well behind rows already stored, without ever being
  future-dated. Detected by walking rows in insertion order (`id`, an
  auto-incrementing surrogate key) and flagging any row whose timestamp
  falls more than `DEFAULT_MONOTONICITY_TOLERANCE` (10 minutes -- ordinary
  out-of-order arrival from network retries or concurrent requests is
  expected up to this tolerance) behind the running maximum seen so far.
  Severity: WARNING if any violation, else INFO.

### Unexpected station / schedule drift (`drift.py`)

- **Orphan stations** (`find_orphan_stations`, checks
  `unexpected_stations_observations` / `unexpected_stations_forecasts`) --
  station ids present in collected data but absent from the current
  in-code registry (`weather.stations.list_stations`) -- e.g. a station
  renamed or removed from the registry while its historical data remains,
  or a data-entry bug bypassing the registry lookup entirely. Severity:
  WARNING if any orphan, else INFO.
- Issuance-schedule anomalies are **not** reimplemented here --
  `ops/forecast_cadence.py`'s alerts already cover this and are folded in
  via `adapt_cadence_alert` (see "What's reused vs. new").

### Parser failures (`parsing.py`)

Weather CLI parsing: the weather collector already counts
`invalid_items`/`errors` per cycle (`WeatherCycleStats`,
`ingestion/weather_collector.py`) and persists them into
`collector_runs.stats_json`. `summarize_weather_parser_failures` (check
`weather_parser_failures`) mines that existing history for a trend rather
than adding new instrumentation to the collector. Severity: CRITICAL if the
failing-cycle rate is at/above `CRITICAL_FAILURE_RATE` (50%, sustained
across the window -- isolated failures happen; a persistently high rate
suggests the parser itself is broken against a changed upstream format),
WARNING if any failure below that rate, else INFO. (Settlement parsing
failures are `ops/quality.py`'s `settlement_resolution_failures`, folded in
via `adapt_quality_finding`, not reimplemented here.)

### Point-in-time integrity + forecast eligibility (`pit_consistency.py`)

Powered directly by `kalshi_weather.verification` (the reusable framework
that closed the exact gap H0003's first execution surfaced --
`docs/runbooks/forecast_verification.md`). This module adds no new
eligibility or matching logic of its own.

- **Forecast eligibility rate** (`summarize_forecast_eligibility`, check
  `forecast_eligibility_rate`) -- fraction of (station, variable,
  observation-date) cells in the window that are eligible (genuinely
  finalized) vs. not-yet-final vs. entirely absent, using the same
  local-midnight-boundary classification `verification.eligibility` uses
  everywhere else. A trend signal, not a confirmatory statistic. Severity:
  WARNING if any cell is `unknown_station` (a registry/data mismatch),
  else INFO -- `not_yet_final` cells alone are expected (the trailing days
  of any window are always still in progress) and do not raise severity.
- **Point-in-time integrity** (`summarize_point_in_time_integrity`, check
  `point_in_time_integrity`) -- count of forecast rows whose `issue_time`
  does not strictly precede their observation's finalizing issuance: the
  exact H0003 G2c leakage scenario, generalized and run continuously.
  Severity: **CRITICAL** on any occurrence -- this is a leakage risk
  masquerading as a join, never a benign data gap.

Both draw on a recent window (`ObservatoryConfig.pit_window_days`, default
45 days -- deliberately small so the observatory stays cheap enough to run
frequently; a genuine regression would first appear within a few weeks),
built via `dataset.builder.build_forecast_horizon_frame` +
`load_source_frames` (`pit_consistency.load_pit_frames`).
`build_forecast_horizon_frame` is the canonical, reusable form of the
NWS period high/low aggregation (ADR 0004) for this and future new
consumers -- a *second*, deliberate implementation alongside
`dataset.builder._forecast_issue_high_low` (used by `build_weather_panel`)
and `scripts/build_h0003_forecast_extract.py`'s private `_forecast_groups`
(a closed experiment's frozen artifact, never refactored to share code).

### Observation completeness (`pit_consistency.adapt_completeness`)

Not a new check -- folds `ops/health.py`'s existing per-station
`dataset_completeness` dict into `Finding`s (checks
`observation_completeness_<station_id>`). Severity: CRITICAL if coverage <
0.90, WARNING if 0.90-0.98, else INFO; `coverage is None` (no data yet) is
INFO.

## Historical trends

"Historical trends" (an explicit deliverable) come from windows over data
the platform already stores append-only (`collector_runs`, and every
monitored table itself) -- every check above already looks back over a
historical window, which is exactly what a trend is. There is **no new
persisted table** recording each report run; that would be infrastructure
the observatory does not yet need (CLAUDE.md: don't scaffold ahead of a
demonstrated need). If a future need emerges to compare report-over-report
(rather than window-over-window within one report), add an
`observatory_runs` table then, with its own ADR.

## Determinism

`ObservatoryReport.to_dict()` is byte-identical across repeated calls
against unchanged data, except for `generated_at` -- verified in
`tests/unit/test_observatory_report.py::test_report_reruns_deterministically_excluding_timestamp`,
matching the `--raw` byte-identical rerun convention used throughout the
H-series experiment scripts.

## Recovering from findings

| Finding | Likely cause | Action |
|---|---|---|
| `archive_continuity_<stream>` WARNING | upstream provider outage, or a still-provisional window (check before alerting on the newest day) | inspect the named gap window; if genuine, treat as `ops health` STALE would be |
| `missed_collection_cycles_<collector>` CRITICAL | collector process died or wedged | restart it (`ops run`); no data repair needed -- see `docs/runbooks/operations.md`'s "Restart recovery" |
| `duplicate_market_snapshots` / `duplicate_candlesticks` / `duplicate_forecasts` CRITICAL | a DB unique constraint was bypassed (raw SQL, a migration gap) | stop and investigate immediately; this should be structurally impossible |
| `timestamp_monotonicity_<stream>` WARNING | collector clock skew, or a backfill replay | inspect the flagged rows; confirm the affected timestamps are still correct, don't delete/reorder anything (data is append-only) |
| `unexpected_stations_*` WARNING | a station was renamed/removed from the registry, or a data-entry bug bypassed it | reconcile the registry (`weather/stations.py`) against the orphan ids; do not delete historical rows |
| `weather_parser_failures` CRITICAL | upstream NWS CLI format changed | inspect recent `collector_runs.stats_json` for the weather collector; the parser likely needs an update |
| `point_in_time_integrity` CRITICAL | a leakage bug in whatever built the forecast/observation frames passed in | stop and investigate before trusting any downstream analysis that used the same frames |
