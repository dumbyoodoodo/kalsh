# ADR 0024: SEA/PHX/MIA station-pilot extension (attempt-attribution window)

Date: 2026-08-04
Status: Accepted
Operator: Wilson Tu
Registered at: **2026-08-04T03:34:30Z**
Registration id: `STATION-PILOT-EXT-0001`

## Context

The original pilot (ADR 0023) ran 2026-07-27T21:55:00Z → 2026-08-04T00:00:00Z
and was reviewed at its registered gate. Its verdict is
**`PILOT_COLLECTION_EXTENSION_REQUIRED`**, recorded immutably in
[`docs/research/station_pilot_review_2026-08-04.md`](../research/station_pilot_review_2026-08-04.md)
(sha256 `3df31e08ce12ec7fbe3113dde0ef98e7ed9d9d564685456bde08870f5b947c09`).

That verdict was **not** a station defect. Each of SEA, PHX and MIA showed 7/7
complete observation dates, both tmax and tmin present, zero duplicates, zero
provenance orphans, zero parser failures, zero timezone/date mismatches, and
forecast issuances above expectation. The extension was forced by a platform
evidence limit: `WeatherCycleStats` records only cycle-level scalars, so
`invalid_items` and `errors` carry no station dimension. A parser rejection or
an unavailable source therefore could not be attributed to the station that
caused it, `parser_failures` and `source_unavailable_attempts` were marked
`evidence_unavailable`, and KEEP was unreachable **by construction**.

Successes were already attributable — `weather_observations` rows carry
`station_id`. It is failures and non-events that leave no trace. Re-running the
same seven days would have produced the same blocked result.

## Decision

Register a **seven-complete-local-date** operational extension, beginning
prospectively only after station-level weather-attempt evidence is deployed and
its first complete production cycle has reconciled.

This is a **new observation period, not a replacement**. ADR 0023's pilot and
the August 4 artifact are not revised, superseded, regenerated, or
reinterpreted.

### Why it is registered now

**No station-level attempt outcome existed anywhere when this was frozen.**
Migration 0012 is not applied, the `weather_collection_attempts` table does not
exist in the production database (verified: 0 matching rows in
`information_schema.tables`), and no attempt row has ever been written. The
window length, start rule, review gate, and decision criteria therefore cannot
have been selected in response to observed failure counts. A window chosen
after seeing outcomes is not evidence; it is selection.

## Deployment anchor

`deployment_validated_at` is the UTC timestamp at which **all** hold:

- migration `0012` is applied
- the collector is running the attempt-attribution commit
- exactly one collector process exists
- one full weather cycle has completed
- every expected station/product attempt row exists
- attempt reconciliation passes
- raw-payload provenance checks pass
- no critical observatory finding is active

It **must be recorded from deployment evidence** by a separate, separately
reviewed task. It is deliberately **not a date in this document**: hard-coding
a start before deployment would either exclude real collection or admit a
partially instrumented day.

## Included dates

For each station, **seven consecutive complete station-local dates**.

The first included date is the first station-local calendar date whose **local
midnight occurs strictly after `deployment_validated_at`**. The deployment day
itself is always excluded — it was only partially instrumented.

Station timezones (unchanged from ADR 0023):

| Station | Timezone | Note |
|---|---|---|
| SEA | `America/Los_Angeles` | observes DST |
| PHX | `America/Phoenix` | **fixed UTC−7, never `America/Denver`** |
| MIA | `America/New_York` | observes DST |

## Review gate

The **latest** UTC end boundary among the three stations' seventh included
local date — i.e. `max` over stations of the local midnight ending that
station's seventh date, converted to UTC. The maximum, not the minimum: a gate
at the earliest station would review a station whose seventh local date had not
finished.

No review before that instant. `--as-of` may not exceed the real clock, and
there is no bypass flag.

## Evidence cutoff

Only evidence with `observed_at <= review_as_of` may be used. Data recovered
later carries a historical issuance time but was **not** contemporaneously
available and must not be treated as such.

## Frozen review dimensions

Observation completeness · forecast continuity · station-level request outcomes
· parser health · source availability · persistence integrity · provenance
integrity · timezone and local-date correctness · outage-adjusted collection
reliability.

**Forbidden, under every outcome:** forecast error, Brier score, calibration,
market prices, liquidity, P&L, expected value, profitability, station ranking,
hypothesis support.

## Frozen evidence requirements

Per station, the extension review requires: seven complete included local
dates; station-level attempt records for every expected station/product
request; exact collector-run linkage; a terminal outcome for every logical
attempt; raw-payload linkage wherever a response body existed; passing cycle
reconciliation; no unattributable cycle aggregate inside the window; and no
unresolved station-specific provenance defect.

Evidence states remain `KNOWN_ZERO` / `KNOWN_NONZERO` / `LEGACY_UNKNOWN` /
`EVIDENCE_MISSING`. Inside the extension window **`LEGACY_UNKNOWN` and
`EVIDENCE_MISSING` are not acceptable** — eliminating them is the entire
purpose of the extension. Either results in `EXTEND_COLLECTION` or
`REVIEW_BLOCKED` under the existing integrity rules.

## Frozen decision rules

Precedence unchanged: `REVIEW_BLOCKED` > `REMOVE_FOR_DATA_QUALITY` >
`EXTEND_COLLECTION` > `KEEP`.

- **KEEP** — seven complete dates; required tmax/tmin present per existing
  completeness policy; expected attempts fully attributable; no persistent
  station-specific parser failure; no station-specific persistence failure; no
  mapping or timezone defect; no provenance integrity failure; missingness
  explained only by confirmed source or host evidence; evidence sufficient to
  decide.
- **EXTEND_COLLECTION** — fewer than seven complete dates; incomplete
  station-level evidence; an operational outage preventing attribution;
  isolated failures needing more collection; inconclusive evidence.
- **REMOVE_FOR_DATA_QUALITY** — only for a confirmed persistent
  station-specific defect.
- **REVIEW_BLOCKED** — invalid evidence ledger; failed reconciliation;
  contradictory mapping; provenance corruption; missing required registration
  metadata.

Overall verdict aggregates deterministically: any `REVIEW_BLOCKED` →
`PILOT_REVIEW_BLOCKED`; else any `REMOVE_FOR_DATA_QUALITY` →
`PILOT_DATA_QUALITY_FAILURE`; else any `EXTEND_COLLECTION` →
`PILOT_COLLECTION_EXTENSION_REQUIRED`; else all `KEEP` →
`PILOT_OPERATIONALLY_ACCEPTABLE`.

**No new threshold may be introduced after outcomes are observed.**

## Registry-change prohibition

This registration mutates no station registry and authorizes no station
addition or removal. Any registry change — keep-as-is, extend, or removal —
requires a **separate explicit human approval** after reading the extension
review artifact.

## Executable form

`src/kalshi_weather/research/station_pilot_extension.py` (pure; no database, no
wall clock, no I/O). Registration id `STATION-PILOT-EXT-0001`, with a
deterministic `registration_hash` over every frozen field.

## Consequences

Nothing is observed until deployment. The next task resumes and completes the
station-level attempt-attribution implementation, deploys it, records
`deployment_validated_at`, and only then does the extension window begin.
