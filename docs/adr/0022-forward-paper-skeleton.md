# ADR 0022: Forward paper-trading skeleton (synthetic/manual signals only)

Date: 2026-07-27
Status: Accepted

## Context

The execution simulator (ADR 0015-0019) is complete for historical replay,
but nothing validated the *forward* operational chain — live market data →
signal → intent → risk → simulated fill → immutable ledgers → reporting.
CLAUDE.md gates strategy-driven trading logic behind a calibrated,
backtested model; no such model exists (H0019/H0020 are NOT_READY). This
skeleton therefore validates execution plumbing only: it accepts
**synthetic or manually supplied signals exclusively**, makes no predictive
claim, and has no exchange path.

## Decision

New package `src/kalshi_weather/paper/`:

- `signals.py` — four non-predictive sources (constant probability,
  deterministic mid+offset rule, manual probability file, manual intent
  file). Every signal is versioned, expiring, and content-hashed;
  unversioned/expired/tampered signals are rejected.
- `evidence.py` — read-only production loader (latest snapshot, latest
  book via the existing `history.normalize_book`, latest direct-poll
  evidence per ADR 0020).
- `engine.py` — pure forward loop reusing the audited `execution/`
  Simulator, fills, fees (authoritative Kalshi model), and ledger.
  Fail-closed gates (stale snapshot/book, missing poll evidence, low
  confidence), fee-aware side selection at *executable* prices (asks for
  YES, bids for NO — never mid), and a versioned draft paper risk policy
  (`DRAFT — NOT VALIDATED FOR LIVE TRADING`): per-intent, gross/net,
  per-market, per-station, daily-loss caps.
- `store.py` — **dedicated paper database** (`PAPER_DATABASE_URL`, SQLite
  default) on its own metadata: ten append-only `paper_*` tables (runs,
  signals, market snapshots, intents, risk decisions, fills, cash ledger,
  positions, P&L snapshots, attribution) plus a kill-switch control table.
  No Alembic migration and no production schema change — the strongest
  isolation option in the task's menu, chosen so simulated fills can never
  share a database with authoritative collected history.
- `runner.py`/`reporting.py`/CLI (`paper forward-run|forward-status|
  forward-report|kill|resume|reconcile`) — provenance-stamped runs (git
  SHA, config hash, fee/fill/risk versions), banner-labelled reports,
  ledger-replay reconciliation.

Idempotency: an intent's order id is a hash of (strategy, version, ticker,
signal content hash); reruns with the same signal content are recorded as
`duplicate_intent` rejections and can never re-fill. Cross-run accounting
state is rebuilt by replaying the stored cash ledger through
`execution/ledger.py`'s validating arithmetic.

Isolation is enforced by tests (`test_paper_isolation.py`): no experiment
imports, no Kalshi transport imports (no exchange path exists), no
live-order symbols, no research-row construction, no update/delete.

## Consequences

The operational chain is provable end-to-end today (disposable-DB
integration tests cover the evidence-backed fill path; a live controlled
session proved the fail-closed stale-data path during a real Kalshi API
outage). When a hypothesis someday earns a paper pilot, only a signal
source needs to be added — behind its own preregistration, per
`docs/research/backtest_to_paper_bridge.md`. Any recurring schedule
remains deliberately out of scope.

**Update 2026-07-27 — settlement implemented.** `paper/settlement.py` +
`paper settle` close the position lifecycle: authoritative outcomes come
from append-only `market_snapshots` results (`yes`/`no`, `settlement_ts`,
terminal status); the full recorded result history per ticker must agree
(conflicts fail closed as ambiguous); payouts flow through the audited
`Simulator.settle`/`Position.settle` arithmetic into the same paper cash
ledger; each position settles exactly once (idempotent reruns skip via
recorded `paper_settlements` state); void/unsupported outcomes are
recorded skips; and evidence contradicting an already-paid settlement
appends a `correction_detected` successor (`supersedes_id`) with **no
automatic money movement** — reversal is a human decision. No settlement
fee is charged (Kalshi assesses trading fees, not settlement).
