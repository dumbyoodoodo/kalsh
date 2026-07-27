# ADR 0018: Historical-replay coverage gates and confidence classification

## Status

Accepted (2026-07-26). Extends the historical replay adapter (ADR 0017).
Read-only; isolated from H0019.

## Context

A deterministic, reconciled historical replay can still be a poor representation
of real execution when the source market data is sparse (wide book gaps, few
trades, unknown trade direction). ADR 0017's quality report surfaced these issues
but did not prevent a low-quality replay from running or grade the confidence of
its fills. Accounting correctness (the ledger reconciles) and execution-data
quality are separate concerns.

## Decision

Add `execution/coverage.py`: a versioned `ReplayCoveragePolicy`, per-gate
severity, an overall verdict, per-market quality scoring, and per-fill
confidence. Refine passive fills to be trade-direction- and gap-aware.

1. **ReplayCoveragePolicy** (versioned; every threshold in the manifest, none a
   hidden constant): ticker book/trade/settlement coverage %, median/max book
   age, book/trade gaps, min snapshots/trades per market, malformed rate, max
   conflicts, required production %, and strategy-requirement flags.
2. **Severity + verdict.** Each gate is `PASS`/`WARNING`/`FAIL`; a `FAIL` on a
   *required* gate makes the run `INSUFFICIENT_DATA`, any `WARNING` makes it
   `LIMITED_CONFIDENCE`, else `HIGH_CONFIDENCE`. A `FAIL` is never silently
   downgraded to a `WARNING`.
3. **Pre-run enforcement.** Coverage is evaluated BEFORE order intents are
   processed. `HIGH` runs normally; `LIMITED` runs with a recorded warning;
   `INSUFFICIENT_DATA` **refuses** unless `--allow-insufficient-coverage` is
   passed, in which case the run, manifest, and summary are prominently labelled
   `UNSAFE_COVERAGE_OVERRIDE` and no execution-fidelity claim is permitted.
4. **Per-market quality** (never a single global average): counts, staleness,
   gaps, malformed, conflicts, provenance, marketable/passive eligibility, and a
   confidence class with explicit reasons. A single unusable market in a set is
   named, not averaged away.
5. **Per-order/fill confidence** (`HIGH`/`MEDIUM`/`LOW`/`UNSUPPORTED`), derived
   ONLY from execution-data quality -- never from ledger reconciliation. A fresh
   marketable fill in a strong market is `HIGH`; a passive fill on sparse
   historical trades is `LOW`; a fill beyond the max book age is `UNSUPPORTED`
   (and should not occur); an unsafe override caps marketable fills at `MEDIUM`.
6. **Trade-direction-aware passive fills.** A resting order fills only from taker
   trades on the compatible aggressive side (`compatible_taker_side`); unknown
   `taker_side` is excluded by default (never guessed) unless
   `passive_allow_unknown_taker` is set. Passive fills are never converted to
   marketable.
7. **Gap-aware passive fills.** A gap between consecutive qualifying trades wider
   than `max_passive_trade_gap_seconds` truncates the evidence -- queue position
   is not assumed to persist across an unobserved interval. (The wait from
   submission to the first fill-trade is normal resting, not a data gap.)
8. **Artifacts.** `coverage_policy.json`, `coverage_results.json`,
   `market_quality.parquet`, `order_confidence.parquet`, `coverage_summary.md`,
   each hashed into the run manifest. The summary prominently states the verdict,
   failed/warning gates, excluded markets, coverage-rejected orders, low/
   unsupported-confidence fills, and whether an unsafe override was used.
9. **CLI.** `--coverage-policy` on `export-replay-data` and `simulate-history`;
   `--allow-insufficient-coverage` (never default) on `simulate-history`; and a
   read-only `paper inspect-coverage` that reports quality without simulating.

## Consequences

- Low-quality historical replays are refused by default and, when run under an
  explicit override, are unmistakably labelled unsafe.
- Execution confidence is explicit per market and per fill, and is independent of
  accounting correctness.
- The previous smoke market is honestly `LIMITED_CONFIDENCE` (median book age
  621s > 300s) with a `MEDIUM` fill -- thresholds were not loosened to preserve
  the earlier result.
- Follow-ups: an observed-availability timeline (rather than trade-gap proxy) for
  passive continuity, and per-strategy coverage-policy presets.
