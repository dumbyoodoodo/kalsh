# Runbook: Adding a weather station

Expanding to a new city is a **data-only change** -- one registry entry in
`weather/stations.py`. No parser, collector, settlement, or dataset code
changes are needed (verified in Milestone 2b's live validation: 19 cities'
markets classify as `unsupported` today solely because their stations aren't
registered, each with a note naming the exact code to add).

## Required metadata (all live-verified, never guessed)

| Field | What it is | Where to verify |
|---|---|---|
| `station_id` | our internal id (convention: the CLI location code) | chosen by you |
| `source_location_code` | NWS CLI product location code (e.g. `MDW`) | the Kalshi series' `settlement_sources[].url` `issuedby=` param |
| `wfo_site` | issuing WFO office (e.g. `LOT`) | same URL's `site=` param, cross-checked via NWS `/points` |
| `name` | station name as the CLI report titles it | the fetched CLI product text |
| `latitude`/`longitude` | station coordinates | NWS station pages / the CLI product's station |
| `timezone` | IANA zone for the station | the WFO region |
| `city` | human city name (settlement specs) | -- |

## Verification procedure (per ADR 0003/0005; ~10 minutes)

1. **Kalshi side**: fetch the target series (read-only) and confirm its
   `settlement_sources[].url` is a CLI product URL; record `issuedby` and
   `site`. If it isn't a CLI URL, the market is genuinely unsupported --
   stop, don't force it.
2. **NWS side**: `GET api.weather.gov/points/{lat},{lon}` for the station
   coordinates and confirm the returned office (`cwa`) equals the URL's
   `site`. Fetch one live CLI product for the location code and confirm the
   `weather/cli_parser.py` format (`CLIMATE SUMMARY FOR`, `TEMPERATURE (F)`,
   `MAXIMUM`/`MINIMUM`) parses.
3. **IEM side**: confirm `mesonet.agron.iastate.edu/api/1/nws/afos/list.json?
   pil=CLI<code>&date=<some past date>` returns products (historical
   backfill coverage).

## Rollout steps

1. Add the `Station` entry to `weather/stations.py` (data only) with a
   comment citing the verification (mirroring the NYC entry).
2. `uv run kalshi-weather weather collect --once --station <ID>` -- confirms
   metadata resolution (office matches `wfo_site`), live observations, and a
   forecast collect cleanly.
3. Backfill history: `uv run kalshi-weather weather backfill --station <ID>
   --start <date> --end <date>` (see operations.md for runtime).
4. `uv run kalshi-weather settlement resolve-all` -- the city's markets flip
   from `unsupported` (with the "not in the station registry" note) to
   `resolved`; spot-check one with `settlement resolve <TICKER>`.
5. `uv run kalshi-weather ops health` / `ops quality` -- the new station
   appears in coverage; expect a gap warning until backfill completes.

## Expected parser behavior

- **Before**: the city's markets parse to `status=unsupported`,
  `source_location_code=<code>`, note "CLI location code '<code>' ... is not
  in the station registry".
- **After**: the same markets parse to `resolved` (confidence `high` when
  all cross-checks agree) with no parser code change and no
  `PARSER_VERSION` bump -- the registry is data, not parser logic.
- The `wfo_site` cross-check protects against a bad entry: a wrong office in
  the registry makes the parser return `ambiguous`, never a silently wrong
  station.

## Current candidates (from Milestone 2b live validation)

ATL, AUS, BOS, DCA, DEN, DFW, HOU, LAS, LAX, MDW, MIA, MSP, MSY, OKC, PHL,
PHX, SAT, SEA, SFO -- each already observed with a valid CLI settlement URL.
Adding all multiplies weather-sample accumulation ~20x (see
docs/research/2026-07-21-research-plan.md).
