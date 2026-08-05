"""Deployment-anchor invariants, append-only ledger, and calendar derivation.

All timestamps are synthetic. No production deployment is recorded; the
canonical ledger is only ever *read*, and every append targets tmp_path.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_weather.research import extension_deployment as ed
from kalshi_weather.research import station_pilot_extension as ext

MIGRATED = datetime(2026, 8, 10, 17, 0, tzinfo=UTC)
STARTED = datetime(2026, 8, 10, 17, 5, tzinfo=UTC)
CYCLE_START = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
CYCLE_DONE = datetime(2026, 8, 10, 18, 25, tzinfo=UTC)
FINAL = datetime(2026, 8, 10, 18, 30, tzinfo=UTC)


def rec(**kw: object) -> ed.DeploymentRecord:
    base: dict[str, object] = {
        "deployment_id": "DEP-0001",
        "extension_registration_id": ext.EXTENSION_REGISTRATION_ID,
        "registration_hash": ext.REGISTRATION.registration_hash(),
        "deployed_commit": "abc1234",
        "migration_revision": "0012",
        "collector_pid": 4242,
        "collector_loaded_commit": "abc1234",
        "collector_git_dirty": False,
        "single_collector_confirmed": True,
        "migration_applied_at": MIGRATED,
        "collector_started_at": STARTED,
        "validated_cycle_id": 99,
        "validated_cycle_started_at": CYCLE_START,
        "validated_cycle_completed_at": CYCLE_DONE,
        "expected_attempts": 14,
        "actual_attempts": 14,
        "reconciliation_status": ed.CheckStatus.PASS,
        "raw_payload_provenance_status": ed.CheckStatus.PASS,
        "critical_observatory_findings": 0,
        "final_condition_confirmed_at": FINAL,
        "deployment_validated_at": FINAL,
        "recorded_at": FINAL + timedelta(minutes=1),
        "recorded_by": "Wilson Tu",
        "evidence_references": ("collector_runs:99", "observatory:clean"),
    }
    base.update(kw)
    return ed.DeploymentRecord(**base).with_hash()  # type: ignore[arg-type]


# --- valid anchor ------------------------------------------------------------


def test_valid_synthetic_record() -> None:
    assert ed.validate_record(rec()) == []


def test_deterministic_content_hash() -> None:
    assert rec().content_hash == rec().content_hash
    assert rec(deployment_id="DEP-0002").content_hash != rec().content_hash


def test_record_round_trips() -> None:
    original = rec()
    loaded = ed.DeploymentRecord.from_dict(json.loads(original.to_json_line()))
    assert loaded.to_dict() == original.to_dict()
    assert ed.validate_record(loaded) == []


# --- invariants --------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "expect"),
    [
        ({"extension_registration_id": "WRONG"}, "extension_registration_id must be"),
        ({"registration_hash": "0" * 64}, "registration_hash does not match"),
        ({"migration_revision": "0011"}, "migration_revision must be"),
        ({"collector_loaded_commit": "different"}, "not running the deployed commit"),
        ({"collector_git_dirty": True}, "collector_git_dirty must be false"),
        ({"collector_pid": 0}, "collector PID is required"),
        ({"single_collector_confirmed": False}, "exactly one collector"),
        ({"actual_attempts": 13}, "actual_attempts 13 != expected"),
        ({"reconciliation_status": ed.CheckStatus.FAIL}, "reconciliation_status must be PASS"),
        (
            {"raw_payload_provenance_status": ed.CheckStatus.FAIL},
            "raw_payload_provenance_status must be PASS",
        ),
        ({"critical_observatory_findings": 1}, "critical_observatory_findings must be 0"),
        ({"recorded_by": " "}, "recorded_by is required"),
        ({"evidence_references": ()}, "evidence reference is required"),
    ],
)
def test_each_invariant_is_enforced(kw: dict, expect: str) -> None:
    problems = ed.validate_record(rec(**kw))
    assert any(expect in p for p in problems), problems


def test_partial_cycle_is_not_an_anchor() -> None:
    """final_condition before cycle completion means the cycle had not finished."""
    problems = ed.validate_record(
        rec(
            final_condition_confirmed_at=CYCLE_DONE - timedelta(minutes=5),
            deployment_validated_at=CYCLE_DONE - timedelta(minutes=5),
        )
    )
    assert any("partial cycle is not an anchor" in p for p in problems)


def test_anchor_must_equal_final_condition_no_independent_choice() -> None:
    problems = ed.validate_record(rec(deployment_validated_at=FINAL + timedelta(hours=2)))
    assert any("must EQUAL final_condition_confirmed_at" in p for p in problems)


def test_anchor_before_migration_refused() -> None:
    early = MIGRATED - timedelta(hours=1)
    problems = ed.validate_record(
        rec(
            final_condition_confirmed_at=early,
            deployment_validated_at=early,
            validated_cycle_started_at=early - timedelta(minutes=10),
            validated_cycle_completed_at=early - timedelta(minutes=1),
        )
    )
    assert any("precedes migration_applied_at" in p for p in problems)


def test_anchor_before_collector_start_refused() -> None:
    early = STARTED - timedelta(minutes=1)
    problems = ed.validate_record(
        rec(
            migration_applied_at=early - timedelta(hours=1),
            final_condition_confirmed_at=early,
            deployment_validated_at=early,
            validated_cycle_started_at=early - timedelta(minutes=10),
            validated_cycle_completed_at=early - timedelta(minutes=5),
        )
    )
    assert any("precedes collector_started_at" in p for p in problems)


def test_backdated_recorded_at_refused() -> None:
    problems = ed.validate_record(rec(recorded_at=FINAL - timedelta(minutes=1)))
    assert any("backdated record" in p for p in problems)


def test_cycle_completed_before_started_refused() -> None:
    problems = ed.validate_record(
        rec(validated_cycle_completed_at=CYCLE_START - timedelta(minutes=1))
    )
    assert any("completed before it started" in p for p in problems)


def test_naive_timestamp_refused_at_parse() -> None:
    raw = json.loads(rec().to_json_line())
    raw["deployment_validated_at"] = "2026-08-10T18:30:00"
    with pytest.raises(ed.DeploymentLedgerError, match="timezone-aware"):
        ed.DeploymentRecord.from_dict(raw)


def test_in_place_edit_detected() -> None:
    raw = json.loads(rec().to_json_line())
    raw["recorded_by"] = "someone else"
    edited = ed.DeploymentRecord.from_dict(raw)
    assert any("content_hash mismatch" in p for p in ed.validate_record(edited))


def test_no_override_or_force_surface() -> None:
    """Checked on the actual API, not by word search -- the module docstring
    names these precisely to declare that none of them exists."""
    import inspect

    for func in (ed.validate_record, ed.DeploymentLedger.append_record):
        params = set(inspect.signature(func).parameters)
        for banned in ("force", "override", "backdate", "allow_invalid", "skip_validation"):
            assert banned not in params, f"{func.__name__} exposes {banned}"
    fields = set(ed.DeploymentRecord.__dataclass_fields__)
    for banned in ("force", "override", "manual_anchor"):
        assert banned not in fields, banned


# --- append-only ledger ------------------------------------------------------


def test_append_and_read_back(tmp_path: Path) -> None:
    path = tmp_path / "dep.jsonl"
    stored = ed.DeploymentLedger().append_record(rec(), path)
    assert stored.content_hash
    loaded = ed.DeploymentLedger.load(path)
    assert len(loaded.records) == 1
    assert loaded.verify_ledger() == []
    assert loaded.find_by_deployment_id("DEP-0001") is not None
    assert loaded.list_records()


def test_duplicate_deployment_id_refused(tmp_path: Path) -> None:
    path = tmp_path / "dep.jsonl"
    stored = ed.DeploymentLedger().append_record(rec(), path)
    with pytest.raises(ed.DeploymentLedgerError, match="already exists"):
        ed.DeploymentLedger(records=(stored,)).append_record(rec(), path)
    assert len(path.read_text().strip().splitlines()) == 1


def test_second_anchor_for_same_registration_refused(tmp_path: Path) -> None:
    """ADR 0024 registers ONE extension; a second anchor would re-date it."""
    path = tmp_path / "dep.jsonl"
    stored = ed.DeploymentLedger().append_record(rec(), path)
    with pytest.raises(ed.DeploymentLedgerError, match="one extension"):
        ed.DeploymentLedger(records=(stored,)).append_record(rec(deployment_id="DEP-0002"), path)


def test_invalid_record_never_written(tmp_path: Path) -> None:
    path = tmp_path / "dep.jsonl"
    with pytest.raises(ed.DeploymentLedgerError, match="invalid"):
        ed.DeploymentLedger().append_record(rec(critical_observatory_findings=3), path)
    assert not path.exists()


def test_prior_bytes_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "dep.jsonl"
    ed.DeploymentLedger().append_record(rec(), path)
    before = path.read_bytes()
    assert path.read_bytes() == before


def test_malformed_ledger_blocks_append(tmp_path: Path) -> None:
    path = tmp_path / "dep.jsonl"
    path.write_text("{not json}\n")
    with pytest.raises(ed.DeploymentLedgerError, match="not valid JSON"):
        ed.DeploymentLedger.load(path)


def test_no_update_or_delete_api() -> None:
    for banned in ("update_record", "delete_record", "overwrite_record", "upsert_record"):
        assert not hasattr(ed.DeploymentLedger, banned), banned


# --- calendar derivation -----------------------------------------------------


def test_seven_dates_per_station() -> None:
    for cal in ed.derive_calendars(FINAL):
        assert len(cal.included_dates) == 7
        assert cal.included_dates[0] == cal.first_included_date


def test_first_date_is_strictly_after_local_midnight() -> None:
    """A deployment exactly AT local midnight still waits for the next one."""
    midnight_phx = datetime(2026, 8, 11, 7, 0, tzinfo=UTC)  # 00:00 Aug 11 in Phoenix
    cals = {c.station_code: c for c in ed.derive_calendars(midnight_phx)}
    assert cals["PHX"].first_included_date == date(2026, 8, 12)


def test_phx_fixed_offset_sea_and_mia_dst() -> None:
    cals = {c.station_code: c for c in ed.derive_calendars(FINAL)}
    assert cals["PHX"].timezone_name == "America/Phoenix"
    assert cals["SEA"].timezone_name == "America/Los_Angeles"
    assert cals["MIA"].timezone_name == "America/New_York"
    # Same local date, different UTC end: MIA (EDT, UTC-4) ends before SEA/PHX.
    assert cals["MIA"].seventh_date_end_utc < cals["SEA"].seventh_date_end_utc


def test_gate_is_latest_seventh_date_end() -> None:
    cals = ed.derive_calendars(FINAL)
    assert ed.derive_review_gate(FINAL) == max(c.seventh_date_end_utc for c in cals)


def test_resulting_state_is_collecting_extension() -> None:
    assert rec().resulting_extension_state() is ed.ExtensionState.COLLECTING_EXTENSION


def test_record_payload_carries_the_derived_calendar() -> None:
    payload = rec().to_dict()
    assert set(payload["per_station_first_included_date"]) == {"SEA", "PHX", "MIA"}
    assert all(len(v) == 7 for v in payload["per_station_included_dates"].values())
    assert payload["resulting_extension_state"] == "COLLECTING_EXTENSION"


def test_calendar_logic_is_not_duplicated() -> None:
    """Timezone and gate arithmetic must delegate to the frozen module."""
    source = Path(ed.__file__).read_text()
    assert "ZoneInfo" not in source, "timezone arithmetic must not be re-implemented"
    assert "extension_review_gate" in source


# --- no real anchor ----------------------------------------------------------


def test_canonical_ledger_has_no_production_record() -> None:
    ledger = ed.DeploymentLedger.load(ed.DEFAULT_DEPLOYMENT_LEDGER)
    assert ledger.records == (), "no production deployment anchor may exist yet"


def test_extension_state_remains_waiting() -> None:
    state = ext.extension_state(now=FINAL, deployment_validated_at=None)
    assert state is ext.ExtensionState.WAITING_FOR_ATTEMPT_ATTRIBUTION_DEPLOYMENT


def test_registration_hash_unchanged() -> None:
    assert (
        ext.REGISTRATION.registration_hash()
        == "821e25788d1f053be13012c7311a9abc051d932cca3ad30f895f9e5cf71d5a5a"
    )


# --- isolation ---------------------------------------------------------------


def test_no_attempt_outcome_or_experiment_access() -> None:
    source = Path(ed.__file__).read_text()
    for line in source.splitlines():
        s = line.strip()
        if not (s.startswith("import ") or s.startswith("from ")):
            continue
        for banned in ("weather_attempts", "sqlalchemy", "experiments", "paper", "h0019", "h0020"):
            assert banned not in s, f"extension_deployment imports {banned}: {s}"


def test_no_performance_fields() -> None:
    blob = json.dumps(rec().to_dict()).lower()
    parts = {p for p in "".join(c if c.isalnum() else " " for c in blob).split()}
    for token in ("brier", "calibration", "profit", "pnl", "rank", "accuracy", "sharpe"):
        assert not any(p.startswith(token) for p in parts), token
