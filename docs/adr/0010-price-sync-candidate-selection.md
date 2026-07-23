# ADR 0010: Coverage-aware price-sync candidate selection

## Status

Accepted (2026-07-23). Amends ADR 0007 (price ingestion), whose "continuous
preservation" mechanism this repairs. ADR 0007 stands otherwise.

## Context

ADR 0007 introduced a continuous price-sync loop (`run_price_sync_loop`) whose
job is to capture candlestick history for markets as they settle, before that
history ages out of Kalshi's rolling discoverability window. Each cycle runs a
bounded pass (`PRICE_SYNC_LIMIT_PER_CYCLE`, default 50) with
`skip_covered=True`.

The 2026-07-23 post-deployment operational baseline established that **the loop
had never captured a single candle in production**. Every candle in the archive
came from a manual `prices backfill` invocation.

### Root cause

`select_backfill_candidates` selected settled markets, ordered by `close_time`
ascending, and applied `LIMIT` — with no coverage predicate. Coverage was only
consulted afterwards, per market, inside `backfill_one_market`'s `skip_covered`
branch.

So the limit was spent entirely on the *oldest* settled markets, which the
initial backfill had covered months earlier. Each cycle selected the same 50
May-2026 markets, skipped all 50, and ended. The loop could not advance past
its own head, and no market that settled after the initial backfill was ever
reachable.

Measured over 24 hours (48 cycles):

```
markets_attempted                 2400
markets_skipped_already_covered   2400   (50/cycle)
saved                                0
```

Confirmed directly against the production database: `select_backfill_candidates
(limit=50)` returned 50 markets, 50 of which already had candles, spanning
`KXHIGHNY-26MAY15-*` … `KXHIGHNY-26MAY19-*`.

Corroborating signals that were already firing and had been misread as
historical noise:

- observatory `archive_continuity_candle`: an **open, growing** gap
  (`2026-07-23 15:01 .. now`) — the only warning in the set that was not aging
  out;
- `ops health`: `oldest_uncaptured=KXTEMPNYCH-26JUL2120-T75.99 (1d)`;
- settled-market candle coverage since 2026-07-21: 2208/2297 (96.1%), with the
  entire shortfall concentrated in markets that settled after the last manual
  backfill.

### A coverage predicate alone is not sufficient

Excluding covered markets before `LIMIT` fixes the reported defect but
reintroduces the same starvation with a different head. A market that
legitimately returns zero candles (`no_price_data` — a real market that never
traded) is *permanently* uncovered, so it would occupy the head of the
oldest-first queue on every subsequent cycle. On 2026-07-23 a single day
produced 85 such markets; at `limit=50` they would have blocked the queue
outright.

### Why not compare coverage in SQL

`market_snapshots.close_time` is `TIMESTAMPTZ`; `market_candlesticks.period_end`
is a naive `TIMESTAMP`. A `NOT EXISTS` predicate comparing them directly would
have PostgreSQL reinterpret the naive side against the session `TimeZone`,
producing a silently wrong answer whenever that is not UTC — and the unit
suite runs on SQLite, where the required interval arithmetic differs again. A
predicate that disagreed with `backfill_one_market`'s own check by even the
tolerance would re-admit markets into the limit and quietly restore the bug.

## Decision

1. **Filter coverage before `LIMIT`.** `select_backfill_candidates` gains
   `exclude_covered`, `exclude_tickers`, and `period_interval_seconds`. It
   projects `(market_ticker, close_time, max(period_end))` via a grouped left
   join, filters in Python, and only then applies `limit` and hydrates the
   surviving rows.

2. **One shared definition of "covered".** A new `coverage_reaches_close()`
   helper is the single implementation of the rule (latest `period_end` reaches
   `close_time` within `COMPLETENESS_TOLERANCE_SECONDS`, both normalized to
   naive UTC via `to_naive_utc`). Both the candidate query and
   `backfill_one_market` call it, so they cannot drift.

3. **Keep `skip_covered` as a defensive second check**, unchanged in behavior.

4. **Deterministic ordering**: oldest uncovered `close_time` first, ties broken
   by `market_ticker`.

5. **Terminal-outcome exclusion in the sync loop.** `run_price_sync_loop` keeps
   an in-process set of markets that resolved to `no_price_data`, `expired`,
   `unsupported`, or `partial`-with-nothing-saved, and excludes them from later
   cycles. `api_failure` is deliberately excluded from that set so transient
   errors stay retryable. Surfaced as `terminal_excluded` in
   `collector_runs.stats_json`.

6. **The exclusion applies to sweep-shaped runs only.** Naming `--ticker` or
   `--event`, or passing `--no-skip-covered`, opts out, so an operator can
   still inspect or deliberately re-fetch a covered market. `--start`/`--end`
   narrow a sweep and keep the exclusion. Documented in
   `docs/runbooks/price_ingestion.md`.

## Alternatives considered

**Persist per-market attempt outcomes in a new table or column.** The
principled fix for terminal-outcome tracking, and it would survive restarts.
Rejected *for this change*: it requires a migration and a retention/
invalidation design (when does a `no_price_data` verdict expire?), which is a
larger decision than the verified selection defect warrants. The in-process set
needs no migration and its failure mode on restart — re-checking quiet markets
once — is harmless and arguably desirable. Recorded as available future work if
the quiet-market population grows enough to make per-restart re-checking
expensive.

**Order newest-`close_time`-first in the sync loop.** Would sidestep
head-of-queue starvation entirely without any exclusion set. Rejected: it
inverts ADR 0007's risk model, in which the oldest uncovered market is the one
closest to aging out permanently.

**Record a sentinel row for "no data".** Rejected outright: it would make an
HTTP 200 with an empty result indistinguishable from real coverage, corrupting
the archive to solve a scheduling problem.

## Consequences

- The sync loop makes forward progress: a market that settles is reachable
  within one cycle instead of never.
- A sweep run's report no longer lists covered markets as
  `skipped_already_covered`; they are absent. `markets_attempted` on a healthy
  idle cycle is now `0` rather than `50`, and `prices backfill --limit N` now
  attempts up to N markets that actually need work. Explicit `--ticker`/
  `--event` invocations are unchanged.
- Steady-state API volume drops (an idle cycle issues zero requests) while
  useful capture rises; the client throttle and bounded retries are untouched.
- No schema migration. No change to what is written, only to which markets are
  attempted. Candle writes remain append-only and deduplicated on
  `(market_ticker, period_interval_seconds, period_end)`.
- `archive_continuity_candle` becomes a meaningful liveness signal for price
  capture rather than a permanently open warning.

## Follow-up (not addressed here)

The baseline observed candlestick 404s far inside ADR 0007's measured ~67-day
discoverability window — `KXTEMPNYCH-26JUL2120-T75.99` at 1.8 days past close
and `KXTEMPAUSH-26JUL2310-T89.99` at 5.5 hours. `PRICE_OBSERVED_RETENTION_DAYS`
is deliberately **not** changed here; the retention model needs its own
investigation, and until then the ~67-day figure should not be relied on. The
runbook's retention note records this caveat.
