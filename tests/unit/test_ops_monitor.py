import json
from datetime import UTC, datetime

from kalshi_weather.observatory.report import ObservatoryReport
from kalshi_weather.observatory.severity import Finding, Severity
from kalshi_weather.ops.monitor import (
    TRANSITION_NEW_PROBLEM,
    TRANSITION_NO_CHANGE,
    TRANSITION_RECOVERED,
    TRANSITION_SEVERITY_CHANGED,
    MonitorState,
    append_history,
    decide_alert,
    format_alert_message,
    load_state,
    save_state,
    summarize_reason,
    try_acquire_lock,
)

NOW = datetime(2026, 7, 23, 0, 0, 0, tzinfo=UTC)


def _finding(domain: str, check: str, severity: Severity, message: str = "m") -> Finding:
    return Finding(domain=domain, check=check, severity=severity, count=1, message=message)


def _report(*findings: Finding) -> ObservatoryReport:
    return ObservatoryReport(generated_at=NOW.isoformat(), findings=findings)


# --- decide_alert: state machine -------------------------------------------------


def test_first_run_healthy_baseline_does_not_alert() -> None:
    decision = decide_alert(Severity.INFO, None, now=NOW)
    assert decision.should_alert is False
    assert decision.transition == TRANSITION_NO_CHANGE
    assert decision.new_state.severity == Severity.INFO
    assert decision.new_state.last_alert_at is None
    assert decision.prior_severity is None


def test_first_run_already_critical_alerts_immediately() -> None:
    decision = decide_alert(Severity.CRITICAL, None, now=NOW)
    assert decision.should_alert is True
    assert decision.transition == TRANSITION_NEW_PROBLEM
    assert decision.new_state.severity == Severity.CRITICAL
    assert decision.new_state.last_alert_at == NOW.isoformat()


def test_healthy_to_critical_alerts_once() -> None:
    prior = MonitorState(
        severity=Severity.INFO, since="2026-07-22T00:00:00+00:00", last_alert_at=None
    )
    decision = decide_alert(Severity.CRITICAL, prior, now=NOW)
    assert decision.should_alert is True
    assert decision.transition == TRANSITION_NEW_PROBLEM
    assert decision.prior_severity == Severity.INFO
    assert decision.new_state.severity == Severity.CRITICAL


def test_critical_to_critical_never_repeats() -> None:
    prior = MonitorState(
        severity=Severity.CRITICAL,
        since="2026-07-22T00:00:00+00:00",
        last_alert_at="2026-07-22T00:00:00+00:00",
    )
    decision = decide_alert(Severity.CRITICAL, prior, now=NOW)
    assert decision.should_alert is False
    assert decision.transition == TRANSITION_NO_CHANGE
    assert decision.new_state == prior  # state (including `since`) is preserved, not refreshed


def test_critical_to_healthy_sends_recovery() -> None:
    prior = MonitorState(
        severity=Severity.CRITICAL,
        since="2026-07-22T00:00:00+00:00",
        last_alert_at="2026-07-22T00:00:00+00:00",
    )
    decision = decide_alert(Severity.INFO, prior, now=NOW)
    assert decision.should_alert is True
    assert decision.transition == TRANSITION_RECOVERED
    assert decision.prior_severity == Severity.CRITICAL
    assert decision.new_state.severity == Severity.INFO


def test_warning_to_critical_is_a_severity_change_and_alerts() -> None:
    prior = MonitorState(
        severity=Severity.WARNING,
        since="2026-07-22T00:00:00+00:00",
        last_alert_at="2026-07-22T00:00:00+00:00",
    )
    decision = decide_alert(Severity.CRITICAL, prior, now=NOW)
    assert decision.should_alert is True
    assert decision.transition == TRANSITION_SEVERITY_CHANGED


