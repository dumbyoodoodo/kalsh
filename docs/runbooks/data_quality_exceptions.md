# Data-quality exceptions & remediation queue

Read-only persistent-warning audit of the production platform. Re-run the
commands below to refresh; this file records the **finite remediation queue**,
not raw data. Audit as-of: **2026-07-28T14:30Z** (HEAD `14ebf20`).

## Classification vocabulary

Observatory findings: `EXPECTED_BASELINE`, `TRANSIENT_RECOVERED`,
`KNOWN_OUTAGE_RESIDUE`, `PERSISTENT_ACTIONABLE`, `PERSISTENT_NON_ACTIONABLE`,
`DATA_INTEGRITY_RISK`, `UNKNOWN_REQUIRES_REVIEW`.

Gaps (observation/forecast/book): `EXPECTED_CADENCE`, `SOURCE_GAP`,
`COLLECTION_GAP`, `PARSER_GAP`, `PERSISTENCE_GAP`, `LEGACY_UNKNOWN`,
`RECOVERABLE_BACKFILL`, `PERMANENTLY_UNRECOVERABLE`.

Confidence: `CONFIRMED`, `HIGH`, `MEDIUM`, `LOW`.

## Persistence threshold (enter the queue only if ≥1 holds)

Persists ≥3 expected cycles · recurs on ≥2 complete days · affects multiple
stations/tickers · violates a hard integrity invariant · blocks deterministic
settlement · loses prospective research data · remains after known-outage
recovery · has a reproducible code-level cause. Cosmetic/one-off warnings are
**not** queued.

## Known healed events (excluded from the queue)

- **2026-07-26 04:19–18:50Z (~14.5h):** no collector runs at all — host
  unavailability (laptop asleep). Healed. Explains the forecast
  `cadence_collector_outage_recovered` warnings (all 8 stations, 14.86h) and
  the LAX/CHI/DEN issuance gaps. `COLLECTION_GAP`, non-actionable.
- **2026-07-27 13:00–20:00Z (~7h):** all Kalshi poll attempts failing —
  upstream API outage. Healed. Explains the market-poll `unknown_failure`
  cluster and the market/candle archive-continuity gaps. `KNOWN_OUTAGE_RESIDUE`.

## Findings summary (2026-07-28)

| Area | Result | Class |
|---|---|---|
| Hard provenance invariants (9 checks) | **0 violations** | — |
| Settlement resolution | 1560 resolved, 8909 unsupported, **0 unresolved/ambiguous/parser-error** | EXPECTED_BASELINE |
| Book-continuity (3d) | 0 invariant violations | — |
| Missing observation days | LAX 07-17, MIA 07-16, NYC 07-02, PHX 07-24 (1 each, different days) | SOURCE_GAP (below threshold) |
| Weather parser errors | 1 isolated cycle (run 676, 07-24), 0.4% | TRANSIENT_RECOVERED |
| Backups / restore-drill | local+remote success 07-27; drill success 07-27, SHA verified | EXPECTED_BASELINE |
| **Order-book price-0 rejection** | 782 malformed, 5 tickers | **PERSISTENT_ACTIONABLE → DQ-001** |
| **Recovery-watch flapping** | RECOVERING↔READY every ~15–30min, notifies each flip | **PERSISTENT_ACTIONABLE → DQ-002** |

## Remediation queue

### DQ-001 — Order-book whole-book rejection on price-0 levels · Priority 1 · CODE · SMALL · **FIXED**

- **Symptom:** `InvalidPriceLevelError: yes price 0 out of [1,99]` →
  `malformed_payload`; the *entire* order-book snapshot for that poll is
  discarded. 782 occurrences, all the orderbook endpoint.
- **Scope:** 5 long-horizon **non-weather** tickers (USCLIMATE-2025/2030,
  EUCLIMATE-2030, KXERUPTSUPER-0-50JAN01, KXGTEMP-26-P0), each ~156 fails vs
  ~160 successes — roughly half their book history dropped. **Latent risk:**
  any weather (KXHIGHT/KXLOWT) book that ever contained a price-0 level would
  be dropped identically → prospective research-data loss.
- **Root cause (CONFIRMED):** `kalshi/orderbook.py::reconstruct_best_quote`
  called `_validate_levels`, which raised on any price outside [1,99]; the
  exception propagated through `save_orderbook_snapshot`, so nothing persisted.
- **Fix (applied):** out-of-range price levels are excluded from best-bid
  derivation (`_quotable_levels`) instead of rejecting the book; negative
  quantity still fails closed; raw levels remain stored verbatim in
  `yes_levels_json`/`no_levels_json` (nothing silently discarded). Unit-tested.
- **Deployment:** takes effect after the next collector restart (a separately
  approved operator action); historical `malformed_payload` rows are left
  as-is (append-only). No migration, no data repair.
- **Reproduce:** `select ticker, count(*) from market_poll_attempts where
  outcome='malformed_payload' group by 1 order by 2 desc;`

### DQ-002 — Recovery-watch flapping → duplicate Telegram notifications · Priority 2 · CODE · MEDIUM · **DRAFT ONLY**

- **Symptom:** the recovery watch oscillates `RECOVERING ↔
  PAPER_VALIDATION_READY` every ~15–30 min and delivers a Telegram
  notification on **every** transition (dedup only suppresses same-state
  repeats). Alert fatigue; risk of masking a genuine outage alert.
- **Root cause (CONFIRMED):** `PaperRiskPolicy.max_book_age_seconds = 300s`
  (the paper-**execution** fill gate — intentionally strict) is reused by the
  watch as a steady-state health gate, but the collector's inherent cadence is
  ~555s (throttled over ~4675 tickers). The freshest book is therefore >300s
  old for most of every cycle, so `stale_book_evidence` fires intermittently.
- **Minimal safe fix (proposed, not applied):** decouple the watch's health
  signal from the fill gate — either give the watch a cadence-aware book bound
  (≥ ~2× cadence, e.g. 1200s) **or** add notification hysteresis/cooldown so a
  re-entry within a cooldown is not re-alerted. **Do NOT loosen the 300s
  paper-execution fill gate** (a fill still needs a genuinely fresh book).
- **Why not applied here:** safety-adjacent alerting semantics; the approach
  needs operator sign-off; validating that flapping stops wants the running
  monitor (a restart is out of scope). Unrelated to DQ-001 — not bundled.
- **Reproduce:** `tail recovery_watch_history.jsonl` and look for alternating
  `entered_recovering` / `entered_paper_validation_ready` with
  `"detail":"telegram: delivered"`; compare `max_book_age_seconds` (300)
  against the median kalshi run cadence (~555s).

## Resolved / known-issue history

- 2026-07-28: DQ-001 order-book price-0 whole-book rejection — fixed
  (`kalshi/orderbook.py`); deployment pending an approved collector restart.
