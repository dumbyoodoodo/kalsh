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
markets), so a healthy steady state costs zero extra API calls per cycle — it
only does work when a market newly settles or a previous cycle left something
incomplete. Recorded under `collector="prices"` in `collector_runs`, visible
to `ops health`/`ops quality` like every other collector.

### Coverage-aware candidate selection

**The per-cycle limit is spent only on markets that still need work.** This is
load-bearing, not an optimization. Candidates are selected as: settled markets
(latest snapshot, `result` in `yes`/`no`), **minus** markets whose stored
coverage already reaches close, **minus** markets this process has already
resolved to a terminal outcome — *then* ordered oldest-`close_time`-first and
*then* limited.

**Why the coverage filter must precede `LIMIT`.** It did not until 2026-07-23,
and the loop consequently never captured a single candle in production. With
`ORDER BY close_time ASC LIMIT 50` applied to *all* settled markets, the 50
oldest — long since covered by the initial backfill — filled the limit every
cycle, were skipped one by one, and the cycle ended having saved nothing. The
loop could not advance past its own head. Symptoms were `markets_attempted=50`,
`markets_skipped_already_covered=50`, `saved=0`, repeated indefinitely, with
`ops health` reporting a stale `oldest_uncaptured` and the observatory raising
a permanently open `archive_continuity_candle` gap. See ADR 0010.

**Ordering.** Oldest uncovered `close_time` first (the rolling window makes the
oldest uncovered market the highest-risk), ties broken by `market_ticker` so a
bounded run is deterministic and reproducible.

**`skip_covered` is now a second, defensive check.** It stays inside
`backfill_one_market` and still resolves a covered market from stored data
without an API call. Both it and the selection filter call the same
`coverage_reaches_close()` helper, so they cannot drift apart — if they did,
markets excluded by one and re-admitted by the other would silently start
consuming the limit again.

**Partial coverage stays eligible.** "Covered" means the latest stored
candle's `period_end` reaches `close_time` within
`COMPLETENESS_TOLERANCE_SECONDS` (3600s) *at the resolution being backfilled*.
A market whose candles stop short of that is still selected, so the gap can be
filled. Candles stored at a different resolution do not count as coverage.

The comparison is deliberately done in Python rather than pushed into SQL:
`market_snapshots.close_time` is `TIMESTAMPTZ` while
`market_candlesticks.period_end` is a naive `TIMESTAMP`, and a raw SQL
comparison would reinterpret the naive side against the server's `TimeZone`
setting.

### Terminal outcomes and head-of-queue starvation

A coverage filter alone is not sufficient. A market that legitimately returns
zero candles (`no_price_data` — a real market that never traded) stays
uncovered *forever*, so it would re-occupy the head of the oldest-first queue
every cycle and starve newer markets exactly the way covered markets did. On
2026-07-23 there were 85 such markets from a single day.

The sync loop therefore keeps an **in-process** set of markets that resolved to
a terminal outcome — `no_price_data`, `expired`, `unsupported`, or `partial`
that fetched and saved nothing — and excludes them from later cycles. Reported
as `terminal_excluded` in the cycle's `collector_runs.stats_json`.

- `api_failure` is **not** terminal — transient errors stay retryable.
- The set is intentionally **not persisted**: it needs no migration, and
  forgetting it on restart is the desirable failure mode. A market that was
  quiet an hour ago gets one more chance after a restart, and the set drains
  again within a few cycles. It is capped at `MAX_TERMINAL_SYNC_TICKERS`.
- Nothing is ever written to represent "no data". An HTTP 200 with an empty
  candlestick set is never recorded as coverage, and no candle is synthesized.

### Bounded execution and throttling

Each cycle is bounded by `PRICE_SYNC_LIMIT_PER_CYCLE` markets and inherits the
client's existing `KALSHI_MIN_REQUEST_INTERVAL_SECONDS` throttle and bounded
retries; the fix changes *which* markets are attempted, never how many, and
adds no new request path. A cycle that finds nothing to do issues zero API
calls.

### Explicit-invocation semantics

The coverage exclusion applies to **sweep-shaped** runs only:

| Invocation | Coverage exclusion | Rationale |
|---|---|---|
| continuous sync loop | **on** | its whole job is reaching uncovered markets |
| `prices backfill` (no selector) | **on** | `--limit` should buy real work |
| `prices backfill --start/--end` | **on** | date bounds narrow a sweep; they do not name markets |
| `prices backfill --ticker …` | **off** | an operator naming a market must still reach it |
| `prices backfill --event …` | **off** | same — an explicitly named target |
| any run with `--no-skip-covered` | **off** | explicitly asks for an unconditional re-fetch |

So `prices backfill --ticker X --dry-run` still inspects a covered market, and
`--no-skip-covered` still forces a real re-fetch. Only the unfiltered sweep
changed, and there it now *omits* covered markets from the report rather than
listing them as `skipped_already_covered`.

### Known limitations

- A market that returns no price data is indistinguishable, from the API, from
  one whose data is simply absent. It is recorded as `no_price_data`, excluded
  from the current process's queue, and re-checked after a restart — it is
  never marked covered.
- **Do not treat the ~67-day retention window as reliable.** The 2026-07-23
  baseline observed 404s from the candlesticks endpoint far inside it — one
  market 1.8 days past close, another 5.5 hours. The true discoverability
  window is not established. This is why prompt capture matters and why the
  sync loop's forward progress is a data-integrity concern, not a convenience.
  Tracked as a separate follow-up investigation.
- The terminal set is per-process, so a restart re-attempts quiet markets once.
  That is bounded (they drain within a few cycles) and deliberate.

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
| `prices` cycles succeed but `saved=0` while `oldest_uncaptured` is recent, and `archive_continuity_candle` stays open | the sync loop is running but not making forward progress — the 2026-07-23 defect (ADR 0010). Check a cycle's `collector_runs.stats_json`: `markets_attempted` equal to `PRICE_SYNC_LIMIT_PER_CYCLE` with `markets_skipped_already_covered` equal to it is the signature | confirm the deployed code has coverage-aware selection (`select_backfill_candidates(..., exclude_covered=True)`); a healthy idle cycle reports `markets_attempted=0`, not a full limit of skips |
| `terminal_excluded` grows every cycle and `markets_attempted` is persistently 0 | many settled markets legitimately return no price data; they are set aside per-process so they cannot block newer markets | expected on quiet days — verify with `prices backfill --ticker <one of them> --dry-run` that the API really returns 200-with-zero-candles; the set resets on restart |
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

> **The 67-day figure is currently contradicted and should not be relied on.**
> The 2026-07-23 baseline observed the candlesticks endpoint returning HTTP 404
> for markets far inside that window — `KXTEMPNYCH-26JUL2120-T75.99` at 1.8
> days past close, and `KXTEMPAUSH-26JUL2310-T89.99` at 5.5 hours past close —
> while sibling markets in the same series answered 200 normally. Whether this
> is a shorter real window, a per-market discoverability quirk, or something
> else is **not established**. `PRICE_OBSERVED_RETENTION_DAYS` was deliberately
> left unchanged pending a dedicated investigation (ADR 0010, "Follow-up").
> Until then, assume capture urgency is measured in **hours**, not weeks: the
> practical implication is that the sync loop's forward progress and any
> `oldest_uncaptured` signal should be treated as data-integrity concerns, not
> housekeeping.
