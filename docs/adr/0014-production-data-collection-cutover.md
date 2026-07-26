# ADR 0014: Cut Kalshi research-data collection over to production

## Status

Accepted (2026-07-26). Follows ADR 0013 (environment provenance), which
instrumented the demo/production split this resolves. Independent of the
live-trading safety design, which is deliberately left untouched.

## Context

Since inception the main collector read the **demo** Kalshi API
(`demo-api.kalshi.co`) while price-sync and metadata-revision read
**production** (`api.elections.kalshi.com`). ADR 0013 proved demo and
production share market definitions and settlement results but not liquidity:
demo carries ~zero volume/open-interest, empty order books, and a near-empty
trade feed. So newly collected volume, open interest, trades, order books, and
liquidity snapshots reflected demo, not the real market — a correctness defect
for any liquidity- or microstructure-dependent research.

The provenance column (migration 0010) now stamps every new row's source, so
the cutover is verifiable: demo- and production-sourced rows are distinguishable
before, during, and after the change.

## Decision

Route **all** research/archival Kalshi reads to production, via one explicit,
centralized configuration knob, without touching the live-trading gate.

1. **A dedicated data environment.** New setting `kalshi_data_env`
   (`KALSHI_DATA_ENV`, default **production**) selects where discovery,
   snapshots, trades, order books, settlement, candlesticks, and metadata
   revision read from. It is validated to be `demo` or `production` (the only
   two with a base URL) and fails clearly at startup otherwise.

2. **Kept separate from the trading gate.** `kalshi_env` (`KALSHI_ENV`) remains
   the demo-defaulted live-trading safety gate (CLAUDE.md non-negotiable: live
   order submission is impossible unless `kalshi_env=production` plus explicit
   flags). It is **not** repurposed as a data source. Decoupling the two means
   a future demo execution client and the production data client can never be
   confused — the exact failure this ADR's structure exists to prevent.

3. **One centralized resolution path.** `Settings.kalshi_data_base_url` is the
   single source of truth; both data-client builders (`_build_client` and
   `_build_price_client`) resolve through it. No workflow hard-codes an
   environment independently (previously `_build_price_client` hard-coded
   production).

4. **Unauthenticated data client.** Every endpoint the collector uses is public
   (there is no `require_auth=True` anywhere), and production public reads need
   no credentials — confirmed live by the price client, which has always read
   production unauthenticated. The data client therefore attaches **no** signing
   credentials. The demo API key/private key stay in `Settings`, reserved for
   the future demo execution client, and are never attached to a data client.

5. **Provenance unchanged.** New rows are still stamped from the producing
   client's resolved host (ADR 0013). After cutover all research workflows
   stamp `production`; weather payloads stay `NULL`.

6. **Startup visibility.** The resolved data environment (env label, API host,
   row-provenance value, and the separate trading env) is logged at collector
   and combined-runner startup — public config, no secrets.

### Why a new variable rather than flipping `KALSHI_ENV`

`KALSHI_ENV=production` is one of the four conditions that unlock live order
submission. Flipping it to move data collection would erode a safety gate and
conflate "where I read data" with "am I cleared to trade real money." A separate
read-only data variable is the smallest change that cuts the data over while
leaving the trading gate demo-locked.

## Historical-data policy (no rewrite in this task)

Historical rows are **not** deleted, relabeled, or rewritten here. Classify by
the `environment` column:

- `environment='demo'` — known demo-era data. Its volume/OI/trades/order books
  are **not** real liquidity and must never be used as production liquidity.
- `environment='production'` — known production data (price-sync candlesticks,
  metadata-revision snapshots, and, going forward, all collection). Usable.
- `environment IS NULL` — pre-provenance (pre-0010). Source uncertain; must
  **not** be auto-relabeled as demo or production. Treat liquidity as
  unattributable unless a separate remediation proves provenance.
- `environment='unknown'` — post-provenance but unresolved host; not expected in
  recognized live workflows.

Consequences for research use:

1. Historical demo liquidity must not be used as production liquidity.
2. NULL historical rows must not be automatically relabeled.
3. Existing settlement labels remain usable where prior audits established
   environment equivalence (definitions/results are environment-invariant,
   ADR 0013).
4. Existing production candlestick prices remain usable.
5. Liquidity-derived research should use only verified production rows collected
   after this cutover, unless a separate remediation proves earlier provenance.

**Dataset-builder filtering is not changed in this task** — the cutover stops
new contamination; a separate canonical-dataset task will exclude contaminated
demo-era liquidity using the `environment` column.

## Consequences

- New volume/OI/trades/order books reflect the real production market; they are
  no longer systematically zero.
- Demo's only remaining role is the future simulated order execution client, an
  explicit, separate, authenticated client — never a data source.
- Rollback is a code/config revert (no schema change): restore the previous
  data-client construction (or set `KALSHI_DATA_ENV=demo`). Migration 0010 is
  never downgraded for an endpoint rollback; already-collected production rows
  are preserved.
