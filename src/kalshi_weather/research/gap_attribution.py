"""Uncovered-collection-gap attribution: which weather-collection outages in a
window are NOT explained by the permanent collection-gap ledger.

The defect this closes. The station-pilot review previously derived
"attribution is unavailable" from ledger *confidence* -- if every stored record
was CONFIRMED, it concluded nothing was ambiguous. That is backwards: a ledger
containing only high-confidence records says nothing about the outages it never
recorded. As of 2026-08-03 the ledger holds 10 records while collector history
shows 34 gaps over two hours, so the old test evaluated False precisely when
attribution was least complete.

The fix is to derive candidate gaps from production collector-run continuity
and *subtract* the intervals the ledger actually covers. What remains is
uncovered, and uncovered means unclassified -- regardless of how confident the
records that exist happen to be.

Two rules keep coverage honest:

- **Subsystem compatibility.** A Kalshi-only ledger record can never cover a
  weather gap, so a Kalshi outage cannot be borrowed to explain missing weather
  collection.
- **Cause compatibility.** Only classifications that denote an actual
  *collection outage* (host down, collector stopped/crashed, upstream weather
  source down, request failure) can cover an absence of collector runs. A
  PARSER_FAILURE or PERSISTENCE_FAILURE cannot: the collector was running and
  polling normally, it just mishandled a payload. This is what stops a
  recovered single-station-day parser record from being stretched over a
  host-wide interval it does not explain.

Everything here is pure: no database, no wall clock, no I/O. Callers inject
runs, ledger records, and the window.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from kalshi_weather.ops.forecast_cadence import CadenceConfig

#: Registered tolerance, reused (never redefined): a weather-collector run gap
#: wider than this is an outage. Same constant the observatory alerts on.
DEFAULT_OUTAGE_THRESHOLD_HOURS = CadenceConfig().outage_threshold_hours


class GapAttributionError(ValueError):
    """An interval or input violates the attribution contract."""


class CoverageStatus(StrEnum):
    COVERED_CONFIRMED = "COVERED_CONFIRMED"
    COVERED_PARTIAL = "COVERED_PARTIAL"
    UNCOVERED = "UNCOVERED"
    COVERAGE_AMBIGUOUS = "COVERAGE_AMBIGUOUS"


#: Ledger classifications that can explain an ABSENCE OF COLLECTOR RUNS.
#: Deliberately excludes PARSER_FAILURE / PERSISTENCE_FAILURE / DISCOVERY_
#: OMISSION: in those the collector ran, so they cannot cover a continuity gap.
OUTAGE_CLASSIFICATIONS = frozenset(
    {
        "HOST_UNAVAILABLE",
        "COLLECTOR_STOPPED",
        "COLLECTOR_CRASH",
        "UPSTREAM_WEATHER_SOURCE_OUTAGE",
        "REQUEST_FAILED",
    }
)
#: Confidences strong enough to call an interval explained.
CONFIDENT = frozenset({"CONFIRMED", "HIGH"})
#: Subsystems a weather-collection gap can be attributed against.
WEATHER_SUBSYSTEMS = frozenset({"WEATHER_OBSERVATIONS", "WEATHER_FORECASTS"})


@dataclass(frozen=True, slots=True)
class Interval:
    """A half-open UTC interval ``[start, end)``. Timezone-aware only."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for name, value in (("start", self.start), ("end", self.end)):
            if value.tzinfo is None:
                raise GapAttributionError(f"{name} must be timezone-aware UTC: {value!r}")
        if self.end < self.start:
            raise GapAttributionError(f"end {self.end} precedes start {self.start}")

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    @property
    def empty(self) -> bool:
        return self.end <= self.start

    def overlaps(self, other: Interval) -> bool:
        """Strict overlap: touching at a boundary is adjacency, not overlap."""
        return self.start < other.end and other.start < self.end

    def intersect(self, other: Interval) -> Interval | None:
        lo, hi = max(self.start, other.start), min(self.end, other.end)
        return None if hi <= lo else Interval(lo, hi)


def subtract(base: Interval, cuts: Sequence[Interval]) -> tuple[Interval, ...]:
    """``base`` minus every interval in ``cuts``.

    Handles nesting, partial overlap, exact boundaries, adjacency, and several
    cuts jointly covering ``base``. Returns the remaining pieces in order;
    empty tuple means fully covered.
    """
    remaining = [base] if not base.empty else []
    for cut in sorted(cuts, key=lambda i: i.start):
        next_remaining: list[Interval] = []
        for piece in remaining:
            overlap = piece.intersect(cut)
            if overlap is None:
                next_remaining.append(piece)
                continue
            if piece.start < overlap.start:
                next_remaining.append(Interval(piece.start, overlap.start))
            if overlap.end < piece.end:
                next_remaining.append(Interval(overlap.end, piece.end))
        remaining = next_remaining
    return tuple(i for i in remaining if not i.empty)


