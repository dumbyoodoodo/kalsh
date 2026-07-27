# Runbook: settlement-resolution coverage

What the observatory's `settlement_resolution_failures` check counts, which
market families the settlement parser supports, and how to extend support
safely. Baseline audited 2026-07-27 (9,053 markets in store).

**Update 2026-07-27 (ADR 0023):** SEA, PHX, and MIA added to the station
registry as a three-city pilot — their CLI daily-temperature markets
(~252, ~190 settled) now resolve. The remaining audited cities (DFW, ATL,
BOS, DCA, HOU, AUS, PHL, LAS, MSY, MSP, SFO, and discovered OKC/SAT) are
**deferred pending the one-week pilot review and separate explicit human
approval** — they remain intentionally unsupported until then.

## What the numbers mean

The check parses **every market with a stored snapshot** (the entire
collected "Climate and Weather" category — far broader than the families
this project researches). Each market gets exactly one status:

| Status | Meaning | Counts as a problem? |
|---|---|---|
| `resolved` | station, variable, target date all determined; cross-checks passed | no — these feed dataset mappings |
| `unsupported` | recognized and **intentionally out of scope** (see matrix) | **no** — a scope decision, not corruption |
| `unresolved` | a required input is genuinely missing (e.g. no parseable quantity/date) | yes — warning |
| `ambiguous` | inputs conflict (rules date ≠ ticker date, WFO mismatch, both variables) | yes — warning; never guessed |

The observatory warns on `unresolved + ambiguous` only. "Unsupported" is
reported for visibility and is **not** presented as failure.

## Coverage matrix (baseline 2026-07-27)

| Family / pattern | Count | Status | Why |
|---|---|---|---|
| `KXHIGH*`/`KXLOWT*` daily temp, registry cities (NYC, MDW/CHI, DEN, LAX, **SEA, PHX, MIA** since ADR 0023) | ~1,392 | **resolved** | NWS CLI product URL + registry station + temperature variable + cross-checked date |
| `KXTEMP*H` hourly temperature (5 cities) | ~5,843 | unsupported | settlement source is weather.com hourly, not an NWS CLI product |
| `KXHIGHT*`/`KXLOWT*` daily temp, **non-registry cities** (DFW, ATL, BOS, DCA, HOU, AUS, PHL, LAS, MSY, MSP, SFO, OKC, SAT — deferred pending pilot review) | ~1,220 | unsupported | valid CLI source, but the city is not in `weather/stations.py` — see "Adding a city" below |
| `KXRAIN*M` monthly precipitation (4 cities) | 69 | unsupported | CLI-cited but the settlement quantity is precipitation, not temperature (fixed word table) |
| `KXRAIN`, snow, `KXFIRSTHURRICANE`, `KXTORNADO`, `KXHURRICANENAMES`, climate/eruption composites | ~530 | unsupported | non-CLI settlement sources (bare weather.gov, weather.com, others) |
| Genuinely unresolved/ambiguous | 0 | — | after the 2026-07-27 reclassification |

## Reason codes

Every non-resolved spec carries its reason in `notes`; principal codes:
"settlement source is not an NWS CLI product URL" · "CLI location code 'X'
… is not in the station registry" · "settlement variable is a
non-temperature quantity (precipitation/snow)" · "rules text names neither
a highest nor a lowest temperature" (genuine) · date/WFO conflict messages
(ambiguous). Inspect with `uv run kalshi-weather settlement report`.

## Adding deterministic support for a new city

The ~1,470 non-registry CLI-city markets are *resolvable in principle* but
gated on a **data-collection decision**, not a parser defect: the station
registry (`weather/stations.py`) doubles as the weather-collection roster,
so adding a city enrolls it in forecast/observation collection (more NWS
requests, more storage, collector restart to take effect). To add one,
follow the Task 3B precedent per entry:

1. Take `source_location_code` and `wfo_site` verbatim from Kalshi's own
   settlement-source URL (`product.php?site=<WFO>&product=CLI&issuedby=<code>`).
2. Cross-verify live: NWS `/points/{lat,lon}` resolves to the same WFO, and
   IEM has the CLI product at the intended history start and today.
3. Add the `Station(...)` entry (data, not code), tests, and run the
   station-expansion validation before restarting the collector.

Do **not** add cities merely to shrink the unsupported count.

## Why heuristic resolution is forbidden

Settlement rules are authoritative (CLAUDE.md): the station, window,
rounding, and source must come from Kalshi's structured citation and
cross-validated prose — never inferred from a title. A guessed mapping that
is subtly wrong would poison labels silently and irreversibly downstream.
Every extension must be a fixed table or deterministic grammar with
positive and negative fixtures; ambiguity always fails closed.
