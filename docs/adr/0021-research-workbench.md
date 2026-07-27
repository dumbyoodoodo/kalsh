# ADR 0021: Generic research workbench (`kalshi_weather.research`)

Date: 2026-07-27
Status: Accepted

## Context

Through H0018/H0019 the project accumulated proven research machinery —
point-in-time joins (`dataset/asof.py`), probability metrics and a grouped
bootstrap (`experiments/baseline.py`), dataset manifests
(`dataset/manifest.py`), and an execution-aware replay stack
(`execution/`). But the *evaluation workflow* around them lived inside
hypothesis-specific runners: split boundaries hardcoded in
`experiments/runner.py`, multiple-testing accounting frozen per-experiment
(H0005's Bonferroni), and no machine-readable, append-only record of what
has been hypothesized, tested, or burned as a test window. An audit against
the future-experiment checklist found: walk-forward evaluation, feature
ablation, sensitivity analysis, a research ledger, and a fee-aware
probability→decision translation **missing**; splits, benchmark comparison,
and multiple-testing accounting **hypothesis-specific**.

## Decision

Add `src/kalshi_weather/research/` — a hypothesis-agnostic workbench with
six modules and hard rules:

- `splits.py` — group-atomic chronological splits + expanding walk-forward
  folds. A group (e.g. an event-day) is assigned its **latest** date, so a
  boundary-spanning group is held out longer, never leaked earlier. Leakage
  is both prevented by construction and re-checked (`assert_no_group_leakage`).
- `benchmark.py` — grouped-bootstrap benchmark comparison generalized to any
  per-row paired loss. `experiments.baseline.grouped_bootstrap_brier_diff`
  is **left untouched** (it is part of the frozen H0018/H0019 spec); a test
  pins the generic implementation to reproduce it exactly for Brier.
- `ablation.py` — drop-one-declared-family refits via a caller-supplied
  `fit_and_eval`. Refuses evaluation labeled as a test set, and refuses
  undeclared/overlapping features (no hidden feature selection).
- `sensitivity.py` — single-parameter perturbations around a frozen base;
  the base result is the primary and is never replaced. Multi-parameter
  perturbation is unsupported by design (a grid search in disguise).
- `ledger.py` — append-only, hash-chained JSONL research ledger. Enforces:
  no overwriting (supersede-only), success rules frozen once test data was
  accessed, test-window reuse must be acknowledged on the record, and
  explicit family counts for multiple-testing statements. `HYPOTHESES.md`
  remains the narrative source of truth; the ledger is its machine-readable
  companion, adopted for hypotheses going forward (no bulk backfill —
  legacy entries may be backfilled individually, marked via `source`).
- `translation.py` — fee-aware probability→hypothetical-decision bridge on
  the authoritative `KalshiEventContractFeeModel`. Integer-centicent
  arithmetic, draft-only thresholds (constructor rejects a non-draft
  version tag), and no performance claims.

Calibration diagnostics are **re-exported** from `experiments.baseline`,
not duplicated — they were already generic.

Isolation is enforced by tests (`test_research_isolation.py`): no H0019 or
frozen-experiment references, no experiments imports beyond `baseline`, no
wall clock or ambient randomness, no hardcoded `docs/research` paths.

## Alternatives considered

- **Extending `experiments/`** — rejected: that package's modules are
  frozen artifacts of registered experiments; mixing generic evolving code
  into it invites accidental mutation of frozen specs.
- **Backfilling all 19 historical hypotheses into the ledger now** —
  rejected as a bulk transcription with no consumer; the ledger's
  invariants matter for *future* registrations, and legacy entries can be
  added one-by-one, marked `source="HYPOTHESES.md backfill"`, when a new
  registration needs them for overlap/family accounting.
- **scikit-learn for ablation/model plumbing** — rejected: the required
  stack forbids ML libraries without a documented baseline justification;
  the workbench takes caller-supplied callables instead of owning models.

## Consequences

Future hypotheses (H0020+) get leakage-safe splits, benchmark CIs,
diagnostics, and registry discipline without re-implementation — and the
next registration is expected to use the ledger from day one. The bridge
from a supported result to paper trading is specified in
`docs/research/backtest_to_paper_bridge.md` (documentation only; no forward
loop exists yet, per the phase gates).
