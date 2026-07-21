# ADR 0007: Historical price ingestion (candlesticks)

## Status

Accepted.

## Context

H0007 ("preliminary-observation bound violations near settlement") requires
intraday market price/quote history through settlement afternoons. Its
`HYPOTHESES.md` entry was pre-registered assuming the live 5-minute snapshot
collector (Milestone 2) would supply that history. It does not: `market_snapshots`
holds one row per market (upsert-shaped queries throughout the codebase already
assume this — `_settled_markets` takes `max(id)` per ticker), and the collector
has only ever run for short bursts, so its intraday depth is effectively zero.
Separately, Kalshi's `/markets?event_ticker=` discoverability window is a
rolling ~67 days (confirmed live during Milestone 4c and reconfirmed here) —
markets close to that boundary lose their *discoverability* even though this
phase found their candlestick history still fetchable directly by ticker. This
is an **empirical finding about the current API, not a documented guarantee**;
it is stored as a configurable setting (`PRICE_OBSERVED_RETENTION_DAYS`,
default 67) precisely because it could change.

Kalshi separately exposes a candlestick endpoint
(`/series/{series_ticker}/markets/{ticker}/candlesticks`) with `start_ts`/
`end_ts`/`period_interval` parameters, unauthenticated, returning OHLC trade
prices, OHLC bid/ask quotes, volume, and open interest per period — a strict
superset of what the live collector's snapshot cadence could ever have
provided, and fetchable for the full history of every settled market, not just
markets discovered while the collector happened to be running.

## Decision

1. **Backfill from the candlestick endpoint, not from a longer-running live
   snapshot collector.** The live collector's job (discovering markets,
   capturing order books/trades as they happen) is unchanged; this ADR adds a
   parallel, independent price-history path that recovers everything Kalshi
   still serves for markets that already settled, then keeps itself current
   going forward. Reuses `KalshiClient` (`list_candlesticks`, chunked at
   `MAX_CANDLES_PER_REQUEST = 4900` since a single request has a hard candle
   cap), the existing raw-payload-sink pattern, and the existing repository/
   migration/CLI/ops-loop conventions — no new abstractions.
2. **One additive table, `market_candlesticks`** (migration `0007`), unique on
   `(market_ticker, period_interval_seconds, period_end)` — insert-if-not-exists,
   idempotent reruns never overwrite. A `price_close_is_carried_forward` flag
   records when a period had no trade and `price.close` was filled from the
   API's own `previous_dollars` carry-forward value — an explicit, stored fact,
   not silent inference. Quote OHLC (`yes_bid_*`, `yes_ask_*`) is stored
   alongside trade OHLC so **quoted and traded price are both first-class**,
   not merged into one column.
3. **"Zero volume" is not "no data".** Confirmed live: a period with no trade
   still returns a full `yes_bid`/`yes_ask` block (quotes persist without
   trades) but the `price` block carries only `previous_dollars`. Stored
   candles carry a `data_quality_status` (`ok` / `zero_volume`, computed at
   dataset-build time from `volume == 0`) so no consumer can mistake a
   quote-only period for an executed trade.
4. **Per-market backfill unit, oldest-`close_time`-first.** Unlike the weather
   backfill (which chunks by date range), price backfill chunks by market: one
   market's full window is fetched and committed independently, so a crash
   loses at most one market's progress, and a re-run resumes via
   `--skip-covered` (the default) at zero extra API cost. Oldest-first ordering
   is deliberate: the *rolling* retention window means the oldest markets are
   at the highest risk of falling out of API discoverability before they are
   captured — an explicit prioritization, not incidental.
