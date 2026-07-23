# Runbook: post-expiration settled-metadata revision

Kalshi keeps revising a settled market's `volume`, `open_interest`, and
occasionally `result` **after** publishing `status="finalized"` with a
`settlement_ts`. The market's own `expiration_time` (~7 days after
`close_time`) is when they stop. See `docs/adr/0011-settled-metadata-revision.md`
for the measurements behind this.

**`status="finalized"` does not mean the metadata is final.** That distinction
is the whole point of this pass.

## Four operator-visible states

| State | How to recognize it | Action |
|---|---|---|
| **pending settlement** | latest snapshot has no `result` | none — the settlement queue handles it |
| **provisional settled metadata** | has `result`, but `expiration_time` (or `close_time + 7d`) has not passed | **none — this is expected and healthy.** Values may still move |
| **post-expiration verified** | a `market_metadata_verifications` row with outcome `unchanged` or `changed` | none — authoritative |
| **permanently unavailable** | verification outcome `market_removed` or `retention_expired` | none — terminal, unrecoverable |

Only the first and last are ever worth acting on, and neither is an alert.
**Provisional metadata inside the venue's finality window is normal**, applies
to every market that settles, and must not raise a finding — it would fire
constantly and mean nothing.

## Running it

The pass runs automatically inside `ops run` as a fourth concurrent loop,
recorded under `collector="revision"` in `collector_runs`:

```bash
uv run kalshi-weather ops run
```

Cadence `REVISION_SYNC_INTERVAL_SECONDS` (default 3600), bounded by
`REVISION_SYNC_LIMIT_PER_CYCLE` (default 50). Hourly is ample: markets become
eligible only as they cross their finality time, roughly a day's settlements at
a time. A cycle with nothing eligible issues **zero** API calls.

Manually, for one market or a bounded batch:

```bash
uv run kalshi-weather ops revision --limit 5 --dry-run     # classify, write nothing
uv run kalshi-weather ops revision --ticker KXTEMPAUSH-26JUL2310-T79.99
```

## Per-cycle metrics

`collector_runs.stats_json` for `collector="revision"`:

| Metric | Meaning |
|---|---|
| `candidates_selected` | eligible markets found (before the limit) |
| `attempted` | markets actually fetched this cycle |
| `changed` / `unchanged` | venue values differed / matched |
| `snapshots_appended` | new `market_snapshots` rows (equals `changed`) |
| `result_revisions` | **settlement labels the venue corrected — watch this one** |
| `volume_revisions`, `open_interest_revisions` | which fields moved |
| `market_removed` | terminal: venue no longer serves the ticker (recent) |
| `retention_expired` | terminal: aged past the retention horizon |
| `api_failure` | transient; retried next cycle, records nothing |
| `errors` | unexpected failures |

`result_revisions > 0` means a stored settlement label was wrong and has now
been corrected in the archive. That is worth reading the log line for
(`metadata_revision.revised`), not because anything is broken — the mechanism
worked — but because settlement labels are the dependent variable for the
research program.

## Guarantees

- **Nothing is ever rewritten.** A changed market appends exactly one new
  snapshot; the provisional row stays as valid provenance and revision history.
- **Unchanged means no snapshot.** The check is recorded only in
  `market_metadata_verifications`, so snapshot history stays clean.
- **Each market is fetched once**, then retires from the queue via a SQL
  anti-join — not an in-process set, so nothing accumulates and a restart
  changes nothing.
- **Eligibility filters before `LIMIT`**, so ineligible or already-verified
  markets cannot consume the per-cycle budget (the ADR 0010 failure mode).
- **Throttle unchanged** — reuses the existing client spacing and bounded
  retries; no new request path.

## Troubleshooting

| Symptom | Diagnosis | Action |
|---|---|---|
| `candidates_selected=0` every cycle | normal when no market has crossed finality since the last pass | none — confirm with `ops revision --dry-run --limit 5` |
| `attempted=0` but `candidates_selected>0` | should be impossible; indicates a selection bug | check the `metadata_revision.cycle_failed` log |
| `api_failure` persistently on the same market | a market failing every cycle occupies one slot of the limit | it stays retryable by design; if it never resolves, verify it manually with `--ticker` |
| `market_removed` climbing | expected for extreme-strike hourly contracts, which the venue removes within hours of close | none — terminal and unrecoverable |
| `retention_expired` climbing on a backfill | markets aged past ~67 days are no longer fetchable | expected; their data stays provisional and is documented as such |

## Limitation: historical data

Every settled market captured before this pass existed carries provisional
metadata. The historical backfill is **deliberately not run** as part of
introducing this mechanism — see ADR 0011 "Explicitly deferred". Markets past
the ~67-day retention horizon may never be recoverable.
