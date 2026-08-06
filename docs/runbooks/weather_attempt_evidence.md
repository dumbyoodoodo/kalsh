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

---

## Operator tooling (added 2026-08-05)

> Migration 0012 is still **not** applied to production. Every command below
> reports the pre-deployment state; none of them deploys anything.

### `weather attempts validate`

READ-ONLY. Checks schema availability, per-row integrity, lineage, provenance,
and reconciliation over recent rows.

Before deployment it prints:

```
ATTEMPT ATTRIBUTION NOT DEPLOYED — SCHEMA 0012 REQUIRED
status: ATTEMPT_ATTRIBUTION_NOT_DEPLOYED
```

That is the **expected** state, not corrupted data. Options: `--json`,
`--limit`, `--fail-on-warning`.

### `weather attempts summary --station SEA --start <UTC> --end <UTC>`

READ-ONLY per-station rollup. Options: `--product-type`, `--json`,
`--fail-on-unknown`. Rejects an unknown station, a naive timestamp, or
`end <= start` **before** any query (exit 2).

Absent rows are never a known zero — a pre-ledger window is `LEGACY_UNKNOWN`,
a station with no rows is `EVIDENCE_MISSING`. Unattributable cycle aggregates
are shown separately as *platform* uncertainty, never charged to a station.
A future `--end` is accepted purely as a query boundary; no evidence is
fabricated for a period that has not happened.

### `weather attempts reconcile --collector-run-id <id>`

READ-ONLY exact reconciliation of one run: missing pairs, unexpected pairs,
duplicate logical keys, outcome totals, legacy-counter mapping. Exits 1 on
failure. Options: `--json`, `--strict`.

**There is no update, delete, repair, or backfill command.**

### Statuses and exit codes

`ATTEMPT_ATTRIBUTION_NOT_DEPLOYED` · `ATTEMPT_EVIDENCE_VALID` ·
`ATTEMPT_EVIDENCE_WARNING` · `ATTEMPT_EVIDENCE_INVALID` ·
`RECONCILIATION_PASS` · `RECONCILIATION_FAIL` · `LEGACY_UNKNOWN` ·
`EVIDENCE_MISSING`.

`0` success · `1` CRITICAL findings, reconciliation failure, or a warning/unknown
under `--fail-on-warning`/`--fail-on-unknown` · `2` invalid arguments or a
refused operation.

JSON output uses sorted keys, stable enums, and UTC timestamps, and contains no
database URL, credentials, raw payload bodies, or performance fields.

## Observatory findings

`observatory/attempts.py` reports through the existing `Finding`/`Severity`
contract and defines no new vocabulary.

**Severity mapping.** The spec names four levels; this repo's observatory is
three-valued and its roll-up depends on that, so **ERROR maps onto CRITICAL**.
Both mean "an integrity violation an operator must resolve". WARNING and INFO
are unchanged.

- **CRITICAL** — conflicting duplicate logical identity, environment mismatch,
  foreign collector-run lineage, provenance corruption, missing expected
  attempt, counter mismatch, invalid target local date, impossible count
  combination, unrecognized stage/outcome.
- **WARNING** — bounded `SOURCE_UNAVAILABLE`, `REQUEST_FAILED` with intact
  provenance, station-specific parser rejection, an *identical* duplicate
  (idempotency artifact, not corruption).
- **INFO** — pre-deployment absence, schema-revision drift, successful
  no-data.

**Pre-deployment absence never pages.** The canonical
`unexpected_schema_change` finding already reports the drift; a second CRITICAL
here would train operators to ignore both.

**Completed runs only.** A run is judged once it has a completion marker plus a
5-minute grace covering the run-record write. An active or just-finished run
reports `incomplete_terminal_attempt_set` at INFO — reconciliation is
*deferred*, never reported as passing.

---

## Deployment anchor (added 2026-08-05)

> **No production deployment anchor exists.** The extension state is still
> `WAITING_FOR_ATTEMPT_ATTRIBUTION_DEPLOYMENT`, and no real
> `deployment_validated_at` has been chosen.

`deployment_validated_at` is the hinge the whole extension hangs on: it fixes
the first included local date and therefore the review gate. If it can be
guessed, backdated, or half-satisfied, the extension window is not evidence —
it is a preference.

**It must never be chosen manually or backdated.** It is the moment the LAST
required condition became true, and validation enforces
`deployment_validated_at == final_condition_confirmed_at`. There is no
override, force, or manual-anchor path anywhere in the API.

Three plausible shortcuts are each explicitly insufficient:

- a **migration timestamp** alone is not an anchor,
- a **collector start timestamp** alone is not an anchor,
- a **partially completed cycle** is not an anchor.

### Required conditions

