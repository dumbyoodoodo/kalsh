# ADR 0005: Automated settlement resolution

## Status

Accepted.

## Context

Milestone 2b replaces the Milestone 4 config-file market mapping
(`dataset/market_map.py`, ADR 0004 decision 1) with automated settlement
resolution: deriving, from Kalshi market metadata and rules, which weather
station/variable/date a market settles against. The dataset builder's contract
(`list[MarketMapping]`) must not change.

Grounding research was done against live production data (read-only), not
documentation assumptions -- `docs.kalshi.com` remains unreachable from this
environment (see `docs/API_VERIFICATION.md` precedent). Key findings, each
observed in real payloads on 2026-07-21:

1. **Kalshi's series objects carry a structured settlement citation.**
   `settlement_sources[].url` for every daily-temperature series is a
   machine-parseable NWS product link:
   `https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC`.
   `product=CLI` identifies the Climatological Report (the settlement source
   confirmed in ADR 0003), `issuedby` is the CLI location code our station
   registry keys on, and `site` is the issuing WFO office -- usable as a
   cross-check (NYC->OKX was independently verified via `/points` in ADR 0003).
2. **Non-temperature series carry distinguishable non-CLI citations** (bare
   `weather.gov` for snow/rain, `weather.com/kalshi` for hourly series settled
   by The Weather Company), so "supported vs. unsupported" is decidable from
   the structured field alone.
3. **Series ticker naming is irregular** (`KXHIGHTDAL` vs `KXDENHIGH` vs
   `KXLOWTMIA`), so ticker-prefix matching would be brittle -- reinforcing the
   structured-URL approach.
4. **The variable and target date exist only in rules prose**, in observed
   variants: "highest temperature ... for July 21, 2026" and "minimum
   temperature ... for Jul 20, 2026," (full and abbreviated months, optional
   trailing comma). The event ticker's date segment (`KXHIGHNY-26JUL21`,
   YYMMMDD) states the date a second time, enabling cross-validation.

## Decisions

1. **Structured fields first; prose only where nothing structured exists, and
   then always cross-validated.** Station identification comes exclusively
   from the `settlement_sources` URL (`issuedby` matched against the registry's
   `source_location_code`, `site` checked against the registry's new
   `wfo_site`). The variable is read from rules text and cross-checked against
   the series/market title; the target date is read from BOTH rules text and
   the event-ticker date segment and must agree. Any disagreement is an
   explicit `AMBIGUOUS` outcome -- the parser never picks a side.

2. **Resolution is total and typed: every market yields a `SettlementSpec`.**
   Status is one of `resolved` / `ambiguous` / `unresolved` (missing inputs) /
   `unsupported` (recognized but out of scope: non-CLI source, station not in
   registry), with machine-readable notes explaining every non-resolved
   outcome. Confidence (`high`/`medium`) records whether every cross-check
   agreed or a secondary signal was missing. Only `resolved` specs become
   dataset mappings -- "no unresolved contract can reach a strategy"
   (TASKS.md) holds by construction.

3. **The parser is pure and versioned.** `parse_settlement` does no IO; same
   inputs always produce the same spec. `PARSER_VERSION` plus a `rules_hash`
   over the exact parsed inputs make every resolution reproducible and every
   re-parse comparable: the `settlement_specs` table is append-only, unique on
   `(market_ticker, parser_version, rules_hash)`, so unchanged inputs are a
   detectable no-op while parser upgrades or rules changes append new rows
   beside the old (resolution history is never rewritten). Migration `0004`
   is additive only.

4. **The resolver seam is the Milestone 4 contract, unchanged.** A
   `SettlementResolver` produces `list[MarketMapping]`;
   `ParserSettlementResolver` (automated), `ConfigSettlementResolver` (the
   old config file), and `CompositeSettlementResolver` (parser + overrides,
   the default for `dataset build`) all satisfy it. `dataset/builder.py` and
   `dataset/pipeline.py` required no behavioral changes -- `pipeline.build`
   gained only an optional `resolver_meta` passthrough so the manifest records
   how mappings were produced (parser version, resolution counts, overrides).

5. **The config file survives as the manual override layer** (TASKS.md:
   "manual override file with audit fields"). Entries may carry optional
   `reason`/`author`/`added_on` audit fields; an override both replaces a
   parsed mapping and can supply mappings the parser cannot produce. Overrides
   are recorded in the resolution report and the dataset manifest.

6. **The series' settlement citation is now persisted at collection time.**
   `save_series` gained a `settlement_source` parameter (JSON of the
   `settlement_sources` list; the column existed since migration 0001 but was
   never populated), and discovery passes it. A `None` update preserves the
   stored value rather than erasing it. This is what lets the DB-backed
   resolver run without re-fetching Kalshi.

## Live validation (2026-07-21, production, read-only)

- 289 series in "Climate and Weather": 113 CLI-source, 176 non-CLI
  (unsupported by structured classification alone).
- 270 open markets parsed across all 50 CLI-source series with open events:
  - **12 resolved** -- every market for registry stations (KXHIGHNY,
    KXLOWTNYC), all `high` confidence, station/variable/date verified correct.
  - **0 ambiguous, 0 crashes, 0 silent guesses.**
  - **3 unresolved** -- KXRAINNYCM (monthly rain) cites the same NYC CLI URL;
    the parser correctly refused to map it because its rules name no
    temperature variable. Defense in depth: a CLI citation alone cannot pull a
    non-temperature market into the dataset.
  - **255 unsupported** -- all stations absent from the registry, each with an
    actionable note naming the CLI location code (ATL, AUS, BOS, DCA, DEN,
    DFW, HOU, LAS, LAX, MDW, MIA, MSP, MSY, OKC, PHL, PHX, SAT, SEA, SFO).

## Limitations and future extensibility

- **Scope is daily max/min temperature at registry stations.** Supporting a
  new city is a data-only change (add the station, with live-verified
  `source_location_code` and `wfo_site`, to `weather/stations.py`) -- the
  live validation shows 19 cities immediately unlockable this way. Supporting
  a new *variable* (rain, snow) requires parser work: a variable phrase, a
  unit, and a CLI-parser extraction for it.
- Monthly/rain markets citing a CLI URL surface as `unresolved` (no
  temperature phrase) rather than `unsupported`; explicit either way, but a
  future parser version could classify the variable domain more precisely.
- Thresholds/strikes (`T79`, `B85.5`) are deliberately not parsed here --
  they are market payoff structure, not settlement-source identification, and
  Kalshi exposes structured strike fields for when a later phase needs them.
- The rules-prose station name ("Central Park, New York") is not
  independently matched against the registry; the structured URL is treated
  as authoritative. If Kalshi ever ships a URL/prose mismatch, the date/
  variable cross-checks still bound the damage, and the prose is preserved in
  the market snapshot for audit.

## Consequences

- `dataset build` now populates `market_weather` automatically for any
  collected market the parser resolves; the config file is only needed for
  exceptions.
- Anyone adding a station must verify `issuedby`/`site` codes against live
  NWS/Kalshi data (the ADR 0003 procedure), not infer them.
- A parser behavior change MUST bump `PARSER_VERSION`; stored specs from
  older versions remain in `settlement_specs` untouched.
