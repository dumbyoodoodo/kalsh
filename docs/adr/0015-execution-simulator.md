# ADR 0015: Deterministic paper-trading / execution simulator

## Status

Accepted (2026-07-26). Orthogonal to H0018/H0019 (isolated by construction).

## Context

We need to answer execution questions (would an order fill, at what price, what
fees, positions, cash, P&L, risk) BEFORE any strategy is trusted -- and to do so
reproducibly, offline, with no exchange risk. CLAUDE.md forbids order-submitting
trading logic until a backtested model exists; this is an **offline simulator
with no order-submission path** (no real orders, no demo orders, no
authenticated exchange requests), so it satisfies that rule's intent while
providing the execution-modeling half of a backtester.

## Decision

A self-contained `execution/` package that evaluates hypothetical orders against
**synthetic fixtures or explicitly-supplied historical production data**. Key
choices:

1. **Money is integer cents** (reuses `domain/money.py`); no binary float touches
   accounting. A Kalshi binary contract is worth 0 or 100 cents at settlement.

2. **Position representation**: LONG YES + LONG NO lots per market (the only
   holdable things on Kalshi's collateralized binary book), with total-cost-cents
   basis so proportional closes are integer-exact. "Sell YES" beyond held YES
   opens NO at the complementary price `100 - yes_price` (Kalshi mechanics),
   preserving user-facing YES/NO + buy/sell semantics. `max_loss = total_cost -
   min(yes,no)*100`.

3. **Append-only ledger is the sole accounting record**; the portfolio is a pure
   function of ledger replay. Each entry records its cash delta AND enough
   payload to recompute it; the two are asserted equal on apply (per-event
   reconciliation). Invariants (`cash+reserved >= 0`, `reserved >= 0`) checked
   after every event. Settlement is idempotent (a re-applied settle is a
   0-delta no-op).

4. **Three fill models**: A marketable (consume visible depth, respect the
   limit, partial on insufficient depth); B passive (fills only up to realized
   subsequent volume-through minus a queue-ahead assumption, never using
   pre-submission info -- true queue position is NOT reconstructable from public
   data, so this is deliberately pessimistic); C synthetic fixtures (default for
   tests). Order TYPE (marketable_limit vs passive_limit) selects A vs B.

5. **Versioned fee model**, ZERO by default. The configurable formula is a
   labeled PLACEHOLDER -- NOT verified against Kalshi's live schedule, which was
   not fetched from an authoritative source. Its version is recorded on every
   fill and in the run manifest.

6. **Pre-trade risk engine** with specific rejection reasons; runs before
   acceptance; never silently resizes.

7. **Persistence = in-memory + immutable run artifacts** (no DB). Simulated
   activity is NEVER written into any exchange-data table -- it is entirely
   separate from collected market data and from any future real-order store.

8. **Deterministic replay ordering**: events processed in strict
   (timestamp, kind_rank, tiebreak_id) order (book<trade<intent<settlement<mark);
   an intent uses only the latest book at-or-before submission and only trades
   strictly after it -- no future event can affect an earlier fill. A settlement
   is never applied before its recorded availability time.

9. **H0019 isolation**: the package imports nothing from `experiments`, reads no
   H0019/H0018 artifact, computes no model performance; a test pins this.

## Consequences

- Execution assumptions are explicit, versioned, and recorded; runs are
  byte-reproducible from their inputs.
- A profitable synthetic scenario is NOT evidence of a real edge, and demo
  execution is NOT equivalent to realistic production fills -- both stated in the
  metrics and docs.
- Real fee schedule, order-book-derived historical fills, and a generic
  (experiment-agnostic) prediction interface are deferred follow-ups.
