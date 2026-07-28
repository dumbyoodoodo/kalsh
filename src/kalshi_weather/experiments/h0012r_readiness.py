"""H0012r counts-only, metadata-only readiness preflight.

H0012r is the calendar-gated retry of H0012 (HYPOTHESES.md, "Settlement-time
labels are stable; close-time labels are not"). The original 2026-07-21 run
established claim A and left claim B (post-settlement correction rate < 2%)
*inconclusive for sample size*: 0/132 corrections gave a 95% upper confidence
bound of 2.83%, missing the pre-registered 2% bar; zero events need n >= 189
variable-dates. The retry re-runs the SAME single-pass measurement, once, on
or after 2026-08-19, when enough two-variable NYC market-dates have accrued.

This module answers only one question: is H0012r *structurally* ready to run?
It reports counts and integrity flags -- variable-date coverage, exact/bounded
label split, payout-agreement integrity, close-time stability, partition
consistency, leakage status -- and NEVER computes claim A or claim B, the
stage-difference probabilities, any CI, or any outcome. READY means the
preregistered structural gates are satisfied; it does NOT imply claim B will
pass. The design is single-pass with no train/validation/test split, so no
staged readiness states exist. All logic is pure over injected counts and an
injected clock (no wall clock, no database).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

#: Authoritative scientific constants (HYPOTHESES.md H0012 entry + the
#: 2026-07-22 roadmap card). NONE of these may be tuned by this preflight.
CALENDAR_BOUNDARY = date(2026, 8, 19)
MIN_VARIABLE_DATES = 189
FAMILY_PREFIXES = ("KXHIGHNY", "KXLOWTNYC")
STATION_ID = "NYC"
EXPECTED_VARIABLES = ("tmax_f", "tmin_f")

#: Reserved confirmatory windows of the frozen program, for an INFORMATIONAL
#: overlap count only. H0012r consumes settlement labels, not market prices or
#: price-derived signals, so a shared target_date does not consume or reveal anything
#: about H0019/H0020's price-based holdouts -- the overlap is reported so a
#: human can confirm non-contamination, never to exclude a date (the frozen
#: H0012 design excludes no calendar window). See the reserved-window note.
RESERVED_WINDOWS: tuple[tuple[str, date, date], ...] = (
    ("H0019_test", date(2026, 8, 12), date(2026, 8, 25)),
    ("H0020_validation_test", date(2026, 8, 26), date(2026, 10, 6)),
)

#: Two usable variable-dates (tmax_f + tmin_f) accrue per settled NYC day;
#: used ONLY to estimate an earliest-ready date, never as a readiness input.
ACCRUAL_PER_DAY_DEFAULT = 2.0


class ReadinessState(StrEnum):
    SPECIFICATION_BLOCKED = "SPECIFICATION_BLOCKED"
    INTEGRITY_BLOCKED = "INTEGRITY_BLOCKED"
    CALENDAR_GATED = "CALENDAR_GATED"
    COLLECTING = "COLLECTING"
    READY = "READY"


@dataclass(frozen=True, slots=True)
class ReadinessCounts:
    """Structural counts over the NYC settlement-label population. All are
    cardinalities or dates -- never a stage-difference rate or outcome."""

    variable_dates: int
    exact_settlement: int
    bounded_settlement: int
    stations: int
    tmax_dates: int
    tmin_dates: int
    missing_source_data: int
    target_date_min: date | None
    target_date_max: date | None
    reserved_window_overlap: dict[str, int]
    environment_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class IntegrityFlags:
    """Metadata-only integrity gates. A blocking flag forces INTEGRITY_BLOCKED
    regardless of the calendar or counts (fail closed)."""

    spec_recovered: bool
    leakage_status: str  # PASS | FAIL | UNKNOWN
    payout_agreement_ok: bool
    payout_matches: int
    payout_total: int
    close_time_status: str  # PASS | REVIEW_REQUIRED | BLOCKED_INSUFFICIENT | UNKNOWN
    close_time_revised: int
    close_time_null: int
    partition_consistent: bool
    partition_inconsistent_groups: int
    provenance_ok: bool


@dataclass
class ReadinessReport:
    state: ReadinessState
    as_of: date
    counts: ReadinessCounts
    integrity: IntegrityFlags
    calendar_boundary: date = CALENDAR_BOUNDARY
    min_variable_dates: int = MIN_VARIABLE_DATES
    failing_gates: list[str] = field(default_factory=list)
    next_action: str = ""
    earliest_ready_estimate: date | None = None

    def to_dict(self) -> dict[str, Any]:
        c, i = self.counts, self.integrity
        return {
            "hypothesis": "H0012r",
            "banner": "COUNTS ONLY — NOT AN EXPERIMENT RESULT",
            "state": self.state.value,
            "as_of": self.as_of.isoformat(),
            "calendar": {
                "boundary": self.calendar_boundary.isoformat(),
                "reached": self.as_of >= self.calendar_boundary,
                "days_until_boundary": max((self.calendar_boundary - self.as_of).days, 0),
            },
            "counts": {
                "variable_dates": c.variable_dates,
                "min_required": self.min_variable_dates,
                "shortfall": max(self.min_variable_dates - c.variable_dates, 0),
                "exact_settlement": c.exact_settlement,
                "bounded_settlement": c.bounded_settlement,
                "stations": c.stations,
                "tmax_dates": c.tmax_dates,
                "tmin_dates": c.tmin_dates,
                "missing_source_data_excluded": c.missing_source_data,
                "target_date_min": c.target_date_min.isoformat() if c.target_date_min else None,
                "target_date_max": c.target_date_max.isoformat() if c.target_date_max else None,
                "reserved_window_overlap": c.reserved_window_overlap,
                "environment_counts": c.environment_counts,
            },
            "integrity": {
                "spec_recovered": i.spec_recovered,
                "leakage_status": i.leakage_status,
                "payout_agreement_ok": i.payout_agreement_ok,
                "payout_agreement": f"{i.payout_matches}/{i.payout_total}",
                "close_time_status": i.close_time_status,
                "close_time_revised": i.close_time_revised,
                "close_time_null": i.close_time_null,
                "partition_consistent": i.partition_consistent,
                "partition_inconsistent_groups": i.partition_inconsistent_groups,
                "provenance_ok": i.provenance_ok,
            },
            "failing_gates": self.failing_gates,
            "next_action": self.next_action,
            "earliest_ready_estimate": (
                self.earliest_ready_estimate.isoformat() if self.earliest_ready_estimate else None
            ),
            "disclaimer": (
                "READY means structural pre-registered gates are satisfied and the single "
                "retry may be run after human approval; it does NOT imply claim B is or will "
                "be supported. No stage-difference rate or CI is computed here."
            ),
        }


def integrity_blockers(flags: IntegrityFlags) -> list[str]:
    """Metadata-only integrity failures that must block readiness. Fail closed:
    an UNKNOWN close-time or leakage status is treated as not-yet-cleared, not
    as a pass."""
    blockers: list[str] = []
    if flags.leakage_status == "FAIL":
        blockers.append("leakage_lint_error")
    if not flags.payout_agreement_ok:
        blockers.append("payout_agreement_gate_failed")
    if flags.close_time_status in ("REVIEW_REQUIRED", "BLOCKED_INSUFFICIENT"):
        blockers.append(f"close_time_{flags.close_time_status.lower()}")
    if not flags.partition_consistent:
        blockers.append("partition_inconsistent_strikes")
    if not flags.provenance_ok:
        blockers.append("provenance_incomplete")
    return blockers


def earliest_ready_estimate(
    variable_dates: int, *, as_of: date, accrual_per_day: float = ACCRUAL_PER_DAY_DEFAULT
) -> date:
    """max(calendar boundary, projected date to reach n>=189). The projection
    is a coverage-accrual estimate only -- it uses no outcome and is labeled an
    estimate everywhere it appears."""
    if variable_dates >= MIN_VARIABLE_DATES:
        n_ready = as_of
    else:
        needed = MIN_VARIABLE_DATES - variable_dates
        if accrual_per_day > 0:
            days = int((needed + accrual_per_day - 1) // accrual_per_day)
        else:
            days = 999
        n_ready = as_of + timedelta(days=days)
    return max(CALENDAR_BOUNDARY, n_ready)


def classify_readiness(
    counts: ReadinessCounts,
    flags: IntegrityFlags,
    *,
    now: date,
    accrual_per_day: float = ACCRUAL_PER_DAY_DEFAULT,
) -> ReadinessReport:
    """Pure classification. Precedence (fail-closed): specification ->
    integrity -> calendar -> collecting -> ready. READY is impossible before
    the calendar boundary by construction."""
    failing: list[str] = []
    report = ReadinessReport(
        state=ReadinessState.COLLECTING, as_of=now, counts=counts, integrity=flags
    )
    report.earliest_ready_estimate = earliest_ready_estimate(
        counts.variable_dates, as_of=now, accrual_per_day=accrual_per_day
    )

    if not flags.spec_recovered:
        report.state = ReadinessState.SPECIFICATION_BLOCKED
        report.failing_gates = ["specification_not_recovered"]
        report.next_action = "Recover the authoritative H0012r specification before any readiness."
        return report

    blockers = integrity_blockers(flags)
    calendar_open = now >= CALENDAR_BOUNDARY
    counts_met = counts.variable_dates >= MIN_VARIABLE_DATES

    if not calendar_open:
        failing.append("calendar_boundary_not_reached")
    if not counts_met:
        failing.append(f"variable_dates<{MIN_VARIABLE_DATES}")
    failing.extend(blockers)
    report.failing_gates = failing

    if blockers:
        report.state = ReadinessState.INTEGRITY_BLOCKED
        report.next_action = (
            "STOP: resolve the integrity failure (" + ", ".join(blockers) + ") read-only; "
            "change no frozen artifact."
        )
        return report

    if not calendar_open:
        report.state = ReadinessState.CALENDAR_GATED
        report.next_action = (
            f"Wait until {CALENDAR_BOUNDARY.isoformat()}; re-run this counts-only preflight then. "
            f"Coverage {counts.variable_dates}/{MIN_VARIABLE_DATES}."
        )
        return report

    if not counts_met:
        report.state = ReadinessState.COLLECTING
        report.next_action = (
            f"Calendar reached but coverage {counts.variable_dates}/{MIN_VARIABLE_DATES}; "
            "keep collecting and re-run the preflight."
        )
        return report

    report.state = ReadinessState.READY
    report.next_action = (
        "Structurally ready. Follow the runbook H0012r gate sequence "
        "(leakage audit → close-time preflight → partition/hash review → human approval) "
        "before the single registered retry. READY does NOT imply claim B support."
    )
    return report