Registration id and hash must match `STATION-PILOT-EXT-0001` exactly; migration
revision `0012`; `deployed_commit == collector_loaded_commit`;
`collector_git_dirty` false; a PID present and exactly one collector confirmed;
the validated cycle complete; `expected_attempts == actual_attempts`;
reconciliation `PASS`; raw-payload provenance `PASS`; zero critical observatory
findings; ordering consistent (`final_condition ≥ cycle completion`, anchor not
before migration or collector start, `recorded_at ≥ anchor`); at least one
evidence reference; deterministic content hash.

### Append-only policy

`data/operations/station_pilot_extension_deployments.jsonl` — a separate ledger
from the research ledger and the permanent gap ledger, because this records an
*operational* deployment fact, not a research result or a data-loss
classification. Duplicate deployment ids are refused; a **second anchor for the
same registration is refused outright** (ADR 0024 registers one extension, and
a second anchor would silently re-date the window); a malformed ledger blocks
any append; there is no update, delete, or overwrite.

### Commands

```
research station-pilot-extension deployment status
research station-pilot-extension deployment preview --deployment-validated-at <ISO>
research station-pilot-extension deployment validate-file --file <record.json>
```

All three are read-only. `preview` is **arithmetic only** — it prints
`PREVIEW — NOT A RECORDED DEPLOYMENT`, queries no production data, appends
nothing, and changes no state. `validate-file` reports every failed invariant
and never appends. **No append command exists yet**; recording the anchor
belongs to the deployment task.

### Extension calendar

Derived entirely by the frozen ADR 0024 logic — no timezone or gate arithmetic
is duplicated, so the module and a recorded anchor cannot diverge. Per station:
the first local date whose midnight is **strictly after** the anchor, seven
consecutive dates, and that date's UTC end. The review gate is the **latest**
seventh-date end across SEA/PHX/MIA. Resulting state: `COLLECTING_EXTENSION`.

Production deployment remains pending.

---

## Production integration (2026-08-05)

`weather attempts reconcile` now loads real rows: the collector run, its
environment and completion state, every terminal attempt for that run, and the
legacy cycle counters. Expected station/product pairs are derived from the
**active station registry** and the instrumented products — never assumed from
a remembered count. The reconciliation rules stay in the evidence layer; the
CLI only loads and renders. An incomplete run reports
`RECONCILIATION_DEFERRED` rather than a false pass or fail.

Attempt findings are contributed to the **main** `ops observatory` report, so
they surface in the canonical operator view rather than only via
`weather attempts validate`. The schema probe uses SQLAlchemy's inspector
rather than `information_schema`, because the observatory's own tests run
against SQLite.

Pre-deployment the main report shows exactly two attempt findings, both INFO,
alongside the one canonical `unexpected_schema_change` CRITICAL — **zero**
duplicate paging-level alerts for the same fact.

---

## Inert-deployment defect and forward fix (2026-08-06)

### The defect

Migration 0012 deployed cleanly and the collector started on `bf8d0ca`. Weather
run **3290** then completed successfully (`success=true`) and recorded **0 of 14
expected attempts**. Attribution was deployed and inert.

Root cause: `run_weather_collector_loop` called

```python
stats = await run_weather_collection_cycle(
    provider, session, stations=stations, backfill_days=backfill_days)
```

without `attempts=` or `collector_run_id=`, so `_record_attempt` short-circuited
and nothing was ever appended. The tests passed because they called the cycle
directly *with* those parameters — nothing asserted the production call site
supplied them.

### Architecture

`collector_run_id` genuinely does not exist during collection: the run record is
written after the cycle, in a separate session scope. So the cycle now produces
**`PendingWeatherCollectionAttempt`** objects, which have **no
`collector_run_id` field at all** — no `0`, no `""`, no sentinel that could
reach the database. `materialize(run_id, created_at)` binds a real, persisted id
and is purely additive: outcome, counts, stage, availability, identity and
timestamps are carried through unchanged.

`run_weather_collection_cycle` now **returns** a `WeatherCycleResult` (stats +
pending attempts + expected pairs) instead of accepting an optional sink.
`attempts=None` can no longer silently mean "record nothing" — a caller cannot
forget to opt in.

### Transaction semantics

One `session_scope` owns phase 2: the run record is flushed first so the FK
target exists, every attempt is appended, then the scope commits. A failure
anywhere rolls the whole scope back, so a failed run insert leaves no orphan
attempts and a failed append never commits a run that looks fully attributed.

Business weather data committed in phase 1 is **not** rolled back. Collection
succeeding while its evidence fails is a real, separately-visible state — not
something to conceal.

### Attempt-persistence failure

If fewer attempts persist than expected, `AttemptEvidenceIncomplete` is raised
*inside* the persistence scope, rolling it back and logging
`weather_collector.attempt_evidence_failed`. The cycle's business data stands;
the evidence does not. Reconciliation fails and no anchor can use that cycle.

### Zero-row completed runs are now CRITICAL

Previously a completed post-deployment run with zero attempts could still report
`ATTEMPT_EVIDENCE_VALID`. Now
`completed_weather_run_missing_all_attempt_evidence` (and its partial variant)
is **CRITICAL**, and CLI validate returns `ATTEMPT_EVIDENCE_INVALID`.