def test_critical_to_warning_is_a_severity_change_and_alerts() -> None:
    prior = MonitorState(
        severity=Severity.CRITICAL,
        since="2026-07-22T00:00:00+00:00",
        last_alert_at="2026-07-22T00:00:00+00:00",
    )
    decision = decide_alert(Severity.WARNING, prior, now=NOW)
    assert decision.should_alert is True
    assert decision.transition == TRANSITION_SEVERITY_CHANGED


def test_warning_to_warning_never_repeats() -> None:
    prior = MonitorState(
        severity=Severity.WARNING,
        since="2026-07-22T00:00:00+00:00",
        last_alert_at="2026-07-22T00:00:00+00:00",
    )
    decision = decide_alert(Severity.WARNING, prior, now=NOW)
    assert decision.should_alert is False
    assert decision.transition == TRANSITION_NO_CHANGE


def test_healthy_to_healthy_never_alerts() -> None:
    prior = MonitorState(
        severity=Severity.INFO, since="2026-07-22T00:00:00+00:00", last_alert_at=None
    )
    decision = decide_alert(Severity.INFO, prior, now=NOW)
    assert decision.should_alert is False
    assert decision.transition == TRANSITION_NO_CHANGE
    assert decision.new_state == prior


def test_decide_alert_is_deterministic() -> None:
    prior = MonitorState(
        severity=Severity.INFO, since="2026-07-22T00:00:00+00:00", last_alert_at=None
    )
    d1 = decide_alert(Severity.CRITICAL, prior, now=NOW)
    d2 = decide_alert(Severity.CRITICAL, prior, now=NOW)
    assert d1 == d2


# --- format_alert_message / summarize_reason -------------------------------------


def test_format_alert_message_includes_findings_for_new_problem() -> None:
    report = _report(
        _finding(
            "platform", "missed_collection_cycles_kalshi", Severity.CRITICAL, "collector dead"
        ),
        _finding("market", "archive_continuity_market", Severity.WARNING, "gap"),
    )
    prior = MonitorState(severity=Severity.INFO, since="x", last_alert_at=None)
    decision = decide_alert(report.status, prior, now=NOW)
    message = format_alert_message(report, decision)
    assert "CRITICAL" in message
    assert "missed_collection_cycles_kalshi" in message
    assert "collector dead" in message


def test_format_alert_message_recovery_omits_findings() -> None:
    report = _report(_finding("platform", "x", Severity.INFO))
    prior = MonitorState(severity=Severity.CRITICAL, since="x", last_alert_at="x")
    decision = decide_alert(report.status, prior, now=NOW)
    message = format_alert_message(report, decision)
    assert "RECOVERED" in message
    assert "no action needed" in message


def test_format_alert_message_truncates_long_finding_lists() -> None:
    findings = tuple(
        _finding("market", f"check_{i}", Severity.CRITICAL, f"m{i}") for i in range(15)
    )
    report = _report(*findings)
    decision = decide_alert(report.status, None, now=NOW)
    message = format_alert_message(report, decision)
    assert "... and 5 more" in message


def test_summarize_reason_no_change() -> None:
    report = _report(_finding("platform", "x", Severity.INFO))
    decision = decide_alert(report.status, MonitorState(Severity.INFO, "x", None), now=NOW)
    assert "unchanged" in summarize_reason(report, decision)


def test_summarize_reason_recovered() -> None:
    report = _report(_finding("platform", "x", Severity.INFO))
    prior = MonitorState(Severity.CRITICAL, "x", "x")
    decision = decide_alert(report.status, prior, now=NOW)
    assert summarize_reason(report, decision) == "all findings returned to INFO"


def test_summarize_reason_lists_bad_findings() -> None:
    report = _report(
        _finding("market", "archive_continuity_market", Severity.CRITICAL),
        _finding("forecast", "cadence_collector_outage", Severity.WARNING),
    )
    decision = decide_alert(report.status, None, now=NOW)
    reason = summarize_reason(report, decision)
    assert "market/archive_continuity_market" in reason
    assert "forecast/cadence_collector_outage" in reason


