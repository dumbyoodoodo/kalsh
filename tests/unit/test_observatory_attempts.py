"""Observatory attempt-integrity checks and the weather-attempt CLI surface.

Synthetic rows and injected clocks only. No production connection is opened.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_weather.ingestion.weather_attempts import (
    AttemptOutcome as O,
)
from kalshi_weather.ingestion.weather_attempts import (
    AttemptStage as S,
)
from kalshi_weather.ingestion.weather_attempts import (
    SourceAvailability as A,
)
from kalshi_weather.observatory import attempts as oa
from kalshi_weather.observatory.severity import Severity

NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
T0 = NOW - timedelta(minutes=30)
TZ = {"SEA": "America/Los_Angeles", "PHX": "America/Phoenix", "MIA": "America/New_York"}
STATIONS = frozenset(TZ)


def row(**kw: object) -> oa.AttemptRow:
    base: dict[str, object] = {
        "attempt_id": "a1",
        "collector_run_id": 1,
        "environment": "production",
        "station_code": "SEA",
        "product_type": "CLI_OBSERVATIONS",
        "logical_request_key": "1|production|SEA|CLI_OBSERVATIONS|2026-08-12",
        "stage": str(S.NORMALIZED_PERSISTED),
        "outcome": str(O.SUCCEEDED_NEW_DATA),
        "source_availability": str(A.AVAILABLE),
        "requested_at": T0,
        "completed_at": T0 + timedelta(seconds=8),
        "target_station_local_date": date(2026, 8, 12),
        "raw_payload_id": 7,
        "parsed_entity_count": 4,
        "persisted_entity_count": 4,
        "duplicate_entity_count": 0,
    }
    base.update(kw)
    return oa.AttemptRow(**base)  # type: ignore[arg-type]


def checks(findings: list, name: str) -> list:
    return [f for f in findings if f.check == name]


# --- pre-deployment quiet ----------------------------------------------------


def test_legacy_absence_is_info_and_never_pages() -> None:
    f = oa.legacy_absence_finding(schema_deployed=False)
    assert f.severity is Severity.INFO
    assert "expected, not corruption" in f.message


def test_schema_revision_drift_is_info_not_duplicate_critical() -> None:
    f = oa.schema_revision_finding(db_revision="0011", expected_revision="0012")
    assert f.severity is Severity.INFO


def test_summarize_short_circuits_before_deployment() -> None:
    findings = oa.summarize(
        [],
        [],
        now=NOW,
        known_stations=STATIONS,
        station_timezones=TZ,
        schema_deployed=False,
        db_revision="0011",
        expected_revision="0012",
    )
    assert len(findings) == 2
    assert all(f.severity is Severity.INFO for f in findings)


# --- per-row integrity -------------------------------------------------------


def test_clean_rows_produce_no_integrity_findings() -> None:
    assert oa.check_attempt_integrity([row()], now=NOW, known_stations=STATIONS) == []


def test_invalid_station_identity() -> None:
    f = oa.check_attempt_integrity([row(station_code="ZZZ")], now=NOW, known_stations=STATIONS)
    assert checks(f, "invalid_station_identity")[0].severity is Severity.CRITICAL


def test_invalid_product_identity() -> None:
    f = oa.check_attempt_integrity([row(product_type="NOPE")], now=NOW, known_stations=STATIONS)
    assert checks(f, "invalid_product_identity")


def test_missing_collector_run_lineage() -> None:
    f = oa.check_attempt_integrity([row(collector_run_id=None)], now=NOW, known_stations=STATIONS)
    assert checks(f, "missing_collector_run_lineage")


def test_terminal_outcome_missing() -> None:
    f = oa.check_attempt_integrity([row(outcome="WAT")], now=NOW, known_stations=STATIONS)
    assert checks(f, "terminal_outcome_missing")


def test_future_timestamp() -> None:
    f = oa.check_attempt_integrity(
        [row(requested_at=NOW + timedelta(hours=1), completed_at=NOW + timedelta(hours=1))],
        now=NOW,
        known_stations=STATIONS,
    )
    assert checks(f, "future_attempt_timestamp")


@pytest.mark.parametrize(
    "kw",
    [
        {"parsed_entity_count": 1, "persisted_entity_count": 5},
        {"parsed_entity_count": 2, "persisted_entity_count": 2, "duplicate_entity_count": 2},
        {"outcome": str(O.SUCCEEDED_NEW_DATA), "persisted_entity_count": 0},
        {
            "outcome": str(O.SOURCE_UNAVAILABLE),
            "source_availability": str(A.CONFIRMED_UNAVAILABLE),
            "parsed_entity_count": 3,
        },
    ],
)
def test_impossible_count_combinations(kw: dict) -> None:
    f = oa.check_attempt_integrity([row(**kw)], now=NOW, known_stations=STATIONS)
    assert checks(f, "impossible_outcome_count_combination")


def test_conflicting_duplicate_is_critical_identical_is_warning() -> None:
    conflicting = oa.check_attempt_integrity(
        [
            row(attempt_id="a1"),
            row(
                attempt_id="a2",
                outcome=str(O.SUCCEEDED_NO_DATA),
                parsed_entity_count=0,
                persisted_entity_count=0,
            ),
        ],
        now=NOW,
        known_stations=STATIONS,
    )
    dup = checks(conflicting, "duplicate_logical_attempt")
    assert any(f.severity is Severity.CRITICAL for f in dup)

    identical = oa.check_attempt_integrity(
        [row(attempt_id="a1"), row(attempt_id="a2")], now=NOW, known_stations=STATIONS
    )
    dup2 = checks(identical, "duplicate_logical_attempt")
    assert dup2 and all(f.severity is Severity.WARNING for f in dup2)


# --- lineage / environment ---------------------------------------------------


def run_ctx(**kw: object) -> oa.RunContext:
    base: dict[str, object] = {
        "collector_run_id": 1,
        "environment": "production",
        "started_at": T0,
        "finished_at": T0 + timedelta(seconds=30),
        "expected_pairs": (("SEA", "CLI_OBSERVATIONS"),),
    }
    base.update(kw)
    return oa.RunContext(**base)  # type: ignore[arg-type]


def test_foreign_collector_run_lineage() -> None:
    f = oa.check_environment_and_lineage([row(collector_run_id=999)], [run_ctx()])
    assert checks(f, "foreign_collector_run_lineage")[0].severity is Severity.CRITICAL


def test_environment_mismatch() -> None:
    f = oa.check_environment_and_lineage([row(environment="demo")], [run_ctx()])
    assert checks(f, "environment_mismatch")[0].severity is Severity.CRITICAL


# --- target local dates ------------------------------------------------------


def test_target_date_matching_local_day_is_clean() -> None:
    assert oa.check_target_local_dates([row()], station_timezones=TZ) == []


def test_utc_day_mistake_detected() -> None:
    far = oa.check_target_local_dates(
        [row(target_station_local_date=date(2026, 9, 30))], station_timezones=TZ
    )
    assert checks(far, "invalid_target_local_date")


def test_phx_fixed_offset_and_mia_dst_resolve_through_own_zones() -> None:
    """Same instant, different local days: PHX is UTC-7 year round."""
    moment = datetime(2026, 8, 12, 4, 0, tzinfo=UTC)
    phx_ok = row(
        station_code="PHX",
        requested_at=moment,
        completed_at=moment,
        target_station_local_date=date(2026, 8, 11),
    )
    mia_ok = row(
        station_code="MIA",
        requested_at=moment,
        completed_at=moment,
        target_station_local_date=date(2026, 8, 12),
    )
    assert oa.check_target_local_dates([phx_ok, mia_ok], station_timezones=TZ) == []


# --- provenance --------------------------------------------------------------


def test_parser_rejection_without_payload_is_critical() -> None:
    f = oa.check_provenance(
        [
            row(
                outcome=str(O.PARSER_REJECTED),
                raw_payload_id=None,
                parsed_entity_count=0,
                persisted_entity_count=0,
                stage=str(S.PARSED),
            )
        ]
    )
    assert checks(f, "parser_rejection_missing_raw_payload")[0].severity is Severity.CRITICAL


def test_malformed_with_body_requires_payload() -> None:
    f = oa.check_provenance(
        [
            row(
                outcome=str(O.MALFORMED_RESPONSE),
                raw_payload_id=None,
                stage=str(S.RAW_PAYLOAD_PERSISTED),
                parsed_entity_count=0,
                persisted_entity_count=0,
            )
        ]
    )
    assert checks(f, "raw_payload_missing_for_malformed_response")


def test_parsed_entities_require_preserved_body() -> None:
    f = oa.check_provenance([row(stage=str(S.PARSED), raw_payload_id=None)])
    assert checks(f, "raw_payload_missing_for_parser_input")


def test_persistence_failure_requires_provenance() -> None:
    f = oa.check_provenance(
        [
            row(
                outcome=str(O.PERSISTENCE_FAILED),
                raw_payload_id=None,
                persistence_error_type=None,
                persisted_entity_count=0,
            )
        ]
    )
    assert checks(f, "persistence_failure_missing_provenance")


def test_clean_provenance_is_silent() -> None:
    assert oa.check_provenance([row()]) == []


# --- run reconciliation ------------------------------------------------------


def test_active_run_is_not_falsely_flagged() -> None:
    active = run_ctx(finished_at=None)
    f = oa.check_run_reconciliation(active, [], now=NOW)
    assert checks(f, "incomplete_terminal_attempt_set")[0].severity is Severity.INFO
    assert not checks(f, "missing_expected_attempt"), "an active run must not be judged"


def test_run_inside_grace_period_is_not_judged() -> None:
    just_done = run_ctx(finished_at=NOW - timedelta(seconds=10))
    f = oa.check_run_reconciliation(just_done, [], now=NOW)
    assert checks(f, "incomplete_terminal_attempt_set")


def test_completed_run_with_missing_attempt_is_flagged() -> None:
    f = oa.check_run_reconciliation(run_ctx(), [], now=NOW)
    assert checks(f, "missing_expected_attempt")[0].severity is Severity.CRITICAL


def test_completed_run_exact_match_is_clean() -> None:
    assert oa.check_run_reconciliation(run_ctx(), [row()], now=NOW) == []


def test_unexpected_pair_flagged() -> None:
    f = oa.check_run_reconciliation(
        run_ctx(), [row(), row(station_code="PHX", attempt_id="a2")], now=NOW
    )
    assert checks(f, "collector_run_pair_mismatch")


def test_legacy_counter_mismatch_flagged() -> None:
    f = oa.check_run_reconciliation(run_ctx(legacy_invalid_items=3), [row()], now=NOW)
    assert checks(f, "collector_run_counter_mismatch")


# --- operational outcomes ----------------------------------------------------


def test_bounded_operational_failures_are_warnings_not_criticals() -> None:
    f = oa.check_operational_outcomes(
        [
            row(
                outcome=str(O.SOURCE_UNAVAILABLE),
                source_availability=str(A.CONFIRMED_UNAVAILABLE),
                parsed_entity_count=0,
                persisted_entity_count=0,
                raw_payload_id=None,
            ),
            row(
                outcome=str(O.REQUEST_FAILED),
                source_availability=str(A.UNKNOWN),
                parsed_entity_count=0,
                persisted_entity_count=0,
            ),
        ]
    )
    assert all(x.severity is Severity.WARNING for x in f)


def test_successful_no_data_is_info_and_distinct_from_unavailable() -> None:
    f = oa.check_operational_outcomes(
        [row(outcome=str(O.SUCCEEDED_NO_DATA), parsed_entity_count=0, persisted_entity_count=0)]
    )
    assert checks(f, "successful_no_data")[0].severity is Severity.INFO
    assert not checks(f, "bounded_source_unavailable")


# --- CLI surface -------------------------------------------------------------


def test_cli_has_no_mutation_commands() -> None:
    from kalshi_weather.cli import weather_attempts_app

    names = {c.name for c in weather_attempts_app.registered_commands}
    assert {"validate", "summary", "reconcile"} <= names
    for banned in ("update", "delete", "repair", "backfill", "append", "fix"):
        assert banned not in names, banned


@pytest.mark.parametrize(
    "args,expect",
    [
        (
            [
                "--station",
                "ZZZ",
                "--start",
                "2026-08-01T00:00:00+00:00",
                "--end",
                "2026-08-02T00:00:00+00:00",
            ],
            "unknown station",
        ),
        (
            [
                "--station",
                "SEA",
                "--start",
                "2026-08-02T00:00:00+00:00",
                "--end",
                "2026-08-01T00:00:00+00:00",
            ],
            "strictly after",
        ),
        (
            ["--station", "SEA", "--start", "2026-08-01T00:00:00", "--end", "2026-08-02T00:00:00"],
            "timezone-aware",
        ),
    ],
)
def test_summary_rejects_bad_arguments_before_any_query(args: list[str], expect: str) -> None:
    from typer.testing import CliRunner

    from kalshi_weather.cli import app

    result = CliRunner().invoke(app, ["weather", "attempts", "summary", *args])
    assert result.exit_code == 2
    assert expect in result.stdout


def test_cli_statuses_are_stable_constants() -> None:
    from kalshi_weather import cli

    assert cli._ATTEMPT_NOT_DEPLOYED == "ATTEMPT_ATTRIBUTION_NOT_DEPLOYED"
    assert cli._ATTEMPT_VALID == "ATTEMPT_EVIDENCE_VALID"
    assert cli._ATTEMPT_WARNING == "ATTEMPT_EVIDENCE_WARNING"
    assert cli._ATTEMPT_INVALID == "ATTEMPT_EVIDENCE_INVALID"
    assert cli._RECON_PASS == "RECONCILIATION_PASS"
    assert cli._RECON_FAIL == "RECONCILIATION_FAIL"
    assert cli._NOT_DEPLOYED_BANNER == "ATTEMPT ATTRIBUTION NOT DEPLOYED — SCHEMA 0012 REQUIRED"


# --- isolation guards --------------------------------------------------------


def test_observatory_module_defines_no_new_vocabulary() -> None:
    """It must import the shipped enums, not restate them."""
    source = Path(oa.__file__).read_text()
    assert "from kalshi_weather.ingestion.weather_attempts import" in source
    for restated in ("class AttemptOutcome", "class AttemptStage", "class SourceAvailability"):
        assert restated not in source, restated


def test_no_performance_or_experiment_imports() -> None:
    source = Path(oa.__file__).read_text()
    for line in source.splitlines():
        s = line.strip()
        if not (s.startswith("import ") or s.startswith("from ")):
            continue
        for banned in ("experiments", "h0019", "h0020", "paper", "broker", "sqlalchemy"):
            assert banned not in s, f"observatory/attempts imports {banned}: {s}"
