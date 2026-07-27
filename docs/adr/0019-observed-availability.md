# ADR 0019: Observed-availability timelines and strategy coverage presets

## Status

Accepted (2026-07-27). Extends the historical replay adapter (ADR 0017) and
coverage gates (ADR 0018). Read-only; isolated from H0019. No migration.

## Context

ADR 0018 truncated passive-order continuity with a trade-gap proxy: a gap between
consecutive qualifying trades reset queue evidence. That proxy cannot tell "no
trades happened" from "the collector was down and observed nothing" -- so a
passive order could appear to hold its queue position across a real collector
outage. We need to distinguish (1) actively observed, (2) inactive market, and
(3) collector unavailable / delayed / rate-limited, and to forbid passive queue
persistence across periods where observation continuity is not established.

## Availability-evidence audit (Phase 3)

`collector_runs` records each ~5-10 min cycle collector-wide (started/finished,
success, requests, retries, aggregate stats) -- but NO per-ticker polling list.
Per-market observation POINTS come from `orderbook_snapshots.captured_at` and
`market_snapshots.observed_at` (both dedup unchanged content, so they are
positive witnesses, not continuous coverage). Real data shows a normal ~5-10 min
cadence plus genuine multi-hour outages and failed cycles (DNS, connect-timeout,
two 429 rate-limits). Classification:

- DIRECT per-market: a snapshot for the ticker proves it was polled at that
  instant.
- Per-cycle uptime: a successful `collector_runs` cycle polls all discovered
  weather tickers.
- Collector-wide gaps / failed cycles: COLLECTOR_UNAVAILABLE.
- No collector run covering the time: UNKNOWN.

This is SUFFICIENT for a cycle-granular per-market timeline -- **no migration
required**. Limitation: LIKELY_OBSERVED is a cycle-level inference (collector up
+ ticker within its observed active window), not per-cycle proof.

## Decision

`execution/availability.py`: a frozen, versioned `ObservationAvailabilityPolicy`
and a read-only builder producing a bounded, deterministic per-ticker timeline of
non-overlapping `ObservationInterval`s in states OBSERVED / LIKELY_OBSERVED /
COLLECTOR_UNAVAILABLE / MARKET_NOT_ELIGIBLE / UNKNOWN, each carrying evidence
type, confidence, reason, collector-run IDs, source refs, and policy version.

- Between two direct observations while the collector is continuously up, the
  span is OBSERVED (unchanged books deduped -- the market WAS polled). Collector
  up + ticker eligible but no direct witness -> LIKELY_OBSERVED. Gaps beyond
  cadence / failed cycles -> COLLECTOR_UNAVAILABLE. No collector run -> UNKNOWN.
  Missing evidence is NEVER counted as observed. Explicit tickers + bounded range
  required; no writes to PostgreSQL.

**Passive fills** (Phase 6): queue-ahead accumulation continues only while
continuity holds. The first continuity-breaking interval after submission
(COLLECTOR_UNAVAILABLE, UNKNOWN, MARKET_NOT_ELIGIBLE, or -- unless the policy
opts in -- LIKELY_OBSERVED) truncates the evidence; a later trade never bridges
an unobserved interval. The trade-gap proxy remains a SECONDARY safeguard and
never overrides an availability interruption. Direction-aware compatibility and
unknown-taker exclusion are unchanged; passive never becomes marketable.

**Strategy presets** (Phase 7): versioned `strict-marketable` (fresh books,
settlement, production; passive disabled), `passive-research` (trades +
observed-availability continuity + low unknown tolerance + settlement +
production), and `exploratory` (LOW-FIDELITY; permits warnings but never relaxes
the fatal conflict/provenance gates). Presets serialize every threshold and their
name+version into run artifacts; a JSON policy may override a preset only as an
explicit recorded config; no preset uses an unsafe override.

**Coverage/confidence** (Phase 8): per-market gains observed/likely/unknown/
collector-unavailable percentages, max unknown/unavailable gaps, and continuity
breaks; availability gates apply when a timeline is supplied. Confidence still
measures execution-data quality only -- ledger reconciliation never raises it.

**Artifacts** (Phase 9): `availability_policy.json`,
`availability_timeline.parquet`, `availability_summary.json/.md`,
`coverage_preset.json`, and enhanced market_quality / order_confidence, all hashed
into the manifest and byte-deterministic for identical inputs.

**CLI** (Phase 10): read-only `paper inspect-availability` (build from DB, with
optional `--output` export), `--coverage-preset` on `simulate-history`,
`--preset` on `inspect-coverage`; export writes the availability timeline.

## Consequences

- Passive continuity now respects real collector downtime: a 22h collector
  outage in the archive is surfaced (46% collector-unavailable over a 2-day
  range) and blocks passive fills across it, where the trade-gap proxy could not.
- The earlier "sparse book" reading is corrected: ~10 min book gaps are the
  polling cadence with deduped books -> continuously OBSERVED, not gaps.
- Availability is cycle-granular and collector-wide; LIKELY_OBSERVED is inferred.
  A future additive per-ticker polling record could make it per-cycle exact --
  deferred (no migration now). No strategy-performance claim is made.
