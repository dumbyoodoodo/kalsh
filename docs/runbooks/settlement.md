# Runbook: Settlement resolution

Derives, for every collected Kalshi market, the weather station/variable/date
it settles against -- automatically, from the series' structured
settlement-source citation and the market's rules text. Read-only over
collected data (nothing is re-fetched); no trading logic. See
`docs/adr/0005-settlement-resolution.md` for design and validation results.
This is Phase 2 / Milestone 2b of `TASKS.md`.

## How resolution works

1. The series' `settlement_sources[].url`
   (`...product.php?site=OKX&product=CLI&issuedby=NYC`) identifies the NWS CLI
   product and station: `issuedby` is matched against the station registry's
   `source_location_code`, `site` against its `wfo_site`.
2. The settled variable ("highest"/"maximum" -> `tmax_f`, "lowest"/"minimum"
   -> `tmin_f`) and target date are read from rules text; the date must agree
   with the event ticker's date segment, the variable with the series/market
   title.
3. Every market gets a `SettlementSpec` with a status -- `resolved`,
   `ambiguous` (signals conflict), `unresolved` (inputs missing), or
   `unsupported` (non-CLI source, or station not in the registry) -- plus
   notes explaining any non-resolved outcome. **Only resolved specs become
   dataset mappings.**

## Prerequisites

- Collected market data (`collector run` -- the parser reads stored series and
  market snapshots; series collected before Milestone 2b lack the stored
  settlement citation until re-collected, since discovery now persists it).
- Migrations applied (`alembic upgrade head`; `settlement_specs` arrived in
  `0004`).

## Commands

```bash
uv run kalshi-weather settlement resolve KXHIGHNY-26JUL21-T79   # one market
uv run kalshi-weather settlement resolve-all --persist          # all markets, stored
uv run kalshi-weather settlement report                          # full report (JSON)
```

- `resolve` exits non-zero if the market does not resolve -- scriptable as a
  gate.
- `--persist` stores specs append-only in `settlement_specs`, versioned by
  `(market_ticker, parser_version, rules_hash)`: re-running on unchanged rules
  is a no-op; a parser upgrade or rules change appends a new row.
- `dataset build` runs resolution automatically (parser + overrides); no
  separate step is required to populate `market_weather`.

## Reading the report

`settlement report` groups markets by status with per-market notes:

- `resolved` -- mapped; confidence `high` (all cross-checks agreed) or
  `medium` (a secondary signal was missing; the note says which).
- `ambiguous` -- conflicting signals (e.g. rules date != ticker date). Never
  auto-mapped; fix the data or add an override.
- `unresolved` -- missing inputs (no rules text, no settlement source stored,
  no temperature phrase). Re-collect the series/market, or override.
- `unsupported` -- non-CLI settlement source, or a CLI station not in the
  registry. The note names the exact CLI location code; adding that station
  to `weather/stations.py` (with live-verified codes, per ADR 0003) is all it
  takes to support that city.

## Manual overrides

The Milestone 4 market-map file (`DATASET_MARKET_MAP_PATH`) is now the
override layer: entries take precedence over parsed output and may add
markets the parser cannot resolve. Record the audit trail:

```yaml
markets:
  - market_ticker: KXHIGHNY-26JUL21-T79
    station_id: NYC
    variable: tmax_f
    target_date: 2026-07-21
    reason: parser ambiguity NN investigated by hand, rules confirmed
    author: your-name
    added_on: 2026-07-21
```

Overridden markets are listed in the resolution report and the dataset
manifest.

## Known limitations (see the ADR)

- Daily max/min temperature only; new cities are data-only registry
  additions, new variables (rain, snow) need parser work.
- Thresholds/strikes are not parsed (payoff structure, not settlement-source
  identity).
- A CLI-citing non-temperature market (observed live: monthly rain) surfaces
  as `unresolved`, not `unsupported`.
