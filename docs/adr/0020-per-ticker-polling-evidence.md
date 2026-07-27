# ADR 0020: Append-only per-ticker polling-evidence ledger

## Status

Accepted (2026-07-27). Additive migration 0010 -> 0011. Extends observed
availability (ADR 0019). Prospective-only; no historical backfill. Read/write to
one NEW table only; H0018/H0019 untouched.

## Context

`collector_runs` proves only that the collector ran a cycle COLLECTOR-WIDE. It
cannot prove a SPECIFIC ticker was eligible, requested, succeeded, returned
UNCHANGED content, failed, was rate-limited, or was skipped. Order-book and
market snapshots dedup unchanged content, so a successful poll that returned an
identical book leaves NO row -- indistinguishable, from the snapshot tables
alone, from a ticker that was never polled. ADR 0019 therefore had to INFER
per-ticker availability (LIKELY_OBSERVED) from collector-wide uptime. Direct
per-ticker evidence removes that inference for future cycles.

## Audit (what exists vs what is missing)

- `collector_runs`: per-cycle, collector-wide (started/finished, success,
  requests, retries, aggregate `stats_json`). No per-ticker list.
- The cycle polls order-book and trades PER TICKER (`get_orderbook`,
  `list_trades`); each ticker is isolated by its own try/except. `save_*`
  returns `was_duplicate` (dedup), and the client exposes `last_raw_payload_id`,
  `source_environment`, cumulative `retries`/`rate_limit_hits`. A successful API
  response CAN produce zero new rows (dedup). Rate limits surface as
  `KalshiAPIError(429)` after the client's internal retries.
- The `collector_runs.id` is created AFTER the cycle in a separate best-effort
  session, so evidence written DURING the cycle cannot FK to it.

## Decision

New append-only table `market_poll_attempts` (one row per logical ticker-endpoint
attempt) with an explicit taxonomy (`ingestion/poll_ledger.py`): eligibility
state, attempt state, and an outcome in {succeeded_new_data, succeeded_unchanged,
succeeded_empty, skipped_not_eligible, skipped_policy, rate_limited, api_failure,
malformed_payload, persistence_failure, unsupported, cancelled, unknown_failure}.
Environment provenance, raw-payload FK, bounded/sanitized error detail, retry
count, rate-limited flag, persisted-row count, and deduplicated flag are recorded.

**Retry representation.** ONE logical row per collector decision, with a
`retry_count` = the client's internal HTTP retries for that one request (snapshot
the cumulative counter before/after). The collector makes one logical decision
per ticker per endpoint; HTTP retries are an internal client concern. One row per
logical attempt keeps the ledger aligned with collector decisions and avoids
double-counting -- the simplest auditable model.

**Transaction model.** Evidence is BUFFERED in memory during the cycle (recording
the actual outcome of each `save_*` / request), then written WITH the cycle's
`collector_runs` record post-cycle (the run id only exists then) inside a
SAVEPOINT, so a ledger-write failure can never abort the operational run record.
Evidence is written only when the cycle committed its data (`run_error is None`);
a whole-cycle failure rolled the data back, so per-ticker success claims would be
false and collector-wide unavailability is represented by the run row instead.
This guarantees: a persisted snapshot never yields `persistence_failure`, and a
failed persistence never yields `succeeded_new_data`. `collector_runs` keeps its
insert-once write pattern (unchanged).

**Why unchanged != missed.** `succeeded_unchanged` (deduplicated=true,
persisted_row_count=0) still PROVES the ticker was observed. The availability
builder now marks such cycles OBSERVED with DIRECT evidence, where before they
were only LIKELY_OBSERVED (inferred).

**Availability integration.** For a ticker with any 0011+ evidence, the builder
prefers it: successful polls are direct OBSERVED points; failed/rate-limited
polls downgrade their interval to UNKNOWN (evidence DIRECT_POLL_FAILED) so they
break passive-fill continuity. Pre-0011 periods stay legacy/inferred -- history
is NEVER reinterpreted as direct evidence. Timelines record the evidence source
and schema era per ticker.

## Storage growth

At the current cadence (~120 cycles/day) and ~1000 polled weather tickers/cycle,
2 endpoints -> ~2,000 rows/cycle, ~243k rows/day, **~7.3M rows/month** (~1.4 GB
data + ~2x for the three indexes). Append-only; only ACTUAL attempted tickers are
recorded (no speculative skip rows). No retention deletion in this task. If
growth becomes a problem, endpoint scope or a coarser eligible-set summary is the
lever -- but evidence required for replay (per-ticker observed/failed) must not be
aggregated away.

## Consequences

- Future replay confidence improves: unchanged polls count as OBSERVED with
  DIRECT proof; per-ticker failures/rate-limits break continuity precisely.
- Backfill is PROHIBITED: the table is empty until the collector next runs on a
  0011 database; pre-0011 availability remains inferred and is labelled as such.
- Known limitations: market_snapshot/settlement/metadata endpoints are recorded
  via the same ledger when later instrumented (this change covers the two genuine
  per-ticker request endpoints, orderbook and trades); the ledger grows steadily
  (retention is a separate future decision).