# --- persistence -------------------------------------------------------------


def test_save_and_load_state_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "state.json"
    state = MonitorState(
        severity=Severity.WARNING,
        since="2026-07-22T00:00:00+00:00",
        last_alert_at="2026-07-22T00:00:00+00:00",
    )
    save_state(path, state)
    loaded = load_state(path)
    assert loaded == state


def test_load_state_missing_file_returns_none(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert load_state(tmp_path / "does-not-exist.json") is None


def test_save_state_creates_parent_dirs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "nested" / "dir" / "state.json"
    save_state(path, MonitorState(Severity.INFO, "x", None))
    assert path.exists()


def test_append_history_is_append_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "history.jsonl"
    append_history(
        path,
        timestamp="t1",
        severity=Severity.CRITICAL,
        transition="new_problem",
        reason="r1",
        delivered=True,
        detail="d1",
    )
    append_history(
        path,
        timestamp="t2",
        severity=Severity.INFO,
        transition="recovered",
        reason="r2",
        delivered=False,
        detail="d2",
    )
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    record1 = json.loads(lines[0])
    record2 = json.loads(lines[1])
    assert record1 == {
        "timestamp": "t1",
        "severity": "critical",
        "transition": "new_problem",
        "reason": "r1",
        "delivered": True,
        "detail": "d1",
    }
    assert record2["severity"] == "info"
    assert record2["delivered"] is False


def test_append_history_creates_parent_dirs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "nested" / "history.jsonl"
    append_history(
        path,
        timestamp="t",
        severity=Severity.INFO,
        transition="no_change",
        reason="r",
        delivered=None,
        detail="d",
    )
    assert path.exists()


# --- lock guard ----------------------------------------------------------------


def test_try_acquire_lock_succeeds_when_unheld(tmp_path) -> None:  # type: ignore[no-untyped-def]
    lock_path = tmp_path / "monitor.lock"
    handle = try_acquire_lock(lock_path)
    assert handle is not None
    handle.close()


def test_try_acquire_lock_fails_when_already_held(tmp_path) -> None:  # type: ignore[no-untyped-def]
    lock_path = tmp_path / "monitor.lock"
    holder = try_acquire_lock(lock_path)
    assert holder is not None
    try:
        second = try_acquire_lock(lock_path)
        assert second is None
    finally:
        holder.close()


def test_try_acquire_lock_available_again_after_release(tmp_path) -> None:  # type: ignore[no-untyped-def]
    lock_path = tmp_path / "monitor.lock"
    first = try_acquire_lock(lock_path)
    assert first is not None
    first.close()
    second = try_acquire_lock(lock_path)
    assert second is not None
    second.close()


def test_deescalation_sequence_alerts_exactly_once() -> None:
    """The production-gap deployment path: a latched CRITICAL de-escalates
    to WARNING when healed history is reclassified -- exactly one alert
    fires, then repeats at WARNING are suppressed, and a later recovery to
    INFO fires exactly one more."""
    prior = MonitorState(
        severity=Severity.CRITICAL,
        since="2026-07-21T00:00:00+00:00",
        last_alert_at="2026-07-21T00:00:00+00:00",
    )
    first = decide_alert(Severity.WARNING, prior, now=NOW)
    assert first.should_alert is True
    assert first.transition == TRANSITION_SEVERITY_CHANGED

    second = decide_alert(Severity.WARNING, first.new_state, now=NOW)
    assert second.should_alert is False
    assert second.transition == TRANSITION_NO_CHANGE

    third = decide_alert(Severity.INFO, second.new_state, now=NOW)
    assert third.should_alert is True
    assert third.transition == TRANSITION_RECOVERED

    fourth = decide_alert(Severity.INFO, third.new_state, now=NOW)
    assert fourth.should_alert is False
