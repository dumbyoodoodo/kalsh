# Station-pilot extension: terminal disposition

Operational record of how `STATION-PILOT-EXT-0001` ended, and what must be true
before another prospective window is registered.

## Outcome in one line

The window ran its full registered course and produced **no station-level
conclusion**, because the evidence it existed to collect was never acquired.

```
STATION PILOT EXTENSION
registration: STATION-PILOT-EXT-0001
calendar gate: PASSED
evidence completeness: FAILED
disposition: EXTENSION_INCOMPLETE_COLLECTION
station verdicts: NOT EVALUATED
new registration: NOT CREATED
```

Read it with `uv run kalshi-weather research station-pilot-extension deployment status`.

## Calendar readiness is not evidence readiness

These are different facts and the state machine originally expressed only the
first.

- **Calendar readiness** — the seventh registered station-local date has
  elapsed. `extension_state()` computes this from `deployment_validated_at` and
  the clock, and nothing else. The gate passed at `2026-08-14T04:00:00Z`.
- **Evidence readiness** — the seven complete local dates per station that ADR
  0024 freezes as the review's input actually exist. They do not.
- **Station outcome** — `UNKNOWN`. No station was evaluated.

Before this task, `READY_FOR_EXTENSION_REVIEW` was the terminal calendar state
and could easily be read as "go run the review". Running it would have applied
the frozen station decision rules to an evidence set that was mostly absent,
turning host downtime into a judgement about SEA, PHX and MIA. A recorded
disposition now moves the registration to the terminal state
`EXTENSION_DISPOSED`, so status output cannot imply the review is actionable.

## Why the evidence is insufficient

Frozen requirement (ADR 0024): **seven complete station-local dates per
station**. Observed, against a 30-minute cadence (48 cycles/day):

| Station | Complete dates | Registered range |
|---|---|---|
| SEA | **1 / 7** | 2026-08-06 … 2026-08-12 |
| PHX | **1 / 7** | 2026-08-06 … 2026-08-12 |
| MIA | **0 / 7** | 2026-08-07 … 2026-08-13 |

Three consecutive registered station-local dates — **2026-08-09, 2026-08-10,
2026-08-11** — recorded **zero** weather cycles. (In UTC-date terms the run of
empty days is four; the registered dates are station-local, so three is the
figure that applies here.) Because whole dates are empty, no choice of
completeness threshold changes the determination — this is not a borderline
judgement, and no new threshold was introduced after seeing the data.

The limiting factor is **host/platform availability**, recorded independently in
the permanent collection-gap ledger: 191.15h of weather collection lost between
2026-08-05 and 2026-08-20, of which 139.50h falls inside the extension window.
Six gap records intersect the registered dates. Where the collector did run,
every expected station/product attempt was recorded — so this is not a station
defect, and the disposition asserts nothing about station quality.

## What this disposition is not

It is **not** a station verdict, and it is **not** a negative or failed
experimental result — nothing about the stations was measured. It is an
**evidence-acquisition failure**. The pilot question stays open:

> Whether SEA, PHX and MIA meet the pilot's operational collection-quality bar
> remains UNRESOLVED. No station was evaluated.

## Immutability

The disposition references the frozen objects by hash and rewrites none of them:
the registration hash, the deployment anchor and its `deployment_validated_at`,
the registered date ranges, and the intersecting gap IDs. It lives in its own
append-only ledger at `data/operations/station_pilot_extension_dispositions.jsonl`
— deliberately **not** in the permanent gap ledger, which records collection
loss only. A registration is disposed exactly once; a duplicate or conflicting
disposition is refused rather than superseding the first.

## Before registering another extension

No `STATION-PILOT-EXT-0002` was created, and re-registration is **not** automatic.
Repeating the window on the current host would very likely repeat the outcome.
Minimum prerequisites, recorded in the disposition itself:

- collector hosted on a platform intended to remain continuously available
- exactly one collector process
- service survives unattended operation across a bounded validation period
- no recurring host-sleep collection gaps during that validation period
- weather-attempt evidence complete for every instrumented cycle
- observatory reports no CRITICAL findings

Choosing the infrastructure is a separate decision; this list is about the
property that must hold, not the machine that provides it.

## Separate, still-open issue

`weather attempts validate` has a known query-window mismatch — attempts are
fetched as the most recent 500 rows while runs are fetched over the last 24
hours, so attempts referencing older runs are misreported as
`foreign_collector_run_lineage` and the command can return
`ATTEMPT_EVIDENCE_INVALID` on healthy data. That verdict was **not** relied on
here; completeness was computed directly from `collector_runs` and
`weather_collection_attempts`. Fixing it is its own task.
