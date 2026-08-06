"""Empty-ledger integrity: a completed, instrumented run with zero attempts is
a CRITICAL failure, not 'valid'.

This is the rule that would have caught production run 3290 rather than
reporting ATTEMPT_EVIDENCE_VALID over an empty table.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from kalshi_weather.ingestion import weather_attempts as wa
from kalshi_weather.observatory import attempts as oa
from kalshi_weather.observatory.severity import Severity

NOW = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
T0 = NOW - timedelta(minutes=30)
STATIONS = frozenset({"SEA", "PHX", "MIA"})
TZ = {"SEA": "America/Los_Angeles", "PHX": "America/Phoenix", "MIA": "America/New_York"}
PAIRS = tuple(
    [(s, "CLI_OBSERVATIONS") for s in sorted(STATIONS)]
    + [(s, "GRIDPOINT_FORECAST") for s in sorted(STATIONS)]
)


def row(station: str, product: str) -> oa.AttemptRow:
    return oa.AttemptRow(
        attempt_id=f"{station}-{product}",
        collector_run_id=1,
        environment="production",
        station_code=station,
        product_type=product,
        logical_request_key=f"production|{station}|{product}|2026-08-06",
        stage=str(wa.AttemptStage.NORMALIZED_PERSISTED),
        outcome=str(wa.AttemptOutcome.SUCCEEDED_NEW_DATA),
        source_availability=str(wa.SourceAvailability.AVAILABLE),
        requested_at=T0,
        completed_at=T0 + timedelta(seconds=5),
        target_station_local_date=date(2026, 8, 6),
        raw_payload_id=7,
        parsed_entity_count=2,
        persisted_entity_count=2,
        duplicate_entity_count=0,
    )


def run_ctx(*, instrumented: bool = True, finished: bool = True) -> oa.RunContext:
    return oa.RunContext(
        collector_run_id=1,
        environment="production",
        started_at=T0,
        finished_at=(T0 + timedelta(seconds=20)) if finished else None,
        expected_pairs=PAIRS,
        attempt_instrumented=instrumented,
    )


def summarize(rows, runs):
    return oa.summarize(
        rows,
        runs,
        now=NOW,
        known_stations=STATIONS,
        station_timezones=TZ,
        schema_deployed=True,
        db_revision="0012",
        expected_revision="0012",
    )


def find(fs, name):
    return [f for f in fs if f.check == name]


# --- the 3290 rule -----------------------------------------------------------


def test_completed_instrumented_run_with_zero_attempts_is_critical() -> None:
    fs = summarize([], [run_ctx()])
    hit = find(fs, "completed_weather_run_missing_all_attempt_evidence")
    assert hit and hit[0].severity is Severity.CRITICAL
    assert "inert" in hit[0].message


# 3 synthetic stations x 2 products = 6 expected pairs.
@pytest.mark.parametrize("n", [1, 5])
def test_partial_evidence_is_critical(n: int) -> None:
    rows = [row(s, p) for s, p in PAIRS[:n]]
    fs = summarize(rows, [run_ctx()])
    assert find(fs, "completed_weather_run_partial_attempt_evidence")
    assert any(f.severity is Severity.CRITICAL for f in fs)


def test_full_evidence_is_clean() -> None:
    rows = [row(s, p) for s, p in PAIRS]
    fs = summarize(rows, [run_ctx()])
    assert not [f for f in fs if f.severity is Severity.CRITICAL]


# --- boundary: legacy and active runs are NOT failures ------------------------


def test_uninstrumented_legacy_run_is_not_a_failure() -> None:
    """Run 3290 and every pre-fix run legitimately have no attempts."""
    fs = summarize([], [run_ctx(instrumented=False)])
    assert not [f for f in fs if f.severity is Severity.CRITICAL]
    assert not find(fs, "completed_weather_run_missing_all_attempt_evidence")
    assert not find(fs, "missing_expected_attempt")


def test_active_instrumented_run_is_deferred_not_critical() -> None:
    fs = summarize([], [run_ctx(finished=False)])
    assert not [f for f in fs if f.severity is Severity.CRITICAL]
    assert find(fs, "incomplete_terminal_attempt_set")


def test_boundary_is_a_recorded_marker_not_a_timestamp_guess() -> None:
    assert "attempt_instrumented" in oa.RunContext.__dataclass_fields__
    assert wa.ATTEMPT_INSTRUMENTATION_KEY == "attempt_instrumentation_version"


def test_pre_deployment_schema_absent_still_short_circuits() -> None:
    fs = oa.summarize(
        [],
        [],
        now=NOW,
        known_stations=STATIONS,
        station_timezones=TZ,
        schema_deployed=False,
        db_revision="0011",
        expected_revision="0012",
    )
    assert all(f.severity is Severity.INFO for f in fs)


# --- anchor rejection ---------------------------------------------------------


def test_anchor_rejects_a_run_with_missing_evidence() -> None:
    from kalshi_weather.research import extension_deployment as ed
    from kalshi_weather.research import station_pilot_extension as ext

    base = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
    rec = ed.DeploymentRecord(
        deployment_id="DEP-X",
        extension_registration_id=ext.EXTENSION_REGISTRATION_ID,
        registration_hash=ext.REGISTRATION.registration_hash(),
        deployed_commit="c",
        migration_revision="0012",
        collector_pid=1,
        collector_loaded_commit="c",
        collector_git_dirty=False,
        single_collector_confirmed=True,
        migration_applied_at=base,
        collector_started_at=base,
        validated_cycle_id=3290,
        validated_cycle_started_at=base,
        validated_cycle_completed_at=base + timedelta(minutes=1),
        expected_attempts=14,
        actual_attempts=0,  # the 3290 shape
        reconciliation_status=ed.CheckStatus.FAIL,
        raw_payload_provenance_status=ed.CheckStatus.PASS,
        critical_observatory_findings=1,
        final_condition_confirmed_at=base + timedelta(minutes=2),
        deployment_validated_at=base + timedelta(minutes=2),
        recorded_at=base + timedelta(minutes=3),
        recorded_by="op",
        evidence_references=("run:3290",),
    ).with_hash()
    problems = ed.validate_record(rec)
    assert any("actual_attempts 0 != expected" in p for p in problems)
    assert any("reconciliation_status must be PASS" in p for p in problems)
    assert any("critical_observatory_findings must be 0" in p for p in problems)
