# Runbook: permanent collection-gap ledger

> **A gap ledger records operational evidence. It does not decide whether
> excluding data improves a research result.**

Established 2026-08-03. Implementation
`src/kalshi_weather/data_quality/gap_ledger.py`; records
`data/quality/permanent_gap_ledger.jsonl` (version-controlled, append-only).

## Purpose

Collected history is irreplaceable, but *absence* in that history is
ambiguous. A missing row can mean any of:

- the source never issued the product
- the host was off
- the upstream API was unreachable
- a request failed
- the parser rejected the payload
- persistence failed
- discovery never listed the market

Those are different facts with different research consequences, and the
distinction is only cheaply recoverable **while the operational evidence is
still at hand**. This ledger captures it then — before any experiment outcome
that might be tempted to use it exists.

The ledger answers: what interval, which subsystem, what evidence, was the
source available, was the collector running, was it later recovered, is it
usable / excludable / annotatable / unresolved, when was that decided, and has
a later append-only amendment changed it.

The ledger **must never** answer: whether excluding a gap improves a metric,
whether a station looks better without it, whether a hypothesis becomes
supported, whether a test window should move, or whether a market outcome is
favourable.

## The rule that motivated the design

Four 2026-07 weather gaps (LAX 07-17, MIA 07-16, NYC 07-02, PHX 07-24) were
initially recorded as source gaps in the 2026-07-28 data-quality audit, purely
because local rows were absent. That was **wrong**: the NWS had issued every
product, and a parser defect (trailing record flags, fixed in `1662ef8`) had
dropped them. All four were later fully recovered.

Hence: **missing local rows never by themselves prove the source was silent.**
`SOURCE_GAP` requires positive source evidence *and*
`source_availability=CONFIRMED_NOT_ISSUED`; validation enforces both. Without
that evidence the honest classification is a collection gap or
`AMBIGUOUS_LEGACY`.

## Schema

One JSON object per line, deterministic key order, sha256 `content_hash` over
every field except the hash itself.

| Field | Meaning |
|---|---|
| `gap_id` | unique, stable, never reused |
| `ledger_version`, `created_at`, `created_by` | provenance of the classification |
| `classification` | primary cause (vocabulary below) |
| `gap_kind` | source / collection / host / upstream / ambiguous-legacy |
| `subsystems` | one or more affected streams (never "everything") |
| `environment` | required |
| `start_at`, `end_at` | UTC; `start_at < end_at` unless `instantaneous` |
| `scope` | station, ticker, or source scope |
| `affected_entity_count` | count only |
| `evidence` | ≥1 `{kind, reference, detail}`; reference is a stable id or re-runnable query |
| `collector_run_refs`, `poll_attempt_refs`, `raw_payload_refs` | stable references |
| `source_availability` | what is known about the upstream source |
| `recovery_state` | what happened |
| `recoverability` | whether it *could* be recovered |
| `research_treatment` | evidence-quality guidance only |
| `confidence` | CONFIRMED / HIGH / MEDIUM / LOW |
| `exclusion_eligible` | LOW confidence may never set this true |
| `reason_code`, `human_note` | stable slug + narrative |
| `supersedes_gap_id`, `related_gap_ids` | amendment and linkage |
| `content_hash` | integrity |

### Controlled vocabularies

**Classification** — `HOST_UNAVAILABLE`, `COLLECTOR_STOPPED`,
`COLLECTOR_CRASH`, `UPSTREAM_KALSHI_OUTAGE`,
`UPSTREAM_WEATHER_SOURCE_OUTAGE`, `SOURCE_PRODUCT_NOT_ISSUED`,
`REQUEST_FAILED`, `PARSER_FAILURE`, `PERSISTENCE_FAILURE`,
`DISCOVERY_OMISSION`, `RATE_LIMIT_TRANSIENT`, `LEGACY_UNKNOWN`,
`PARTIAL_CURRENT_DAY`, `OTHER_CONFIRMED`.

**Gap kind** — `SOURCE_GAP` (source confirmed not to have issued),
`COLLECTION_GAP` (source issued; platform failed to retrieve/parse/persist),
`HOST_GAP` (collector not running), `UPSTREAM_OUTAGE` (collector running,
upstream unavailable), `AMBIGUOUS_LEGACY` (cannot distinguish).