The boundary is a **recorded marker**, not a timestamp guess: the fixed
collector writes `attempt_instrumentation_version` into
`collector_runs.stats_json`. Only runs carrying it are judged. Pre-fix runs have
no marker and stay `LEGACY_UNKNOWN`.

### Run 3290 — no backfill

Run 3290 is preserved as genuine operational evidence of the defect. It is never
backfilled, deleted, or rewritten. It carries no instrumentation marker, so it
is legacy/unattributed rather than a recurring integrity failure, and it can
**never qualify as an anchor cycle**.

### Status

Superseded by the forward-fix deployment below. Attribution **is** active in
production as of 2026-08-06.

---

## Forward-fix deployment (2026-08-06)

### What is running

| | |
|---|---|
| Collector commit | `28cf598bc2900739ca39f817aa7c6c6f26f4b1e1` |
| Collector PID | 22288 (started 2026-08-06T04:21:17.946426Z) |
| Migration revision | 0012 (unchanged; no DDL applied during this deployment) |
| `deployment_validated_at` | **2026-08-06T05:02:25Z** |
| Anchor cycle | **3296** (first qualifying post-fix cycle) |
| Second verification cycle | **3303** |
| Deployment ID | `SPX-EXT-DEPLOY-0001` |
| Extension state | `COLLECTING_EXTENSION` |

The collector was stopped with `launchctl bootout` (graceful, 21s, zero
processes, no respawn) and restarted with `launchctl bootstrap`. Only the
collector agent was touched; logrotate, monitor, backup and heartbeat stayed
loaded, and PostgreSQL was never restarted.

Both cycles recorded **14 of 14** expected station/product attempts — 7 active
stations × 2 instrumented products, derived from the live registry rather than
assumed — with `RECONCILIATION_PASS`, 14/14 raw-payload links, and zero CRITICAL
observatory findings.

### Extension calendar

`deployment_validated_at` is the moment the **last** required validation
completed, not cycle completion and not a rounded midnight. The first included
date per station is the first station-local date whose local midnight falls
strictly after it, so a partially instrumented deployment date is excluded —
which is why MIA starts a day later than SEA and PHX.

| Station | Timezone | Deployment local | First date | Seven included dates | Seventh-date UTC end |
|---|---|---|---|---|---|
| SEA | America/Los_Angeles | 2026-08-05T22:02:25−07:00 | 2026-08-06 | 08-06 … 08-12 | 2026-08-13T07:00:00Z |
| PHX | America/Phoenix | 2026-08-05T22:02:25−07:00 | 2026-08-06 | 08-06 … 08-12 | 2026-08-13T07:00:00Z |
| MIA | America/New_York | 2026-08-06T01:02:25−04:00 | 2026-08-07 | 08-07 … 08-13 | 2026-08-14T04:00:00Z |

**Extension review gate: 2026-08-14T04:00:00Z** (the latest of the three
seventh-date ends). Do not run the review before it. The future command is:

```bash
uv run kalshi-weather research station-pilot-review
```

### Run 3290 remains historical unattributed evidence

The deployment did not touch it. It is still `success=true`, marker-free, with
zero attempt rows and `stats_json` md5 `4ea8a5f26d191d2666cfba6951bf248f`. It
reconciles as `RECONCILIATION_FAIL` (0 of 14) when asked directly, raises no
CRITICAL because it carries no instrumentation marker, and was never eligible as
an anchor cycle.

### Live failure paths unobserved

Every one of the 28 recorded attempts is `SUCCEEDED_DUPLICATE` /
`NORMALIZED_PERSISTED` / `AVAILABLE`. No live request failure, parser rejection,
source unavailability, or persistence failure occurred during the validation
window, so those paths remain **synthetically verified only**. Treat the first
real failure as the moment they are proven, not as a regression.

### `migration_applied_at` is a bound, not an instant

`track_commit_timestamp` is off, so the exact moment 0012 was applied is not
recoverable. The recorded value `2026-08-06T03:54:12Z` is the *confirmed-applied-by*
bound: the migration ran inside the prior maintenance window, bracketed by
2026-08-06T03:51:49Z (last pre-migration collector run finishing) and
2026-08-06T03:54:12Z (prior collector process starting against an already-0012
database).

### Reconcile was broken on arrival

`weather attempts reconcile` selected `collector_runs.environment`, a column that
has never existed, so it raised `UndefinedColumn` the first time it ran against
production. Every guard around it was a source-level string assertion over the
CLI body; none compared the SQL to the schema, so the full suite passed with the
command unusable. Fixed in `34e8780`, which also adds a guard that parses the
selected columns and checks them against the mapped models.

That commit touches only `cli.py` and its test — no collector-runtime module —
and PID 22288 had already loaded `28cf598` into memory, so the running
collector's behaviour is exactly the deployed commit. The on-disk tree is
therefore one commit ahead of the running process in operator tooling only; the
next routine restart picks it up with no change to collection.
