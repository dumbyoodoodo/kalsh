"""Observed-availability timelines for historical replay (read-only).

Passive-order continuity must not assume uninterrupted queue participation across
a period where observation continuity is not established. This module derives,
from collector-run metadata plus per-market observation points, a bounded,
deterministic per-ticker timeline classifying every instant as OBSERVED,
LIKELY_OBSERVED, COLLECTOR_UNAVAILABLE, MARKET_NOT_ELIGIBLE, or UNKNOWN.

Evidence hierarchy (Phase 3 audit; see docs/adr/0019-observed-availability.md):

1. DIRECT per-market observation -- an ``orderbook_snapshots.captured_at`` or
   ``market_snapshots.observed_at`` for the ticker proves it was polled at that
   instant. (These tables dedup unchanged content, so they give positive
   observation POINTS, not continuous coverage.)
2. Per-cycle uptime -- ``collector_runs`` (collector-wide) proves WHEN the kalshi
   collector was running each ~5-10 min cycle and whether it succeeded. A
   successful cycle polls every discovered weather-market ticker.
3. Collector-wide gaps -- a gap between consecutive cycles beyond the normal
   cadence, or a failed cycle, is COLLECTOR_UNAVAILABLE.
4. No availability claim -- time not covered by any collector run is UNKNOWN.

Between two DIRECT observations of a ticker while the collector is continuously
up, the ticker was polled every cycle (unchanged books deduped) -> that span is
OBSERVED. Where the collector is up and the ticker is within its active window
but there is no direct witness in the span, the interval is LIKELY_OBSERVED
(inferred, not proven). Missing evidence is NEVER converted to observed time.

DOCUMENTED LIMITATION: availability is CYCLE-GRANULAR (~5-10 min) and
collector-wide; ``collector_runs`` records no per-ticker polling list, so
LIKELY_OBSERVED is a cycle-level inference, not per-cycle proof. Nothing here
writes to PostgreSQL or mutates source rows.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.storage.models import (
    CollectorRun,
    MarketPollAttempt,
    MarketSnapshot,
    OrderbookSnapshot,
)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class IntervalState(StrEnum):
    OBSERVED = "OBSERVED"
    LIKELY_OBSERVED = "LIKELY_OBSERVED"
    COLLECTOR_UNAVAILABLE = "COLLECTOR_UNAVAILABLE"
    MARKET_NOT_ELIGIBLE = "MARKET_NOT_ELIGIBLE"
    UNKNOWN = "UNKNOWN"


class EvidenceType(StrEnum):
    DIRECT_POLL_SUCCESS = "direct_poll_success"  # per-ticker poll ledger: observed (0011+)
    DIRECT_POLL_FAILED = "direct_poll_failed"  # per-ticker poll ledger: attempted, failed
    DIRECT_PER_MARKET = "direct_per_market"  # legacy witnessed observation point(s)
    CYCLE_ELIGIBLE = "cycle_eligible"  # collector up + ticker in active window
    COLLECTOR_GAP = "collector_gap"  # gap between cycles beyond cadence
    FAILED_CYCLE = "failed_cycle"  # a cycle that did not succeed
    NOT_ELIGIBLE = "not_eligible"  # outside the ticker's active window
    NO_COLLECTOR_DATA = "no_collector_data"  # no collector run covers the time


#: States that keep a passive order's queue participation continuous. OBSERVED
#: always; LIKELY_OBSERVED only when the policy opts in.
_CONTINUOUS_STRICT = frozenset({IntervalState.OBSERVED})
_CONTINUOUS_WITH_LIKELY = frozenset({IntervalState.OBSERVED, IntervalState.LIKELY_OBSERVED})


@dataclass(frozen=True)
class ObservationAvailabilityPolicy:
    """Versioned, explicit availability thresholds (recorded in every manifest)."""

    version: str = "availability-policy-v1"
    #: Start-to-start (or finish-to-next-start) gap beyond which the collector is
    #: treated as UNAVAILABLE rather than mid-cadence. ~2x the ~600s cadence.
    max_normal_cycle_gap_seconds: float = 1200.0
    #: Whether LIKELY_OBSERVED intervals keep passive continuity (default no).
    likely_observed_is_continuous: bool = False

    def to_manifest(self) -> dict[str, object]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class ObservationInterval:
    ticker: str
    start: datetime
    end: datetime
    state: IntervalState
    evidence: EvidenceType
    confidence: str  # high | medium | low | none
    reason: str
    collector_run_ids: tuple[int, ...]
    source_refs: tuple[str, ...]
    policy_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "state": self.state.value,
            "evidence": self.evidence.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "collector_run_ids": list(self.collector_run_ids),
            "source_refs": list(self.source_refs),
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class TickerAvailability:
    ticker: str
    intervals: tuple[ObservationInterval, ...]  # non-overlapping, sorted by start

    def continuity_ok_between(self, a: datetime, b: datetime, *, allow_likely: bool) -> bool:
        """True iff every interval overlapping [a, b] keeps continuity."""
        ok = _CONTINUOUS_WITH_LIKELY if allow_likely else _CONTINUOUS_STRICT
        if b <= a:
            return True
        for iv in self.intervals:
            if iv.end <= a or iv.start >= b:
                continue
            if iv.state not in ok:
                return False
        return True

    def first_break_after(self, t: datetime, *, allow_likely: bool) -> datetime | None:
        """Start of the first continuity-breaking interval strictly after ``t``
        (or containing it). None if continuity holds indefinitely."""
        ok = _CONTINUOUS_WITH_LIKELY if allow_likely else _CONTINUOUS_STRICT
        for iv in self.intervals:
            if iv.end <= t:
                continue
            if iv.state not in ok:
                return max(iv.start, t)
        return None

    def state_at(self, t: datetime) -> IntervalState:
        for iv in self.intervals:
            if iv.start <= t < iv.end:
                return iv.state
        return IntervalState.UNKNOWN


@dataclass
class AvailabilityTimeline:
    policy: ObservationAvailabilityPolicy
    start: datetime
    end: datetime
    tickers: dict[str, TickerAvailability]
    source_db_revision: str | None = None
    collector_runs_considered: int = 0
    failed_runs: int = 0
    downtime_gaps: int = 0
    #: Per-ticker direct-vs-inferred poll-evidence summary (Phase 8).
    poll_evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    def rows(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tk in sorted(self.tickers):
            for iv in self.tickers[tk].intervals:
                out.append(iv.to_dict())
        return out

    def summary(self, ticker: str) -> dict[str, Any]:
        ta = self.tickers.get(ticker)
        total = (self.end - self.start).total_seconds()
        by_state: dict[str, float] = {s.value: 0.0 for s in IntervalState}
        max_unknown = 0.0
        max_unavail = 0.0
        breaks = 0
        prev_continuous = True
        if ta:
            for iv in ta.intervals:
                dur = (iv.end - iv.start).total_seconds()
                by_state[iv.state.value] = by_state.get(iv.state.value, 0.0) + dur
                if iv.state is IntervalState.UNKNOWN:
                    max_unknown = max(max_unknown, dur)
                if iv.state is IntervalState.COLLECTOR_UNAVAILABLE:
                    max_unavail = max(max_unavail, dur)
                cont = iv.state in _CONTINUOUS_WITH_LIKELY
                if prev_continuous and not cont:
                    breaks += 1
                prev_continuous = cont
        pct = {k: round(100.0 * v / total, 2) if total else 0.0 for k, v in by_state.items()}
        out = {
            "ticker": ticker,
            "observed_pct": pct[IntervalState.OBSERVED.value],
            "likely_observed_pct": pct[IntervalState.LIKELY_OBSERVED.value],
            "unknown_pct": pct[IntervalState.UNKNOWN.value],
            "collector_unavailable_pct": pct[IntervalState.COLLECTOR_UNAVAILABLE.value],
            "not_eligible_pct": pct[IntervalState.MARKET_NOT_ELIGIBLE.value],
            "max_unknown_gap_seconds": round(max_unknown, 2),
            "max_collector_unavailable_gap_seconds": round(max_unavail, 2),
            "continuity_breaks": breaks,
            "availability_policy_version": self.policy.version,
        }
        # Phase 8: direct-vs-inferred poll-evidence provenance (empty pre-0011).
        out.update(
            self.poll_evidence.get(
                ticker,
                {"evidence_source": "legacy_inferred", "evidence_schema_era": "pre-0011-inferred"},
            )
        )
        return out

    def to_manifest(self) -> dict[str, Any]:
        return {
            "availability_policy": self.policy.to_manifest(),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "source_db_revision": self.source_db_revision,
            "collector_runs_considered": self.collector_runs_considered,
            "failed_runs": self.failed_runs,
            "downtime_gaps": self.downtime_gaps,
            "per_ticker_summary": {tk: self.summary(tk) for tk in sorted(self.tickers)},
        }


# --- builder ---------------------------------------------------------------


@dataclass(frozen=True)
class _Cycle:
    run_id: int
    start: datetime
    end: datetime
    success: bool


def _mk(
    ticker: str,
    start: datetime,
    end: datetime,
    state: IntervalState,
    evidence: EvidenceType,
    confidence: str,
    reason: str,
    policy_version: str,
    run_ids: tuple[int, ...] = (),
    refs: tuple[str, ...] = (),
) -> ObservationInterval:
    return ObservationInterval(
        ticker=ticker,
        start=start,
        end=end,
        state=state,
        evidence=evidence,
        confidence=confidence,
        reason=reason,
        collector_run_ids=run_ids,
        source_refs=refs,
        policy_version=policy_version,
    )


def _merge_adjacent(intervals: list[ObservationInterval]) -> list[ObservationInterval]:
    """Coalesce touching intervals with identical state/evidence (deterministic)."""
    merged: list[ObservationInterval] = []
    for iv in intervals:
        if (
            merged
            and merged[-1].state is iv.state
            and merged[-1].evidence is iv.evidence
            and merged[-1].end == iv.start
        ):
            prev = merged[-1]
            merged[-1] = _mk(
                prev.ticker,
                prev.start,
                iv.end,
                prev.state,
                prev.evidence,
                prev.confidence,
                prev.reason,
                prev.policy_version,
                tuple(dict.fromkeys(prev.collector_run_ids + iv.collector_run_ids)),
                tuple(dict.fromkeys(prev.source_refs + iv.source_refs)),
            )
        else:
            merged.append(iv)
    return merged


def _build_ticker(
    ticker: str,
    start: datetime,
    end: datetime,
    cycles: list[_Cycle],
    obs_points: list[tuple[datetime, str]],
    policy: ObservationAvailabilityPolicy,
    failure_times: list[datetime] | None = None,
) -> TickerAvailability:
    pv = policy.version
    failure_times = sorted(failure_times or [])
    # A direct per-ticker poll FAILURE also establishes eligibility (the ticker
    # was attempted), so failures extend the active window alongside successes.
    obs_times = sorted([t for t, _ in obs_points] + failure_times)
    active_start = obs_times[0] if obs_times else None
    active_end = obs_times[-1] if obs_times else None

    intervals: list[ObservationInterval] = []
    cursor = start
    # Walk cycles in order; the collector is "up" during a cycle. Gaps between
    # cycles beyond cadence are COLLECTOR_UNAVAILABLE. Time before the first /
    # after the last cycle with no collector data is UNKNOWN.
    prev_cycle_end: datetime | None = None
    for cyc in cycles:
        cyc_start = max(cyc.start, start)
        cyc_end = min(cyc.end, end)
        if cyc_end <= cursor:
            prev_cycle_end = max(prev_cycle_end or cyc.end, cyc.end)
            continue
        # gap before this cycle
        gap_ref = prev_cycle_end if prev_cycle_end is not None else start
        if cyc_start > cursor:
            gap = (cyc_start - (prev_cycle_end or cursor)).total_seconds()
            if prev_cycle_end is None:
                state, ev, conf, reason = (
                    IntervalState.UNKNOWN,
                    EvidenceType.NO_COLLECTOR_DATA,
                    "none",
                    "no collector run before this time",
                )
            elif gap > policy.max_normal_cycle_gap_seconds:
                state, ev, conf, reason = (
                    IntervalState.COLLECTOR_UNAVAILABLE,
                    EvidenceType.COLLECTOR_GAP,
                    "high",
                    f"collector gap {gap:.0f}s exceeds cadence",
                )
            else:
                # normal short idle between cycles: treat as continuous with the
                # surrounding coverage (eligible if within active window).
                state, ev, conf, reason = _eligible_state(
                    cursor, cyc_start, active_start, active_end
                )
            intervals.append(_mk(ticker, cursor, cyc_start, state, ev, conf, reason, pv))
            cursor = cyc_start
        # the cycle window itself
        if not cyc.success:
            intervals.append(
                _mk(
                    ticker,
                    cyc_start,
                    cyc_end,
                    IntervalState.COLLECTOR_UNAVAILABLE,
                    EvidenceType.FAILED_CYCLE,
                    "high",
                    "collector cycle failed",
                    pv,
                    (cyc.run_id,),
                )
            )
        else:
            direct = [(t, r) for t, r in obs_points if cyc.start <= t < cyc.end]
            if direct:
                refs = tuple(r for _, r in direct)
                intervals.append(
                    _mk(
                        ticker,
                        cyc_start,
                        cyc_end,
                        IntervalState.OBSERVED,
                        EvidenceType.DIRECT_PER_MARKET,
                        "high",
                        "direct observation point in cycle",
                        pv,
                        (cyc.run_id,),
                        refs,
                    )
                )
            else:
                state, ev, conf, reason = _eligible_state(
                    cyc_start, cyc_end, active_start, active_end
                )
                intervals.append(
                    _mk(ticker, cyc_start, cyc_end, state, ev, conf, reason, pv, (cyc.run_id,))
                )
        cursor = cyc_end
        prev_cycle_end = max(prev_cycle_end or cyc.end, cyc.end)
        _ = gap_ref
    # tail after last cycle
    if cursor < end:
        if prev_cycle_end is None:
            intervals.append(
                _mk(
                    ticker,
                    cursor,
                    end,
                    IntervalState.UNKNOWN,
                    EvidenceType.NO_COLLECTOR_DATA,
                    "none",
                    "no collector run covers this range",
                    pv,
                )
            )
        else:
            gap = (end - prev_cycle_end).total_seconds()
            if gap > policy.max_normal_cycle_gap_seconds:
                intervals.append(
                    _mk(
                        ticker,
                        cursor,
                        end,
                        IntervalState.COLLECTOR_UNAVAILABLE,
                        EvidenceType.COLLECTOR_GAP,
                        "high",
                        f"no collector run for {gap:.0f}s after last cycle",
                        pv,
                    )
                )
            else:
                state, ev, conf, reason = _eligible_state(cursor, end, active_start, active_end)
                intervals.append(_mk(ticker, cursor, end, state, ev, conf, reason, pv))

    # Upgrade LIKELY_OBSERVED spans that lie strictly between two OBSERVED
    # intervals with no intervening break -> OBSERVED (continuously polled,
    # witnessed at both ends; unchanged books deduped).
    merged = _merge_adjacent([iv for iv in intervals if iv.end > iv.start])
    merged = _bridge_observed(merged, ticker, pv)
    if failure_times:
        merged = _apply_direct_failures(merged, ticker, pv, failure_times)
    return TickerAvailability(ticker=ticker, intervals=tuple(_merge_adjacent(merged)))


def _apply_direct_failures(
    intervals: list[ObservationInterval],
    ticker: str,
    pv: str,
    failure_times: list[datetime],
) -> list[ObservationInterval]:
    """A direct per-ticker poll failure proves the ticker was NOT observed that
    cycle. Any non-OBSERVED interval containing a failure is downgraded to
    UNKNOWN (evidence DIRECT_POLL_FAILED) so it breaks passive continuity.
    Successful direct observations (OBSERVED) are never overridden."""
    out: list[ObservationInterval] = []
    for iv in intervals:
        if iv.state is not IntervalState.OBSERVED and any(
            iv.start <= t < iv.end for t in failure_times
        ):
            out.append(
                _mk(
                    ticker,
                    iv.start,
                    iv.end,
                    IntervalState.UNKNOWN,
                    EvidenceType.DIRECT_POLL_FAILED,
                    "high",
                    "direct per-ticker poll failed (attempted, not observed)",
                    pv,
                )
            )
        else:
            out.append(iv)
    return out


def _eligible_state(
    a: datetime, b: datetime, active_start: datetime | None, active_end: datetime | None
) -> tuple[IntervalState, EvidenceType, str, str]:
    if active_start is None or active_end is None or b <= active_start or a >= active_end:
        return (
            IntervalState.MARKET_NOT_ELIGIBLE,
            EvidenceType.NOT_ELIGIBLE,
            "medium",
            "no per-market observation establishes eligibility here",
        )
    return (
        IntervalState.LIKELY_OBSERVED,
        EvidenceType.CYCLE_ELIGIBLE,
        "medium",
        "collector up and ticker within its observed active window (no direct witness)",
    )


def _bridge_observed(
    intervals: list[ObservationInterval], ticker: str, pv: str
) -> list[ObservationInterval]:
    """Promote a LIKELY_OBSERVED interval to OBSERVED when it sits directly
    between two OBSERVED intervals (continuous witnessed polling)."""
    out = list(intervals)
    for i in range(1, len(out) - 1):
        if (
            out[i].state is IntervalState.LIKELY_OBSERVED
            and out[i - 1].state is IntervalState.OBSERVED
            and out[i + 1].state is IntervalState.OBSERVED
            and out[i - 1].end == out[i].start
            and out[i].end == out[i + 1].start
        ):
            out[i] = _mk(
                ticker,
                out[i].start,
                out[i].end,
                IntervalState.OBSERVED,
                EvidenceType.CYCLE_ELIGIBLE,
                "high",
                "between two direct observations with collector continuously up",
                pv,
                out[i].collector_run_ids,
                out[i].source_refs,
            )
    return out


def reconstruct_from_rows(rows: list[dict[str, Any]]) -> dict[str, TickerAvailability]:
    """Rebuild per-ticker availability from serialized interval rows (the inverse
    of ``AvailabilityTimeline.rows``), for use after loading an export."""
    by_ticker: dict[str, list[ObservationInterval]] = {}
    for r in rows:
        iv = ObservationInterval(
            ticker=r["ticker"],
            start=_utc(datetime.fromisoformat(r["start"])),
            end=_utc(datetime.fromisoformat(r["end"])),
            state=IntervalState(r["state"]),
            evidence=EvidenceType(r["evidence"]),
            confidence=r["confidence"],
            reason=r["reason"],
            collector_run_ids=tuple(r.get("collector_run_ids", []) or []),
            source_refs=tuple(r.get("source_refs", []) or []),
            policy_version=r["policy_version"],
        )
        by_ticker.setdefault(iv.ticker, []).append(iv)
    return {
        tk: TickerAvailability(ticker=tk, intervals=tuple(sorted(ivs, key=lambda x: x.start)))
        for tk, ivs in by_ticker.items()
    }


async def build_availability_timeline(
    session: AsyncSession,
    *,
    tickers: tuple[str, ...],
    start: datetime,
    end: datetime,
    policy: ObservationAvailabilityPolicy | None = None,
    source_db_revision: str | None = None,
) -> AvailabilityTimeline:
    """Read-only bounded availability timeline for an explicit ticker list. Never
    writes to PostgreSQL. Requires explicit tickers and a bounded range."""
    if not tickers:
        raise ValueError("build_availability_timeline requires an explicit ticker list")
    if end <= start:
        raise ValueError("end must be after start")
    policy = policy or ObservationAvailabilityPolicy()

    # kalshi collector cycles overlapping [start, end] (collector-wide uptime).
    run_rows = list(
        await session.scalars(
            select(CollectorRun)
            .where(
                CollectorRun.collector == "kalshi",
                CollectorRun.finished_at >= start,
                CollectorRun.started_at <= end,
            )
            .order_by(CollectorRun.started_at, CollectorRun.id)
        )
    )
    cycles = [
        _Cycle(r.id, _utc(r.started_at), _utc(r.finished_at), bool(r.success)) for r in run_rows
    ]
    failed = sum(1 for c in cycles if not c.success)
    downtime = 0
    for a, b in itertools.pairwise(cycles):
        if (b.start - a.end).total_seconds() > policy.max_normal_cycle_gap_seconds:
            downtime += 1

    # per-ticker evidence. Direct per-ticker poll evidence (market_poll_attempts,
    # migration 0011+) is preferred; otherwise legacy snapshot observation points.
    ta: dict[str, TickerAvailability] = {}
    poll_evidence: dict[str, dict[str, Any]] = {}
    for tk in tickers:
        polls = list(
            await session.execute(
                select(
                    MarketPollAttempt.requested_at,
                    MarketPollAttempt.id,
                    MarketPollAttempt.outcome,
                ).where(
                    MarketPollAttempt.ticker == tk,
                    MarketPollAttempt.environment == "production",
                    MarketPollAttempt.requested_at >= start,
                    MarketPollAttempt.requested_at <= end,
                )
            )
        )
        if polls:
            direct_obs = [
                (_utc(t), f"market_poll_attempts:{i}") for t, i, o in polls if o in _POLL_OBSERVED
            ]
            failures = [_utc(t) for t, _i, o in polls if o in _POLL_BREAKING]
            ta[tk] = _build_ticker(tk, start, end, cycles, direct_obs, policy, failures)
            poll_evidence[tk] = _poll_evidence_summary(polls, direct=True, start=start)
        else:
            books = await session.execute(
                select(OrderbookSnapshot.captured_at, OrderbookSnapshot.id).where(
                    OrderbookSnapshot.market_ticker == tk,
                    OrderbookSnapshot.environment == "production",
                    OrderbookSnapshot.captured_at >= start,
                    OrderbookSnapshot.captured_at <= end,
                )
            )
            snaps = await session.execute(
                select(MarketSnapshot.observed_at, MarketSnapshot.id).where(
                    MarketSnapshot.market_ticker == tk,
                    MarketSnapshot.environment == "production",
                    MarketSnapshot.observed_at >= start,
                    MarketSnapshot.observed_at <= end,
                )
            )
            obs: list[tuple[datetime, str]] = [
                (_utc(t), f"orderbook_snapshots:{i}") for t, i in books.all()
            ] + [(_utc(t), f"market_snapshots:{i}") for t, i in snaps.all()]
            ta[tk] = _build_ticker(tk, start, end, cycles, obs, policy)
            poll_evidence[tk] = _poll_evidence_summary([], direct=False, start=start)

    return AvailabilityTimeline(
        policy=policy,
        start=start,
        end=end,
        tickers=ta,
        source_db_revision=source_db_revision,
        collector_runs_considered=len(cycles),
        failed_runs=failed,
        downtime_gaps=downtime,
        poll_evidence=poll_evidence,
    )


#: Poll outcomes that prove observation / that break continuity (mirror
#: ingestion.poll_ledger; kept as strings to avoid an execution->ingestion import).
_POLL_OBSERVED = frozenset({"succeeded_new_data", "succeeded_unchanged", "succeeded_empty"})
_POLL_BREAKING = frozenset(
    {"rate_limited", "api_failure", "malformed_payload", "persistence_failure", "unknown_failure"}
)


def _poll_evidence_summary(polls: list[Any], *, direct: bool, start: datetime) -> dict[str, Any]:
    total = len(polls)
    observed = sum(1 for _t, _i, o in polls if o in _POLL_OBSERVED)
    failed = sum(1 for _t, _i, o in polls if o in _POLL_BREAKING)
    rate_limited = sum(1 for _t, _i, o in polls if o == "rate_limited")
    unchanged = sum(1 for _t, _i, o in polls if o == "succeeded_unchanged")
    first = min((_utc(t) for t, _i, _o in polls), default=None) if polls else None
    return {
        "evidence_source": "direct_per_ticker" if direct else "legacy_inferred",
        "evidence_schema_era": "poll-ledger-0011" if direct else "pre-0011-inferred",
        "direct_poll_evidence_pct": round(100.0 * observed / total, 2) if total else 0.0,
        "inferred_poll_evidence_pct": 0.0 if direct else 100.0,
        "failed_poll_pct": round(100.0 * failed / total, 2) if total else 0.0,
        "rate_limited_poll_pct": round(100.0 * rate_limited / total, 2) if total else 0.0,
        "unchanged_success_pct": round(100.0 * unchanged / total, 2) if total else 0.0,
        "polling_evidence_start": first.isoformat() if first else None,
    }