def merge(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """Union of overlapping/adjacent intervals, ordered. Callers must only pass
    intervals already known to be compatible (same subsystem and cause) --
    merging across incompatible causes would invent coverage."""
    ordered = sorted(intervals, key=lambda i: (i.start, i.end))
    out: list[Interval] = []
    for interval in ordered:
        if out and interval.start <= out[-1].end:
            out[-1] = Interval(out[-1].start, max(out[-1].end, interval.end))
        else:
            out.append(interval)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class CollectorRun:
    """One weather-collector cycle, reduced to what attribution needs."""

    started_at: datetime
    success: bool


@dataclass(frozen=True, slots=True)
class CollectorGap:
    """A derived continuity break in weather collection."""

    interval: Interval
    subsystem: str
    kind: str  # "no_runs" | "failed_cycles"
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_at": self.interval.start.isoformat(),
            "end_at": self.interval.end.isoformat(),
            "duration_hours": round(self.interval.duration_hours, 3),
            "subsystem": self.subsystem,
            "kind": self.kind,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class LedgerCoverageRecord:
    """The ledger fields attribution is allowed to consult."""

    gap_id: str
    classification: str
    subsystems: tuple[str, ...]
    confidence: str
    interval: Interval

    def can_cover(self, gap: CollectorGap) -> bool:
        """Subsystem AND cause compatibility -- both required."""
        return (
            gap.subsystem in self.subsystems
            and bool(WEATHER_SUBSYSTEMS & set(self.subsystems))
            and self.classification in OUTAGE_CLASSIFICATIONS
        )


@dataclass(frozen=True, slots=True)
class GapCoverage:
    """One collector gap, and how much of it the ledger explains."""

    gap: CollectorGap
    status: CoverageStatus
    covering_gap_ids: tuple[str, ...]
    covered_intervals: tuple[Interval, ...]
    uncovered_intervals: tuple[Interval, ...]

    @property
    def uncovered_hours(self) -> float:
        return sum(i.duration_hours for i in self.uncovered_intervals)

    @property
    def unclassified(self) -> bool:
        """Any material uncovered or ambiguously-covered portion."""
        return self.status in (CoverageStatus.UNCOVERED, CoverageStatus.COVERAGE_AMBIGUOUS) or (
            self.uncovered_hours > 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap": self.gap.to_dict(),
            "status": self.status.value,
            "covering_gap_ids": list(self.covering_gap_ids),
            "covered_intervals": [
                {"start_at": i.start.isoformat(), "end_at": i.end.isoformat()}
                for i in self.covered_intervals
            ],
            "uncovered_intervals": [
                {"start_at": i.start.isoformat(), "end_at": i.end.isoformat()}
                for i in self.uncovered_intervals
            ],
            "uncovered_hours": round(self.uncovered_hours, 3),
            "unclassified": self.unclassified,
        }


def derive_collector_gap_intervals(
    runs: Sequence[CollectorRun],
    *,
    window: Interval,
    subsystem: str = "WEATHER_OBSERVATIONS",
    threshold_hours: float = DEFAULT_OUTAGE_THRESHOLD_HOURS,
) -> tuple[CollectorGap, ...]:
    """Candidate weather-collection continuity breaks inside ``window``.

    Two kinds, both measured against the registered tolerance:

    - ``no_runs``: no weather cycle started for longer than the tolerance.
    - ``failed_cycles``: cycles kept starting but every one failed across a
      span longer than the tolerance -- runs existed, collection did not.

    Kalshi-only failures never reach here: the caller passes *weather* runs, and
    a weather cycle that succeeded is never part of a gap.
    """
    threshold = timedelta(hours=threshold_hours)
    ordered = sorted(runs, key=lambda r: r.started_at)
    for run in ordered:
        if run.started_at.tzinfo is None:
            raise GapAttributionError(f"run.started_at must be tz-aware: {run.started_at!r}")

    gaps: list[CollectorGap] = []

    # 1) Absence of runs, including the leading and trailing edges of the window.
    boundaries: list[tuple[datetime, datetime, str]] = []
    previous = window.start
    for run in ordered:
        if run.started_at > previous:
            boundaries.append((previous, run.started_at, "no_runs"))
        previous = max(previous, run.started_at)
    if previous < window.end:
        boundaries.append((previous, window.end, "no_runs"))

    for lo, hi, kind in boundaries:
        if hi - lo <= threshold:
            continue
        clipped = Interval(lo, hi).intersect(window)
        if clipped is None or clipped.duration_hours * 3600 <= threshold.total_seconds():
            continue
        gaps.append(
            CollectorGap(
                interval=clipped,
                subsystem=subsystem,
                kind=kind,
                evidence_refs=(
                    f"collector_runs:weather:no run between {lo.isoformat()} and {hi.isoformat()}",
                ),
            )
        )

    # 2) Consecutive failures spanning longer than the tolerance.
    run_start: datetime | None = None
    run_end: datetime | None = None
    count = 0
    streak: list[CollectorRun | None] = [*ordered, None]  # sentinel flushes a trailing streak
    for candidate in streak:
        if candidate is not None and not candidate.success:
            run_start = candidate.started_at if run_start is None else run_start
            run_end = candidate.started_at
            count += 1
            continue
        if run_start is not None and run_end is not None and run_end - run_start > threshold:
            clipped = Interval(run_start, run_end).intersect(window)
            if clipped is not None and not clipped.empty:
                gaps.append(
                    CollectorGap(
                        interval=clipped,
                        subsystem=subsystem,
                        kind="failed_cycles",
                        evidence_refs=(
                            f"collector_runs:weather:{count} consecutive failed cycle(s) "
                            f"{run_start.isoformat()}..{run_end.isoformat()}",
                        ),
                    )
                )
        run_start = run_end = None
        count = 0

    return tuple(sorted(gaps, key=lambda g: (g.interval.start, g.kind)))


def compute_ledger_coverage(
    gap: CollectorGap, records: Sequence[LedgerCoverageRecord]
) -> GapCoverage:
    """Classify how completely the ledger explains one collector gap."""
    eligible = [r for r in records if r.can_cover(gap) and r.interval.overlaps(gap.interval)]
    if not eligible:
        return GapCoverage(
            gap=gap,
            status=CoverageStatus.UNCOVERED,
            covering_gap_ids=(),
            covered_intervals=(),
            uncovered_intervals=(gap.interval,),
        )

    pieces = [r.interval.intersect(gap.interval) for r in eligible]
    covered = merge([p for p in pieces if p is not None])
    uncovered = subtract_covered_intervals(gap.interval, covered)
    any_low_confidence = any(r.confidence not in CONFIDENT for r in eligible)

    if uncovered:
        status = CoverageStatus.COVERED_PARTIAL
    elif any_low_confidence:
        status = CoverageStatus.COVERAGE_AMBIGUOUS
    else:
        status = CoverageStatus.COVERED_CONFIRMED

    return GapCoverage(
        gap=gap,
        status=status,
        covering_gap_ids=tuple(sorted(r.gap_id for r in eligible)),
        covered_intervals=covered,
        uncovered_intervals=uncovered,
    )


def subtract_covered_intervals(
    gap_interval: Interval, covered: Sequence[Interval]
) -> tuple[Interval, ...]:
    """Portions of ``gap_interval`` that no compatible ledger record covers."""
    return subtract(gap_interval, covered)


def affected_local_dates(
    intervals: Sequence[Interval], *, timezone_name: str, complete_dates: Sequence[date]
) -> tuple[str, ...]:
    """Complete station-local dates any uncovered interval touches.

    Station-local, so PHX (fixed UTC-7, no DST) and MIA/SEA (DST-observing)
    each map correctly rather than through a shared UTC day.
    """
    zone = ZoneInfo(timezone_name)
    allowed = {d.isoformat() for d in complete_dates}
    hit: set[str] = set()
    for interval in intervals:
        cursor = interval.start.astimezone(zone)
        end_local = interval.end.astimezone(zone)
        while cursor.date() <= end_local.date():
            iso = cursor.date().isoformat()
            if iso in allowed:
                hit.add(iso)
            cursor = cursor + timedelta(days=1)
    return tuple(sorted(hit))


@dataclass(frozen=True, slots=True)
class UnclassifiedOverlapSummary:
    """Counts-only rollup. No performance information of any kind."""

    coverages: tuple[GapCoverage, ...]
    total_uncovered_hours: float
    unclassified_overlap: bool
    affected_local_dates_by_station: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "unclassified_overlap": self.unclassified_overlap,
            "total_uncovered_hours": round(self.total_uncovered_hours, 3),
            "candidate_gaps": len(self.coverages),
            "uncovered_or_ambiguous_gaps": sum(1 for c in self.coverages if c.unclassified),
            "coverages": [c.to_dict() for c in self.coverages],
            "affected_local_dates_by_station": {
                station: list(dates) for station, dates in self.affected_local_dates_by_station
            },
            "note": (
                "derived from collector-run continuity minus active permanent-gap-ledger "
                "coverage; ledger confidence alone never implies attribution is complete"
            ),
        }


def summarize_unclassified_overlap(
    gaps: Sequence[CollectorGap],
    records: Sequence[LedgerCoverageRecord],
    *,
    station_timezones: Sequence[tuple[str, str]] = (),
    complete_dates: Sequence[date] = (),
) -> UnclassifiedOverlapSummary:
    """Full attribution pass over the window's candidate gaps."""
    coverages = tuple(compute_ledger_coverage(g, records) for g in gaps)
    uncovered = [i for c in coverages for i in c.uncovered_intervals]
    total = sum(i.duration_hours for i in uncovered)
    ambiguous = any(c.unclassified for c in coverages)
    by_station = tuple(
        (
            station,
            affected_local_dates(uncovered, timezone_name=tz, complete_dates=complete_dates),
        )
        for station, tz in station_timezones
    )
    return UnclassifiedOverlapSummary(
        coverages=coverages,
        total_uncovered_hours=total,
        unclassified_overlap=ambiguous,
        affected_local_dates_by_station=by_station,
    )


def utc(value: datetime) -> datetime:
    """Normalize to UTC, rejecting naive input (fail closed)."""
    if value.tzinfo is None:
        raise GapAttributionError(f"timestamp must be timezone-aware: {value!r}")
    return value.astimezone(UTC)
