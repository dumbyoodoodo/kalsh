# Runbook: Settlement-time labels

Rebuilding and validating the canonical settlement labels (E-A,
`docs/adr/0006-settlement-labels.md`): the per-market distinction between the
value at market close, the value at Kalshi's settlement determination, and
the latest (possibly corrected) final value.

## What the labels are

One label per market, derived deterministically from stored market snapshots
(settlement fields: `result`, `expiration_value`, `settlement_ts`, strikes)
plus the CLI issuance history, via strict as-of selection. Statuses:

- `resolved` — exact `settlement_ts` available; fully reconstructed.
- `bounded` — `settlement_ts` missing; conservative close+48h window used
  (assumption documented on the label itself).
- `ambiguous` / `unsupported` / `missing_source_data` — unusable, with
  machine-readable reasons; excluded from downstream research and counted.

**Downstream rule: market experiments use `value_at_settlement`** (or
Kalshi's own `kalshi_expiration_value` where present) as the outcome label —
never `settled_value`/`latest_final_value`, which may include post-settlement
corrections the market never saw.

## Rebuilding

Labels are derived data — rebuilt on every dataset export, no separate step:

```bash
uv run kalshi-weather dataset build          # or ops snapshot
```

The export contains `settlement_labels.parquet` (all labels + provenance +
per-label notes) and `market_weather.parquet` gains `value_at_close`,
`value_at_settlement`, `latest_final_value`, `settlement_label_status`,
`settlement_label_version`. The manifest records
`settlement_label_reconstruction_version` and the frame's content hash.

Prerequisites for meaningful labels: settled market snapshots with settlement
fields (collected by `collector run`/`ops run` since migration `0006`;
retro-fetchable only ~2-3 months back — older market history is
unrecoverable, see the ADR) and CLI issuance history (`weather backfill`).

## Validating

```bash
PYTHONPATH=src python scripts/exp_ea_settlement_labels.py \
    --snapshot-dir <DATASET_DIR> --out <RESULTS_JSON>
```

Reports: label status counts; **payout agreement** (reconstructed
`value_at_settlement` vs Kalshi's `expiration_value`, and implied result vs
`result`) with every mismatch listed individually; and the H0012
stage-difference metrics. Treat a value-agreement rate below ~99% as a
reconstruction defect to investigate, not a finding to report.

## Interpreting mismatches

- `payout_value_agrees=False` — our as-of issuance at `settlement_ts`
  differs from what Kalshi paid on: either a missing issuance in our archive
  (check IEM for that date), or Kalshi used a source revision we lack.
  Investigate before trusting that date's label.
- `payout_result_agrees=False` with `payout_value_agrees=True` — strike
  semantics bug; must be fixed, never rationalized.
- `bounded` labels — acceptable for research with the pre-registered
  sensitivity check (exact-only subset); prefer exact where present.
