# Runbook: station-level weather collection-attempt evidence

> **Migration 0012 has NOT been applied to production.** The table does not
> exist yet, no attempt row has ever been written, and every window to date is
> `LEGACY_UNKNOWN`. Deployment is a separate, later task.

## Why this exists

`WeatherCycleStats` records seven cycle-level scalars — `stations_processed`,
`observations_saved`, `observations_duplicate`, `forecasts_saved`,
`forecasts_duplicate`, `invalid_items`, `errors` — and **none carries a station
dimension**. In `run_weather_collection_cycle` a per-station exception is caught
and the station *logged*, but only `stats.errors += 1` is persisted;
`_collect_station` does the same for validation rejections via
`stats.invalid_items += 1`.

Consequently the 2026-08-04 SEA/PHX/MIA pilot review had to mark
`parser_failures` and `source_unavailable_attempts` as `evidence_unavailable`,
which **blocks KEEP by construction** for every station. All three stations were
individually clean; the extension was forced by this platform limit.

### What was actually missing

Successes were **already attributable** — `weather_observations` rows carry
`station_id`. It is **failures and non-events** that left no trace:

| Fact | Where it was lost |
|---|---|
| station identity on failure | logged only; `stats.errors` is a scalar |
| product identity on failure | never recorded |
| request attempted at all | no row when a request returns `[]` |
| HTTP status | internal to `NwsProvider._request` |
| retry count | aggregated provider-wide, not per request |
| target station-local date | never stored |
| parser outcome | `invalid_items` scalar, no station |
| persistence outcome | exception → generic `errors` |
| raw payload of rejected input | orphaned; no link from the rejection |
| no-data vs source-unavailable | indistinguishable — both produce nothing |

## Schema

`weather_collection_attempts` (migration 0012), append-only, mirroring the
ADR-0020 `market_poll_attempts` precedent. FKs to `collector_runs` and
`raw_api_payloads`; five indexes; uniqueness on
`(collector_run_id, environment, station_code, product_type, logical_request_key)`.

**Downgrade drops the table.** Safe only while unpopulated — once production
evidence exists this becomes a destructive migration requiring explicit
operator approval.

## Vocabularies

Product (`STATION_METADATA`, `CLI_OBSERVATIONS`, `GRIDPOINT_FORECAST`), stage
(`REQUEST_STARTED` → `RESPONSE_RECEIVED` → `RAW_PAYLOAD_PERSISTED` → `PARSED` →
`NORMALIZED_PERSISTED` → `TERMINAL`), outcome (11 values), source availability
(`AVAILABLE`, `CONFIRMED_UNAVAILABLE`, `UNKNOWN`, `NOT_APPLICABLE`), and
evidence state (`KNOWN_ZERO`, `KNOWN_NONZERO`, `LEGACY_UNKNOWN`,
`EVIDENCE_MISSING`) are **orthogonal**. `SUCCEEDED_NO_DATA` is deliberately
distinct from `SOURCE_UNAVAILABLE`.

## Logical attempts and retries

`logical_request_key` = `run|env|station|product|target_window`. It **excludes
the retry number**: retries mutate one in-memory `_AttemptContext` and produce
exactly **one** terminal row, summarized by `retry_count`. `attempt_id` is a
sha256 prefix of that key, generated **before** network I/O.

### Mixed new + duplicate entities

When one request yields both, `SUCCEEDED_NEW_DATA` wins — new data is the
stronger operational fact — and **both counts are preserved** on the record.
Neither is discarded.

## Invariants (fail closed)

Aware-UTC timestamps; `completed_at`/`observed_at`/`created_at` ≥ `requested_at`
(checked only once all are aware, so a naive value reports rather than crashes);
non-negative counts; `persisted ≤ parsed`, `duplicate ≤ parsed`,
`persisted + duplicate ≤ parsed`; per-outcome rules (notably `PARSER_REJECTED`
**requires** `raw_payload_id`, and `SOURCE_UNAVAILABLE` must **not** have one);
and no credential-shaped text in any stored message.

`sanitize_error` drops a whole line mentioning a sensitive token rather than
substituting inside it — partial redaction of an unknown format is a guess.

## Transaction semantics

The provider persists the raw payload **before** parsing (`_raw_payload_sink`
inside `_request`), so a rejected body is already durable and linkable. Attempt
records are accumulated **in memory** during the cycle and appended by the
caller **after** the cycle's business persistence, in the same session scope —
so a success is never recorded before normalized persistence commits, and
counts reflect committed state. `_record_attempt` never raises into the station
loop: losing one station's weather data to an evidence bug would be strictly
worse than losing the evidence.

A database-wide failure fails the cycle loudly; reconciliation then reports
missing pairs rather than a false clean run.

## Reconciliation

`reconcile_collector_run` requires exactly one terminal attempt per expected
`(station, product)` pair, no unexpected pair, no duplicate logical key, and
matching run id and environment. Legacy mapping is recorded in
`LEGACY_COUNTER_MAPPING`: `invalid_items` maps exactly to
`PARSER_REJECTED + MALFORMED_RESPONSE`; **`errors` counts stations while
attempts count station/product pairs**, so it is checked as a *lower bound*, not
equality. Cycle counters are never rewritten to match.

## Legacy handling

Pre-deployment windows → `LEGACY_UNKNOWN` (`LEGACY_NO_ATTEMPT_LEDGER`). Ledger
present but station absent → `EVIDENCE_MISSING`. Attempts present but fewer than
expected → `EVIDENCE_MISSING` (`INCOMPLETE_ATTEMPTS_IN_WINDOW`), **never**
`KNOWN_ZERO`. No fabrication, no proportional division, no inference of station
failure from missing business rows.

## Station summary contract

`summarize_station_window` returns per-product breakdowns, three evidence states
with counts, raw-payload coverage, collector-run coverage, affected
station-local dates, an evidence reason, and any unattributable cycle aggregate
**reported separately**. Station and product isolation are asserted by test.

## Deployment prerequisites (later task)

Verified backup → apply 0012 → restart collector → observe two full weather
cycles → reconcile exactly → record `deployment_validated_at`. CLI, observatory
findings, and the deployment anchor are separate later tasks.
