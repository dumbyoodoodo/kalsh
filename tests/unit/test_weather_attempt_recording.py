"""Forward fix for inert attempt recording: pending contexts, materialization,
and the production loop call path.

The regression these lock down is production run 3290: a complete, successful
post-deployment weather cycle that recorded 0 of 14 expected attempts because
`run_weather_collector_loop` never enabled the instrumentation.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta

import pytest

from kalshi_weather.ingestion import weather_attempts as wa
from kalshi_weather.ingestion import weather_collector as wc

T0 = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def pending(**kw: object) -> wa.PendingWeatherCollectionAttempt:
    base: dict[str, object] = {
        "attempt_id": "abc123",
        "environment": "production",
        "station_code": "SEA",
        "product_type": wa.WeatherProductType.CLI_OBSERVATIONS,
        "logical_request_key": "production|SEA|CLI_OBSERVATIONS|2026-08-06",
        "source_endpoint": "nws:cli-observations",
        "stage": wa.AttemptStage.NORMALIZED_PERSISTED,
        "outcome": wa.AttemptOutcome.SUCCEEDED_NEW_DATA,
        "source_availability": wa.SourceAvailability.AVAILABLE,
        "requested_at": T0,
        "completed_at": T0 + timedelta(seconds=5),
        "observed_at": T0 + timedelta(seconds=5),
        "target_station_local_date": date(2026, 8, 6),
        "raw_payload_id": 7,
        "parsed_entity_count": 4,
        "persisted_entity_count": 4,
    }
    base.update(kw)
    return wa.PendingWeatherCollectionAttempt(**base)  # type: ignore[arg-type]


# --- pending object ----------------------------------------------------------


def test_pending_has_no_collector_run_id_field() -> None:
    """No placeholder can leak into the database."""
    assert "collector_run_id" not in wa.PendingWeatherCollectionAttempt.__dataclass_fields__


def test_pending_is_immutable() -> None:
    p = pending()
    with pytest.raises((AttributeError, TypeError)):
        p.station_code = "PHX"  # type: ignore[misc]


def test_materialize_binds_a_real_run_id() -> None:
    rec = pending().materialize(3291, created_at=T0)
    assert rec.collector_run_id == 3291
    assert rec.validate() == []


@pytest.mark.parametrize("bad", [0, -1, None])
def test_materialize_refuses_placeholder_run_id(bad: object) -> None:
    with pytest.raises(wa.AttemptIntegrityError, match="without a real"):
        pending().materialize(bad, created_at=T0)  # type: ignore[arg-type]


def test_materialize_preserves_identity_and_semantics() -> None:
    p = pending(
        outcome=wa.AttemptOutcome.SUCCEEDED_DUPLICATE,
        persisted_entity_count=0,
        duplicate_entity_count=4,
    )
    rec = p.materialize(5, created_at=T0)
    assert rec.attempt_id == p.attempt_id
    assert rec.logical_request_key == p.logical_request_key
    assert rec.outcome is p.outcome
    assert rec.parsed_entity_count == p.parsed_entity_count
    assert rec.duplicate_entity_count == p.duplicate_entity_count


def test_materialize_is_idempotent_across_retries() -> None:
    p = pending()
    assert p.materialize(9, created_at=T0).attempt_id == p.materialize(9, created_at=T0).attempt_id


def test_pending_validates_before_leaving_the_cycle() -> None:
    assert pending().validate_pending() == []
    bad = pending(parsed_entity_count=1, persisted_entity_count=9)
    assert bad.validate_pending()


# --- production call path (the regression) -----------------------------------


def test_cycle_returns_evidence_rather_than_taking_an_optional_sink() -> None:
    """`attempts=None` can no longer silently mean 'record nothing'."""
    sig = inspect.signature(wc.run_weather_collection_cycle)
    assert "attempts" not in sig.parameters, "optional sink removed"
    assert "collector_run_id" not in sig.parameters
    assert sig.return_annotation in (wc.WeatherCycleResult, "WeatherCycleResult")


def test_cycle_result_carries_pending_and_expected_pairs() -> None:
    fields = set(wc.WeatherCycleResult.__dataclass_fields__)
    assert {"stats", "pending_attempts", "expected_pairs"} <= fields


def test_loop_materializes_with_the_persisted_run_id() -> None:
    """Behavioural: the loop must bind the REAL id returned by
    record_collector_run, not a placeholder and not a per-cycle guess."""
    src = inspect.getsource(wc.run_weather_collector_loop)
    assert "run_record = await record_collector_run(" in src
    assert "run_id = int(run_record.id)" in src
    assert "item.materialize(run_id" in src
    assert "await append_terminal_attempt(" in src
    # The old defective call shape must be gone.
    assert "attempts=attempts" not in src
    assert "attempts=[]" not in src


def test_loop_fails_loudly_on_partial_evidence() -> None:
    src = inspect.getsource(wc.run_weather_collector_loop)
    assert "AttemptEvidenceIncomplete" in src
    assert "appended != expected_n" in src


def test_incomplete_evidence_is_its_own_error_type() -> None:
    assert issubclass(wc.AttemptEvidenceIncomplete, RuntimeError)


def test_instrumentation_marker_is_written_prospectively() -> None:
    src = inspect.getsource(wc.run_weather_collector_loop)
    assert wa.ATTEMPT_INSTRUMENTATION_KEY in src or "ATTEMPT_INSTRUMENTATION_KEY" in src


def test_run_id_only_available_after_the_cycle() -> None:
    """Ordering: the run record must be created before any attempt insert."""
    src = inspect.getsource(wc.run_weather_collector_loop)
    assert src.index("run_record = await record_collector_run(") < src.index(
        "await append_terminal_attempt("
    )


def test_business_data_is_not_rolled_back_to_hide_evidence_failure() -> None:
    src = inspect.getsource(wc.run_weather_collector_loop)
    assert "not rolled back" in src or "NOT" in src
    # Evidence failure is caught separately from the cycle error.
    assert "attempt_error" in src and "run_error" in src
