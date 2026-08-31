# DESIGN-0001 — Should this project support Weather Company-settled temperature markets?

Status: **OPEN — decision required.** Opened 2026-08-31.
Type: **scope/design decision.** This is **not** an experiment, **not** a
hypothesis, and **not** an H0019 successor.

## Why this exists

On approximately **2026-08-14** Kalshi migrated daily-temperature settlement
from the **NWS Climatological Report (Daily)** to **The Weather Company**. The
transition is a hard cutover: 100% NWS CLI through 2026-08-13, 100% Weather
Company from 2026-08-15 (see ADR 0026).

This project's entire settlement model targets the NWS CLI product:
`weather/cli_parser.py`, the CLI observation timeline, the station registry
keyed on CLI location codes, and the settlement spec constants (whole-degree
Fahrenheit, local calendar day). **No market with a target date after
2026-08-13 is eligible under that model.**

The immediate consequence has already landed: H0019 terminated NON-EVALUABLE
without ever executing
(`docs/research/postmortems/2026-08-31-h0019-closeout.md`).

**No successor experiment may be registered until this decision is resolved.**
Registering one first would mean registering an experiment whose settlement
model is undefined.

## The decision

**Should Weather Company-settled temperature markets be in scope at all?**

That is a genuine question, not a formality. Reasonable answers include "no —
this project's premise was NWS CLI settlement and that premise has expired."
Deciding *not* to support them is a legitimate outcome and would make this a
data-collection wind-down rather than a build.

## What must be answered before any implementation

### Scope and premise
- Does supporting a commercial settlement source fit the project's purpose, or
  was the NWS CLI product's public, auditable, archived nature load-bearing?
- Is the population of Weather Company markets large and durable enough to
  justify the work, given Kalshi changed settlement once already?

### Authoritative source and observations
- What is the authoritative settlement payload, and is it machine-parseable?
- **How would actual settlement observations be obtained?** The CLI pipeline
  reads a published NWS text product. Is there an equivalent for Weather
  Company data, and is it retrievable, archived, and licensed for this use?
- Without an independent observation source, settlement can only be taken from
  Kalshi's own reported result — which is a materially weaker basis than
  reconstructing it from the authority's own report, and would end the
  project's ability to detect settlement revisions (the subject of confirmed
  hypotheses H0002, H0011, H0013).

### Identity and semantics
- **Station/location identity**, including coordinate-based contracts. Observed
  rules cite raw coordinates (`KORD`) and parenthetical codes (`CLINYC`); the
  registry is keyed on CLI location codes and would need a new identity scheme.
- **Timezone and observation-time semantics**: the CLI model is a local
  calendar-day aggregate. Weather Company hourly contracts settle on an
  instantaneous reading at a named hour in a named zone.
- **Daily versus hourly products**: `KXTEMP*H` are a different instrument, not
  a variant. Are both in scope, or only daily?
- **Rounding and units** — the CLI model assumes whole-degree Fahrenheit;
  observed Weather Company thresholds use hundredths (`70.99°`).

### Architecture
- Resolver architecture: a second resolver alongside `ParserSettlementResolver`,
  or a source-dispatching layer?
- Registry changes required beyond CLI location codes.
- Provenance and reproducibility: `source_provenance` already distinguishes
  `structured_cli_url` from `rules_text`; a third regime needs its own value
  and a `PARSER_VERSION` bump.
- Validation against known settled Kalshi markets before any research use.

### Scientific continuity — the hardest question
- Is data collected under NWS CLI settlement **the same data-generating
  process** as data collected under Weather Company settlement? Different
  authority, different observation source, possibly different rounding and
  station siting.
- **Is cross-regime modelling scientifically defensible** — can train/validation
  data from the CLI regime inform a test set from the Weather Company regime?
  If not, the 112 train and 26 validation event-groups already accumulated
  cannot seed a successor, and any new experiment starts from zero.
- If the regimes are not comparable, the honest framing is a **new research
  programme**, not a continuation.

## Explicitly out of scope for this item

- Implementing a Weather Company resolver.
- Registering any successor experiment.
- Modifying H0019 in any way — it is terminal.
- Any change to the frozen CLI settlement model.

## Prerequisite for a successor experiment

A successor may be registered only after this item is resolved with a recorded
decision. If the answer is **yes, support it**, the successor also needs:

- a working, validated settlement resolver for the new source;
- an independent settlement-observation path, or an explicit recorded
  acceptance that settlement is taken from Kalshi's reported result;
- a documented position on cross-regime comparability;
- enough prospective accumulation under the new regime to satisfy its own
  readiness gates — which, given the collection-reliability history, should not
  be assumed.

## Related

- ADR 0026 — settlement-source migration and the rules-text fallback
- ADR 0005 — the original structured-citation settlement design
- `docs/research/postmortems/2026-08-31-h0019-closeout.md` — H0019 NON-EVALUABLE
- `docs/adr/0003-weather-data-source.md` — the NWS data-source decision this would revisit
