# ADR 0016: Authoritative Kalshi event-contract fee model

## Status

Accepted (2026-07-26). Extends the execution simulator (ADR 0015). Orthogonal to
H0019.

## Context

ADR 0015 shipped the simulator with a ZERO-default, labelled PLACEHOLDER fee
model. To evaluate hypothetical orders against realistic costs we need an
event-contract fee model grounded ONLY in official Kalshi documentation, not
memory or third-party calculators, and versioned so historical simulations stay
reproducible if Kalshi changes its schedule.

## Authoritative sources (what grounds each term)

1. **Kalshi Fee Schedule, filed with the CFTC** — cftc.gov filing
   `rule091222kexdcm003` ("Kalshi Fee Schedule.docx"), effective **2022-09-12**.
   Primary regulatory source. Provides, verbatim:
   - General trade fee: `fees = round up(0.07 x C x P x (1 - P))`, where `P` is
     price in dollars, `C` the number of contracts, "round up" is to the next
     cent.
   - S&P-500 / NASDAQ-100 special schedule: `round up(0.035 x C x P x (1 - P))`
     for markets whose ticker begins `INX` or `NASDAQ100`.
   - No settlement fee, no membership fee. Resting orders are not charged a trade
     fee in this filing (no maker fee).
   - Published fee tables for 1 and 100 contracts (transcribed into
     `tests/unit/_fee_tables.py` and asserted).
2. **Kalshi Predictions API docs — "Fee Rounding"** —
   `docs.kalshi.com/getting_started/fee_rounding`, accessed **2026-07-26**.
   Provides, verbatim: per-fill trade fee ceiled to the nearest **centicent**
   ($0.0001); a balance-precision **rounding fee** (target $0.01 for non-direct
   members, $0.0001 for direct members); and a **per-order fee accumulator** that
   issues a whole-cent ($0.01) **rebate** whenever accumulated rounding
   overpayment exceeds $0.01, spanning both taker and maker fills. Two worked
   examples are reproduced as tests.

The two sources are consistent: for whole-cent prices at $0.01 target precision
the rounding mechanics reproduce the CFTC per-trade table exactly (verified in
tests), and the accumulator adds multi-fill convergence on top.

### Documented limitation (why the verdict is "with limitations")

The **current** consolidated schedule PDF `kalshi.com/docs/kalshi-fee-schedule.pdf`
("Fee Schedule for July 2026") was **not reachable from this environment (HTTP
429)** — consistent with `docs/API_VERIFICATION.md`'s note that Kalshi docs are
unreachable here. Consequences:
- The general `0.07` coefficient is grounded on the CFTC-filed schedule and could
  not be re-confirmed as unchanged for July 2026 from an accessible official
  source (third-party sources corroborate it but are not authoritative and were
  not relied on).
- Current per-market **maker-fee** coefficients could not be retrieved from an
  official source. The model therefore **defaults to charging no maker fee**
  (matching the CFTC filing) and exposes the maker coefficient as configuration
  for when an official value is supplied. Some current markets do charge maker
  fees; that value is intentionally not hard-coded.

## Decision

Add three concrete fee models behind one structural interface (`FeeModelLike`),
preserving the existing abstraction:

- `ZeroFeeModel` — charges nothing (safe default).
- `ConfigurableFeeModel` — the original labelled placeholder (unchanged math;
  `FeeModel` remains an alias to it so existing configs/tests reproduce).
- `KalshiEventContractFeeModel` — authoritative. Trade fee per fill:
  `ceil(coeff x C x p x (100 - p) / denom)` centicents, `coeff` = 0.07 general or
  0.035 for `INX*`/`NASDAQ100*`; then the Predictions-API balance rounding and
  per-order $0.01 rebate accumulator (`apply_rounding`). Records fee-model
  version, market schedule, trade fee, rounding fee, rebate, and net fee per
  fill.

**Precision.** Fee intermediates are **integer centicents** ($0.0001); the cash
ledger stays in **integer cents**. No binary floating point touches any fee.
For whole-cent prices and whole-contract quantities (the only inputs the
simulator produces) the net fee is always cent-aligned and the model reproduces
both sources' worked examples exactly. Sub-cent prices / fractional contracts
(direct-member, 6-decimal intermediates) are out of the simulator's input domain
and are documented as such.

**Accumulator.** Held per order by the engine and threaded across all fills of an
order, so multi-fill rounding converges (rebates) exactly as documented — not
rounded independently per fill.

**Versioning.** A run pins the fee-model version and records full provenance
(sources, effective date, coefficients, limitations) in the run manifest, so a
schedule change later is a new version, and old runs stay reproducible.

## Consequences

- Fees are grounded in official documentation, versioned, and reproducible;
  every published example is a test.
- The general coefficient's July-2026 currency and current maker coefficients are
  not confirmed (current PDF unreachable) — hence maker fee off by default and a
  "documented limitations" verdict rather than "verified".
- Simulated fees are an estimate; the exchange reports realized fees after
  execution and Kalshi may change its schedule. This is not tradable-edge
  evidence and this simulator still submits no orders.
