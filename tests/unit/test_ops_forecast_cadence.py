"""Tests for ops/forecast_cadence.py — the pure analysis core only (the
thin DB loaders mirror ops/quality.py's untested adapters). All inputs
are hand-constructed aware-UTC records; no randomness anywhere."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from kalshi_weather.ops.forecast_cadence import (
    CadenceConfig,
    ForecastCapture,
    RunAttempt,
    build_cadence_report,
    cycle_of,
    generate_alerts,
    window_index,
    window_label,
)

NY = ZoneInfo("America/New_York")
LA = ZoneInfo("America/Los_Angeles")
TIMEZONES = {"NYC": "America/New_York", "LAX": "America/Los_Angeles"}
CONFIG = CadenceConfig()


def _capture(
    station: str,
    issue_local: datetime,
    tz: ZoneInfo,
    *,
    variable: str = "tmax_f",
    observed_delay_minutes: int = 5,
) -> ForecastCapture:
    """One capture whose issue_time is the given *local* wall-clock time
    (converted to UTC), observed shortly after issuance."""
    issue_utc = issue_local.replace(tzinfo=tz).astimezone(UTC)
    return ForecastCapture(
        station_id=station,
        variable=variable,
        issue_time=issue_utc,
        valid_start=issue_utc + timedelta(hours=6),
        observed_at=issue_utc + timedelta(minutes=observed_delay_minutes),
    )


def _perfect_day_captures(
    station: str, day: date, tz: ZoneInfo, *, variable: str = "tmax_f"
) -> list[ForecastCapture]:
    """Two issuances per local day, one in each expected window
    (03:30 local -> window 0; 15:30 local -> window 1)."""
    morning = datetime(day.year, day.month, day.day, 3, 30)
    afternoon = datetime(day.year, day.month, day.day, 15, 30)
    return [
        _capture(station, morning, tz, variable=variable),
        _capture(station, afternoon, tz, variable=variable),
    ]


def _regular_runs(start: datetime, *, hours: int, interval_minutes: int = 30) -> list[RunAttempt]:
    n = hours * 60 // interval_minutes
    return [
        RunAttempt(started_at=start + timedelta(minutes=interval_minutes * i), success=True)
        for i in range(n + 1)
    ]


def _days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


# --- Window / cycle primitives -----------------------------------------------


def test_window_index_two_per_day() -> None:
    assert window_index(datetime(2026, 1, 1, 0, 0), 2) == 0
    assert window_index(datetime(2026, 1, 1, 11, 59), 2) == 0
    assert window_index(datetime(2026, 1, 1, 12, 0), 2) == 1
    assert window_index(datetime(2026, 1, 1, 23, 59), 2) == 1


def test_window_label() -> None:
    assert window_label(0, 2) == "00:00-12:00 local"
    assert window_label(1, 2) == "12:00-24:00 local"


def test_cycle_of_uses_station_local_date() -> None:
    # 2026-01-02 03:00 UTC is 2026-01-01 22:00 in New York -- prior local day, window 1
    utc_dt = datetime(2026, 1, 2, 3, 0, tzinfo=UTC)
    assert cycle_of(utc_dt, NY, 2) == (date(2026, 1, 1), 1)


# --- Perfect cadence ---------------------------------------------------------


def test_perfect_cadence_no_findings_no_alerts() -> None:
    captures: list[ForecastCapture] = []
    for day in _days(date(2026, 6, 1), 4):
        captures += _perfect_day_captures("NYC", day, NY)
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    runs = _regular_runs(min(c.observed_at for c in captures) - timedelta(hours=1), hours=96)
    runs = [r for r in runs if r.started_at <= now]

    report = build_cadence_report(captures, runs, config=CONFIG, timezones=TIMEZONES, now=now)
    assert len(report.stations) == 1
    station = report.stations[0]
    assert station.n_issuances == 8
    assert station.missing_cycles == []
    assert station.duplicates == []
    assert station.out_of_order_arrivals == []
    assert station.delayed_issuances == []
    assert station.incomplete_issuances == 0
    assert station.health.completeness_pct == 100.0
    assert station.health.observed_issuances_per_day == 2.0
    assert report.alerts == []


def test_perfect_cadence_histogram_concentrates_at_issue_hours() -> None:
    captures: list[ForecastCapture] = []
    for day in _days(date(2026, 6, 1), 4):
        captures += _perfect_day_captures("NYC", day, NY)
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    report = build_cadence_report(captures, [], config=CONFIG, timezones=TIMEZONES, now=now)
    histogram = report.stations[0].issuance_local_hour_histogram
    assert histogram[3] == 4  # 03:30 local x 4 days
    assert histogram[15] == 4  # 15:30 local x 4 days
    assert sum(histogram) == 8


# --- One missed issuance -----------------------------------------------------


def test_one_missed_issuance_reported_and_alerted() -> None:
    captures: list[ForecastCapture] = []
    for i, day in enumerate(_days(date(2026, 6, 1), 4)):
        day_captures = _perfect_day_captures("NYC", day, NY)
        if i == 1:
            day_captures = day_captures[:1]  # drop the afternoon issuance of day 2
        captures += day_captures
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    runs = _regular_runs(min(c.observed_at for c in captures) - timedelta(hours=1), hours=96)
    runs = [r for r in runs if r.started_at <= now]

    report = build_cadence_report(captures, runs, config=CONFIG, timezones=TIMEZONES, now=now)
    station = report.stations[0]
    assert len(station.missing_cycles) == 1
    missing = station.missing_cycles[0]
    assert missing.local_date == "2026-06-02"
    assert missing.window_index == 1
    assert missing.window_label == "12:00-24:00 local"
    # 7 of 8 expected cycles observed
    assert station.health.completeness_pct == 87.5
    kinds = {a.kind for a in report.alerts}
    assert "missing_issuance" in kinds
    assert "abnormal_cadence" in kinds  # 87.5 < 90 default threshold
    missing_alert = next(a for a in report.alerts if a.kind == "missing_issuance")
    assert missing_alert.station_id == "NYC"
    assert "2026-06-02" in missing_alert.observed
    assert missing_alert.recommended_action  # non-empty, actionable


# --- Duplicate captures ------------------------------------------------------


def test_duplicate_capture_reported_and_alerted() -> None:
    captures: list[ForecastCapture] = []
    for day in _days(date(2026, 6, 1), 2):
        captures += _perfect_day_captures("NYC", day, NY)
    captures.append(captures[0])  # exact duplicate (variable, issue_time, valid_start)
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    runs = _regular_runs(min(c.observed_at for c in captures) - timedelta(hours=1), hours=48)
    runs = [r for r in runs if r.started_at <= now]

    report = build_cadence_report(captures, runs, config=CONFIG, timezones=TIMEZONES, now=now)
    station = report.stations[0]
    assert len(station.duplicates) == 1
    assert station.duplicates[0].occurrences == 2
    kinds = [a.kind for a in report.alerts]
    assert "duplicate_issuance" in kinds


# --- Reordered arrival -------------------------------------------------------


def test_reordered_arrival_detected() -> None:
    tz = NY
    early_issue = datetime(2026, 6, 1, 3, 30).replace(tzinfo=tz).astimezone(UTC)
    late_issue = datetime(2026, 6, 1, 15, 30).replace(tzinfo=tz).astimezone(UTC)
    captures = [
        # the later issuance is observed FIRST...
        ForecastCapture("NYC", "tmax_f", late_issue, late_issue, late_issue + timedelta(minutes=5)),
        # ...and the earlier issuance only arrives after it
        ForecastCapture("NYC", "tmax_f", early_issue, early_issue, late_issue + timedelta(hours=1)),
    ]
    now = late_issue + timedelta(hours=2)
    report = build_cadence_report(captures, [], config=CONFIG, timezones=TIMEZONES, now=now)
    station = report.stations[0]
    assert len(station.out_of_order_arrivals) == 1
    arrival = station.out_of_order_arrivals[0]
    assert arrival.issue_time == early_issue.isoformat()
    assert arrival.later_issue_time_observed_earlier == late_issue.isoformat()


# --- Delayed arrival ---------------------------------------------------------


def test_delayed_arrival_detected_beyond_threshold() -> None:
    captures = [
        _capture("NYC", datetime(2026, 6, 1, 3, 30), NY, observed_delay_minutes=5),
        _capture("NYC", datetime(2026, 6, 1, 15, 30), NY, observed_delay_minutes=300),  # 5h > 2h
    ]
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    report = build_cadence_report(captures, [], config=CONFIG, timezones=TIMEZONES, now=now)
    station = report.stations[0]
    assert len(station.delayed_issuances) == 1
    assert station.delayed_issuances[0].delay_hours == 5.0


def test_arrival_at_exact_threshold_is_not_delayed() -> None:
    captures = [
        _capture("NYC", datetime(2026, 6, 1, 3, 30), NY, observed_delay_minutes=120),  # == 2h
    ]
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    report = build_cadence_report(captures, [], config=CONFIG, timezones=TIMEZONES, now=now)
    assert report.stations[0].delayed_issuances == []  # strict >, not >=


# --- DST transitions ---------------------------------------------------------


def test_dst_spring_forward_no_spurious_missing_cycles() -> None:
    """2026-03-08: the 02:00 local hour does not exist; issuances at the
    usual 03:30/15:30 local still populate both windows on every day."""
    captures: list[ForecastCapture] = []
    for day in _days(date(2026, 3, 7), 3):  # spans the transition
        captures += _perfect_day_captures("NYC", day, NY)
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    report = build_cadence_report(captures, [], config=CONFIG, timezones=TIMEZONES, now=now)
    station = report.stations[0]
    assert station.missing_cycles == []
    assert station.health.completeness_pct == 100.0


def test_dst_fall_back_no_spurious_missing_cycles() -> None:
    """2025-11-02: the 01:00 local hour repeats; window assignment stays
    well-defined and no cycle is double-counted or lost."""
    captures: list[ForecastCapture] = []
    for day in _days(date(2025, 11, 1), 3):
        captures += _perfect_day_captures("NYC", day, NY)
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    report = build_cadence_report(captures, [], config=CONFIG, timezones=TIMEZONES, now=now)
    station = report.stations[0]
    assert station.n_issuances == 6
    assert station.missing_cycles == []
    assert station.health.completeness_pct == 100.0


def test_dst_fold_hour_issuances_assign_to_window_zero() -> None:
    """Both instants of the repeated 01:30 local wall clock (EDT and EST)
    are morning-window issuances; the two UTC instants are distinct
    issuances landing in the same cycle -- neither lost nor duplicated."""
    edt_instant = datetime(2025, 11, 2, 5, 30, tzinfo=UTC)  # 01:30 EDT
    est_instant = datetime(2025, 11, 2, 6, 30, tzinfo=UTC)  # 01:30 EST
    assert cycle_of(edt_instant, NY, 2) == (date(2025, 11, 2), 0)
    assert cycle_of(est_instant, NY, 2) == (date(2025, 11, 2), 0)


# --- Station independence ----------------------------------------------------


def test_station_independence_metrics_and_alerts() -> None:
    captures: list[ForecastCapture] = []
    for day in _days(date(2026, 6, 1), 4):
        captures += _perfect_day_captures("NYC", day, NY)
    for i, day in enumerate(_days(date(2026, 6, 1), 4)):
        day_captures = _perfect_day_captures("LAX", day, LA)
        if i == 2:
            day_captures = day_captures[1:]  # LAX misses a morning issuance
        captures += day_captures
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    runs = _regular_runs(min(c.observed_at for c in captures) - timedelta(hours=1), hours=100)
    runs = [r for r in runs if r.started_at <= now]

    report = build_cadence_report(captures, runs, config=CONFIG, timezones=TIMEZONES, now=now)
    assert [s.station_id for s in report.stations] == ["LAX", "NYC"]  # deterministic order
    lax = next(s for s in report.stations if s.station_id == "LAX")
    nyc = next(s for s in report.stations if s.station_id == "NYC")
    assert nyc.missing_cycles == []
    assert nyc.health.completeness_pct == 100.0
    assert len(lax.missing_cycles) == 1
    assert lax.health.completeness_pct == 87.5
    assert all(a.station_id == "LAX" for a in report.alerts)
    assert sum(nyc.issuance_local_hour_histogram) == 8
    assert sum(lax.issuance_local_hour_histogram) == 7


# --- Health metrics ----------------------------------------------------------


def test_gap_distribution_and_max_gap() -> None:
    captures: list[ForecastCapture] = []
    for day in _days(date(2026, 6, 1), 3):
        captures += _perfect_day_captures("NYC", day, NY)
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    report = build_cadence_report(captures, [], config=CONFIG, timezones=TIMEZONES, now=now)
    health = report.stations[0].health
    # alternating gaps: 12h (03:30->15:30) and 12h (15:30->03:30 next day)
    assert health.gap_distribution.count == 5
    assert health.gap_distribution.min_hours == 12.0
    assert health.gap_distribution.max_hours == 12.0
    assert health.max_capture_gap_hours == 12.0


def test_last_collection_and_run_fields() -> None:
    captures = _perfect_day_captures("NYC", date(2026, 6, 1), NY)
    runs = [
        RunAttempt(started_at=datetime(2026, 6, 1, 8, 0, tzinfo=UTC), success=True),
        RunAttempt(started_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC), success=False),
    ]
    now = datetime(2026, 6, 1, 21, 0, tzinfo=UTC)
    report = build_cadence_report(captures, runs, config=CONFIG, timezones=TIMEZONES, now=now)
    health = report.stations[0].health
    assert health.last_capture_at == max(c.observed_at for c in captures).isoformat()
    assert health.last_run_at == datetime(2026, 6, 1, 9, 0, tzinfo=UTC).isoformat()
    assert health.last_successful_run_at == datetime(2026, 6, 1, 8, 0, tzinfo=UTC).isoformat()
    assert health.run_attempts == 2
    # longest outage: max(run-to-run gap 1h, tail gap 9:00 -> 21:00 = 12h)
    assert health.longest_outage_hours == 12.0


# --- Alerts: outage and stalled ----------------------------------------------


def test_collector_outage_alert_on_run_gap() -> None:
    captures = _perfect_day_captures("NYC", date(2026, 6, 1), NY)
    runs = [
        RunAttempt(started_at=datetime(2026, 6, 1, 6, 0, tzinfo=UTC), success=True),
        RunAttempt(started_at=datetime(2026, 6, 1, 16, 0, tzinfo=UTC), success=True),  # 10h gap
    ]
    now = datetime(2026, 6, 1, 16, 30, tzinfo=UTC)
    report = build_cadence_report(captures, runs, config=CONFIG, timezones=TIMEZONES, now=now)
    outage_alerts = [a for a in report.alerts if a.kind == "collector_outage"]
    assert len(outage_alerts) == 1
    assert "10.0h" in outage_alerts[0].observed


def test_collector_outage_alert_when_no_runs_recorded() -> None:
    captures = _perfect_day_captures("NYC", date(2026, 6, 1), NY)
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    report = build_cadence_report(captures, [], config=CONFIG, timezones=TIMEZONES, now=now)
    outage_alerts = [a for a in report.alerts if a.kind == "collector_outage"]
    assert len(outage_alerts) == 1
    assert "no weather-collector run attempts recorded" in outage_alerts[0].observed


def test_stalled_updates_alert_when_newest_issuance_is_old() -> None:
    captures = _perfect_day_captures("NYC", date(2026, 6, 1), NY)
    last_issue = max(c.issue_time for c in captures)
    now = last_issue + timedelta(hours=48)  # > 24h stalled threshold
    runs = _regular_runs(now - timedelta(hours=1), hours=1)
    report = build_cadence_report(captures, runs, config=CONFIG, timezones=TIMEZONES, now=now)
    stalled = [a for a in report.alerts if a.kind == "stalled_updates"]
    assert len(stalled) == 1
    assert "48.0h old" in stalled[0].observed


# --- Incomplete issuances / unknown station / determinism --------------------


def test_incomplete_issuance_counted_when_a_variable_is_missing() -> None:
    captures = [
        _capture("NYC", datetime(2026, 6, 1, 3, 30), NY, variable="tmax_f"),
        _capture("NYC", datetime(2026, 6, 1, 3, 30), NY, variable="tmin_f"),
        _capture("NYC", datetime(2026, 6, 1, 15, 30), NY, variable="tmax_f"),  # tmin missing
    ]
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    report = build_cadence_report(captures, [], config=CONFIG, timezones=TIMEZONES, now=now)
    station = report.stations[0]
    assert station.variables == ["tmax_f", "tmin_f"]
    assert station.incomplete_issuances == 1


def test_unknown_station_falls_back_to_utc_and_is_flagged() -> None:
    captures = [
        ForecastCapture(
            "XXX",
            "tmax_f",
            datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 8, 5, tzinfo=UTC),
        )
    ]
    now = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    report = build_cadence_report(captures, [], config=CONFIG, timezones=TIMEZONES, now=now)
    station = report.stations[0]
    assert station.timezone == "UTC"
    assert station.timezone_assumed_utc is True


def test_report_is_deterministic() -> None:
    captures: list[ForecastCapture] = []
    for day in _days(date(2026, 6, 1), 3):
        captures += _perfect_day_captures("NYC", day, NY)
        captures += _perfect_day_captures("LAX", day, LA)
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    runs = _regular_runs(min(c.observed_at for c in captures) - timedelta(hours=1), hours=80)
    runs = [r for r in runs if r.started_at <= now]
    first = build_cadence_report(captures, runs, config=CONFIG, timezones=TIMEZONES, now=now)
    second = build_cadence_report(
        list(reversed(captures)), list(reversed(runs)), config=CONFIG, timezones=TIMEZONES, now=now
    )
    assert first.to_dict() == second.to_dict()  # input order never matters


def test_empty_input_produces_empty_report() -> None:
    now = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    report = build_cadence_report([], [], config=CONFIG, timezones=TIMEZONES, now=now)
    assert report.stations == []
    assert report.alerts == []


# --- Schedule-agnostic issuance-gap alert (architecture amendment) -----------


def test_normal_quiet_period_below_gap_threshold_no_gap_alert() -> None:
    """A quiet day with only the two scheduled packages ~12h apart is
    normal: well under the 18h threshold, no issuance_gap alert."""
    captures: list[ForecastCapture] = []
    for day in _days(date(2026, 1, 5), 3):  # quiet winter days
        captures += _perfect_day_captures("NYC", day, NY)
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    runs = _regular_runs(min(c.observed_at for c in captures) - timedelta(hours=1), hours=80)
    runs = [r for r in runs if r.started_at <= now]
    report = build_cadence_report(captures, runs, config=CONFIG, timezones=TIMEZONES, now=now)
    assert [a for a in report.alerts if a.kind == "issuance_gap"] == []
    assert report.stations[0].health.max_capture_gap_hours == 12.0


def test_gap_breach_fires_even_when_every_window_is_occupied() -> None:
    """The case the window check structurally cannot catch: all four
    half-day windows occupied, yet 22.5h passes between issuances --
    exactly why the schedule-agnostic alert exists."""
    captures = [
        _capture("NYC", datetime(2026, 6, 1, 0, 30), NY),  # day1 w0
        _capture("NYC", datetime(2026, 6, 1, 12, 30), NY),  # day1 w1
        _capture("NYC", datetime(2026, 6, 2, 11, 0), NY),  # day2 w0 -- 22.5h after previous
        _capture("NYC", datetime(2026, 6, 2, 13, 0), NY),  # day2 w1
    ]
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    runs = _regular_runs(min(c.observed_at for c in captures) - timedelta(hours=1), hours=48)
    runs = [r for r in runs if r.started_at <= now]
    report = build_cadence_report(captures, runs, config=CONFIG, timezones=TIMEZONES, now=now)
    station = report.stations[0]
    assert station.missing_cycles == []  # every window occupied -- floor check silent
    gap_alerts = [a for a in report.alerts if a.kind == "issuance_gap"]
    assert len(gap_alerts) == 1
    assert "22.5h" in gap_alerts[0].observed
    assert gap_alerts[0].station_id == "NYC"
    assert gap_alerts[0].recommended_action


def test_gap_exactly_at_threshold_does_not_fire() -> None:
    """Strictly greater fires; equality does not (platform convention)."""
    captures = [
        _capture("NYC", datetime(2026, 6, 1, 3, 30), NY),  # w0
        _capture("NYC", datetime(2026, 6, 1, 21, 30), NY),  # w1 -- exactly 18.0h later
    ]
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    report = build_cadence_report(captures, [], config=CONFIG, timezones=TIMEZONES, now=now)
    assert report.stations[0].max_issuance_gap_hours == 18.0
    assert [a for a in report.alerts if a.kind == "issuance_gap"] == []


def test_gap_breach_coexists_with_genuine_collector_outage() -> None:
    """A real outage produces both signals: a run-attempt gap (collector
    side) and an issuance gap (data side)."""
    captures = [
        _capture("NYC", datetime(2026, 6, 1, 3, 30), NY),
        _capture("NYC", datetime(2026, 6, 2, 3, 30), NY),  # 24h issuance gap
    ]
    runs = [
        RunAttempt(started_at=datetime(2026, 6, 1, 8, 0, tzinfo=UTC), success=True),
        RunAttempt(started_at=datetime(2026, 6, 2, 8, 0, tzinfo=UTC), success=True),  # 24h gap
    ]
    now = datetime(2026, 6, 2, 8, 30, tzinfo=UTC)
    report = build_cadence_report(captures, runs, config=CONFIG, timezones=TIMEZONES, now=now)
    kinds = {a.kind for a in report.alerts}
    assert "issuance_gap" in kinds
    assert "collector_outage" in kinds


# --- expected_issuances_per_day clamp (architecture amendment) ---------------


def test_expected_per_day_above_two_is_clamped() -> None:
    config = CadenceConfig(expected_issuances_per_day=6)
    assert config.expected_issuances_per_day == 2


def test_expected_per_day_below_one_is_clamped_to_one() -> None:
    config = CadenceConfig(expected_issuances_per_day=0)
    assert config.expected_issuances_per_day == 1


def test_clamped_config_produces_no_spurious_missing_cycles() -> None:
    """The failure mode the clamp prevents: real NWS-like issuance times
    (early morning + mid afternoon) under a requested N=4 would report
    two windows missing every day forever. Clamped to N=2, the same data
    is correctly complete."""
    captures: list[ForecastCapture] = []
    for day in _days(date(2026, 6, 1), 3):
        # realistic clustered updateTimes: ~02:38 and ~14:49 local
        captures.append(_capture("NYC", datetime(day.year, day.month, day.day, 2, 38), NY))
        captures.append(_capture("NYC", datetime(day.year, day.month, day.day, 14, 49), NY))
    now = max(c.observed_at for c in captures) + timedelta(minutes=10)
    runs = _regular_runs(min(c.observed_at for c in captures) - timedelta(hours=1), hours=80)
    runs = [r for r in runs if r.started_at <= now]
    config = CadenceConfig(expected_issuances_per_day=4)  # clamped to 2
    report = build_cadence_report(captures, runs, config=config, timezones=TIMEZONES, now=now)
    station = report.stations[0]
    assert report.config["expected_issuances_per_day"] == 2  # echo shows the effective value
    assert station.missing_cycles == []
    assert station.health.completeness_pct == 100.0
    assert report.alerts == []


def test_generate_alerts_sorted_and_complete_fields() -> None:
    captures: list[ForecastCapture] = []
    for i, day in enumerate(_days(date(2026, 6, 1), 4)):
        day_captures = _perfect_day_captures("NYC", day, NY)
        if i == 1:
            day_captures = day_captures[:1]
        captures += day_captures
    now = max(c.issue_time for c in captures) + timedelta(hours=48)
    report = build_cadence_report(captures, [], config=CONFIG, timezones=TIMEZONES, now=now)
    station = report.stations[0]
    alerts = generate_alerts(station, config=CONFIG, now=now)
    kinds = [a.kind for a in alerts]
    assert kinds == sorted(kinds)  # deterministic ordering
    for alert in alerts:
        assert alert.station_id == "NYC"
        assert alert.expected
        assert alert.observed
        assert alert.recommended_action
