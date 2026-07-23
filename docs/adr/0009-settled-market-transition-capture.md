# ADR 0009: Settled-market transition capture

## Status

Accepted (2026-07-23).

## Context

The historical collector discovers markets with an open-status filter
(`COLLECTOR_MARKET_STATUS=open`, `ingestion/discovery.py`): each cycle lists
the series in the configured category, their events, and their **open**
markets, and snapshots each. This is correct for live order-book/trade
collection — an already-settled market has no live book to poll — but it has
a structural blind spot for the *settlement transition itself*.

A market leaves the open list the moment Kalshi closes it, which happens
*before* its final, result-bearing snapshot (the row carrying `result` ∈
{`yes`,`no`}, `expiration_value`, and `settlement_ts`) is ever captured.
Everything downstream that keys off "the latest snapshot has a result" then
starves:

- settled-market snapshots (never recorded),
- settlement metadata (`result`/`expiration_value`/`settlement_ts`),
- price-sync/candlestick ingestion (candidacy is derived from settled markets),
- settlement-label generation (`settlement/labels.py`),
- price retention monitoring.

This was not hypothetical. During the 2026-07-23 production-gap investigation,
settled-market snapshot capture had stalled for every market settling after
2026-07-21: only the ~12 registry (mapped-station) markets per day were being
captured — via a separate path — while ~1000+/day of the full discovered set
transitioned to settled entirely uncaptured. Candle ingestion for those
markets stalled with it.

## Problem

Recover the final result-bearing snapshot for a market that has already left
the open-discovery list, without:

- re-fetching the entire historical settled archive every cycle (unbounded,
  abusive to the API, and pointless — most settled markets are long done),
- adding process state or a separate scheduler,
- creating duplicate snapshots/candles/labels,
- letting one bad ticker abort the collection cycle,
- losing point-in-time provenance.

## Decision

Add a bounded, data-derived **settled-transition capture** pass
(`ingestion/settlement_sync.py`, `capture_settled_transitions`) that runs
after the per-market discovery/order-book/trade work in each Kalshi
collection cycle (`ingestion/collector.py`).

1. **The pending queue is derived purely from stored data, not from process
   state.** `find_tickers_pending_settlement_check` selects tickers whose
   *latest* stored snapshot (a) carries no settlement `result` yet, (b) was
   observed within `COLLECTOR_SETTLE_CHECK_DAYS` (default 7), and (c) is
   **absent** from the current cycle's open-discovery set. That is exactly
   "recently tracked, now gone from the open list, settlement not yet
   captured." Ordering is oldest-`close_time`-first, mirroring price-sync's
   retention-priority ordering.

2. **Work is bounded per cycle.** At most `COLLECTOR_SETTLE_CHECK_LIMIT`
   (default 25) tickers are re-checked per cycle, one `get_market` call each.
   The recency window (b) means a market that vanishes without ever exposing a
   settlement (delisted/expired-unsettled) ages out of the queue instead of
   being retried forever. Together these bound the queue's size and the
   per-cycle API cost, and guarantee the full historical settled archive is
   never re-fetched.

3. **The pass is idempotent and self-draining.** Each captured market is
   persisted through the *same* `persist_market_snapshot` helper that open
   discovery uses (extracted so the two paths cannot drift), which routes to
   `save_market_snapshot`'s content-hash dedup. Capturing a settled snapshot
   removes the ticker from the queue on the next cycle (its latest snapshot now
   bears a result); a re-check of an unchanged market is a content-hash no-op
   (`was_duplicate`). Retries are therefore naturally idempotent — no separate
   dedup bookkeeping.

4. **Per-ticker failures are isolated.** A 404 for a delisted market or a
   transient API error is logged (`collector.settle_capture.failed`) and
   counted (`SettleSyncStats.errors`), never raised — one bad ticker must not
   abort the cycle, matching the collector's existing error-isolation
   contract. The whole pass is additionally wrapped so a pass-level failure
   is logged/counted, never fatal.

5. **Provenance is preserved by construction.** Captured snapshots carry the
   raw payload id (`raw_api_payloads`), the source `updated_time`, and
   `settlement_ts` exactly as `save_market_snapshot` records for any snapshot;
   nothing is synthesized.

## Alternatives considered

- **Widen discovery to `COLLECTOR_MARKET_STATUS=settled` / all statuses.**
  Rejected: it would re-list and re-snapshot the entire settled archive every
  cycle (thousands of markets, most long-final), for no benefit, and would
  pull settled markets into the order-book/trade loop that has nothing live to
  fetch for them.
- **A separate scheduled "settlement sweep" job.** Rejected: it adds a second
  scheduler and process-state to reason about; the collection cycle already
  has the open set in hand (needed to compute "absent from open"), so the
  check belongs there. No new launchd agent.
- **A persisted queue table of "markets awaiting settlement".** Rejected:
  process state that can desync from reality. The "latest snapshot has no
  result yet" predicate over `market_snapshots` *is* the queue, derived fresh
  each cycle — no new table, no migration, no drift.
- **Unbounded retry of every no-result ticker.** Rejected: a delisted/expired
  market that never settles would be re-fetched forever; the recency window +
  per-cycle limit bound both the queue and the API cost.

## Operational consequences

- Each Kalshi cycle now issues up to `COLLECTOR_SETTLE_CHECK_LIMIT` extra
  `get_market` calls. Bounded and subject to the same client-side request
  pacing as all other calls; negligible against the ~1000-market discovery
  pass.
- `CycleStats` gains `settle_checks`, `settled_captured`, `settle_errors`,
  logged in `collector.cycle_complete`. A cycle showing `settled_captured>0`
  is the queue draining a backlog; steady-state `settled_captured=0` with
  `settle_checks>0` is normal (the queue is checking recent no-result tickers
  that have not settled yet).
- No migration. No new table. No new scheduler/agent. Behavior on a fresh
  checkout is active by default (defaults 25/7) — the fix is not gated on
  machine-specific config.

## Retention / backfill implications

- The pass captures settlements **going forward** for markets still inside the
  recency window. Markets that settled and left the open list *before* the fix
  deployed, and are now older than the window, are not reached by the
  steady-state pass — those were recovered by a one-time bounded backfill (the
  same `get_market` → `persist_market_snapshot` path, applied to the affected
  Jul-21+ tickers), which is idempotent against the steady-state pass by the
  same content-hash dedup.
- Feeding newly-settled markets into candle sync is deferred to the existing
  price-sync loop and `prices backfill` (ADR 0007): once a market has a
  result-bearing snapshot, it becomes a price-sync candidate normally. Markets
  with zero lifetime volume (quote-only, no trades) legitimately have no
  candles and are not a capture failure.
- The permanent 2026-07-21..22 collector-outage gaps in the *live* snapshot
  history are not recoverable and are documented as permanent
  (`ROADMAP.md`); this capture path only prevents the *forward* recurrence of
  settlement starvation, it does not manufacture history that was never
  collected.
