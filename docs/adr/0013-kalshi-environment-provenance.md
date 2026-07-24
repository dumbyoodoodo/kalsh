# ADR 0013: Kalshi source-environment provenance

## Status

Accepted (2026-07-24).

## Context

A read-only audit (recorded against ADR 0012's deferred environment question)
confirmed that the system reads two Kalshi environments at once:

- the main collector (discovery, market metadata, trades, order books, and
  settlement) reads **demo** (`demo-api.kalshi.co`), because `KALSHI_ENV=demo`
  and `_build_client` resolves `base_url_for(kalshi_env)`;
- price-sync and metadata-revision read **production**
  (`api.elections.kalshi.com`), because `_build_price_client` hard-codes it.

Empirically (5 sampled tickers + a trade-feed probe, both environments):

- Demo and production share **market definitions and settlement results** —
  same event, close time, strike, title, and `result`.
- Demo has **~no real trading**: `volume`/`open_interest` are 0 and the trade
  feed is empty, while production carries the real liquidity (hundreds to
  hundreds of thousands of contracts).

So results and prices are environment-invariant and coherent, but the
**liquidity dimension is contaminated**: collector-sourced volume/OI/trades/
order-books are demo (~0), production-sourced rows carry real values, and both
land in the same tables under the same ticker. No column recorded which
environment produced a row, and `raw_api_payloads.endpoint_or_channel` stores
only the path (no host), so existing rows cannot be attributed at all.

## Decision

Add source-environment provenance to newly written Kalshi data, so demo- and
production-sourced rows become distinguishable going forward. This is
**instrumentation only**: no endpoint, environment selection, dataset rule, or
historical row is changed.

1. **Canonical values** — `demo`, `production`, `unknown`, as a dedicated
   `SourceEnvironment` StrEnum (`kalshi/provenance.py`). Deliberately distinct
   from `config.Environment` (which carries development/backtest and is about
   *runtime selection*, not *stored provenance*).

2. **One central resolver** — `resolve_kalshi_environment(base_url)` maps a
   client's effective base URL to the canonical value by **parsed hostname**,
   never substring: a recognized demo host → `demo`, a recognized production
   host → `production`, and anything missing/malformed/unrecognized →
   `unknown` (fail-safe). `KalshiClient.source_environment` exposes it, so
   every write path stamps from the client that actually produced the data,
   not a global assumption.

3. **Migration 0010** (additive) — a nullable `environment VARCHAR(16)` column
   on `market_snapshots`, `trades`, `orderbook_snapshots`, `market_candlesticks`,
   `settlement_attempts`, and `raw_api_payloads`, each with a CHECK constraint
   `environment IS NULL OR environment IN ('demo','production','unknown')`.
   NULL stays valid so every pre-0010 row remains a legitimate
   historical-unknown; a stray value is rejected.

4. **Write paths** stamp `client.source_environment`. Under the current runtime
   (`KALSHI_ENV=demo`): collector snapshots/trades/order-books/settlement →
   `demo`; price-sync candlesticks and metadata-revision snapshots →
   `production`. The stamp is provenance and is **not** part of any content
   hash, so it can never by itself force a duplicate snapshot.

### Why provenance before an endpoint change

Flipping the collector to production, or isolating datasets, is a larger
decision that changes what the archive *contains*. Instrumenting provenance
first is the smallest safe step: it makes the contamination visible and
disambiguable, and it is a prerequisite for any later cutover or remediation to
be verifiable. Nothing downstream is corrected yet.

### Why historical rows stay `unknown`

The source environment of a pre-0010 row genuinely cannot be recovered from
stored data (no host, generic `source`). Guessing would be worse than an honest
NULL. Historical liquidity remains contaminated and unattributable; this change
does not fix that, and deliberately does not pretend to.

### Raw-payload provenance

`raw_api_payloads` gets the same `environment` column rather than a host-bearing
endpoint change, keeping one consistent representation across the schema.
Non-Kalshi payloads (weather) are left `NULL` — the field does not describe
their source. No authorization header, token, or credential is ever stored.

## Consequences

- New rows are attributable to demo or production; the two can be separated in
  any query or dataset build.
- `data_quality_status="zero_volume"` can now be scoped to non-demo rows in a
  future dataset change (not done here), removing the systematic mislabelling
  of demo-collected markets.
- The metadata-revision "volume revisions" observed under ADR 0011 are now
  understood to be largely the demo→production delta; ADR 0011 is **not**
  corrected in this task, only noted.
- No behavior change: same endpoints, same collection, same dedup, same
  historical rows.

## Planned follow-up (not decided here)

The target architecture is one of:

- **production-only data collection** (recommended by the audit: the archive's
  value is real microstructure, which only production has; demo duplicates
  definitions/results already available on production), or
- **fully environment-isolated datasets** if demo data must be retained.

Demo's legitimate future role is **simulated order execution** in a later
paper-trading phase — via an explicit, separate client, never as a data source.
Demo execution data must never silently mix with production research data; the
`environment` column is the mechanism that will enforce that separation.
