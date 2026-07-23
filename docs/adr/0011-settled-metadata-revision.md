# ADR 0011: Post-expiration settled-metadata revision capture

## Status

Accepted (2026-07-23). Extends ADR 0009 (settled-market transition capture),
which established *first* result capture; this adds the missing second pass.

## Context

A read-only integrity investigation on 2026-07-23 established that Kalshi
continues to revise a settled market's `volume`, `open_interest`, and
occasionally `result` **after** publishing `status="finalized"` with a
`settlement_ts`.

### What was measured

Event `KXTEMPAUSH-26JUL2310`, captured at 17:09-17:10 UTC — three hours after
its 14:00 close, `status="finalized"`, result present:

| | our archive | venue later |
|---|---|---|
| markets reporting `volume=0` | 10 of 11 | 0 of 11 |
| event total volume | 4 | 2,950 (737×) |

Direction is not monotonic. `KXHIGHLAX-26JUL22-T82` went 8,542 → 408,202
(+48×) while its sibling `B81.5` went 670,482 → 130,823 (−5×), and the event
total moved 1,212,144 → 984,449 (0.81×) — restatement, not reassignment.

Worse, the **settlement label itself** was revised. Raw payload id=177534,
received 17:09:59, contains simultaneously:

```
expiration_value = '85.00'   floor_strike = 79.99   strike_type = 'greater'
result = 'no'                settlement_value_dollars = '0.0000'
status = 'finalized'
```

85.00 > 79.99 under `greater` implies **yes**. The venue published a finalized
settlement contradicting its own strike arithmetic, and later corrected it to
`yes`. An archive-wide check found 3 such internally-inconsistent labels out of
2,608 checkable (0.115%), all from 2026-07-22/23, all the lowest strike in
their event.

### What was NOT the cause

Verified against preserved raw payloads and live probes, and excluded:

- **field mapping** — `volume_fp='0.00'` is exactly what the venue returned
- **unit conversion** — `'0.00'`→0, `'4.00'`→4, correct
- **persistence** — append-only, nothing overwritten
- **content hashing** — already covers `volume`, `open_interest`, `result`, so
  a revision *would* append correctly if fetched
- **deduplication** — only suppresses a snapshot identical to the prior one
- **endpoint disagreement** — `/markets` list and `/markets/{ticker}` detail
  returned identical values at the same instant for every market tested

The pipeline was correct. The defect was that **nothing ever asked again.**

### The actual defect

`settlement_sync.find_tickers_pending_settlement_check` keys the queue on
`result IS NULL OR result = ''`. The instant a result-bearing snapshot lands,
the ticker leaves the queue permanently. All 3,089 settled markets had exactly
one result-bearing snapshot; zero had more than one.

The venue's own finality marker — `expiration_time`, consistently ~7 days
after `close_time` (`settlement_timer_seconds=3600`) — was not even stored.
76.7% of settled markets had their last snapshot taken inside that window.

## Decision

1. **Store `expiration_time`** on `market_snapshots` (nullable, TIMESTAMPTZ,
   migration 0008). Declared timezone-aware to match `close_time` from
   migration 0001; `settlement_ts` in 0006 is naive, a pre-existing
   inconsistency this deliberately does not propagate.

2. **Model two distinct concepts.** *Result available* = a snapshot carries
   `result` in {yes, no}. *Settled metadata stable* = the venue's
   `expiration_time` has passed. `status="finalized"` proves only the first.

3. **A separate bounded revision pass** (`ingestion/metadata_revision.py`),
   distinct from the pending-settlement queue, selecting settled markets past
   their finality time. Eligibility filters run **before** `LIMIT`, and
   already-verified markets are excluded by an anti-join in SQL — applying
   ADR 0010's lesson directly, since the shape that broke price sync was a
   fixed head selected first and filtered second.

4. **A separate `market_metadata_verifications` table** to record that a check
   happened. Alternatives rejected: deriving completion from a post-expiration
   snapshot fails outright, because an *unchanged* verification correctly
   appends no snapshot and would be indistinguishable from never having
   checked, re-fetching the market forever; a `metadata_verified_at` column
   would require UPDATE-ing a historical row, violating append-only.

5. **`expiration_time` is included in the content hash.** A venue that moves a
   market's finality deadline has made a meaningful change that should append a
   snapshot. Disclosed consequence: the hash function itself changes, so the
   first poll after deploy appends one extra snapshot per active market — a
   one-time, additive, append-only effect that also timestamps when capture
   began. Covered by a test.

6. **Terminal 404s are split** into `market_removed` (recent — the venue
   removed or re-struck the ticker, observed for extreme-strike contracts
   within hours of close) and `retention_expired` (older than
   `PRICE_OBSERVED_RETENTION_DAYS`). Both terminal, but conflating them would
   hide the first.

7. **Transient failures record nothing**, so the market is naturally
   re-selected next cycle. Only success and the two 404 classifications retire
   a market from the queue.

## Consequences

- Settled metadata converges to the venue's final values instead of being
  frozen at a provisional reading, with the revision itself preserved as
  history rather than overwritten.
- `data_quality_status = "zero_volume"` in the dataset builder becomes
  trustworthy for recent settlements; it was systematically wrong before.
- No historical row is rewritten or deleted. Frozen datasets are unaffected
  until deliberately rebuilt.
- Steady-state cost is small and bounded: markets become eligible only as they
  cross their finality time, each is fetched once, and a cycle with nothing
  eligible issues zero API calls.
- `market_snapshots` keeps a single meaning — observed market state — because
  "we checked and nothing moved" lives in a different table.

## Explicitly deferred

- **The historical backfill.** ~2,369 settled markets whose last snapshot fell
  inside the finality window still carry provisional values. The mechanism
  lands first, tested; the sweep is a separate, reviewable operation. Markets
  past the ~67-day retention horizon may no longer be fetchable, so the
  earliest data may remain provisional — acceptable and documented.
- **The settlement-queue starvation risk** (oldest-first + hard limit 25, head
  currently occupied by 25 `KXRAIN` markets with 344 eligible). Real, related
  in shape, but a distinct defect on a distinct queue.
- **Re-verifying frozen experiments.** No evidence any completed finding is
  affected — every sampled market older than ~2 days agreed exactly with the
  venue, and the 3 label errors postdate every closed experiment. Converting
  "no evidence of harm" into "verified unaffected" is cheap and worth doing.
