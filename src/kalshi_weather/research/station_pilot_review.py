"""Calendar-gated, counts-only SEA/PHX/MIA station-pilot operational review
(ADR 0023) -- READ-ONLY.

Reports collection completeness, forecast coverage, provenance integrity,
parser/source health, and operational reliability, and maps them to a
per-station KEEP / EXTEND_COLLECTION / REMOVE_FOR_DATA_QUALITY decision plus an
overall operational verdict -- ALL gated behind the registered review time
(2026-08-04T00:00Z). It computes NO predictive or market quantity (forecast
error, calibration, expected/realized value, spread, liquidity, price, trade
outcome, profitability, station ranking, model comparison), touches no
experiment, mutates no station registry, and writes nothing.

Pure functions over injected counts and an injected as-of clock (no wall
clock). Before the gate the caller may expose only raw counts and MUST refuse
to issue any per-station decision, any overall operational verdict, or the
review artifact.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

# Single source of truth for the registered pilot (ADR 0023); explicit
# re-export so the CLI can read them from this review module.
from kalshi_weather.ops.station_pilot_review import PILOT_START_DEFAULT as PILOT_START_DEFAULT
from kalshi_weather.ops.station_pilot_review import PILOT_STATIONS as PILOT_STATIONS

#: Registered review gate (ADR 0023 one-week observation period; the first full
#: pilot day is 2026-07-28, +7 days). Before this instant no decision is made.
REVIEW_GATE = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
#: Expected NWS CLI issuances per station-local day (feasibility audit: 2/day).
EXPECTED_CLI_ISSUANCES_PER_DAY = 2
#: A station must have both tmax and tmin on at least this many of the 7 full
#: review days (net of outage-explained days) to KEEP -- "nearly all".
MIN_COMPLETE_DATES_FOR_KEEP = 6

BANNER = "OPERATIONAL DATA-QUALITY REVIEW ONLY — NO PERFORMANCE ANALYSIS"
NOT_READY_VERDICT = "STATION PILOT REVIEW NOT READY — CALENDAR GATED"


class StationDecision(StrEnum):
    KEEP = "KEEP"
    EXTEND_COLLECTION = "EXTEND_COLLECTION"
    REMOVE_FOR_DATA_QUALITY = "REMOVE_FOR_DATA_QUALITY"
    PENDING_GATE = "PENDING_GATE"  # before the review gate; no decision issued


class OverallVerdict(StrEnum):
    PILOT_OPERATIONALLY_ACCEPTABLE = "PILOT_OPERATIONALLY_ACCEPTABLE"
    PILOT_COLLECTION_EXTENSION_REQUIRED = "PILOT_COLLECTION_EXTENSION_REQUIRED"
    PILOT_DATA_QUALITY_FAILURE = "PILOT_DATA_QUALITY_FAILURE"
    PILOT_REVIEW_NOT_READY = "PILOT_REVIEW_NOT_READY"
    PILOT_SPECIFICATION_INCOMPLETE = "PILOT_SPECIFICATION_INCOMPLETE"


class OutageKind(StrEnum):
    HOST_DOWN = "host_down"  # collector/host unavailable: excuses weather gaps
    UPSTREAM_WEATHER = "upstream_weather"  # NWS/IEM outage: excuses weather gaps
    KALSHI_ONLY = "kalshi_only"  # Kalshi API outage: NEVER a weather-health fault


@dataclass(frozen=True, slots=True)
class OutageInterval:
    start: date
    end: date
    kind: OutageKind


@dataclass(frozen=True, slots=True)
class StationCounts:
    """Counts-only operational evidence for one pilot station over the window.

    Every field is a cardinality, a date, or a duration -- never a predictive
    or market quantity."""

    station: str
    # completeness
    expected_dates: int
    dates_with_tmax: int
    dates_with_tmin: int
    dates_with_both: int
    missing_dates: tuple[str, ...]
    partial_current_excluded: bool
    duplicate_observation_rows: int
    backfill_only_dates: int
    # forecast coverage
    forecast_issuances: int
    expected_forecast_issuances: int
    longest_forecast_gap_hours: float
    forecast_source_gap_windows: int
    # provenance
    observations_missing_raw_payload: int
    observations_missing_source_product: int
    provenance_orphans: int
    environment_inconsistencies: int
    # parser / source health
    parser_failures: int
    malformed_products: int
    source_unavailable_attempts: int
    timezone_date_mismatches: int
    # operational
    weather_gap_dates_outage_explained: int
    weather_gap_dates_unexplained: int
    #: Field names whose per-station evidence could NOT be reliably derived from
    #: existing production data (e.g. a non-zero cycle-level parser-error
    #: aggregate that cannot be attributed to one station). Unavailable evidence
    #: is never reported as clean: a station with any unavailable field cannot
    #: be KEEP (it falls to EXTEND_COLLECTION unless a hard defect forces REMOVE).
    evidence_unavailable: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PilotReviewInputs:
    as_of: datetime
    stations: tuple[StationCounts, ...]
    outages: tuple[OutageInterval, ...]
    specification_registered: bool
    observatory_critical_count: int
    backup_healthy: bool
    review_gate: datetime = REVIEW_GATE
    pilot_start: datetime = PILOT_START_DEFAULT


@dataclass(frozen=True, slots=True)
class StationReview:
    station: str
    decision: StationDecision
    reasons: tuple[str, ...]


@dataclass
class PilotReviewReport:
    banner: str
    gate_open: bool
    as_of: datetime
    review_gate: datetime
    seconds_until_gate: float
    overall_verdict: OverallVerdict
    stations: list[StationReview] = field(default_factory=list)
    station_counts: list[StationCounts] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "banner": self.banner,
            "not_ready_verdict": None if self.gate_open else NOT_READY_VERDICT,
            "gate_open": self.gate_open,
            "as_of": self.as_of.isoformat(),
            "review_gate": self.review_gate.isoformat(),
            "seconds_until_gate": round(self.seconds_until_gate, 1),
            "overall_verdict": self.overall_verdict.value,
            "stations": [
                {"station": s.station, "decision": s.decision.value, "reasons": list(s.reasons)}
                for s in self.stations
            ],
            "station_counts": [_counts_to_dict(c) for c in self.station_counts],
            "reasons": self.reasons,
            "forbidden_analysis": (
                "no forecast error / calibration / expected or realized value / spread / "
                "liquidity / price / trade outcome / profitability / station ranking / "
                "model comparison is computed by this review"
            ),
            "operator_approval_required": (
                "any station-registry change (keep-as-is, extend, or removal) requires a "
                "separate explicit human approval; this review mutates nothing"
            ),
        }


def _counts_to_dict(c: StationCounts) -> dict[str, Any]:
    return {
        "station": c.station,
        "completeness": {
            "expected_dates": c.expected_dates,
            "dates_with_tmax": c.dates_with_tmax,
            "dates_with_tmin": c.dates_with_tmin,
            "dates_with_both": c.dates_with_both,
            "missing_dates": list(c.missing_dates),
            "partial_current_excluded": c.partial_current_excluded,
            "duplicate_observation_rows": c.duplicate_observation_rows,
            "backfill_only_dates": c.backfill_only_dates,
        },
        "forecast_coverage": {
            "issuances": c.forecast_issuances,
            "expected_issuances": c.expected_forecast_issuances,
            "longest_gap_hours": c.longest_forecast_gap_hours,
            "source_gap_windows": c.forecast_source_gap_windows,
        },
        "provenance": {
            "observations_missing_raw_payload": c.observations_missing_raw_payload,
            "observations_missing_source_product": c.observations_missing_source_product,
            "orphans": c.provenance_orphans,
            "environment_inconsistencies": c.environment_inconsistencies,
        },
        "parser_source_health": {
            "parser_failures": c.parser_failures,
            "malformed_products": c.malformed_products,
            "source_unavailable_attempts": c.source_unavailable_attempts,
            "timezone_date_mismatches": c.timezone_date_mismatches,
        },
        "operational": {
            "weather_gap_dates_outage_explained": c.weather_gap_dates_outage_explained,
            "weather_gap_dates_unexplained": c.weather_gap_dates_unexplained,
        },
        "evidence_unavailable": list(c.evidence_unavailable),
    }


def gate_open(as_of: datetime, *, review_gate: datetime = REVIEW_GATE) -> bool:
    """True at or after the registered review gate (fail-closed: naive input is
    treated as UTC)."""
    a = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
    return a >= review_gate


def classify_station(counts: StationCounts) -> StationReview:
    """Map one station's counts to KEEP / EXTEND / REMOVE using ONLY the
    registered operational criteria (ADR 0023). Precedence: a hard data-quality
    violation (REMOVE) outranks insufficiency (EXTEND) outranks KEEP. A
    Kalshi-only outage never enters here -- weather gaps are attributed to
    host/upstream-weather outages only."""
    remove: list[str] = []
    extend: list[str] = []

    # --- REMOVE_FOR_DATA_QUALITY: hard, deterministic defects.
    if counts.timezone_date_mismatches > 0:
        remove.append(f"timezone/date mismatch on {counts.timezone_date_mismatches} row(s)")
    if counts.provenance_orphans > 0:
        remove.append(f"{counts.provenance_orphans} provenance orphan(s)")
    if counts.environment_inconsistencies > 0:
        remove.append(f"{counts.environment_inconsistencies} environment inconsistency(ies)")
    if counts.duplicate_observation_rows > 0:
        remove.append(
            f"{counts.duplicate_observation_rows} duplicate/conflicting observation row(s)"
        )
    # A persistent, NOT outage-explained station-specific gap or parser defect.
    if counts.weather_gap_dates_unexplained >= 2:
        remove.append(
            f"{counts.weather_gap_dates_unexplained} unexplained station-specific missing date(s)"
        )
    if counts.parser_failures >= 2:
        remove.append(f"{counts.parser_failures} parser failure(s) (persistent)")

    # --- EXTEND_COLLECTION: insufficient/inconclusive but not defective.
    effective_expected = max(counts.expected_dates - counts.weather_gap_dates_outage_explained, 0)
    if counts.dates_with_both < min(MIN_COMPLETE_DATES_FOR_KEEP, effective_expected):
        extend.append(
            f"only {counts.dates_with_both} complete date(s) (need "
            f"{min(MIN_COMPLETE_DATES_FOR_KEEP, effective_expected)} net of outages)"
        )
    if counts.weather_gap_dates_outage_explained > 0:
        extend.append(
            f"{counts.weather_gap_dates_outage_explained} date(s) affected by a known "
            "host/upstream outage; more observation warranted"
        )
    if counts.source_unavailable_attempts >= 1 and counts.weather_gap_dates_unexplained < 2:
        extend.append(
            f"{counts.source_unavailable_attempts} isolated source-unavailable attempt(s)"
        )
    if counts.parser_failures == 1 or counts.malformed_products >= 1:
        extend.append("isolated parser/malformed-product event(s) need further observation")

    # Unavailable evidence can never be reported as clean: it blocks KEEP.
    if counts.evidence_unavailable:
        extend.append(
            "evidence incomplete — cannot confirm clean for: "
            + ", ".join(counts.evidence_unavailable)
        )

    if remove:
        return StationReview(counts.station, StationDecision.REMOVE_FOR_DATA_QUALITY, tuple(remove))
    if extend:
        return StationReview(counts.station, StationDecision.EXTEND_COLLECTION, tuple(extend))
    return StationReview(
        counts.station,
        StationDecision.KEEP,
        (
            f"{counts.dates_with_both} complete date(s); no parser/provenance defect; "
            "no unexplained station-specific gap; timezone/date handling correct; "
            "all registered evidence available",
        ),
    )


def review(inputs: PilotReviewInputs) -> PilotReviewReport:
    """Full review. Before the gate: no per-station decision, no overall
    operational verdict, artifact suppressed -- only raw counts are exposed."""
    open_ = gate_open(inputs.as_of, review_gate=inputs.review_gate)
    a = inputs.as_of if inputs.as_of.tzinfo else inputs.as_of.replace(tzinfo=UTC)
    seconds_until = max((inputs.review_gate - a).total_seconds(), 0.0)

    report = PilotReviewReport(
        banner=BANNER,
        gate_open=open_,
        as_of=inputs.as_of,
        review_gate=inputs.review_gate,
        seconds_until_gate=seconds_until,
        overall_verdict=OverallVerdict.PILOT_REVIEW_NOT_READY,
        station_counts=list(inputs.stations),
    )

    if not open_:
        # Calendar-gated: expose counts only; issue no decision or verdict.
        report.stations = [
            StationReview(c.station, StationDecision.PENDING_GATE, ("gate not open",))
            for c in inputs.stations
        ]
        report.reasons = [
            f"review gate {inputs.review_gate.isoformat()} not reached; "
            f"{round(seconds_until / 3600, 1)}h remaining"
        ]
        return report

    if not inputs.specification_registered:
        report.overall_verdict = OverallVerdict.PILOT_SPECIFICATION_INCOMPLETE
        report.reasons = ["registered pilot criteria could not be recovered"]
        return report

    report.stations = [classify_station(c) for c in inputs.stations]
    decisions = {s.decision for s in report.stations}
    if StationDecision.REMOVE_FOR_DATA_QUALITY in decisions:
        report.overall_verdict = OverallVerdict.PILOT_DATA_QUALITY_FAILURE
    elif StationDecision.EXTEND_COLLECTION in decisions:
        report.overall_verdict = OverallVerdict.PILOT_COLLECTION_EXTENSION_REQUIRED
    else:
        report.overall_verdict = OverallVerdict.PILOT_OPERATIONALLY_ACCEPTABLE
    report.reasons = [
        f"{s.station}: {s.decision.value} — {'; '.join(s.reasons)}" for s in report.stations
    ]
    if inputs.observatory_critical_count > 0:
        report.reasons.append(
            f"{inputs.observatory_critical_count} observatory CRITICAL finding(s) — investigate"
        )
    return report


# --- pure evidence builder (from read-only production rows) -------------------


@dataclass(frozen=True, slots=True)
class ObsRowLite:
    """One weather_observation row, only the fields the review may see."""

    observation_date: date
    variable: str
    raw_payload_id: int | None
    raw_payload_orphan: bool  # raw_payload_id set but no matching raw_api_payloads row
    issuance_time: datetime
    observed_at: datetime
    provider: str


#: A gap between successive forecast issuances longer than this is a
#: "source-gap window" (schedule-agnostic; mirrors the observatory cadence
#: check's 18h threshold for two ~2/day issuances).
FORECAST_GAP_THRESHOLD_HOURS = 18.0
#: |observation_date - issuance date| beyond this signals a timezone/date-
#: assignment defect (a correct CLI reports day D issued on D or D+1).
TZ_DATE_TOLERANCE_DAYS = 2
#: An observation whose observed_at is this many days after its
#: observation_date was collected only via later backfill, not contemporaneously.
BACKFILL_LAG_DAYS = 2


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


def build_station_counts(
    station: str,
    *,
    review_dates: tuple[date, ...],
    observations: tuple[ObsRowLite, ...],
    forecast_issue_times: tuple[datetime, ...],
    window_weather_errors: int,
    window_weather_invalid: int,
    outage_weather_dates: frozenset[str] = frozenset(),
    expected_iss_per_var: int = EXPECTED_CLI_ISSUANCES_PER_DAY,
) -> StationCounts:
    """Derive one pilot station's counts-only evidence from read-only rows.

    Every field comes from the passed rows (already station- and window-scoped
    by the caller). Fields that cannot be attributed per-station from existing
    data -- parser failures / malformed products / source-unavailable attempts,
    which are recorded only as cycle-level aggregates -- are 0 when the whole
    review window is clean (``window_weather_errors``/``invalid`` == 0, so no
    station had one) and otherwise marked UNAVAILABLE (never silently 0).
    """
    review_date_strs = {d.isoformat() for d in review_dates}
    by_date: dict[str, set[str]] = {}
    per_dv: dict[tuple[str, str], int] = {}
    missing_raw = orphans = provider_mismatch = tz_mismatch = 0
    backfill_dates: dict[str, bool] = {}

    for o in observations:
        ds = o.observation_date.isoformat()
        by_date.setdefault(ds, set()).add(o.variable)
        per_dv[(ds, o.variable)] = per_dv.get((ds, o.variable), 0) + 1
        if o.raw_payload_id is None:
            missing_raw += 1
        if o.raw_payload_orphan:
            orphans += 1
        if o.provider != "nws":
            provider_mismatch += 1
        if abs((o.observation_date - _naive(o.issuance_time).date()).days) > TZ_DATE_TOLERANCE_DAYS:
            tz_mismatch += 1
        is_backfill = (_naive(o.observed_at).date() - o.observation_date).days > BACKFILL_LAG_DAYS
        backfill_dates[ds] = backfill_dates.get(ds, True) and is_backfill

    with_tmax = sum(1 for v in by_date.values() if "tmax_f" in v)
    with_tmin = sum(1 for v in by_date.values() if "tmin_f" in v)
    with_both = sum(1 for v in by_date.values() if {"tmax_f", "tmin_f"} <= v)
    missing = tuple(sorted(review_date_strs - set(by_date)))
    duplicates = sum(max(n - expected_iss_per_var, 0) for n in per_dv.values())
    backfill_only = sum(1 for ds, only in backfill_dates.items() if only and ds in review_date_strs)

    # forecast gaps
    issues = sorted(_naive(t) for t in forecast_issue_times)
    gaps_h = [(b - a).total_seconds() / 3600.0 for a, b in itertools.pairwise(issues)]
    longest_gap = max(gaps_h) if gaps_h else 0.0
    source_gap_windows = sum(1 for g in gaps_h if g > FORECAST_GAP_THRESHOLD_HOURS)

    # aggregate-only fields: clean window => 0 (supported); else UNAVAILABLE.
    unavailable: list[str] = []
    if window_weather_errors != 0:
        unavailable.extend(["parser_failures", "source_unavailable_attempts"])
    if window_weather_invalid != 0:
        unavailable.append("malformed_products")

    exp_out = sum(1 for m in missing if m in outage_weather_dates)
    return StationCounts(
        station=station,
        expected_dates=len(review_dates),
        dates_with_tmax=with_tmax,
        dates_with_tmin=with_tmin,
        dates_with_both=with_both,
        missing_dates=missing,
        partial_current_excluded=True,
        duplicate_observation_rows=duplicates,
        backfill_only_dates=backfill_only,
        forecast_issuances=len(set(issues)),
        expected_forecast_issuances=len(review_dates) * expected_iss_per_var,
        longest_forecast_gap_hours=round(longest_gap, 2),
        forecast_source_gap_windows=source_gap_windows,
        observations_missing_raw_payload=missing_raw,
        observations_missing_source_product=0,  # source_product_id is NOT NULL
        provenance_orphans=orphans,
        environment_inconsistencies=provider_mismatch,  # weather has no env; provider proxy
        parser_failures=0,
        malformed_products=0,
        source_unavailable_attempts=0,
        timezone_date_mismatches=tz_mismatch,
        weather_gap_dates_outage_explained=exp_out,
        weather_gap_dates_unexplained=len(missing) - exp_out,
        evidence_unavailable=tuple(unavailable),
    )