**Subsystem** — `WEATHER_OBSERVATIONS`, `WEATHER_FORECASTS`,
`KALSHI_MARKET_DISCOVERY`, `KALSHI_MARKET_SNAPSHOTS`, `KALSHI_ORDER_BOOKS`,
`TRADES`, `CANDLES`, `PAPER_VALIDATION`, `OPERATIONAL_MONITORING`.

**Recovery state** — `NOT_RECOVERABLE`, `RECOVERABLE_NOT_YET_RECOVERED`,
`PARTIALLY_RECOVERED`, `FULLY_RECOVERED`, `NOT_APPLICABLE`, `UNKNOWN`.

**Research treatment** (evidence quality, never performance) —
`INCLUDE_WITH_ANNOTATION`, `EXCLUDE_FOR_MISSING_COLLECTION`,
`EXCLUDE_FOR_SOURCE_UNAVAILABILITY`, `RECOVERED_USE_AS_OBSERVED_AT`,
`RECOVERED_NOT_CONTEMPORANEOUS`, `INSUFFICIENT_EVIDENCE`, `NOT_APPLICABLE`.

## Evidence requirements

A record may be `CONFIRMED` only when backed by ≥1 of: collector-run history,
poll-attempt history, raw-payload presence/absence, service logs, observatory
findings, source-archive evidence, HTTP status, parser exception, persistence
exception, host/backup evidence, or an existing audit report.

**A human narrative alone is never sufficient for `CONFIRMED`.** Validation
additionally enforces:

- `SOURCE_GAP` needs source evidence *and* `CONFIRMED_NOT_ISSUED`
- `COLLECTION_GAP` / `HOST_GAP` / `UPSTREAM_OUTAGE` need collector-side
  evidence (parser and persistence exceptions count — a payload the platform
  received and then mishandled is collector-side proof)
- a recovered record needs recovery evidence
- `SOURCE_PRODUCT_NOT_ISSUED` cannot be recovered (if data was later obtained,
  the source *did* issue it and the classification was wrong)
- `LOW` confidence can never authorize exclusion

## Subsystem isolation

One stream's outage never implies another's. The canonical case is
`GAP-20260727-KALSHI-UPSTREAM`: 89 consecutive Kalshi cycles failed with
`ConnectError` while the weather collector ran 18 cycles, 18 successful. A
Kalshi outage is **not** a weather-collection fault. Likewise DQ-001 affected
normalized order-book persistence only — the API responses were fine — and a
startup 429 affects one Kalshi discovery cycle, not the weather loop.

## Recovered-data semantics

A recovered row carries two separate facts that must never be conflated: the
**event value and its source issuance time**, and **when the platform actually
obtained it**. A recovered row is *not* contemporaneously available merely
because its issuance timestamp is historical.

- `RECOVERED_USE_AS_OBSERVED_AT` — usable if keyed on `observed_at`
- `RECOVERED_NOT_CONTEMPORANEOUS` — valid for settlement reconstruction and
  descriptive completeness; **invalid** as a point-in-time feature
- `NOT_RECOVERABLE` — permanently missing

`GapLedger.contemporaneous_use_allowed()` returns `False` for the latter two.

## Append-only amendment policy

Records are frozen. Corrections **append a new record** naming
`supersedes_gap_id`; `active_records()` then hides the superseded one. There is
deliberately **no update and no delete command**. Validation rejects duplicate
ids, unknown or self-referential supersede targets, supersede cycles, and
unlinked duplicates (same cause, scope, subsystems, environment, overlapping
interval, with no explicit link). Every load re-verifies `content_hash`, so an
in-place edit of an earlier line is detected.

## Research-integration boundary

`GapLedger` exposes a **pure, read-only** query interface:

```python
ledger = GapLedger.load()
ledger.overlapping(moment, subsystem=Subsystem.WEATHER_OBSERVATIONS)
ledger.contemporaneous_use_allowed(moment, Subsystem.WEATHER_OBSERVATIONS)
ledger.annotations_for(moment, Subsystem.WEATHER_OBSERVATIONS)
```

It changes **no** existing dataset and is wired into **no** experiment. H0019
and H0020 runner behaviour is untouched. Any future experiment integration is a
separate, prospectively reviewed change — never a retrofit onto a frozen
registration.