5. **Six per-attempt outcomes, not seven.** `MarketOutcome` is `complete` /
   `partial` / `no_price_data` / `expired` / `api_failure` / `unsupported`.
   "Unresolved coverage gap" (listed as a category the milestone wanted
   distinguished) is deliberately **not** a seventh enum value: it describes a
   *persistent, cross-run* state ("still incomplete for no single-run reason"),
   which a single attempt cannot classify. It is computed instead at query time
   over stored data (`ops/price_coverage.py`'s `markets_with_missing_intervals`
   and `retention_risk_summary`'s `markets_incomplete_coverage`) — a considered
   split between per-run outcome and cross-run coverage state, not an omission.
6. **Always targets production, never `settings.kalshi_env`.** Every weather
   market this project has ever collected — the entire settled-market archive
   this backfill runs against — came from production's public "Climate and
   Weather" category (ADR 0002 precedent). Candlestick data for a public market
   needs no credentials; signing against `.env`'s default `KALSHI_ENV=demo`
   would be both unnecessary and point at the wrong environment's tickers
   entirely (this was caught live: the first backfill attempt failed on a
   demo-key PEM error before this was fixed).
7. **Scoped to candlesticks, not individual trades.** The milestone's stated
   objective is candlesticks specifically ("backfill all currently recoverable
   historical candlesticks"); trade-level tick data is a larger, unrequested
   surface. 1-minute OHLC trade prices plus 1-minute OHLC bid/ask quotes
   already provide both "what traded" and "what was executable" at finer
   resolution than the originally-assumed 5-minute live cadence, satisfying
   the research need this phase was scoped to without adding a second
   ingestion surface. Revisit only if a specific hypothesis needs individual
   fills (queue position, fill probability) rather than interval OHLC.
8. **Continuous preservation reuses the existing `ops run` loop**, not a new
   scheduler: `run_price_sync_loop` is a third task alongside the two existing
   collector loops in the same `asyncio.gather`, recording `collector="prices"`
   rows through the existing `collector_runs` mechanism — `ops health` already
   knows how to report liveness/staleness per named collector.
9. **`market_prices` is a dedicated dataset frame**, not columns bolted onto
   `market_weather` — a price candle and a market snapshot are different
   grains (many candles per market vs. one row per polled snapshot) and mixing
   them would force either fan-out on the snapshot side or fake candle rows on
   the snapshot side. Unlike `market_weather` (which only includes markets
   present in the settlement map), `market_prices` covers **every captured
   market** — settlement-derived columns (`station_id`, `variable`,
   `target_date`, ...) are simply `null` for a market whose settlement spec
   doesn't resolve, matching how `settlement_labels` already represents
   unsupported markets.

## Verification (live, 2026-07-21)

- Migration `0007` round-tripped (`upgrade head` → `downgrade 0006` → `upgrade
  head`) against local Postgres.
- Full backfill run against all 804 discoverable settled markets (KXHIGHNY
  402, KXLOWTNYC 402): **804/804 `complete`, 0 `partial`, 0 `no_price_data`, 0
  `expired`, 0 `api_failure`, 0 `unsupported`**; 761,475 candles saved, 0
  duplicates.
- Measured coverage report (`ops/price_coverage.py`) over the resulting store
  agrees with the backfill's own totals exactly: `markets_captured=804`,
  `markets_complete=804`, `markets_incomplete=0`, `markets_never_captured=0`,
  `duplicate_row_count=0`, `likely_lost_to_retention=0` (see the price
  ingestion runbook for the full report).
- 12-market stratified live sample (oldest 2, newest 2, 6 random, 2 flagged as
  sparse-coverage) re-fetched directly from the API and diffed against stored
  counts: **12/12 exact matches**.
- 10 of 804 markets show far fewer stored candles than their own observed span
  would imply (down to ~45 candles across a multi-day window) — all 10 are
  `KXLOWTNYC` "between"-strike (`B`) contracts, i.e. illiquid, far off the
  day's realized range. This is the API returning no candle at all for most
  idle minutes on thin strikes, not a backfill defect (the same 10 markets'
  live counts match their DB counts exactly). Recorded as a genuine,
  measured data-availability finding, not smoothed over.

## Consequences

- H0007 gains its price/quote data dependency at 1-minute resolution and full
  historical depth — see the H0007 readiness artifact
  (`docs/research/2026-07-21-h0007-readiness.md`) for the complete,
  per-dependency verdict. Running H0007 itself is out of scope for this ADR.
- The 67-day rolling discoverability window is now a monitored, alertable
  quantity (`ops health`'s `price_retention` block: oldest uncaptured market,
  markets nearing expiry, ingestion lag) rather than an assumption — see
  `docs/runbooks/price_ingestion.md`.
- Thin/illiquid strikes have measurably sparser intraday candle coverage than
  liquid ones; any hypothesis sizing its sample by strike liquidity should use
  the coverage report's `markets_with_missing_intervals`, not assume uniform
  density.
- No trading, feature-engineering, or model code was introduced; `strategy/`,
  `features/`, and `models/` remain absent, matching ADR 0001's precedent of
  not scaffolding packages ahead of a consumer.
