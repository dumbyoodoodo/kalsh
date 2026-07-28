"""Order-book continuity classification from append-only evidence.

Read-only measurement tooling: given lightweight metadata records for stored
book snapshots, per-ticker poll attempts, and collector cycles, classify each
inter-snapshot interval as confirmed-unchanged, changed, failed-poll,
no-evidence, collection-gap, ambiguous, legacy, or still-open. Purely
descriptive bookkeeping over caller-supplied rows -- no outcome fields, no
scoring, no strategy logic, and no ambient clock (the caller injects
``as_of``). Fails closed: an interval is only called confirmed-unchanged when
a successful same-ticker poll with matching provenance completed inside it.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

#: Poll outcomes proving the ticker was observed successfully.
SUCCESS_OUTCOMES = frozenset({"succeeded_new_data", "succeeded_unchanged", "succeeded_empty"})
#: Poll outcomes proving an attempt happened but observation failed.
FAILURE_OUTCOMES = frozenset(
    {
        "rate_limited",
        "api_failure",
        "malformed_payload",
        "persistence_failure",
        "unknown_failure",
        "cancelled",
    }
)


class IntervalClass(StrEnum):
    CHANGED_AFTER_SUCCESSFUL_POLL = "CHANGED_AFTER_SUCCESSFUL_POLL"
    UNCHANGED_CONFIRMED_BY_SUCCESSFUL_POLL = "UNCHANGED_CONFIRMED_BY_SUCCESSFUL_POLL"
    FAILED_POLL_INTERVAL = "FAILED_POLL_INTERVAL"
    NO_DIRECT_POLL_EVIDENCE = "NO_DIRECT_POLL_EVIDENCE"
    COLLECTION_GAP = "COLLECTION_GAP"
    AMBIGUOUS_PROVENANCE = "AMBIGUOUS_PROVENANCE"
    LEGACY_PRE_POLL_LEDGER = "LEGACY_PRE_POLL_LEDGER"
    OPEN_INTERVAL_NOT_YET_CLASSIFIABLE = "OPEN_INTERVAL_NOT_YET_CLASSIFIABLE"


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BookMeta:
    """Metadata for one stored book snapshot (no levels, no prices)."""

    ticker: str
    snapshot_id: int
    captured_at: datetime
    content_hash: str | None
    environment: str | None


@dataclass(frozen=True, slots=True)
class PollMeta:
    """Metadata for one logical poll-attempt row (order-book endpoint)."""

    ticker: str
    requested_at: datetime
    completed_at: datetime
    outcome: str
    environment: str | None
    collector_run_id: int
    deduplicated: bool
    persisted_row_count: int
    raw_payload_id: int | None


@dataclass(frozen=True, slots=True)
class RunMeta:
    """One collector cycle."""

    run_id: int
    started_at: datetime
    finished_at: datetime
    success: bool


@dataclass(frozen=True, slots=True)
class Interval:
    """One classified interval between consecutive stored book states."""

    ticker: str
    prev_snapshot_id: int
    next_snapshot_id: int | None
    start: datetime
    end: datetime
    elapsed_seconds: float
    poll_attempts: int
    successful_polls: int
    unchanged_confirmations: int
    failed_polls: int
    collector_run_ids: tuple[int, ...]
    raw_payload_attempts: int
    content_hash_changed: bool | None
    environment: str | None
    classification: IntervalClass
    confidence: Confidence
    reason_code: str


@dataclass(frozen=True, slots=True)
class ContinuityConfig:
    """Injected analysis parameters. ``poll_ledger_start`` is the first
    instant per-ticker poll evidence exists (prospective only -- never
    backfilled); ``expected_cadence_seconds`` is the collection cadence below
    which the absence of a poll attempt is expected rather than anomalous."""

    as_of: datetime
    poll_ledger_start: datetime
    expected_cadence_seconds: float = 900.0
    environment: str = "production"


@dataclass(frozen=True, slots=True)
class InvariantViolation:
    check: str
    detail: str


@dataclass
class ContinuityReport:
    intervals: list[Interval] = field(default_factory=list)
    violations: list[InvariantViolation] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        by_class: dict[str, dict[str, float]] = {}
        by_conf: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        for iv in self.intervals:
            c = by_class.setdefault(iv.classification.value, {"count": 0, "seconds": 0.0})
            c["count"] += 1
            c["seconds"] += iv.elapsed_seconds
            by_conf[iv.confidence.value] = by_conf.get(iv.confidence.value, 0) + 1
            by_reason[iv.reason_code] = by_reason.get(iv.reason_code, 0) + 1
        total_s = sum(iv.elapsed_seconds for iv in self.intervals)
        open_s = sum(
            iv.elapsed_seconds
            for iv in self.intervals
            if iv.classification is IntervalClass.OPEN_INTERVAL_NOT_YET_CLASSIFIABLE
        )
        classified_s = total_s - open_s
        return {
            "intervals": len(self.intervals),
            "tickers": len({iv.ticker for iv in self.intervals}),
            "total_seconds": round(total_s, 3),
            "classified_seconds": round(classified_s, 3),
            "open_seconds": round(open_s, 3),
            "classified_ratio": round(classified_s / total_s, 4) if total_s else None,
            "by_classification": {
                k: {"count": int(v["count"]), "seconds": round(v["seconds"], 3)}
                for k, v in sorted(by_class.items())
            },
            "by_confidence": dict(sorted(by_conf.items())),
            "by_reason_code": dict(sorted(by_reason.items())),
            "invariant_violations": [
                {"check": v.check, "detail": v.detail} for v in self.violations
            ],
        }


def _check_invariants(
    books: list[BookMeta], polls: list[PollMeta], config: ContinuityConfig
) -> list[InvariantViolation]:
    out: list[InvariantViolation] = []
    for p in polls:
        if p.completed_at < p.requested_at:
            out.append(
                InvariantViolation(
                    "poll_completed_before_requested",
                    f"{p.ticker} at {p.requested_at.isoformat()}",
                )
            )
    seen_ids: set[int] = set()
    prev_by_ticker: dict[str, BookMeta] = {}
    for b in sorted(books, key=lambda b: (b.ticker, b.captured_at, b.snapshot_id)):
        if b.captured_at > config.as_of:
            out.append(
                InvariantViolation(
                    "book_captured_after_as_of", f"{b.ticker} id={b.snapshot_id}"
                )
            )
        if b.snapshot_id in seen_ids:
            out.append(InvariantViolation("duplicate_snapshot_id", f"id={b.snapshot_id}"))
        seen_ids.add(b.snapshot_id)
        prev = prev_by_ticker.get(b.ticker)
        if (
            prev is not None
            and prev.content_hash is not None
            and prev.content_hash == b.content_hash
        ):
            out.append(
                InvariantViolation(
                    "duplicate_logical_snapshot",
                    f"{b.ticker} ids={prev.snapshot_id},{b.snapshot_id} share a content hash",
                )
            )
        prev_by_ticker[b.ticker] = b
    return out


def _runs_overlapping(runs: list[RunMeta], start: datetime, end: datetime) -> list[RunMeta]:
    return [r for r in runs if r.finished_at > start and r.started_at < end]


def _classify_closed(
    prev: BookMeta,
    nxt: BookMeta,
    polls: list[PollMeta],
    runs: list[RunMeta],
    config: ContinuityConfig,
) -> Interval:
    start, end = prev.captured_at, nxt.captured_at
    elapsed = (end - start).total_seconds()
    within = [p for p in polls if start < p.completed_at <= end]
    successes = [p for p in within if p.outcome in SUCCESS_OUTCOMES]
    unchanged = [p for p in within if p.outcome == "succeeded_unchanged"]
    failures = [p for p in within if p.outcome in FAILURE_OUTCOMES]
    run_ids = tuple(sorted({p.collector_run_id for p in within}))
    raw_attempts = sum(1 for p in within if p.raw_payload_id is not None)
    hash_changed = (
        None
        if prev.content_hash is None or nxt.content_hash is None
        else prev.content_hash != nxt.content_hash
    )

    def make(cls: IntervalClass, conf: Confidence, reason: str) -> Interval:
        return Interval(
            ticker=prev.ticker,
            prev_snapshot_id=prev.snapshot_id,
            next_snapshot_id=nxt.snapshot_id,
            start=start,
            end=end,
            elapsed_seconds=elapsed,
            poll_attempts=len(within),
            successful_polls=len(successes),
            unchanged_confirmations=len(unchanged),
            failed_polls=len(failures),
            collector_run_ids=run_ids,
            raw_payload_attempts=raw_attempts,
            content_hash_changed=hash_changed,
            environment=prev.environment,
            classification=cls,
            confidence=conf,
            reason_code=reason,
        )

    if end <= config.poll_ledger_start:
        return make(
            IntervalClass.LEGACY_PRE_POLL_LEDGER, Confidence.UNKNOWN, "pre_poll_ledger"
        )
    if start < config.poll_ledger_start:
        return make(
            IntervalClass.LEGACY_PRE_POLL_LEDGER,
            Confidence.LOW,
            "straddles_poll_ledger_start",
        )
    if prev.environment != nxt.environment or (
        config.environment and prev.environment != config.environment
    ):
        return make(
            IntervalClass.AMBIGUOUS_PROVENANCE, Confidence.LOW, "environment_mismatch"
        )
    env_mismatch = [p for p in within if p.environment != prev.environment]
    if env_mismatch:
        return make(
            IntervalClass.AMBIGUOUS_PROVENANCE, Confidence.LOW, "poll_environment_mismatch"
        )
    if unchanged:
        return make(
            IntervalClass.UNCHANGED_CONFIRMED_BY_SUCCESSFUL_POLL,
            Confidence.HIGH,
            "unchanged_confirmed_in_interval",
        )
    if successes:
        return make(
            IntervalClass.CHANGED_AFTER_SUCCESSFUL_POLL,
            Confidence.HIGH,
            "next_state_from_successful_poll",
        )
    if failures:
        return make(
            IntervalClass.FAILED_POLL_INTERVAL,
            Confidence.HIGH if len(failures) == len(within) else Confidence.MEDIUM,
            "only_failed_polls_in_interval",
        )
    overlapping = _runs_overlapping(runs, start, end)
    if not overlapping:
        return make(
            IntervalClass.COLLECTION_GAP, Confidence.MEDIUM, "no_collector_runs_in_interval"
        )
    if not any(r.success for r in overlapping):
        return make(
            IntervalClass.COLLECTION_GAP, Confidence.MEDIUM, "only_failed_collector_runs"
        )
    if elapsed < config.expected_cadence_seconds:
        return make(
            IntervalClass.NO_DIRECT_POLL_EVIDENCE,
            Confidence.MEDIUM,
            "within_expected_cadence",
        )
    return make(
        IntervalClass.NO_DIRECT_POLL_EVIDENCE,
        Confidence.LOW,
        "ticker_omitted_from_successful_cycles",
    )


def _open_interval(last: BookMeta, polls: list[PollMeta], config: ContinuityConfig) -> Interval:
    start, end = last.captured_at, config.as_of
    within = [p for p in polls if start < p.completed_at <= end]
    return Interval(
        ticker=last.ticker,
        prev_snapshot_id=last.snapshot_id,
        next_snapshot_id=None,
        start=start,
        end=end,
        elapsed_seconds=max((end - start).total_seconds(), 0.0),
        poll_attempts=len(within),
        successful_polls=sum(1 for p in within if p.outcome in SUCCESS_OUTCOMES),
        unchanged_confirmations=sum(1 for p in within if p.outcome == "succeeded_unchanged"),
        failed_polls=sum(1 for p in within if p.outcome in FAILURE_OUTCOMES),
        collector_run_ids=tuple(sorted({p.collector_run_id for p in within})),
        raw_payload_attempts=sum(1 for p in within if p.raw_payload_id is not None),
        content_hash_changed=None,
        environment=last.environment,
        classification=IntervalClass.OPEN_INTERVAL_NOT_YET_CLASSIFIABLE,
        confidence=Confidence.UNKNOWN,
        reason_code="awaiting_next_stored_state",
    )


def classify_intervals(
    books: list[BookMeta],
    polls: list[PollMeta],
    runs: list[RunMeta],
    config: ContinuityConfig,
) -> ContinuityReport:
    """Classify every inter-snapshot interval per ticker. Deterministic:
    ordering is (captured_at, snapshot_id); ties cannot reorder. Open
    intervals (last stored state -> as_of) are never given a terminal class."""
    report = ContinuityReport()
    report.violations = _check_invariants(books, polls, config)
    polls_by_ticker: dict[str, list[PollMeta]] = {}
    for p in sorted(polls, key=lambda p: (p.completed_at, p.requested_at)):
        polls_by_ticker.setdefault(p.ticker, []).append(p)
    books_by_ticker: dict[str, list[BookMeta]] = {}
    for b in sorted(books, key=lambda b: (b.captured_at, b.snapshot_id)):
        books_by_ticker.setdefault(b.ticker, []).append(b)
    ordered_runs = sorted(runs, key=lambda r: (r.started_at, r.run_id))
    for ticker in sorted(books_by_ticker):
        series = books_by_ticker[ticker]
        t_polls = polls_by_ticker.get(ticker, [])
        for prev, nxt in itertools.pairwise(series):
            report.intervals.append(
                _classify_closed(prev, nxt, t_polls, ordered_runs, config)
            )
        report.intervals.append(_open_interval(series[-1], t_polls, config))
    return report