## Outcome isolation

The schema has no field for metric impact, profitability, station ranking,
hypothesis support, model improvement, or preferred exclusion, and
`GapRecord.from_dict` raises `ForbiddenFieldError` on any key whose snake_case
parts begin with an outcome-shaped stem. An exclusion-oriented
`research_treatment` may rest **only** on operational evidence plus
prospectively defined methodological rules — never on a measured effect.

## Operator commands

```
uv run kalshi-weather data-quality gap-ledger validate          # read-only, exit 1 if invalid
uv run kalshi-weather data-quality gap-ledger list [--subsystem WEATHER_OBSERVATIONS]
uv run kalshi-weather data-quality gap-ledger show --gap-id GAP-...
uv run kalshi-weather data-quality gap-ledger export --json
uv run kalshi-weather data-quality gap-ledger propose --output cand.json --gap-id GAP-...
uv run kalshi-weather data-quality gap-ledger append --file cand.json --confirm
```

Default behaviour is read-only. `propose` writes an editable skeleton and
**never** appends. `append` requires `--confirm`, explicit file input, schema
validation, hash verification, a unique `gap_id`, and a ledger that already
validates; it writes exactly one line and never rewrites an existing one.

## Adding a record

1. Gather read-only evidence; record stable ids or re-runnable queries.
2. `gap-ledger propose --output cand.json --gap-id GAP-<date>-<scope>-<cause>`
3. Fill every field. Choose `gap_kind` honestly — if you cannot prove the
   source was silent, it is not a `SOURCE_GAP`.
4. Set `confidence` to what the evidence supports, not what you would like.
   Bound the interval by what is *evidenced*; **never invent a start time**
   (see `GAP-CLI-RECORD-FLAG-DEFECT`, whose interval bounds observed impact
   only, which is why it is `HIGH` and not `CONFIRMED`).
5. `gap-ledger validate` on a scratch copy, then `append --confirm`.
6. Commit the JSONL. Never edit an earlier line.

## Seeded records (2026-08-03)

Ten confirmed records; high confidence preferred over completeness. Legacy
gaps are **not** exhaustively classified — see "Known unclassified" below.

| gap_id | cause | subsystems | recovery |
|---|---|---|---|
| `GAP-20260726-HOST-LIVE` | HOST_UNAVAILABLE | weather + Kalshi live streams | NOT_RECOVERABLE |
| `GAP-20260726-HOST-BACKFILLED` | HOST_UNAVAILABLE | trades, candles | FULLY_RECOVERED |
| `GAP-20260727-KALSHI-UPSTREAM` | UPSTREAM_KALSHI_OUTAGE | Kalshi only | NOT_RECOVERABLE |
| `GAP-20260717-LAX-CLI-RECORD-FLAG` | PARSER_FAILURE | weather observations | FULLY_RECOVERED |
| `GAP-20260716-MIA-CLI-RECORD-FLAG` | PARSER_FAILURE | weather observations | FULLY_RECOVERED |
| `GAP-20260702-NYC-CLI-RECORD-FLAG` | PARSER_FAILURE | weather observations | FULLY_RECOVERED |
| `GAP-20260724-PHX-CLI-RECORD-FLAG` | PARSER_FAILURE | weather observations | FULLY_RECOVERED |
| `GAP-CLI-RECORD-FLAG-DEFECT` | PARSER_FAILURE (umbrella, HIGH) | weather observations | FULLY_RECOVERED |
| `GAP-20260727-ORDERBOOK-PRICE0` | PERSISTENCE_FAILURE (DQ-001) | Kalshi order books | NOT_RECOVERABLE |
| `GAP-20260803-KALSHI-RESTART-429` | RATE_LIMIT_TRANSIENT | Kalshi discovery | NOT_APPLICABLE |

## Known unclassified

`collector_runs` shows **34** gaps over 2 hours across all collectors. Ten are
classified above. The remainder — mostly the 2026-07-30 → 2026-08-02 cluster
of simultaneous multi-collector stops consistent with host sleep — are **not
yet classified**, because distinguishing host sleep from deliberate shutdown
needs host-side evidence not yet gathered. They must not be assumed benign, and
they must not be assumed source gaps. Classify them in a later pass, one
evidenced record at a time.
