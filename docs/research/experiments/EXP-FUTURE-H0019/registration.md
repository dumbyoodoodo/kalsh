# EXP-FUTURE-H0019 — registration (frozen 2026-07-26)

**Immutable registration** of the future held-out replication of H0018. Written
*before* its test data exists. See `HYPOTHESES.md` H0019 and `config.json`.

## Why this exists
H0018 (`EXP-20260726-H0018`) is **closed and immutable** with verdict
`NOT SUPPORTED`. Its intended held-out test was not evaluable because reliable
all-station forecast-vintage collection began **2026-07-21**, after H0018's
pre-registered train/validation windows. Missed forecast vintages cannot be
recovered, so those windows cannot be repaired. Re-running H0018 with different
dates is forbidden; a **new experiment identifier (H0019)** with **future**
windows is required — this document.

## Frozen future windows (all future at registration; test untouched)
- Train: 2026-07-22 → 2026-08-04
- Validation: 2026-08-05 → 2026-08-11
- Test: 2026-08-12 → 2026-08-25

Chronological (train < val < test), no overlap. Splits are by `target_date`.

## Independent grouping
Inference unit = **event-group (station, variable, target_date)**. Threshold
markets on the same underlying outcome are not independent. Because the split is
by target_date and the group contains target_date, no event-group crosses a
split (enforced by test).

## Readiness (execution gate)
Stated in event-groups (never raw rows): train ≥ 40, val ≥ 15, test ≥ 25; ≥ 3
test stations; ≥ 4 test event-groups per station; no station > 40% of test
event-groups; ≥ 5 pos & ≥ 5 neg labels in train and test; window-scoped
completion ≥ 0.85; ≥ 25 bootstrap units. **Operational minimums, not a power
analysis.** `experiment readiness h0019` reports the state
(NOT_READY → TRAIN_READY → VALIDATION_READY → READY_FOR_FINAL_TEST → COMPLETED).

## Model & feature spec (frozen)
Identical to H0018 (M0–M4; weather+contract features; no liquidity; L2=1.0; clip
[0.02,0.98]; Platt on train+val only; seed 20260726; 2000 grouped bootstraps;
primary M3−M1 Brier on test). Any change must be documented here *before* any
H0019 test data is inspected.

## Anti-peeking
The readiness command is read-only and never trains a model, produces a test
prediction, or computes a test Brier. The final test is a separate explicit
command that first verifies all readiness conditions. Readiness invocations are
logged (state only) in `readiness_log.jsonl`; no test analysis is ever recorded
there.

## Decision rule
CONFIRMED only if the M3−M1 test Brier 95% grouped-bootstrap CI is entirely < 0
with no major station-subgroup reversal and the concentration cap holds;
SUPPORTED BUT INCONCLUSIVE if the point improves but the CI spans 0 or coverage
is marginal; NOT SUPPORTED otherwise; INVALID on any integrity failure. No
tradable or market-efficiency claim under any outcome.
