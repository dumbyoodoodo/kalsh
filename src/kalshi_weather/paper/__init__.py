"""Forward paper-trading skeleton (synthetic / manual signals ONLY).

Validates the operational chain

    live production market data (read-only)
      -> synthetic/manual signal -> order intent -> risk checks
      -> simulated fill -> immutable paper ledgers -> P&L / attribution

WITHOUT any predictive model. Hard boundaries, enforced by tests
(``tests/unit/test_paper_isolation.py``):

- **No exchange path.** Nothing in this package imports any Kalshi API
  transport layer; there is no code path that could submit a real or demo
  order.
- **No experiment coupling.** Nothing here imports the registered
  hypotheses' modules or reads their frozen artifacts -- registered
  experiments stay untouched and unconsumed.
- **Separate data domain.** All records go to a dedicated paper database
  (``PAPER_DATABASE_URL``); the production research database is only ever
  read. Simulated fills are never mixed into historical exchange
  observations.
- **No performance claims.** Every report is labelled synthetic/manual
  operational testing. Nothing here measures or implies predictive edge.

Fills, fees, risk primitives, and accounting reuse the audited
``execution/`` simulator (ADR 0015-0019); this package adds only the
forward-loop plumbing around it (signals, live evidence gates, cross-run
idempotency, kill switch, paper persistence, reporting).
"""
