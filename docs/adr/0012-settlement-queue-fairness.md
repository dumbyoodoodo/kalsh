# ADR 0012: Settlement-queue fairness via a persisted attempt ledger

## Status

Accepted (2026-07-23). Extends ADR 0009 (settled-market transition capture),
which created the queue this repairs. Independent of ADR 0011 (metadata
revision), which uses a separate queue and is untouched.

## Context

The settlement queue re-checks markets that left the open-market discovery
list before settling. It had no memory of having checked anything.

`find_tickers_pending_settlement_check` selected markets whose latest snapshot
carries no `result`, ordered by `close_time ASC`, and took the first N. A
market checked and found *still unsettled* — HTTP 200 with an empty `result`,
which is the correct and expected answer for a market awaiting settlement —
remained eligible at identical priority. Nothing recorded the attempt.

### What was measured in production

For 20+ consecutive cycles (~3.3 hours observed, longer in reality):

```
settle_checks=25   settled_captured=0   settle_errors=0
```

The 25 head slots were held by `KXRAIN` markets, every cycle, unchanged.
Meanwhile **1,501 markets were pending**, of which **1,476 were never
reached**. Ten of ten sampled starved markets returned `status="finalized"`
with a real `result` and would have been captured immediately.

This was silent, ongoing settlement-capture loss. It was invisible precisely
because `settle_errors=0` — nothing was failing; the queue was simply never
advancing.

### KXRAIN behaviour (proven, not assumed)

Against the **demo** API the collector actually uses
(`demo-api.kalshi.co` — `_build_client` resolves `base_url_for(kalshi_env)`,
unlike price-sync/revision which always target production):

| Ticker | HTTP | status | result | settlement_ts | expiration_time |
|---|---|---|---|---|---|
| `KXRAIN-26JUL21-*` | 200 | `closed` | `""` | None | 2026-07-23 04:00 (**passed**) |
| `KXRAIN-26JUL22-*` | 200 | `closed` | `""` | None | 2026-07-24 04:00 (future) |

Stored metadata: `market_type="binary"`, title *"Will it rain in Atlanta on
Jul 21, 2026?"*, `strike_type="greater"`, `floor_strike=0.00`.

So KXRAIN is an **ordinary binary market expecting a yes/no result**. It is
*not* categorical, *not* range-based, and *not* an unsupported contract type.
The settlement parser is not involved at all — `capture_settled_transitions`
only reads `market.result`. Of 80 pending KXRAIN markets, 20 had passed their
`expiration_time` still unsettled and 46 had not. They are simply markets the
venue has not settled, which is a legitimate state, not an error.

They dominated the head only because they were the **oldest by `close_time`**
and the queue re-selected them forever.

### What was *not* the cause

- **LIMIT before filtering** — the `[:limit]` slice already ran in Python
  *after* the open-market exclusion. Ordering plus absent state was the fault.
- **Malformed or categorical settlement values** — proven above.
- **API 404s** — on demo these return 200. (On production the older ones 404,
  a different environment's behaviour, now classified `market_removed`.)
- **Unsupported parsing** — no parser runs in this path.
- **Batch-wide transaction rollback** — failures were already caught per
  ticker; they merely shared one session.

## Decision

1. **Persist an attempt ledger.** New append-only `settlement_attempts` table
   (migration 0009) recording `outcome`, `retryable`, `next_attempt_at`,
   `http_status`, `error_code`, bounded `detail`, and `raw_payload_id` /
   `snapshot_id` provenance. Retry state deliberately does **not** go on
   `market_snapshots`, which is append-only observation data.

2. **An explicit outcome taxonomy**, not one generic "failed": collapsing
   outcomes is what made the starvation invisible. Each outcome carries a
   retry decision and cooldown (`RETRY_POLICY`).

3. **Filter eligibility before the batch limit.** A market inside its cooldown
   or with a terminal outcome is removed from the candidate list *before* the
   limit is applied, so it cannot re-occupy a slot.

4. **Preserve chronological priority among actionable markets.** Ordering is
   still oldest-`close_time`-first; only ineligible markets are skipped.

5. **No per-series cap.** Cooldown alone restores fairness — verified by
   projection against real production data: KXRAIN falls from 25/25 of the
   batch to 15 in cycle 2 and disappears by cycle 3, with 150 distinct markets
   reached in 6 cycles versus 25 forever. A cap would be redundant machinery.

6. **Per-market transactions.** With a `session_factory` each market commits
   independently, so one failure cannot roll back another's success, and each
   market's snapshot and attempt row commit atomically.

## Retry and terminal policy

| Outcome | Retryable | Cooldown | Notes |
|---|---|---|---|
| `settled_and_saved` | no | — | result now present; naturally ineligible too |
| `already_captured` | no | — | dedup hit |
| `not_final_yet` | yes | 1 h | **normal**; never alert |
| `result_missing_retryable` | yes | 6 h | past `expiration_time`, still unsettled |
| `api_failure_retryable` | yes | 15 min | transient |
| `malformed_payload` | yes | 6 h | a result we don't understand — never a settlement |
| `unsupported_contract_type` | yes | 24 h | parked and counted, **never dropped** |
| `market_removed` | **no** | — | venue 404 |
| `persistence_failure` | yes | 15 min | never recorded as success |
| `unexpected_error` | yes | 15 min | |

Nothing is ever silently dropped: every non-terminal outcome returns to the
queue, and every deferral is auditable in the ledger.

## Consequences

- The queue makes provable forward progress; `settle_outcome_*` metrics show
  exactly why each market was deferred.
- A 404 is no longer counted in `settle_errors`. It is terminal
  (`market_removed`), not a retryable failure — one existing test was updated
  to assert the more precise classification while keeping its isolation intent.
- API load is unchanged in shape (one `get_market` per selected market, same
  bounded limit, same throttle) and *falls* in practice, because cooled-down
  markets are not re-fetched every cycle.
- No historical row is read, mutated, or deleted. `market_snapshots` is
  untouched; the metadata-revision system (ADR 0011) is untouched.

## Explicitly not addressed

- **Why the collector targets demo while price-sync and revision target
  production.** A real and possibly significant inconsistency, discovered
  during this investigation, but a separate decision requiring its own
  evidence — changing it would alter what data the archive contains.
  *(Update: this environment inconsistency has since been investigated and
  instrumented — see ADR 0013, which confirms demo/production share
  definitions and results but not liquidity, and adds source-environment
  provenance to new rows. The endpoint decision itself remains open there.)*
- **The 20 KXRAIN markets past `expiration_time` with no result on demo.**
  They now retry on a 6-hour cooldown and are counted under
  `result_missing_retryable`; whether the demo venue ever settles them is an
  open question, not a blocker.
