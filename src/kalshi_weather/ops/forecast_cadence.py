"""Forecast-collection cadence verification and continuous health metrics.

Answers one operational question that no other check answers: *is the
forecast collector capturing every NWS issuance?* Forecast history has no
backfill source (ADR 0003) — a capture gap is permanently unrecoverable —
so under-capture must be detected while it is still preventable, not
discovered when H0003's accumulation gate arrives.

Complements `ops/quality.py` (store-level integrity) and `ops/health.py`
(collector liveness): this module analyzes the *temporal structure* of the
captured forecast issuances themselves — cycles, gaps, duplicates,
ordering, delays — and derives deterministic alerts. Everything in the
analysis core is a pure function over in-memory records: no randomness,
no wall-clock reads (callers pass `now`), no database access. Thin async
loaders at the bottom adapt the database, mirroring `run_quality_checks`.

Cadence model — two complementary, deliberately different invariants
(architecture review, 2026-07-21):

1. **Minimum cadence verification (the N=2 floor check).** NWS operational
   practice guarantees a morning and an afternoon forecast package per
   office at roughly fixed *local* times, year-round. Each station-local
   day is therefore divided into exactly 2 wall-clock windows
   (00-12 / 12-24 local); a window with no captured issuance between the
   first and last observed cycle is a missing cycle. This verifies the
   *documented scheduled floor* and nothing more. The stored `issue_time`
   is the NWS gridpoint `updateTime` — an irregular, clustered event
   stream, not a timetable — so equal partitions finer than the twice-daily
   floor would assert a uniformity the data does not have and generate
   systematic false missing-cycle alerts; `expected_issuances_per_day`
   is accordingly **clamped to at most 2** (and at least 1). Higher values
   do not produce more accurate monitoring.
2. **Schedule-agnostic outage detection (the maximum-gap alert).** The
   largest gap between successive captured issuances must not exceed
   `max_issuance_gap_hours` (strictly greater fires; equality does not).
   This invariant makes no assumption about *when* in the day issuances
   cluster, so it stays valid across seasonal schedule variation and
   catches long silent stretches the window check structurally cannot
   (both windows of a day can be occupied while 20+ hours pass between
   issuances).

Local time uses each station's IANA timezone (registry), so windows
remain well-defined across DST transitions (a local hour always maps to
exactly one window; the 02:00 spring-forward gap and the 01:00 fall-back
fold never remove a whole window).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.domain.time import utc_now
from kalshi_weather.storage.models import CollectorRun, WeatherForecast
from kalshi_weather.weather.stations import list_stations

# --- Inputs ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForecastCapture:
    """One stored forecast row: a (station, variable, issue_time,
    valid_start) period, plus when the collector first stored it."""

    station_id: str
    variable: str
    issue_time: datetime  # aware UTC
    valid_start: datetime  # aware UTC
    observed_at: datetime  # aware UTC — capture (storage) time


@dataclass(frozen=True, slots=True)
class RunAttempt:
    """One weather-collector run attempt (from collector_runs)."""

    started_at: datetime  # aware UTC
    success: bool


#: Ceiling for expected_issuances_per_day. Equal wall-clock partitions
#: above the twice-daily NWS package floor assert a uniform issuance
#: distribution the real (clustered) updateTime stream does not have and
#: generate systematic false missing-cycle alerts — architecture review,
#: 2026-07-21. The schedule-agnostic max-gap alert covers everything a
#: finer partition would have attempted to cover.
MAX_EXPECTED_ISSUANCES_PER_DAY = 2


@dataclass(frozen=True, slots=True)
class CadenceConfig:
    """Deterministic thresholds. Values are operator-calibratable except
    `expected_issuances_per_day`, which is clamped into
    [1, MAX_EXPECTED_ISSUANCES_PER_DAY] at construction — the window
    check verifies the documented NWS scheduled floor only; finer
    partitions are not more accurate (see the module docstring)."""

    expected_issuances_per_day: int = 2
    max_issuance_gap_hours: float = 18.0
    delay_threshold_hours: float = 2.0
    outage_threshold_hours: float = 2.0
    stalled_threshold_hours: float = 24.0
    min_completeness_pct: float = 90.0

    def __post_init__(self) -> None:
        clamped = min(max(self.expected_issuances_per_day, 1), MAX_EXPECTED_ISSUANCES_PER_DAY)
        if clamped != self.expected_issuances_per_day:
            object.__setattr__(self, "expected_issuances_per_day", clamped)


# --- Report structures (plain-serializable fields only) ----------------------


@dataclass(frozen=True, slots=True)
class MissingCycle:
    local_date: str  # ISO date, station-local
    window_index: int
    window_label: str  # e.g. "00:00-12:00 local"


@dataclass(frozen=True, slots=True)
class DuplicateCapture:
    variable: str
    issue_time: str
    valid_start: str
    occurrences: int


@dataclass(frozen=True, slots=True)
class OutOfOrderArrival:
    issue_time: str
    observed_at: str
    later_issue_time_observed_earlier: str


@dataclass(frozen=True, slots=True)
class DelayedIssuance:
    issue_time: str
    first_observed_at: str
    delay_hours: float


@dataclass(frozen=True, slots=True)
class GapDistribution:
    count: int
    min_hours: float | None
    p50_hours: float | None
    p90_hours: float | None
    max_hours: float | None


@dataclass(frozen=True, slots=True)
class StationHealth:
    expected_issuances_per_day: int
    observed_issuances_per_day: float | None
    completeness_pct: float | None
    max_capture_gap_hours: float | None
    gap_distribution: GapDistribution
    last_capture_at: str | None
    last_issue_time: str | None
    last_run_at: str | None
    last_successful_run_at: str | None
    longest_outage_hours: float | None
    run_attempts: int


@dataclass(frozen=True, slots=True)
class StationCadenceReport:
    station_id: str
    timezone: str
    timezone_assumed_utc: bool
    n_captures: int
    n_issuances: int
    variables: list[str]
    first_issue_time: str | None
    last_issue_time: str | None
    mean_issuance_gap_hours: float | None
    max_issuance_gap_hours: float | None
    issuance_local_hour_histogram: list[int]  # 24 bins
    missing_cycles: list[MissingCycle]
    duplicates: list[DuplicateCapture]
    out_of_order_arrivals: list[OutOfOrderArrival]
    delayed_issuances: list[DelayedIssuance]
    incomplete_issuances: int  # issuances missing >=1 of the station's variables
    health: StationHealth


@dataclass(frozen=True, slots=True)
class CadenceAlert:
    station_id: str
    #: missing_issuance | duplicate_issuance | abnormal_cadence |
    #: issuance_gap | collector_outage | stalled_updates
    kind: str
    expected: str
    observed: str
    recommended_action: str


@dataclass(frozen=True, slots=True)
class ForecastCadenceReport:
    generated_at: str
    config: dict[str, Any]
    stations: list[StationCadenceReport]
    alerts: list[CadenceAlert]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- Pure helpers ------------------------------------------------------------


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600.0, 2)


def window_index(local_dt: datetime, n_per_day: int) -> int:
    """Window of a station-local timestamp: local hour mapped onto
    `n_per_day` equal wall-clock-hour windows. Well-defined on DST days:
    every local timestamp has exactly one local hour."""
    return local_dt.hour * n_per_day // 24


def window_label(index: int, n_per_day: int) -> str:
    start = 24 * index // n_per_day
    end = 24 * (index + 1) // n_per_day
    return f"{start:02d}:00-{end:02d}:00 local"


def cycle_of(issue_time_utc: datetime, tz: ZoneInfo, n_per_day: int) -> tuple[date, int]:
    local = issue_time_utc.astimezone(tz)
    return (local.date(), window_index(local, n_per_day))


def _next_cycle(cycle: tuple[date, int], n_per_day: int) -> tuple[date, int]:
    day, w = cycle
    if w + 1 < n_per_day:
        return (day, w + 1)
    return (day + timedelta(days=1), 0)


def _cycles_between(first: tuple[date, int], last: tuple[date, int], n_per_day: int) -> int:
    """Inclusive count of cycles from `first` to `last`."""
    day_span = (last[0] - first[0]).days
    return day_span * n_per_day + (last[1] - first[1]) + 1


def _nearest_rank(sorted_values: list[float], q: float) -> float:
    """Deterministic nearest-rank percentile over an ascending list."""
    idx = max(0, math.ceil(q * len(sorted_values)) - 1)
    return sorted_values[min(idx, len(sorted_values) - 1)]


@dataclass(frozen=True, slots=True)
class _Issuance:
    issue_time: datetime
    first_observed_at: datetime
    variables: frozenset[str]


def _collapse_issuances(captures: list[ForecastCapture]) -> list[_Issuance]:
    """Distinct issue_times for one station, each with its earliest
    capture time and the set of variables it carried; ascending by
    issue_time."""
    by_issue: dict[datetime, list[ForecastCapture]] = {}
    for capture in captures:
        by_issue.setdefault(capture.issue_time, []).append(capture)
    return [
        _Issuance(
            issue_time=issue_time,
            first_observed_at=min(c.observed_at for c in rows),
            variables=frozenset(c.variable for c in rows),
        )
        for issue_time, rows in sorted(by_issue.items())
    ]


# --- Part 1/2: per-station analysis ------------------------------------------


def _find_duplicates(captures: list[ForecastCapture]) -> list[DuplicateCapture]:
    counts: dict[tuple[str, datetime, datetime], int] = {}
    for c in captures:
        key = (c.variable, c.issue_time, c.valid_start)
        counts[key] = counts.get(key, 0) + 1
    return [
        DuplicateCapture(
            variable=variable,
            issue_time=_iso(issue_time),
            valid_start=_iso(valid_start),
            occurrences=n,
        )
        for (variable, issue_time, valid_start), n in sorted(counts.items())
        if n > 1
    ]


def _find_out_of_order(issuances: list[_Issuance]) -> list[OutOfOrderArrival]:
    """Issuances first observed *after* some later-issued issuance was
    already observed — arrival order inverted relative to issue order."""
    out: list[OutOfOrderArrival] = []
    by_observed = sorted(issuances, key=lambda i: (i.first_observed_at, i.issue_time))
    max_issue_seen: datetime | None = None
    max_issue_seen_at: datetime | None = None
    for issuance in by_observed:
        if max_issue_seen is not None and issuance.issue_time < max_issue_seen:
            assert max_issue_seen_at is not None
            out.append(
                OutOfOrderArrival(
                    issue_time=_iso(issuance.issue_time),
                    observed_at=_iso(issuance.first_observed_at),
                    later_issue_time_observed_earlier=_iso(max_issue_seen),
                )
            )
        if max_issue_seen is None or issuance.issue_time > max_issue_seen:
            max_issue_seen = issuance.issue_time
            max_issue_seen_at = issuance.first_observed_at
    return out


def _find_missing_cycles(
    issuances: list[_Issuance], tz: ZoneInfo, n_per_day: int
) -> list[MissingCycle]:
    if len(issuances) < 2:
        return []
    observed = {cycle_of(i.issue_time, tz, n_per_day) for i in issuances}
    first = min(observed)
    last = max(observed)
    missing: list[MissingCycle] = []
    cycle = first
    while cycle <= last:
        if cycle not in observed:
            missing.append(
                MissingCycle(
                    local_date=cycle[0].isoformat(),
                    window_index=cycle[1],
                    window_label=window_label(cycle[1], n_per_day),
                )
            )
        cycle = _next_cycle(cycle, n_per_day)
    return missing


def _station_health(
    issuances: list[_Issuance],
    captures: list[ForecastCapture],
    runs: list[RunAttempt],
    *,
    config: CadenceConfig,
    tz: ZoneInfo,
    now: datetime,
) -> StationHealth:
    n = config.expected_issuances_per_day
    gaps: list[float] = [
        _hours(later.issue_time - earlier.issue_time) for earlier, later in pairwise(issuances)
    ]
    gaps_sorted = sorted(gaps)
    gap_distribution = GapDistribution(
        count=len(gaps_sorted),
        min_hours=gaps_sorted[0] if gaps_sorted else None,
        p50_hours=_nearest_rank(gaps_sorted, 0.5) if gaps_sorted else None,
        p90_hours=_nearest_rank(gaps_sorted, 0.9) if gaps_sorted else None,
        max_hours=gaps_sorted[-1] if gaps_sorted else None,
    )

    observed_per_day: float | None = None
    completeness: float | None = None
    if issuances:
        cycles = {cycle_of(i.issue_time, tz, n) for i in issuances}
        first, last = min(cycles), max(cycles)
        expected_cycles = _cycles_between(first, last, n)
        span_days = max(1, (last[0] - first[0]).days + 1)
        observed_per_day = round(len(issuances) / span_days, 2)
        completeness = round(100.0 * len(cycles) / expected_cycles, 2)

    successful_runs = [r for r in runs if r.success]
    outage: float | None = None
    if runs:
        run_times = sorted(r.started_at for r in runs)
        run_gaps = [_hours(b - a) for a, b in pairwise(run_times)]
        run_gaps.append(_hours(now - run_times[-1]))
        outage = max(run_gaps)

    return StationHealth(
        expected_issuances_per_day=n,
        observed_issuances_per_day=observed_per_day,
        completeness_pct=completeness,
        max_capture_gap_hours=gaps_sorted[-1] if gaps_sorted else None,
        gap_distribution=gap_distribution,
        last_capture_at=_iso(max(c.observed_at for c in captures)) if captures else None,
        last_issue_time=_iso(issuances[-1].issue_time) if issuances else None,
        last_run_at=_iso(max(r.started_at for r in runs)) if runs else None,
        last_successful_run_at=(
            _iso(max(r.started_at for r in successful_runs)) if successful_runs else None
        ),
        longest_outage_hours=outage,
        run_attempts=len(runs),
    )


def analyze_station(
    station_id: str,
    captures: list[ForecastCapture],
    runs: list[RunAttempt],
    *,
    config: CadenceConfig,
    timezone_name: str | None,
    now: datetime,
) -> StationCadenceReport:
    """Full Part 1 + Part 2 analysis for one station. Pure and
    deterministic: identical inputs always produce identical output."""
    tz_name = timezone_name or "UTC"
    tz = ZoneInfo(tz_name)
    issuances = _collapse_issuances(captures)
    n = config.expected_issuances_per_day

    histogram = [0] * 24
    for issuance in issuances:
        histogram[issuance.issue_time.astimezone(tz).hour] += 1

    gaps = [_hours(later.issue_time - earlier.issue_time) for earlier, later in pairwise(issuances)]
    station_variables = frozenset(c.variable for c in captures)
    delayed = [
        DelayedIssuance(
            issue_time=_iso(i.issue_time),
            first_observed_at=_iso(i.first_observed_at),
            delay_hours=_hours(i.first_observed_at - i.issue_time),
        )
        for i in issuances
        if _hours(i.first_observed_at - i.issue_time) > config.delay_threshold_hours
    ]

    return StationCadenceReport(
        station_id=station_id,
        timezone=tz_name,
        timezone_assumed_utc=timezone_name is None,
        n_captures=len(captures),
        n_issuances=len(issuances),
        variables=sorted(station_variables),
        first_issue_time=_iso(issuances[0].issue_time) if issuances else None,
        last_issue_time=_iso(issuances[-1].issue_time) if issuances else None,
        mean_issuance_gap_hours=round(sum(gaps) / len(gaps), 2) if gaps else None,
        max_issuance_gap_hours=max(gaps) if gaps else None,
        issuance_local_hour_histogram=histogram,
        missing_cycles=_find_missing_cycles(issuances, tz, n),
        duplicates=_find_duplicates(captures),
        out_of_order_arrivals=_find_out_of_order(issuances),
        delayed_issuances=delayed,
        incomplete_issuances=sum(1 for i in issuances if i.variables != station_variables),
        health=_station_health(issuances, captures, runs, config=config, tz=tz, now=now),
    )


# --- Part 3: alert generation ------------------------------------------------


def generate_alerts(
    station_report: StationCadenceReport, *, config: CadenceConfig, now: datetime
) -> list[CadenceAlert]:
    """Deterministic alerts for one station, ordered by kind. No
    notification transport — generation only."""
    alerts: list[CadenceAlert] = []
    station = station_report.station_id
    health = station_report.health

    if station_report.missing_cycles:
        latest = station_report.missing_cycles[-1]
        alerts.append(
            CadenceAlert(
                station_id=station,
                kind="missing_issuance",
                expected=(
                    f"{config.expected_issuances_per_day} issuance cycle(s) per "
                    f"station-local day, no interior cycle empty"
                ),
                observed=(
                    f"{len(station_report.missing_cycles)} missing cycle(s); latest: "
                    f"{latest.local_date} window {latest.window_index} ({latest.window_label})"
                ),
                recommended_action=(
                    "Inspect weather-collector logs for the missing windows and verify "
                    "the NWS forecast endpoint was reachable; a genuinely empty window "
                    "is a rare source-side skip of the scheduled morning/afternoon "
                    "package -- cross-check the office's own product history before "
                    "assuming collector fault."
                ),
            )
        )

    if station_report.duplicates:
        alerts.append(
            CadenceAlert(
                station_id=station,
                kind="duplicate_issuance",
                expected="unique (station, variable, issue_time, valid_start) captures",
                observed=f"{len(station_report.duplicates)} duplicated capture key(s)",
                recommended_action=(
                    "Investigate the store/loader path: the database unique constraint "
                    "should make this impossible — treat as data corruption and audit "
                    "the rows before any analysis uses them."
                ),
            )
        )

    if (
        health.completeness_pct is not None
        and health.completeness_pct < config.min_completeness_pct
    ):
        alerts.append(
            CadenceAlert(
                station_id=station,
                kind="abnormal_cadence",
                expected=f"completeness >= {config.min_completeness_pct}%",
                observed=(
                    f"completeness {health.completeness_pct}% "
                    f"({health.observed_issuances_per_day}/day observed vs "
                    f"{health.expected_issuances_per_day}/day expected)"
                ),
                recommended_action=(
                    "Verify the collector's schedule interval covers every NWS update "
                    "window and that the supervised runner has been up continuously; "
                    "this check verifies the documented twice-daily scheduled floor, "
                    "so a shortfall means scheduled packages are being missed."
                ),
            )
        )

    max_gap = station_report.max_issuance_gap_hours
    if max_gap is not None and max_gap > config.max_issuance_gap_hours:
        alerts.append(
            CadenceAlert(
                station_id=station,
                kind="issuance_gap",
                expected=(
                    f"successive captured issuances at most "
                    f"{config.max_issuance_gap_hours}h apart (schedule-agnostic; "
                    f"strictly greater fires, equality does not)"
                ),
                observed=(f"largest gap between successive captured issuances: {max_gap}h"),
                recommended_action=(
                    "A silent stretch this long exceeds any normal NWS update rhythm "
                    "regardless of where in the day issuances cluster: check collector "
                    "uptime over the gap and the provider endpoint's availability; "
                    "forecast history has no backfill source, so confirm and fix the "
                    "cause before the next gap."
                ),
            )
        )

    outage = health.longest_outage_hours
    if health.run_attempts == 0:
        alerts.append(
            CadenceAlert(
                station_id=station,
                kind="collector_outage",
                expected=(
                    f"weather-collector run attempts at most {config.outage_threshold_hours}h apart"
                ),
                observed="no weather-collector run attempts recorded at all",
                recommended_action=(
                    "Start (or verify) the weather collector runner — forecast history "
                    "has no backfill source; every uncollected window is permanent loss."
                ),
            )
        )
    elif outage is not None and outage > config.outage_threshold_hours:
        alerts.append(
            CadenceAlert(
                station_id=station,
                kind="collector_outage",
                expected=f"run attempts at most {config.outage_threshold_hours}h apart",
                observed=f"longest gap between run attempts (or since the last run): {outage}h",
                recommended_action=(
                    "Restart or fix the weather collector runner and check `ops health`; "
                    "confirm the supervising process is scheduled continuously."
                ),
            )
        )

    if station_report.last_issue_time is not None:
        last_issue = datetime.fromisoformat(station_report.last_issue_time)
        staleness = _hours(now - last_issue)
        if staleness > config.stalled_threshold_hours:
            alerts.append(
                CadenceAlert(
                    station_id=station,
                    kind="stalled_updates",
                    expected=f"a new issuance within the last {config.stalled_threshold_hours}h",
                    observed=f"newest captured issuance is {staleness}h old",
                    recommended_action=(
                        "The collector may be running without capturing anything new: "
                        "verify the provider endpoint, the updateTime-change detection, "
                        "and that the station's forecast product is still published."
                    ),
                )
            )

    return sorted(alerts, key=lambda a: (a.station_id, a.kind))


# --- Report assembly ---------------------------------------------------------


def build_cadence_report(
    captures: list[ForecastCapture],
    runs: list[RunAttempt],
    *,
    config: CadenceConfig,
    timezones: dict[str, str],
    now: datetime,
) -> ForecastCadenceReport:
    """Pure top-level assembly: groups captures by station, analyzes each
    independently (station independence by construction — no cross-station
    state exists), and generates alerts. `runs` are platform-wide (the
    weather collector serves all stations) and enter each station's
    outage metrics identically."""
    by_station: dict[str, list[ForecastCapture]] = {}
    for capture in captures:
        by_station.setdefault(capture.station_id, []).append(capture)

    stations = [
        analyze_station(
            station_id,
            station_captures,
            runs,
            config=config,
            timezone_name=timezones.get(station_id),
            now=now,
        )
        for station_id, station_captures in sorted(by_station.items())
    ]
    alerts = [
        alert
        for station_report in stations
        for alert in generate_alerts(station_report, config=config, now=now)
    ]
    return ForecastCadenceReport(
        generated_at=_iso(now),
        config=asdict(config),
        stations=stations,
        alerts=alerts,
    )


# --- Database adapters (thin; mirror ops/quality.py) -------------------------


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def load_forecast_captures(session: AsyncSession) -> list[ForecastCapture]:
    rows = (
        await session.execute(
            select(
                WeatherForecast.station_id,
                WeatherForecast.variable,
                WeatherForecast.issue_time,
                WeatherForecast.valid_start,
                WeatherForecast.observed_at,
            )
        )
    ).all()
    return [
        ForecastCapture(
            station_id=row.station_id,
            variable=row.variable,
            issue_time=_ensure_utc(row.issue_time),
            valid_start=_ensure_utc(row.valid_start),
            observed_at=_ensure_utc(row.observed_at),
        )
        for row in rows
    ]


async def load_weather_runs(session: AsyncSession) -> list[RunAttempt]:
    rows = (
        await session.execute(
            select(CollectorRun.started_at, CollectorRun.success).where(
                CollectorRun.collector == "weather"
            )
        )
    ).all()
    return [RunAttempt(started_at=_ensure_utc(row.started_at), success=row.success) for row in rows]


async def run_forecast_cadence(
    session: AsyncSession,
    *,
    config: CadenceConfig | None = None,
    now: datetime | None = None,
) -> ForecastCadenceReport:
    """Load captures + weather-collector runs from the store and produce
    the full cadence report. Station timezones come from the registry;
    stations present in the data but absent from the registry fall back
    to UTC and are flagged (`timezone_assumed_utc`)."""
    resolved_config = config or CadenceConfig()
    resolved_now = _ensure_utc(now) if now is not None else utc_now()
    captures = await load_forecast_captures(session)
    runs = await load_weather_runs(session)
    timezones = {s.station_id: s.timezone for s in list_stations()}
    return build_cadence_report(
        captures, runs, config=resolved_config, timezones=timezones, now=resolved_now
    )
