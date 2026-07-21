# Runbook: Historical price ingestion

How to backfill, monitor, and recover historical Kalshi candlestick data.
Phase 7A, `docs/adr/0007-price-ingestion.md`. Complements
`docs/runbooks/operations.md` (the combined collector loop this integrates
into) and `docs/runbooks/dataset.md` (the `market_prices` frame this feeds).

## What this collects

1-minute OHLC candlesticks for every settled Kalshi weather market: trade
prices (`price_open/high/low/close/mean_cents`), bid/ask quotes
(`yes_bid_*`/`yes_ask_*`), volume, and open interest. Stored in
`market_candlesticks`, unique on `(market_ticker, period_interval_seconds,
period_end)` — reruns are always safe; a row is never overwritten.

**Not collected**: individual trade ticks (candlesticks only, see ADR 0007
§7), forecast/observation data (that's `weather backfill`, a separate
runbook), anything for still-open markets (candlesticks are only meaningful
once a market has traded history to fetch, and outcome labels only exist once
it settles).

## Initial backfill

```bash
uv run kalshi-weather prices backfill
```

Runs oldest-`close_time`-first over every settled market discoverable via
`/markets?event_ticker=`, one market per commit. Useful flags:

- `--ticker A,B,C` — comma-separated explicit ticker list (not a native list
  option — see ADR 0007 for why; a plain comma-separated string sidesteps a
  ruff false-positive on list-typed Typer options)
- `--event <event_ticker>` — restrict to one event
- `--start`/`--end` — restrict by `close_time` (`YYYY-MM-DD`)
- `--resolution <minutes>` — candle width; default `1` (finest Kalshi supports;
  `60` and `1440` are also confirmed live)
- `--limit N` — cap markets attempted this run
- `--no-skip-covered` — force a full re-fetch even for markets whose stored
  span already reaches close (still duplicate-safe; only useful if you suspect
  the API revised already-stored candles, which has not been observed)
- `--dry-run` — classify every candidate market without writing anything;
  prints the same summary the real run would

The command exits non-zero and lists every `api_failure` market if any
occurred; every other outcome (`complete`/`partial`/`no_price_data`/
`expired`/`unsupported`) is a normal, non-error classification printed in the
summary totals.

**Expected runtime**: ~1 market/second (existing 0.1s client throttle) — the
full 804-market archive took ~8-9 minutes end to end and saved 761,475
candles with zero duplicates and zero failures (2026-07-21 run).

## Incremental sync (continuous preservation)

The combined runner already includes price sync as a third concurrent task
(`ops/price_coverage.py`'s retention monitoring + `ingestion/price_backfill.py`'s
`run_price_sync_loop`, wired into `ops run` exactly like the Kalshi and
weather collector loops):

```bash
uv run kalshi-weather ops run
```

Interval: `PRICE_SYNC_INTERVAL_SECONDS` (default 1800s / 30 min). Each cycle
runs a bounded backfill pass (`PRICE_SYNC_LIMIT_PER_CYCLE`, default 50
markets) with `skip_covered=True`, so a healthy steady state costs zero extra
API calls per cycle — it only does work when a market newly settles or a
previous cycle left something incomplete. Recorded under `collector="prices"`
in `collector_runs`, visible to `ops health`/`ops quality` like every other
collector.

**Why this is enough, and why no new scheduler was built**: the rolling
retention window (~67 days, empirically observed — see ADR 0007) is far
longer than the 30-minute default sync interval, so a market settling today
has enormous headroom before it risks falling out of discoverability. A
30-minute cadence inside the existing loop is the smallest reliable mechanism
that clears that bar; nothing bespoke was needed.

## Retention-risk monitoring

```bash
uv run kalshi-weather ops health          # includes a "price retention" line
uv run kalshi-weather ops health --json   # full price_retention block
```

`price_retention` reports: `oldest_uncaptured_market` (the single highest-risk
market, if any), `markets_nearing_expiry` (age ≥ `PRICE_OBSERVED_RETENTION_DAYS
- PRICE_RETENTION_WARNING_BUFFER_DAYS`, defaults 67-10=57 days), and
`markets_incomplete_coverage`. A non-empty `oldest_uncaptured_market` or a
nonzero `markets_nearing_expiry` means: run `prices backfill` before the next
scheduled sync cycle, don't wait.

## Coverage verification

```bash
uv run kalshi-weather prices coverage
```

Measured (never estimated) report: markets attempted/captured/complete/
incomplete/never-captured, total candles, coverage by event date and by
variable (tmax_f/tmin_f), `markets_with_missing_intervals` (candle count vs.
expected count within a market's *own* observed span — flags thin/illiquid
strikes, see ADR 0007), `likely_lost_to_retention` (age-inferred, explicitly
labeled `"inferred ... not a directly observed 404"` — never asserted as
fact), `duplicate_row_count` (should always be 0; the natural key makes
duplication structurally impossible short of a schema bug), and
`settlement_label_overlap` (captured-vs-resolved-spec cross-check; markets
that are `resolved_only` are expected to be still-open markets not yet
eligible for backfill, not a gap — cross-check against `market_snapshots`'
`result` column if this ever looks larger than the open-market count).

**Validating a sample against the live API directly** (the "database counts
and report counts must agree exactly" requirement) is not yet a CLI command —
call `ops.price_coverage.verify_sample_against_live` directly with a
stratified ticker sample (oldest, newest, random, and any
`markets_with_missing_intervals` entries) via a short script, same shape as
the live verification performed for the 2026-07-21 backfill (ADR 0007). Do
not claim "complete coverage" from the stored report alone without this step.

## Failure recovery

| Symptom | Diagnosis | Action |
|---|---|---|
| `prices backfill` exits non-zero | one or more markets hit `api_failure` (network/API outage) | re-run the identical command; `--skip-covered` (default) means completed markets cost nothing, only the failed ones re-fetch |
| `ops health` shows `prices` STALE | sync loop task died, or `ops run` isn't running | restart `ops run`; no data repair needed — same data-derived-checkpoint recovery as the other two collectors |
| `price_retention.markets_nearing_expiry > 0` | a market is approaching the ~67-day discoverability cutoff and hasn't been captured | run `prices backfill --limit <n>` immediately, don't wait for the next sync cycle |
| `prices coverage` shows `likely_lost_to_retention` entries | a market aged past the retention window with zero stored candles | this is very likely genuinely unrecoverable — Kalshi's own discoverability window has passed; re-run backfill once to confirm the inference before treating it as final (see the coverage report's own note) |
| `markets_with_missing_intervals` for a market you expected to be liquid | check `data_quality_status`/volume in `market_candlesticks` for that ticker directly — could be genuine thin trading, not a bug | no action unless volume looks wrong for a market you know traded actively |

Safe reruns: every command here is idempotent by construction (natural-key
dedup on the table, `--skip-covered` default in the CLI). There is no
"cleanup" step and none should ever be needed — do not delete rows from
`market_candlesticks` to "fix" a coverage report; a genuine gap should stay
visible, never papered over (same principle as `docs/runbooks/operations.md`'s
observation-gap guidance).

## A note on retention

The ~67-day rolling discoverability window is an **empirical finding from
probing the live API on 2026-07-21** (ADR 0007), stored as a configurable
setting (`PRICE_OBSERVED_RETENTION_DAYS`) specifically because Kalshi has
given no documented guarantee it will stay at 67 days, or stay a rolling
window at all. Treat any code or alert threshold derived from it as a current
best estimate, not a permanent contract — re-verify periodically by comparing
`ops health`'s oldest-uncaptured-market age against markets that are still
independently confirmable via other means.
