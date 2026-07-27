"""Seven-day SEA/PHX/MIA station-pilot review (ADR 0023) -- READ-ONLY.

Answers, from collected production data only: did the pilot stations
collect reliably, did the existing four stations stay healthy, did
settlement coverage improve as expected, did collector/storage/backup
health regress, and should the pilot continue, roll back, or graduate to a
second batch (human-approved, never autonomous)?

Pure decision logic with an injected clock; the CLI layer gathers inputs
via SELECTs only. Computes no predictive metric, touches no experiment,
creates no paper action, writes nothing anywhere by default.
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

PILOT_STATIONS = ("SEA", "PHX", "MIA")
BASELINE_STATIONS = ("CHI", "DEN", "LAX", "NYC")
#: First successful seven-station weather cycle (run 1353, ADR 0023 deploy).
PILOT_START_DEFAULT = datetime(2026, 7, 27, 21, 55, tzinfo=UTC)
REVIEW_DAYS_REQUIRED = 7
#: Expected NWS CLI issuance cycles per station-local day (verified live for
#: all three pilot codes during the feasibility audit: 2/day).
EXPECTED_CLI_ISSUANCES_PER_DAY = 2
#: Settlement no-regression floor: resolved four-city markets at pilot
#: deploy (ADR 0023 validation) -- the count may grow, never shrink.
BASELINE_RESOLVED_FLOOR = 1188

# Review states.
PILOT_COLLECTING = "PILOT_COLLECTING"
PILOT_REVIEW_READY = "PILOT_REVIEW_READY"
PILOT_INCOMPLETE = "PILOT_INCOMPLETE"
PILOT_INTEGRITY_ISSUE = "PILOT_INTEGRITY_ISSUE"

# Finding classifications.
EXPECTED_BASELINE = "EXPECTED_BASELINE"
TRANSIENT = "TRANSIENT"
ACTIONABLE_WARNING = "ACTIONABLE_WARNING"
CRITICAL_REGRESSION = "CRITICAL_REGRESSION"

# Recommendations.
CONTINUE_PILOT = "CONTINUE_PILOT"
ROLLBACK_PILOT = "ROLLBACK_PILOT"
SECOND_BATCH_ELIGIBLE = "SECOND_BATCH_ELIGIBLE"


# --- review window (pure, injected clock) ------------------------------------


@dataclass(frozen=True)
class ReviewWindow:
    pilot_start: datetime
    as_of: datetime
    first_full_day: date
    last_complete_day: date | None
    elapsed_full_days: int
    state: str
    review_ready_at: date


def review_window(as_of: datetime, *, pilot_start: datetime = PILOT_START_DEFAULT) -> ReviewWindow:
    """Full UTC days only: day 1 is the first complete UTC day after
    activation; the in-progress current day never counts (and never fails)."""
    first_full = pilot_start.date() + timedelta(days=1)
    last_complete = as_of.date() - timedelta(days=1)
    elapsed = max(0, (last_complete - first_full).days + 1) if last_complete >= first_full else 0
    ready_at = first_full + timedelta(days=REVIEW_DAYS_REQUIRED)
    if as_of < pilot_start:
        state = PILOT_INTEGRITY_ISSUE
    elif elapsed >= REVIEW_DAYS_REQUIRED:
        state = PILOT_REVIEW_READY
    else:
        state = PILOT_COLLECTING
    return ReviewWindow(
        pilot_start=pilot_start,
        as_of=as_of,
        first_full_day=first_full,
        last_complete_day=last_complete if last_complete >= first_full else None,
        elapsed_full_days=elapsed,
        state=state,
        review_ready_at=ready_at,
    )


# --- per-station continuity (pure) -------------------------------------------


@dataclass(frozen=True)
class ForecastRowLite:
    station: str
    issue_time: datetime  # naive UTC
    observed_at: datetime  # naive UTC
    valid_start: datetime  # naive UTC


@dataclass(frozen=True)
class ObservationRowLite:
    station: str
    variable: str  # tmax_f | tmin_f
    observation_date: date


@dataclass
class StationContinuity:
    station: str
    forecast_rows_by_day: dict[str, int] = field(default_factory=dict)
    issuances_by_day: dict[str, int] = field(default_factory=dict)
    observation_days_complete: int = 0
    observation_days_missing: list[str] = field(default_factory=list)
    duplicate_logical_rows: int = 0
    longest_forecast_gap_hours: float = 0.0
    latest_forecast_age_hours: float | None = None
    latest_observation_date: str | None = None
    ingestion_delay_p50_minutes: float | None = None
    ingestion_delay_p90_minutes: float | None = None
    issuance_shortfall_days: list[str] = field(default_factory=list)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, round(q * (len(ordered) - 1)))
    return ordered[idx]


def station_continuity(
    station: str,
    forecasts: list[ForecastRowLite],
    observations: list[ObservationRowLite],
    window: ReviewWindow,
) -> StationContinuity:
    """Continuity over COMPLETE days only; the in-progress day is excluded."""
    out = StationContinuity(station=station)
    days: list[date] = []
    if window.last_complete_day is not None:
        d = window.first_full_day
        while d <= window.last_complete_day:
            days.append(d)
            d += timedelta(days=1)

    fc = [f for f in forecasts if f.station == station]
    by_day: dict[date, list[ForecastRowLite]] = defaultdict(list)
    for f in fc:
        by_day[f.observed_at.date()].append(f)
    for d in days:
        rows = by_day.get(d, [])
        out.forecast_rows_by_day[d.isoformat()] = len(rows)
        out.issuances_by_day[d.isoformat()] = len({f.issue_time for f in rows})
    # duplicates: same (issue_time, valid_start) stored twice
    key_counts = Counter((f.issue_time, f.valid_start) for f in fc)
    out.duplicate_logical_rows = sum(c - 1 for c in key_counts.values() if c > 1)
    # gaps + latest age from ingestion times (whole pilot period, incl. today)
    times = sorted({f.observed_at for f in fc})
    gaps = [(b - a).total_seconds() / 3600 for a, b in itertools.pairwise(times)]
    out.longest_forecast_gap_hours = round(max(gaps), 2) if gaps else 0.0
    as_of_naive = window.as_of.astimezone(UTC).replace(tzinfo=None)
    if times:
        out.latest_forecast_age_hours = round(
            (as_of_naive - times[-1]).total_seconds() / 3600, 2
        )
    delays = [
        max(0.0, (f.observed_at - f.issue_time).total_seconds() / 60) for f in fc
    ]
    if delays:
        out.ingestion_delay_p50_minutes = round(_percentile(delays, 0.5), 1)
        out.ingestion_delay_p90_minutes = round(_percentile(delays, 0.9), 1)

    obs = [o for o in observations if o.station == station]
    have: dict[date, set[str]] = defaultdict(set)
    for o in obs:
        have[o.observation_date].add(o.variable)
    for d in days:
        if {"tmax_f", "tmin_f"} <= have.get(d, set()):
            out.observation_days_complete += 1
        else:
            out.observation_days_missing.append(d.isoformat())
    if obs:
        out.latest_observation_date = max(o.observation_date for o in obs).isoformat()
    for d in days:
        if out.issuances_by_day.get(d.isoformat(), 0) < EXPECTED_CLI_ISSUANCES_PER_DAY:
            out.issuance_shortfall_days.append(d.isoformat())
    return out


# --- timezone validation (pure) ----------------------------------------------


def timezone_checks(as_of: datetime) -> dict[str, dict[str, Any]]:
    """Deterministic zoneinfo assertions for the pilot stations (summer and
    winter offsets; PHX fixed and distinct from Denver in July)."""
    out: dict[str, dict[str, Any]] = {}
    july = datetime(as_of.year, 7, 15, 12, tzinfo=UTC)
    january = datetime(as_of.year, 1, 15, 12, tzinfo=UTC)

    def offset(tz: str, when: datetime) -> float:
        return when.astimezone(ZoneInfo(tz)).utcoffset().total_seconds() / 3600  # type: ignore[union-attr]

    sea = {"summer_utc_offset": offset("America/Los_Angeles", july),
           "winter_utc_offset": offset("America/Los_Angeles", january)}
    sea["ok"] = sea["summer_utc_offset"] == -7.0 and sea["winter_utc_offset"] == -8.0
    phx = {"summer_utc_offset": offset("America/Phoenix", july),
           "winter_utc_offset": offset("America/Phoenix", january),
           "denver_summer_offset": offset("America/Denver", july)}
    phx["ok"] = (
        phx["summer_utc_offset"] == phx["winter_utc_offset"] == -7.0
        and phx["summer_utc_offset"] != phx["denver_summer_offset"]
    )
    mia = {"summer_utc_offset": offset("America/New_York", july),
           "winter_utc_offset": offset("America/New_York", january)}
    mia["ok"] = mia["summer_utc_offset"] == -4.0 and mia["winter_utc_offset"] == -5.0
    out["SEA"], out["PHX"], out["MIA"] = sea, phx, mia
    return out


def observation_date_spillover(
    observations: list[ObservationRowLite], window: ReviewWindow
) -> dict[str, bool]:
    """No station-local observation date may exceed the as-of local date --
    a future-dated observation would indicate UTC-day spillover."""
    out: dict[str, bool] = {}
    tz_map = {"SEA": "America/Los_Angeles", "PHX": "America/Phoenix", "MIA": "America/New_York"}
    for st in PILOT_STATIONS:
        local_today = window.as_of.astimezone(ZoneInfo(tz_map[st])).date()
        dates = [o.observation_date for o in observations if o.station == st]
        out[st] = all(d <= local_today for d in dates)
    return out


# --- collector impact (pure) --------------------------------------------------


@dataclass(frozen=True)
class CycleLite:
    run_id: int
    started_at: datetime
    duration_seconds: float
    requests: int
    success: bool


@dataclass(frozen=True)
class CollectorImpact:
    pre_median_s: float
    pre_p90_s: float
    post_first_backfill_s: float | None
    post_median_s: float
    post_p90_s: float
    post_p95_s: float
    post_requests_median: float
    failed_post_cycles: int
    cycles_exceeding_half_cadence: int


def collector_impact(
    pre: list[CycleLite], post: list[CycleLite], *, cadence_seconds: float
) -> CollectorImpact:
    """First post-pilot cycle (station backfill) is separated from steady
    state; a cycle above half the cadence counts as capacity pressure."""
    post_ok = sorted((c for c in post if c.success), key=lambda c: c.run_id)
    backfill = post_ok[0].duration_seconds if post_ok else None
    steady = post_ok[1:] if len(post_ok) > 1 else []
    pre_dur = [c.duration_seconds for c in pre if c.success]
    steady_dur = [c.duration_seconds for c in steady]
    return CollectorImpact(
        pre_median_s=round(_percentile(pre_dur, 0.5), 2),
        pre_p90_s=round(_percentile(pre_dur, 0.9), 2),
        post_first_backfill_s=round(backfill, 2) if backfill is not None else None,
        post_median_s=round(_percentile(steady_dur, 0.5), 2),
        post_p90_s=round(_percentile(steady_dur, 0.9), 2),
        post_p95_s=round(_percentile(steady_dur, 0.95), 2),
        post_requests_median=_percentile([float(c.requests) for c in steady], 0.5),
        failed_post_cycles=sum(1 for c in post if not c.success),
        cycles_exceeding_half_cadence=sum(
            1 for c in steady if c.duration_seconds > cadence_seconds / 2
        ),
    )


# --- decision gates (pure) ----------------------------------------------------


@dataclass(frozen=True)
class GateInputs:
    review_state: str
    timezone_ok: bool
    spillover_ok: bool
    mapping_ok: bool
    pilot_critical_findings: int
    stations_with_stable_issuance: int
    pilot_observation_completeness: float  # complete station-days / expected
    duplicate_rows: int
    collector_capacity_ok: bool
    baseline_stations_healthy: bool
    settlement_no_regression: bool
    settlement_unresolved_new: int
    storage_growth_mb_per_day: float


def decide(inputs: GateInputs) -> tuple[str, list[str]]:
    """Deterministic recommendation + reasons. SECOND_BATCH_ELIGIBLE still
    requires explicit human approval -- this function never grants it."""
    reasons: list[str] = []
    if not inputs.mapping_ok:
        reasons.append("authoritative_station_mapping_wrong")
    if not (inputs.timezone_ok and inputs.spillover_ok):
        reasons.append("timezone_or_target_date_error")
    if not inputs.collector_capacity_ok:
        reasons.append("collector_instability")
    if not inputs.baseline_stations_healthy:
        reasons.append("existing_station_regression")
    if inputs.duplicate_rows > 0:
        reasons.append("persistent_duplication")
    if reasons:
        return ROLLBACK_PILOT, reasons

    continue_ok = (
        inputs.pilot_critical_findings == 0
        and inputs.pilot_observation_completeness >= 0.85
        and inputs.settlement_no_regression
        and inputs.settlement_unresolved_new == 0
        and inputs.storage_growth_mb_per_day < 50.0
    )
    if not continue_ok:
        if inputs.pilot_critical_findings:
            reasons.append("pilot_attributable_critical")
        if inputs.pilot_observation_completeness < 0.85:
            reasons.append("observation_completeness_below_0.85")
        if not inputs.settlement_no_regression:
            reasons.append("settlement_regression")
        if inputs.settlement_unresolved_new:
            reasons.append("new_unresolved_settlements")
        if inputs.storage_growth_mb_per_day >= 50.0:
            reasons.append("storage_growth_excessive")
        return ROLLBACK_PILOT if "settlement_regression" in reasons else CONTINUE_PILOT, reasons

    if (
        inputs.review_state == PILOT_REVIEW_READY
        and inputs.stations_with_stable_issuance >= 2
    ):
        return SECOND_BATCH_ELIGIBLE, ["all_gates_pass", "human_approval_still_required"]
    if inputs.review_state != PILOT_REVIEW_READY:
        return CONTINUE_PILOT, ["seven_days_not_elapsed"]
    return CONTINUE_PILOT, ["stable_issuance_below_2_of_3"]


# --- second-batch ranking (static, from the frozen feasibility audit) ---------

#: (city, score notes) ordered ranking. Criteria: mapping simplicity, tz &
#: climate-regime value vs what we already collect, observed CLI issuance
#: reliability, operational similarity to the pilot, special conditions.
#: NO predictive/experiment input.
SECOND_BATCH_RANKING: tuple[tuple[str, str], ...] = (
    ("DFW", "plains regime + Central tz (new); 3 CLI/day; only note: Dallas code is DFW"),
    ("MSP", "continental-north regime + Central tz; 3 CLI/day; no special conditions"),
    ("ATL", "humid-subtropical southeast; 3 CLI/day (highest observed); simple mapping"),
    ("MSY", "gulf-coast regime + Central tz; 2 CLI/day; simple mapping"),
    ("HOU", "gulf regime (overlaps MSY); Hobby-station note; 2 CLI/day"),
    ("BOS", "northeast coastal (regime overlaps NYC); simple mapping"),
    ("PHL", "mid-Atlantic (overlaps NYC/DCA); simple mapping"),
    ("DCA", "mid-Atlantic (overlaps NYC/PHL); simple mapping"),
    ("LAS", "desert (regime overlaps PHX); Pacific tz already covered"),
    ("AUS", "Texas hill country (overlaps DFW/SAT); 4 CLI/day multi-cycle note"),
    ("SFO", "marine (regime overlaps SEA/LAX); simple mapping"),
)
SECOND_BATCH_RECOMMENDED = ("DFW", "MSP", "ATL", "MSY")
