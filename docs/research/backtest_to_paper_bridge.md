# Backtest → paper-trading bridge (specification only)

Status: **DRAFT SPECIFICATION — nothing here is implemented as a forward
loop, and every numeric threshold below is a placeholder that must be
separately preregistered before any paper trading begins.** This document
exists so that, *if* a future hypothesis produces a supported predictive
result, the translation into paper trading is already specified — instead
of being improvised (and overfit) after seeing results.

Preconditions (all must hold before any forward loop is built):

1. A registered hypothesis reached its preregistered success rule on an
   untouched future test window (e.g. a successor in the H0019 lineage).
2. The result survived its preregistered replication, or the replication
   is itself the trigger experiment.
3. The phase gates in `ROADMAP.md` for paper trading are met.
4. The thresholds below were re-registered as a frozen paper-trading spec
   (a `PREREG-*` document), **before** observing any forward performance.

## 1. Forecast → edge

- Model output: a calibrated probability `p̂` for the YES outcome at the
  decision timestamp, produced by the frozen model version recorded in the
  experiment artifact (no refit at decision time unless the spec says so).
- Market input: the contemporaneous best executable price from replayed or
  live order-book data — never a candle close when a book is available.
- Edge is computed per side in integer centicents via
  `research/translation.py`: fair value `p̂·100¢` vs the side's cost, minus
  the taker fee from `execution/fees.KalshiEventContractFeeModel`
  (`ADR 0016`). The maker/taker assumption used must be recorded per
  decision.

## 2. Decision gates (all DRAFT values)

| Gate | Draft rule | Rationale |
|---|---|---|
| Minimum net edge | net edge ≥ **300 centicents (3¢)** per contract after fees | below transaction-scale noise, an "edge" is indistinguishable from calibration error |
| Book freshness | order-book snapshot age ≤ **60 s** at decision time | stale books manufacture phantom edges |
| Availability confidence | decision window classified continuous by `execution/availability.py` under the preset in force | passive fills can't be simulated where observation had gaps |
| Coverage gates | replay coverage gates (`execution/coverage.py`, ADR 0018) pass for the market's history | no decisions on markets whose data lineage is broken |
| Liquidity | top-of-book depth ≥ **5×** intended size on the taken side | a 1-lot edge that vanishes at size is not an edge |
| Probability sanity | `p̂` within the model's preregistered clip range | out-of-range probabilities indicate an upstream fault, not opportunity |

A decision failing any gate is a recorded rejection with its reason — the
rejection stream is a first-class output (it is how gate miscalibration is
detected), not silent filtering.

## 3. Sizing and exposure (all DRAFT values)

- Fixed fractional sizing at flat **1-contract** units for the first
  preregistered paper window; no Kelly or edge-proportional sizing until
  calibration of `p̂` in forward data has itself been evaluated.
- Exposure limits, enforced by `execution/risk.py`'s engine: per-market
  max **5** contracts; per-event-day max **10**; total open max **50**;
  daily new-position max **20**. Violations reject the trade, never resize
  it silently.
- One position direction per market at a time; no averaging into losers.

## 4. Kill switch and stop conditions

- Manual kill: a single flag halts new decisions; existing paper positions
  are left to settle (paper positions carry no real risk; unwinding them
  would distort attribution).
- Automatic halts (draft): data-quality CRITICAL alert on any feed the
  strategy consumes; collector gap > 2 cycles; schema-revision mismatch;
  probability-sanity failures > 1% of decisions in a rolling day.
- Every halt is an observatory event with an explicit re-arm procedure —
  re-arming is a human act, never automatic.

## 5. Attribution and reporting

Daily paper P&L must be attributable along four axes, or the number is
noise: by **signal** (which hypothesis/model version produced `p̂`), by
**market family** (station × variable × threshold-direction), by
**station**, and by **execution-confidence class** (availability/coverage
tier under which the fill was simulated). Fees are reported separately from
gross edge capture. Calibration of `p̂` (reliability, Brier vs the market
benchmark) is reported alongside P&L — a profitable-but-miscalibrated
forward run is a red flag, not a success (`RESEARCH.md`).

## 6. What this document is not

- Not an implementation: there is no forward loop, no scheduler, no order
  path, and none may be built before the preconditions above.
- Not a performance claim: no threshold here was chosen from observed
  test-window profits; they are conservative placeholders to be
  re-registered (and only then frozen) at paper-trading time.
- Not a live-trading spec: live trading has its own, stricter gate set
  (`CLAUDE.md` non-negotiables) layered on top of everything here.
