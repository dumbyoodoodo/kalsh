"""Simulated failure-mode validation for the monitoring system (Requirement
5 of the monitoring/alerting task): collector stopped, missing collection,
a continuity gap, and a parser failure -- each built from the *actual*
production detection functions in `observatory/`, not hand-invented
findings, then driven through the real `ops.monitor` state machine to
confirm an alert fires, a recovery notification fires afterward, and the
whole sequence is deterministic on rerun.
"""

from datetime import UTC, datetime, timedelta

from kalshi_weather.observatory import backup_health, continuity, drift, parsing
from kalshi_weather.observatory.report import ObservatoryReport
from kalshi_weather.observatory.severity import Finding, Severity
from kalshi_weather.ops.backup import BackupStatus
from kalshi_weather.ops.forecast_cadence import CadenceAlert
from kalshi_weather.ops.monitor import (
    TRANSITION_NEW_PROBLEM,
    TRANSITION_NO_CHANGE,
    TRANSITION_RECOVERED,
    MonitorState,
    decide_alert,
    format_alert_message,
)

NOW = datetime(2026, 7, 23, 0, 0, 0, tzinfo=UTC)
HEALTHY_REPORT = ObservatoryReport(
    generated_at=NOW.isoformat(),
    findings=(
        Finding(domain="platform", check="ok", severity=Severity.INFO, count=0, message="fine"),
    ),
)


def _run_full_lifecycle(bad_finding: Finding) -> None:
    """Shared assertion sequence: fresh CRITICAL/WARNING alerts once,
    repeats suppress, and recovery to INFO alerts again -- run twice to
    prove determinism."""
    bad_report = ObservatoryReport(generated_at=NOW.isoformat(), findings=(bad_finding,))

    for _ in range(2):  # determinism: identical inputs -> identical decisions
        # 1. Fresh problem detected (no prior state) -> alerts.
        first = decide_alert(bad_report.status, None, now=NOW)
        assert first.should_alert is True
        assert first.transition == TRANSITION_NEW_PROBLEM
        message = format_alert_message(bad_report, first)
        assert bad_finding.check in message
        assert bad_report.status.value.upper() in message

        # 2. Same problem persists on the next run -> suppressed, not repeated.
        second = decide_alert(bad_report.status, first.new_state, now=NOW + timedelta(minutes=15))
        assert second.should_alert is False
        assert second.transition == TRANSITION_NO_CHANGE

        # 3. Recovery: back to INFO -> a recovery notification fires.
        recovered = decide_alert(
            HEALTHY_REPORT.status, second.new_state, now=NOW + timedelta(minutes=30)
        )
        assert recovered.should_alert is True
        assert recovered.transition == TRANSITION_RECOVERED
        recovery_message = format_alert_message(HEALTHY_REPORT, recovered)
        assert "no action needed" in recovery_message

        # 4. Steady healthy state afterward -> no further alerts.
        steady = decide_alert(
            HEALTHY_REPORT.status, recovered.new_state, now=NOW + timedelta(minutes=45)
        )
        assert steady.should_alert is False
        assert steady.transition == TRANSITION_NO_CHANGE


def test_scenario_collector_stopped() -> None:
    """Real `continuity.find_missed_run_cycles` output for a kalshi
    collector whose last run was hours ago, well beyond 3x its interval --
    the exact CRITICAL condition this audit's own live run caught."""
    runs = [
        continuity.RunRecord(collector="kalshi", started_at=NOW - timedelta(hours=6), success=True),
    ]
    missed = continuity.find_missed_run_cycles(
        runs, collector="kalshi", interval_seconds=300, now=NOW
    )
    assert missed  # sanity: the scenario is genuinely a missed-cycle condition
    finding = Finding(
        domain="platform",
        check="missed_collection_cycles_kalshi",
        severity=Severity.CRITICAL if missed else Severity.INFO,
        count=len(missed),
        message=f"{len(missed)} window(s) with no kalshi run recorded well beyond its interval",
    )
    assert finding.severity == Severity.CRITICAL
    _run_full_lifecycle(finding)


