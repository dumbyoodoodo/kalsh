"""Permanent collection-gap ledger: schema, evidence, append-only, isolation.

All fixtures are synthetic except the explicit production-ledger validation
test, which only *reads* the version-controlled JSONL file. Nothing here opens
a database, imports an experiment or paper module, or makes a network call --
asserted by the isolation guards at the end.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kalshi_weather.data_quality import gap_ledger as gl

THIS_FILE = Path(__file__)
T0 = datetime(2026, 7, 26, 4, 19, 26, tzinfo=UTC)


def ev(kind: gl.EvidenceKind = gl.EvidenceKind.COLLECTOR_RUN, ref: str = "run:123") -> gl.Evidence:
    return gl.Evidence(kind=kind, reference=ref, detail="synthetic")


def rec(**over: object) -> gl.GapRecord:
    """A valid confirmed host gap, overridable per test."""
    base: dict[str, object] = {
        "gap_id": "GAP-TEST-0001",
        "created_at": T0,
        "created_by": "tester",
        "classification": gl.Classification.HOST_UNAVAILABLE,
        "gap_kind": gl.GapKind.HOST_GAP,
        "subsystems": (gl.Subsystem.WEATHER_OBSERVATIONS,),
        "environment": "production",
        "start_at": T0,
        "end_at": T0 + timedelta(hours=14),
        "source_availability": gl.SourceAvailability.UNKNOWN,
        "recovery_state": gl.RecoveryState.NOT_RECOVERABLE,
        "recoverability": gl.Recoverability.NOT_RECOVERABLE,
        "research_treatment": gl.ResearchTreatment.EXCLUDE_FOR_MISSING_COLLECTION,
        "confidence": gl.Confidence.CONFIRMED,
        "reason_code": "host_unavailable",
        "human_note": "synthetic",
        "evidence": (ev(),),
    }
    base.update(over)
    return gl.GapRecord(**base).with_hash()  # type: ignore[arg-type]


def ledger(*records: gl.GapRecord) -> gl.GapLedger:
    return gl.GapLedger(records=records)


# --- valid records across the vocabulary ------------------------------------


def test_valid_confirmed_host_gap() -> None:
    assert gl.validate_record(rec()) == []


def test_valid_upstream_kalshi_outage() -> None:
    record = rec(
        gap_id="GAP-K",
        classification=gl.Classification.UPSTREAM_KALSHI_OUTAGE,
        gap_kind=gl.GapKind.UPSTREAM_OUTAGE,
        subsystems=(gl.Subsystem.KALSHI_MARKET_SNAPSHOTS, gl.Subsystem.KALSHI_ORDER_BOOKS),
        reason_code="kalshi_connect_error_storm",
    )
    assert gl.validate_record(record) == []


def test_subsystem_specific_outage_does_not_span_streams() -> None:
    """A Kalshi outage must not answer a weather query."""
    kalshi = rec(
        gap_id="GAP-K",
        classification=gl.Classification.UPSTREAM_KALSHI_OUTAGE,
        gap_kind=gl.GapKind.UPSTREAM_OUTAGE,
        subsystems=(gl.Subsystem.KALSHI_MARKET_SNAPSHOTS,),
    )
    lg = ledger(kalshi)
    moment = T0 + timedelta(hours=1)
    assert lg.overlapping(moment, subsystem=gl.Subsystem.KALSHI_MARKET_SNAPSHOTS)
    assert lg.overlapping(moment, subsystem=gl.Subsystem.WEATHER_OBSERVATIONS) == ()
    assert lg.contemporaneous_use_allowed(moment, gl.Subsystem.WEATHER_OBSERVATIONS) is True


def test_valid_source_not_issued_gap() -> None:
    record = rec(
        gap_id="GAP-S",
        classification=gl.Classification.SOURCE_PRODUCT_NOT_ISSUED,
        gap_kind=gl.GapKind.SOURCE_GAP,
        source_availability=gl.SourceAvailability.CONFIRMED_NOT_ISSUED,
        recovery_state=gl.RecoveryState.NOT_APPLICABLE,
        recoverability=gl.Recoverability.NOT_APPLICABLE,
        research_treatment=gl.ResearchTreatment.EXCLUDE_FOR_SOURCE_UNAVAILABILITY,
        evidence=(ev(gl.EvidenceKind.SOURCE_ARCHIVE, "iem:CLINYC:2026-07-02"),),
    )
    assert gl.validate_record(record) == []


def test_collector_missed_an_issued_product_is_a_collection_gap() -> None:
    record = rec(
        gap_id="GAP-C",
        classification=gl.Classification.PARSER_FAILURE,
        gap_kind=gl.GapKind.COLLECTION_GAP,
        source_availability=gl.SourceAvailability.CONFIRMED_ISSUED,
        recovery_state=gl.RecoveryState.FULLY_RECOVERED,
        recoverability=gl.Recoverability.RECOVERABLE,
        research_treatment=gl.ResearchTreatment.RECOVERED_NOT_CONTEMPORANEOUS,
        evidence=(ev(), ev(gl.EvidenceKind.OBSERVATION_ROW, "obs:26813")),
    )
    assert gl.validate_record(record) == []


def test_valid_parser_and_persistence_failures() -> None:
    for classification in (gl.Classification.PARSER_FAILURE, gl.Classification.PERSISTENCE_FAILURE):
        record = rec(
            gap_id=f"GAP-{classification}",
            classification=classification,
            gap_kind=gl.GapKind.COLLECTION_GAP,
            source_availability=gl.SourceAvailability.CONFIRMED_ISSUED,
            evidence=(ev(gl.EvidenceKind.POLL_ATTEMPT, "poll:malformed_payload"),),
        )
        assert gl.validate_record(record) == []


def test_fully_recovered_historical_gap_and_treatment() -> None:
    record = rec(
        gap_id="GAP-R",
        classification=gl.Classification.PARSER_FAILURE,
        gap_kind=gl.GapKind.COLLECTION_GAP,
        source_availability=gl.SourceAvailability.CONFIRMED_ISSUED,
        recovery_state=gl.RecoveryState.FULLY_RECOVERED,
        recoverability=gl.Recoverability.RECOVERABLE,
        research_treatment=gl.ResearchTreatment.RECOVERED_NOT_CONTEMPORANEOUS,
        evidence=(ev(), ev(gl.EvidenceKind.RAW_PAYLOAD, "raw:752042")),
    )
    assert gl.validate_record(record) == []
    lg = ledger(record)
    moment = record.start_at + timedelta(hours=1)
    # Recovered later => valid for reconstruction, NOT for point-in-time features.
    assert lg.contemporaneous_use_allowed(moment, gl.Subsystem.WEATHER_OBSERVATIONS) is False


def test_permanently_unrecoverable_gap() -> None:
    record = rec(gap_id="GAP-U", recovery_state=gl.RecoveryState.NOT_RECOVERABLE)
    assert gl.validate_record(record) == []
    lg = ledger(record)
    assert (
        lg.contemporaneous_use_allowed(
            record.start_at + timedelta(hours=1), gl.Subsystem.WEATHER_OBSERVATIONS
        )
        is False
    )


def test_ambiguous_legacy_gap_cannot_authorize_exclusion() -> None:
    record = rec(
        gap_id="GAP-L",
        classification=gl.Classification.LEGACY_UNKNOWN,
        gap_kind=gl.GapKind.AMBIGUOUS_LEGACY,
        recovery_state=gl.RecoveryState.UNKNOWN,
        recoverability=gl.Recoverability.UNKNOWN,
        research_treatment=gl.ResearchTreatment.INSUFFICIENT_EVIDENCE,
        confidence=gl.Confidence.LOW,
        exclusion_eligible=False,
        evidence=(ev(gl.EvidenceKind.OBSERVATORY_FINDING, "obs:legacy"),),
    )
    assert gl.validate_record(record) == []
    bad = rec(
        gap_id="GAP-L2",
        classification=gl.Classification.LEGACY_UNKNOWN,
        gap_kind=gl.GapKind.AMBIGUOUS_LEGACY,
        confidence=gl.Confidence.LOW,
        exclusion_eligible=True,
        evidence=(ev(),),
    )
    assert any("LOW-confidence" in p for p in gl.validate_record(bad))


def test_partial_current_day_record() -> None:
    record = rec(
        gap_id="GAP-P",
        classification=gl.Classification.PARTIAL_CURRENT_DAY,
        gap_kind=gl.GapKind.COLLECTION_GAP,
        recovery_state=gl.RecoveryState.NOT_APPLICABLE,
        recoverability=gl.Recoverability.NOT_APPLICABLE,
        research_treatment=gl.ResearchTreatment.INCLUDE_WITH_ANNOTATION,
    )
    assert gl.validate_record(record) == []


# --- validation failures -----------------------------------------------------


def test_invalid_interval_rejected() -> None:
    bad = rec(start_at=T0 + timedelta(hours=5), end_at=T0)
    assert any("start_at must precede end_at" in p for p in gl.validate_record(bad))


def test_instantaneous_event_may_share_start_and_end() -> None:
    ok = rec(start_at=T0, end_at=T0, instantaneous=True)
    assert gl.validate_record(ok) == []


def test_missing_evidence_rejected() -> None:
    bad = rec(evidence=())
    assert any("evidence reference is required" in p for p in gl.validate_record(bad))


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(gl.GapLedgerError, match="timezone-aware"):
        gl.GapRecord.from_dict(
            {**json.loads(rec().to_json_line()), "start_at": "2026-07-26T04:19:26"}
        )


def test_missing_environment_rejected() -> None:
    assert any("environment is required" in p for p in gl.validate_record(rec(environment=" ")))


def test_missing_subsystem_rejected() -> None:
    assert any("subsystem is required" in p for p in gl.validate_record(rec(subsystems=())))


def test_source_gap_requires_source_evidence() -> None:
    bad = rec(
        gap_kind=gl.GapKind.SOURCE_GAP,
        source_availability=gl.SourceAvailability.CONFIRMED_NOT_ISSUED,
        evidence=(ev(gl.EvidenceKind.COLLECTOR_RUN),),
    )
    assert any("SOURCE_GAP requires source evidence" in p for p in gl.validate_record(bad))


def test_source_gap_requires_confirmed_not_issued() -> None:
    bad = rec(
        gap_kind=gl.GapKind.SOURCE_GAP,
        source_availability=gl.SourceAvailability.UNKNOWN,
        evidence=(ev(gl.EvidenceKind.SOURCE_ARCHIVE, "iem:x"),),
    )
    assert any("CONFIRMED_NOT_ISSUED" in p for p in gl.validate_record(bad))


def test_collection_gap_requires_collector_evidence() -> None:
    bad = rec(
        gap_kind=gl.GapKind.COLLECTION_GAP,
        evidence=(ev(gl.EvidenceKind.SOURCE_ARCHIVE, "iem:x"),),
    )
    assert any("requires collector-side evidence" in p for p in gl.validate_record(bad))


@pytest.mark.parametrize(
    "kind", [gl.EvidenceKind.PARSER_EXCEPTION, gl.EvidenceKind.PERSISTENCE_EXCEPTION]
)
def test_parser_and_persistence_exceptions_are_collector_evidence(kind: gl.EvidenceKind) -> None:
    """A payload the platform received and then failed to handle is
    collector-side proof, not source-side."""
    record = rec(gap_kind=gl.GapKind.COLLECTION_GAP, evidence=(ev(kind, "exc:boom"),))
    assert gl.validate_record(record) == []


def test_recovered_record_requires_recovery_evidence() -> None:
    bad = rec(
        recovery_state=gl.RecoveryState.FULLY_RECOVERED,
        recoverability=gl.Recoverability.RECOVERABLE,
        research_treatment=gl.ResearchTreatment.RECOVERED_NOT_CONTEMPORANEOUS,
        evidence=(ev(gl.EvidenceKind.COLLECTOR_RUN),),
    )
    assert any("requires recovery evidence" in p for p in gl.validate_record(bad))


def test_source_not_issued_cannot_be_recovered() -> None:
    """If data was later obtained, the source did issue it -- the classification
    was wrong, and the ledger must say so rather than absorb the contradiction."""
    bad = rec(
        classification=gl.Classification.SOURCE_PRODUCT_NOT_ISSUED,
        recovery_state=gl.RecoveryState.FULLY_RECOVERED,
        recoverability=gl.Recoverability.RECOVERABLE,
        research_treatment=gl.ResearchTreatment.RECOVERED_USE_AS_OBSERVED_AT,
        evidence=(ev(gl.EvidenceKind.OBSERVATION_ROW, "obs:1"),),
    )
    assert any("cannot be recovered" in p for p in gl.validate_record(bad))


def test_recovered_state_contradicting_recoverability_rejected() -> None:
    bad = rec(
        recovery_state=gl.RecoveryState.FULLY_RECOVERED,
        recoverability=gl.Recoverability.NOT_RECOVERABLE,
        research_treatment=gl.ResearchTreatment.RECOVERED_USE_AS_OBSERVED_AT,
        evidence=(ev(gl.EvidenceKind.OBSERVATION_ROW, "obs:1"),),
    )
    assert any("contradicts recoverability" in p for p in gl.validate_record(bad))


# --- append-only integrity ---------------------------------------------------


def test_duplicate_gap_id_rejected() -> None:
    assert any("duplicate gap_id" in p for p in ledger(rec(), rec()).validate())


def test_superseding_amendment_is_valid_and_hides_the_original() -> None:
    first = rec(gap_id="GAP-1")
    amended = rec(
        gap_id="GAP-2",
        supersedes_gap_id="GAP-1",
        classification=gl.Classification.COLLECTOR_STOPPED,
    )
    lg = ledger(first, amended)
    assert lg.validate() == []
    assert [r.gap_id for r in lg.active_records()] == ["GAP-2"]


def test_supersede_unknown_target_rejected() -> None:
    lg = ledger(rec(gap_id="GAP-2", supersedes_gap_id="GAP-NOPE"))
    assert any("supersedes unknown gap_id" in p for p in lg.validate())


def test_supersede_cycle_rejected() -> None:
    a = rec(gap_id="GAP-A", supersedes_gap_id="GAP-B")
    b = rec(
        gap_id="GAP-B",
        supersedes_gap_id="GAP-A",
        classification=gl.Classification.COLLECTOR_STOPPED,
    )
    assert any("supersede cycle" in p for p in ledger(a, b).validate())


def test_self_supersede_rejected() -> None:
    lg = ledger(rec(gap_id="GAP-X", supersedes_gap_id="GAP-X"))
    assert any("cannot supersede itself" in p for p in lg.validate())


def test_unlinked_duplicate_rejected_but_linked_pair_allowed() -> None:
    a = rec(gap_id="GAP-A", scope="NYC")
    b = rec(gap_id="GAP-B", scope="NYC")
    assert any("unlinked duplicate" in p for p in ledger(a, b).validate())
    linked = rec(gap_id="GAP-B", scope="NYC", related_gap_ids=("GAP-A",))
    assert ledger(a, linked).validate() == []


def test_deterministic_hashing() -> None:
    a, b = rec(), rec()
    assert a.content_hash == b.content_hash
    assert a.content_hash == a.compute_hash()
    assert rec(reason_code="different").content_hash != a.content_hash


def test_in_place_edit_is_detected(tmp_path: Path) -> None:
    """The whole point of the hash: an edited earlier line must not validate."""
    path = tmp_path / "ledger.jsonl"
    gl.GapLedger().append(rec(), path)
    raw = json.loads(path.read_text().strip())
    raw["human_note"] = "silently changed after the fact"
    path.write_text(json.dumps(raw) + "\n")
    problems = gl.GapLedger.load(path).validate()
    assert any("content_hash mismatch" in p for p in problems)


def test_append_writes_one_line_and_never_rewrites(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    lg = gl.GapLedger()
    lg = gl.GapLedger(records=(lg.append(rec(gap_id="GAP-1"), path),))
    first_line = path.read_text()
    lg.append(rec(gap_id="GAP-2", scope="other"), path)
    after = path.read_text()
    assert after.startswith(first_line), "the first line must be byte-identical after appending"
    assert len(after.strip().splitlines()) == 2


def test_append_refuses_duplicate_id(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    lg = gl.GapLedger()
    stored = lg.append(rec(gap_id="GAP-1"), path)
    with pytest.raises(gl.GapLedgerError, match="already exists"):
        gl.GapLedger(records=(stored,)).append(rec(gap_id="GAP-1"), path)
    assert len(path.read_text().strip().splitlines()) == 1


def test_append_refuses_invalid_record(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    with pytest.raises(gl.GapLedgerError, match="invalid"):
        gl.GapLedger().append(rec(evidence=()), path)
    assert not path.exists()


def test_jsonl_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    original = rec()
    gl.GapLedger().append(original, path)
    loaded = gl.GapLedger.load(path)
    assert len(loaded.records) == 1
    assert loaded.records[0].to_dict() == original.to_dict()
    assert loaded.validate() == []


def test_malformed_jsonl_line_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text("{not json}\n")
    with pytest.raises(gl.GapLedgerError, match="not valid JSON"):
        gl.GapLedger.load(path)


# --- outcome isolation -------------------------------------------------------


@pytest.mark.parametrize(
    "bad_field",
    [
        "brier_improvement",
        "metric_impact",
        "pnl_effect",
        "station_ranking",
        "hypothesis_support",
        "preferred_exclusion",
        "model_improvement",
        "accuracy_delta",
        "profit_after_exclusion",
        "verdict",
    ],
)
def test_outcome_fields_are_rejected(bad_field: str) -> None:
    raw = json.loads(rec().to_json_line())
    raw[bad_field] = 0.42
    with pytest.raises(gl.ForbiddenFieldError, match="forbidden token"):
        gl.GapRecord.from_dict(raw)


def test_unknown_field_rejected() -> None:
    raw = json.loads(rec().to_json_line())
    raw["some_extra"] = 1
    with pytest.raises(gl.GapLedgerError, match="unknown field"):
        gl.GapRecord.from_dict(raw)


def test_schema_declares_no_outcome_fields() -> None:
    for name in gl.GapRecord.__dataclass_fields__:
        assert gl._forbidden_token(name) is None, f"schema field {name!r} is outcome-shaped"


def test_summary_contains_counts_only() -> None:
    summary = ledger(rec()).summary()
    blob = json.dumps(summary).lower()
    for token in ("brier", "profit", "rank", "support", "improve"):
        assert token not in blob


# --- CLI surface -------------------------------------------------------------


def test_no_update_or_delete_command() -> None:
    from kalshi_weather.cli import gap_ledger_app

    names = {c.name for c in gap_ledger_app.registered_commands}
    assert {"validate", "list", "show", "export", "propose", "append"} <= names
    for forbidden in ("update", "delete", "remove", "edit", "rewrite", "amend-in-place"):
        assert forbidden not in names


def test_list_and_show_are_read_only(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    gl.GapLedger().append(rec(gap_id="GAP-1"), path)
    before = path.read_bytes()
    from kalshi_weather.cli import app

    runner = CliRunner()
    assert (
        runner.invoke(app, ["data-quality", "gap-ledger", "list", "--path", str(path)]).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app, ["data-quality", "gap-ledger", "show", "--gap-id", "GAP-1", "--path", str(path)]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app, ["data-quality", "gap-ledger", "validate", "--path", str(path)]
        ).exit_code
        == 0
    )
    assert path.read_bytes() == before, "read-only commands must not modify the ledger"


def test_append_cli_requires_confirmation(tmp_path: Path) -> None:
    from kalshi_weather.cli import app

    path = tmp_path / "ledger.jsonl"
    candidate = tmp_path / "record.json"
    candidate.write_text(rec().to_json_line())
    result = CliRunner().invoke(
        app,
        ["data-quality", "gap-ledger", "append", "--file", str(candidate), "--path", str(path)],
    )
    assert result.exit_code == 2
    assert "--confirm is required" in result.stdout
    assert not path.exists(), "a refused append must write nothing"


def test_propose_does_not_append(tmp_path: Path) -> None:
    from kalshi_weather.cli import app

    path = tmp_path / "ledger.jsonl"
    out = tmp_path / "candidate.json"
    result = CliRunner().invoke(
        app, ["data-quality", "gap-ledger", "propose", "--output", str(out), "--gap-id", "GAP-N"]
    )
    assert result.exit_code == 0
    assert out.exists()
    assert not path.exists(), "propose must never append to the ledger"


# --- the production ledger ---------------------------------------------------


def test_production_ledger_validates() -> None:
    """The version-controlled ledger must always validate. Read-only."""
    problems = gl.GapLedger.load(gl.DEFAULT_LEDGER_PATH).validate()
    assert problems == [], f"production gap ledger is invalid: {problems}"


def test_production_ledger_hashes_verify() -> None:
    for record in gl.GapLedger.load(gl.DEFAULT_LEDGER_PATH).records:
        assert record.content_hash == record.compute_hash(), f"{record.gap_id} hash mismatch"


# --- module isolation guards -------------------------------------------------
# Scanned region is everything above this header, so the guards' own literals
# are excluded. Marker assembled from fragments to avoid self-match.

_MARKER = "# --- module isolation " + "guards"


def _scanned_region() -> str:
    region = THIS_FILE.read_text().split(_MARKER)[0]
    assert len(region) > 5_000, "guard region split failed -- marker moved?"
    return region


def test_no_experiment_or_paper_imports() -> None:
    """The ledger is an operational asset; it must not reach into research or
    trading code, in the module or in its tests.

    Checked on import statements rather than bare words -- prose that *names*
    an experiment (explaining what the ledger deliberately avoids) is fine; an
    import of one is not."""
    forbidden = (
        "experiments",
        "h0018",
        "h0019",
        "h0020",
        "paper",
        "broker",
        "kalshi.client",
        "KalshiClient",
        "httpx",
        "requests",
        "sqlalchemy",
    )
    for label, source in (
        ("tests", _scanned_region()),
        ("module", Path(gl.__file__).read_text()),
    ):
        for line in source.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            for name in forbidden:
                assert name not in stripped, f"{label} imports {name!r}: {stripped}"


def test_no_database_or_production_writes_in_tests() -> None:
    region = _scanned_region()
    for forbidden in ("get_settings", "_open_session", "AsyncSession", "psycopg", "postgresql"):
        assert forbidden not in region, f"{forbidden} must not appear in gap-ledger tests"


def test_only_tmp_paths_are_written() -> None:
    """Every write in these tests targets tmp_path; the production ledger is
    only ever loaded."""
    region = _scanned_region()
    for line in region.splitlines():
        if ".append(" in line and "gl.GapLedger" in line:
            assert "path" in line or "tmp" in line, f"append without a tmp path: {line.strip()}"
