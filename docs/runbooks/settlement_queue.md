# Runbook: settlement queue

Captures the final result-bearing snapshot for markets that left the
open-market discovery list before settling (ADR 0009), with fairness
guaranteed by a persisted attempt ledger (ADR 0012).

## Lifecycle of one market

```
eligible            latest snapshot has no result, observed within
                    COLLECTOR_SETTLE_CHECK_DAYS, absent from the open list,
                    and no terminal/cooling attempt in settlement_attempts
   -> selected      oldest close_time first, filtered BEFORE the batch limit
   -> fetched       one GET /markets/{ticker}
   -> classified    see the outcome table below
   -> persisted     snapshot appended only if content changed; attempt row
                    always written, in the SAME transaction
   -> eligibility   terminal => never returns; retryable => returns after its
                    cooldown; settled => excluded because the latest snapshot
                    now carries a result
```

## Outcome taxonomy

| Outcome | Retry | Cooldown | Terminal | Alert? |
|---|---|---|---|---|
| `settled_and_saved` | — | — | yes | no |
| `already_captured` | — | — | yes | no |
| `not_final_yet` | yes | 1 h | no | **never** — this is normal |
| `result_missing_retryable` | yes | 6 h | no | only if it grows without bound |
| `api_failure_retryable` | yes | 15 min | no | only if persistent |
| `malformed_payload` | yes | 6 h | no | yes — payload we don't understand |
| `unsupported_contract_type` | yes | 24 h | no | yes if a family dominates |
| `market_removed` | no | — | **yes** | no |
| `persistence_failure` | yes | 15 min | no | yes |
| `unexpected_error` | yes | 15 min | no | yes |

**A market is never silently dropped.** Every non-terminal outcome returns to
the queue after its cooldown, and every deferral is recorded.

## Metrics (`collector_runs.stats_json`, `collector="kalshi"`)

| Metric | Meaning |
|---|---|
| `settle_checks` | markets actually fetched this cycle |
| `settled_captured` | final snapshots captured |
| `settle_errors` | retryable failures only (a 404 is **not** an error) |
| `settle_candidates_considered` | pending markets scanned |
| `settle_candidates_selected` | markets that passed eligibility |
| `settle_deferred_cooldown` | pending but inside a cooldown |
| `settle_terminal_excluded` | pending but terminal |
| `settle_queue_remaining` | considered − selected |
| `settle_unique_series` | **fairness signal** |
| `settle_max_from_one_series` | **fairness signal** |
| `settle_oldest_actionable_age_seconds` | staleness of the oldest actionable market |
| `settle_outcome_<name>` | one counter per taxonomy entry |

## Healthy retries vs starvation

They look similar at a glance; these distinguish them:

| | Healthy | Starved |
|---|---|---|
| `settled_captured` | > 0 over a few cycles | **0 for many cycles** |
| `settle_max_from_one_series` | well below the limit | **equals the limit, every cycle** |
| `settle_deferred_cooldown` | rises then falls | ~0 (nothing is being deferred) |
| selected tickers | rotate between cycles | **identical every cycle** |

The pre-fix signature was `settle_checks=25, settled_captured=0,
settle_errors=0` with the same 25 tickers — no errors at all, which is exactly
why it went unnoticed.

## Inspecting blocked markets

```sql
-- why is a market not being processed?
select outcome, retryable, next_attempt_at, http_status, error_code, detail
from settlement_attempts where market_ticker = 'KXRAIN-26JUL21-ATL'
order by attempted_at desc limit 5;

-- current queue shape
select outcome, count(*) , min(next_attempt_at)
from settlement_attempts a
where a.id in (select max(id) from settlement_attempts group by market_ticker)
group by 1 order by 2 desc;
```

## KXRAIN specifically

Ordinary **binary** markets ("Will it rain in X on DATE?",
`strike_type=greater`, `floor_strike=0.00`) expecting a yes/no result. They are
neither categorical nor unsupported. They return HTTP 200 / `closed` /
`result=""` while awaiting settlement, which is legitimate — they were simply
the oldest by `close_time`, so under the old queue they held every slot.
Expect them under `not_final_yet` (and `result_missing_retryable` once past
`expiration_time`). **This is not an alert condition.**

## Expected observatory alerts

Alert on *absence of progress*, not on cooldown:

- `settled_captured == 0` across many cycles **while**
  `settle_candidates_selected > 0` and `settle_deferred_cooldown` is ~0
- `settle_max_from_one_series == settle_candidates_selected` sustained
- `settle_oldest_actionable_age_seconds` beyond a threshold
- growth in `malformed_payload` or `unsupported_contract_type`

Do **not** alert on `not_final_yet` — it is the normal state of every market
between closing and settling.
