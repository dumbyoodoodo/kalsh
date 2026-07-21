# INV-20260721 — Historical Kalshi Price Recovery

**Type:** engineering investigation (not a hypothesis experiment — no market
claim is tested here; see `RESEARCH.md` for how this differs from an
`EXP-*` record). **Question:** are historical prices for settled Kalshi
weather markets recoverable, and are they sufficient for end-game research
(H0007) to begin without further waiting?

**Answer: yes, and yes.** Every settled market in our archive has fully
recoverable trade and candlestick history. The one caveat that matters for
planning is architectural, not availability: **the recoverable window is a
rolling ~2-month lookback, not a growing archive** — see §3.

All findings below are measured against the live production API and our own
828-market settled archive (E-A/H0002 backfill), not estimated or assumed.

---

## 1. Candlestick investigation

**Endpoint:** `GET /series/{series_ticker}/markets/{ticker}/candlesticks`
(not documented in this repo before now; discovered by live probing since
`docs.kalshi.com` remains unreachable from this environment — same
constraint noted in `docs/API_VERIFICATION.md`). Requires `start_ts`,
`end_ts` (Unix seconds), `period_interval` (minutes: `1`, `60`, or `1440`
confirmed working; other values untested).

**Coverage measured directly, not estimated:** scanned **all 804 settled
markets** in our archive (KXHIGHNY: 402, KXLOWTNYC: 402) with a daily-
resolution request per market. **804/804 = 100% returned valid data.**
(An initial script bug mis-reported 798/804 — see §7; corrected and
re-verified by inspecting the raw response for all 6 "failures": every one
returned a fully valid 200 with real candle data. The bug was in the probe
script's field access, not the API.)

**Resolution / range behavior (measured):**
- `period_interval=1` (1-minute): works, but a single request is capped at
  **5,000 candles** (`"max candlesticks: 5000"` error observed for a
  4-day/5,760-minute window). A 2-day/2,880-minute window succeeded and
  returned candles from before, through, and slightly after the actual
  market open — 1,192 of 1,740 requested minutes had data; the ~548 "gaps"
  correspond to the pre-open period (the endpoint appears to only emit
  candles once the market exists), not missing intraday data. 522/1,192
  candles had zero trading volume — expected for a market this size, and
  those candles still carry valid `yes_bid`/`yes_ask` quote OHLC (see §7).
- `period_interval=60` (hourly) and `1440` (daily): no range cap
  encountered in this investigation.
- **Implication for ingestion:** 1-minute history needs range chunking
  (~3-day windows at most); hourly/daily do not.

**Fields present per candle:** `end_period_ts`, `open_interest_fp`,
`volume_fp`, and OHLC(+mean) sub-objects for `price`, `yes_bid`, `yes_ask`
(all in dollars). No `no_bid`/`no_ask` block — derivable via the existing
`reconstruct_best_quote` complement relation (`ARCHITECTURE.md`).

## 2. Trades investigation

**Endpoint:** already implemented — `KalshiClient.list_trades(ticker=...)`,
which fully paginates via the existing cursor helper. No new client code
was needed to answer this question.

**Availability after settlement (measured):** checked all 804 settled
markets (existence check: ≥1 trade returned for `ticker=<market>`).
**804/804 = 100%** have retrievable trade history. A deep pull on one
market (`KXHIGHNY-26MAY15-B65.5`, opened ~2026-05-15, closed 2026-05-16
04:59 UTC) returned its full lifetime — **45 trades**, earliest
2026-05-16T00:03:07Z, latest 2026-05-16T04:33:59Z (26 minutes before
close) — with `created_time`, `yes_price_dollars`/`no_price_dollars`,
`count_fp`, `taker_side`/`taker_outcome_side`/`taker_book_side`, all fields
the existing `Trade` model already parses.

**Completeness:** consistent with the candlestick volume figures for the
same market (28.40 `volume_fp` in the closing hourly candle matches
observed late-market trade activity); no evidence of gaps or truncation
within a market's lifetime.

**Global (unfiltered) trades feed:** `GET /markets/trades` with `min_ts`
set to 2023-01-01 and no `ticker` filter returns only the **live, current**
cross-market trade feed (most recent trades exchange-wide across all
categories), not a historical browse. `min_ts` is a lower bound on an
otherwise present-time-anchored feed — it is **not** a mechanism for
discovering old tickers or old trades outside the retention window in §3.

## 3. Retention boundary (measured, not estimated)

The binding constraint is **not** the candlestick or trades endpoint
individually — both return complete data for any ticker we can name. The
constraint is **ticker discoverability**: `GET /markets?event_ticker=`
(needed to learn a settled market's exact ticker/strike strings) returns
data only within a bounded recent window.

Bisected directly against `KXHIGHNY` events (which list back to 2021-08-06,
1,808 events total):

| Event date | Markets returned |
|---|---|
| 2026-05-14 and every date tested before it (26 samples spanning 2021–2026) | **0** |
| 2026-05-15 | **6** (`status=finalized`) |
| every date tested 2026-05-15 → today | 6 each |

**The boundary is a hard cutoff at 2026-05-15**, not a gradual thinning —
confirmed by testing every single day from 2026-05-01 through 2026-05-20.
As of this investigation (2026-07-21), that is **67 days of discoverable
history**, matching exactly what our existing E-A backfill already
retrieved (828 markets, close times 2026-05-16 → 2026-07-23).

**No backdoor exists.** Neither the global trades feed (§2) nor guessing a
plausible ticker string for a pre-boundary event (`KXHIGHNY-26MAR15-T50`
tried; `404 not_found`) recovers anything before the boundary. Older
**events** are listed (metadata only: ticker, title, category) but their
**markets** — and therefore their prices — are gone.

**This window rolls forward.** It is not "history since Kalshi's launch,
capped at 2 months of visibility" — it is a **~67-day trailing lookback
from whenever you ask**. A market that settled 68 days ago today will not
be recoverable next week either; it will simply be further outside the
window. This has one concrete consequence: **anything not captured while
recoverable is gone permanently**, which the collector's continuous
operation already exists to prevent going forward (`docs/adr/0006-settlement-labels.md`
noted the general form of this limitation; this investigation puts an exact
number on it: 67 days, hard cutoff).

## 4. Rate limits (measured)

No rate-limit-related response headers are exposed (checked explicitly).
Empirically: 15 unthrottled candlestick requests fired back-to-back
returned `200` for the first ~10 and `429` for the remaining 5, within
1.2 seconds — consistent with roughly a 10 req/s ceiling. The existing
client's default proactive throttle
(`DEFAULT_MIN_REQUEST_INTERVAL_SECONDS=0.1s`, i.e. ≤10 req/s) plus its
existing 429/5xx retry-backoff loop (`kalshi/client.py`) already handles
this correctly and needs no change: the full 804-market × 2-endpoint scan
(~1,600 requests) in this investigation completed cleanly with **zero
unhandled errors** under that exact throttle.

## 5. Coverage report

| Question | Measured answer |
|---|---|
| % of archived settled markets with recoverable candlesticks | **804/804 = 100%** |
| % of archived settled markets with recoverable trades | **804/804 = 100%** |
| Historical depth available | **67 days trailing from today** (hard cutoff, confirmed by bisection); rolls forward daily |
| What is permanently lost | Prices/trades/order-book state for any market that closed **more than ~67 days before it was fetched** and was not captured while in-window. Event *metadata* (title, ticker, category) is not lost — only market-level data (prices, strikes, trades, settlement fields) is |
| Distinct research-usable variable-dates in the current window | **134** (tmax + tmin, 2026-05-16 → 2026-07-21) — matches `EXP-20260721-EA-settlement-labels`'s n=132 closely (that analysis additionally required an exact `settlement_ts`, a slightly stricter filter) |

## 6. Research impact

**Can H0007 (end-game bound-violation study) be executed immediately?**
Yes, once the price data for the 804 already-known settled markets is
ingested (see §8 — not yet done in this investigation, per the "smallest
amount of new code" constraint). Every input H0007 needs is now confirmed
available: settlement-time labels (validated 791/791 against real payouts,
`EXP-20260721-EA-settlement-labels`), the intraday CLI observation timeline
(3.5-year backfill, `EXP-20260721-H0002-cli-revisions`), and now intraday
market prices around settlement for the same 804 markets.

**Can end-game/price studies begin today?** Yes — with the honest caveat
that "today" means a **67-day, ~134-variable-date sample**, not a longer
history. That is smaller than the 3.5-year weather archive, but it is real
price data, available now, and directly overlaps the fully-validated
settlement-label window. It is **not** necessary to wait for further
accumulation before starting price-facing research — the premise in the
prior roadmap ("market hypotheses must wait for accumulation") is **wrong
for the retrospective piece**: 67 days of it already exists and was simply
never fetched. Going-forward accumulation (via continuous `ops run`
collection, which already persists settlement fields per
`docs/adr/0006-settlement-labels.md`) still matters for growing the sample
beyond 67 days and for validating stability across seasons — it is not a
blocker for starting.

## 7. Corrections made during this investigation

- An early probe script mis-reported 798/804 candlestick recoverability.
  Root cause: it accessed `candle["price"]["close_dollars"]` unconditionally
  to sanity-check price-vs-outcome; when a daily candle had **zero trading
  volume**, `price` contains only `previous_dollars` (no `close_dollars`) —
  a real, documented Kalshi response shape, not missing data. `yes_bid`/
  `yes_ask` OHLC and `open_interest_fp` remain fully populated in that case.
  **Any future candlestick ingestion code must treat `price.close_dollars`
  as optional** and fall back to `yes_bid`/`yes_ask` or carry the price
  forward from the prior candle, exactly as `previous_dollars` signals.
- A separate price-vs-outcome sanity check (does the last daily candle's
  close price land near 0 or 1 matching the settled result?) found 42/798
  cases where it did not (e.g., closing at 0.89 for a `yes` result). This
  is **expected market behavior, not a data defect**: a market's last
  traded/quoted price before settlement is not guaranteed to be exactly 0
  or 1 — thin last-mile liquidity and residual uncertainty are normal. This
  repo already has the correct ground-truth check for settlement outcomes
  (Kalshi's own `expiration_value`/`result` fields, validated 791/791 in
  `EXP-20260721-EA-settlement-labels`) — candlestick closing price should
  never be used as a settlement-correctness check; it is a market-behavior
  variable to study, not a label to validate against.

## 8. Recommended architecture (design only — not implemented here)

Per the investigation's own scope constraint ("do not build a permanent
historical price pipeline unless recovery is confirmed" — now confirmed,
but "optimize for certainty with the smallest amount of new code" and "do
not expand scope beyond determining availability" argue for landing this as
a follow-up milestone, not folding it into the investigation itself). This
section is the ready-to-build design for that milestone.

**Reuse, not new machinery — mirrors the existing Kalshi collector shape
exactly:**

1. **`KalshiClient.list_candlesticks(series_ticker, ticker, *, start_ts,
   end_ts, period_interval)`** — one more thin method alongside
   `list_trades`/`get_orderbook`; the raw-payload-sink pattern is identical.
   Chunk internally when the requested range would exceed ~4,900 candles at
   the given interval (only matters for `period_interval=1`).
2. **A `market_candlesticks` table** (additive migration): natural key
   `(market_ticker, period_interval_seconds, end_period_ts)`, append-only
   (a past candle for a settled market is immutable — no revision concern
   like weather's CLI reissuance). Columns: OHLC+mean for `price`,
   `yes_bid`, `yes_ask`; `volume`, `open_interest`; `raw_payload_id`;
   `schema_version`. `save_market_candlestick` follows the existing
   insert-if-not-exists `SaveResult[T]` pattern used throughout
   `storage/repositories.py`.
3. **A `settled_price_backfill` job**, structurally identical to
   `ingestion/backfill.py`'s chunked/resumable design (per-chunk commits,
   partial-failure isolation, `--skip-covered` resume) but iterating
   *already-known settled tickers from `market_snapshots`* rather than
   dates — no new resumability logic needed, the pattern transfers
   directly. Recommended default: 1-minute candlesticks + full trades for
   every settled market not yet captured.
4. **Ongoing capture**: extend the existing discovery/collector cycle
   (`ingestion/discovery.py`) so that once a market's `status` transitions
   to `finalized`/`settled` (already detected — it's how `result`/
   `settlement_ts` get populated per `docs/adr/0006-settlement-labels.md`),
   its candlestick + trade history is fetched once, promptly — **before**
   it ages out of the 67-day window. A weekly scheduled run of the backfill
   job against newly-settled tickers is sufficient headroom.
5. **Storage cost**: trivial. 804 markets × ~1,200 one-minute candles
   (typical ~2-day lifetime) ≈ 1M rows for the current window; at the
   observed ~45 trades/market for a modest-volume strike (busier strikes
   will be higher, bounded by the `volume_fp` already visible in hourly
   candles) trades add a similar order of magnitude. Both fit comfortably
   in Postgres; no infrastructure change implied.

**Not recommended:** persisting anything beyond `period_interval=1`
candlesticks + trades (hourly/daily are cheaply derivable from 1-minute
data if ever needed, per this repo's "raw immutable, normalized
best-effort" pattern — but unlike CLI text, candlesticks are already
structured, so there is no parsing-fragility reason to store multiple
resolutions).

## 9. Recommendation

**Proceed immediately to price experiments — specifically, implement §8's
minimal ingestion (small, well-scoped, reuses existing patterns
end-to-end) and then run H0007 against the resulting 804-market /
67-day / 134-variable-date sample.** Do not gate this on ASOS/METAR
ingestion; that remains valuable (it fixes the *event clock* for H0007/H0008
per the prior review) but is a separate, parallel track, not a
prerequisite — H0007 can be pre-registered and partially executed today
using existing CLI-issuance timestamps as the intraday obs proxy, then
sharpened once ASOS/METAR lands. Recommended order: (1) build §8's
ingestion (small), (2) pre-register and run H0007 on the current window
while continuous collection extends it, (3) ASOS/METAR in parallel or
immediately after.

## Reproducibility

- Investigation date: 2026-07-21.
- Method: live, read-only queries against the production Kalshi API
  (`api.elections.kalshi.com`), plus read-only queries against the existing
  local Postgres archive (828 markets / 804 settled, from the E-A backfill
  committed in `2180dfc`).
- No permanent code or schema changes were made. All probe scripts were
  scratch/throwaway, run outside the repository, and are not committed —
  consistent with "optimize for certainty with the smallest amount of new
  code." The full raw coverage-scan output (per-market candlestick/trade
  status) is not committed either, since it is reproducible by re-running
  the same measurement against the live API at any time; the aggregate
  statistics above are the durable record.
- Git commit this investigation was performed against: `2180dfc` (E-A).