def test_scenario_missing_collection_cycle() -> None:
    """Real `drift.adapt_cadence_alert` output for a missing forecast
    issuance window (ops/forecast_cadence.py's own alert kind), folded
    through the same adapter `report.py` uses in production."""
    alert = CadenceAlert(
        station_id="NYC",
        kind="missing_issuance",
        expected="an issuance in the 00:00-12:00 local window",
        observed="no issuance captured",
        recommended_action="inspect weather-collector logs for that window",
    )
    finding = drift.adapt_cadence_alert(alert)
    assert finding.severity == Severity.WARNING
    _run_full_lifecycle(finding)


def test_scenario_archive_continuity_gap() -> None:
    """Real `continuity.find_continuity_gaps` output for a market stream
    with no data for hours -- the data-itself-stopped-arriving check,
    distinct from the collector-process check above."""
    timestamps = [NOW - timedelta(hours=10)]
    gaps = continuity.find_continuity_gaps(timestamps, stream="market", max_gap_hours=2.0, now=NOW)
    assert gaps
    finding = Finding(
        domain="market",
        check="archive_continuity_market",
        severity=Severity.WARNING if gaps else Severity.INFO,
        count=len(gaps),
        message=f"{len(gaps)} gap(s) exceeding 2.0h in the last 14 days",
    )
    assert finding.severity == Severity.WARNING
    _run_full_lifecycle(finding)


def test_scenario_parser_failure() -> None:
    """Real `parsing.summarize_weather_parser_failures` output for a run of
    weather-collector cycles with a persistently high failure rate (at/above
    CRITICAL_FAILURE_RATE), mined from `collector_runs.stats_json` exactly
    as production does."""
    cycles = [
        parsing.ParserCycleFailures(started_at=NOW, invalid_items=3, errors=1) for _ in range(6)
    ] + [parsing.ParserCycleFailures(started_at=NOW, invalid_items=0, errors=0) for _ in range(2)]
    finding = parsing.summarize_weather_parser_failures(cycles)
    assert finding.severity == Severity.CRITICAL  # 6/8 = 75% >= 50% threshold
    _run_full_lifecycle(finding)


def test_scenario_sequence_is_byte_identical_across_two_independent_runs() -> None:
    """End-to-end determinism check spanning all four scenarios: build the
    exact same sequence of decisions twice from scratch and confirm the
    persisted-state trail (what would be written to state.json/history.jsonl)
    is identical."""

    def _run_sequence() -> list[tuple[str, bool, str]]:
        state: MonitorState | None = None
        trail = []
        for finding in (
            Finding(
                domain="platform",
                check="missed_collection_cycles_kalshi",
                severity=Severity.CRITICAL,
                count=1,
                message="m",
            ),
            Finding(
                domain="market",
                check="archive_continuity_market",
                severity=Severity.WARNING,
                count=1,
                message="m",
            ),
            Finding(domain="platform", check="ok", severity=Severity.INFO, count=0, message="m"),
        ):
            report = ObservatoryReport(generated_at=NOW.isoformat(), findings=(finding,))
            decision = decide_alert(report.status, state, now=NOW)
            trail.append(
                (decision.transition, decision.should_alert, decision.new_state.severity.value)
            )
            state = decision.new_state
        return trail

    assert _run_sequence() == _run_sequence()


def test_scenario_backup_failure() -> None:
    """Real `observatory.backup_health.check_backup_command` output for a
    failed backup run (e.g. disk full, pg_dump killed) -- proves backup
    health rides the same exactly-once alert-transition state machine as
    every other observatory domain, with no new alerting code."""
    failed_status = BackupStatus(
        schema=1,
        timestamp=NOW.isoformat(),
        local_outcome="failed",
        local_path=None,
        size_bytes=None,
        entries=None,
        duration_seconds=None,
        error="SIMULATED: disk full during pg_dump (production validation drill)",
        remote_outcome="skipped",
        remote_detail="local backup failed; nothing to copy",
    )
    finding = backup_health.check_backup_command(failed_status)
    assert finding.severity == Severity.CRITICAL
    _run_full_lifecycle(finding)
