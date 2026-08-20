"""Terminal disposition of a station-pilot extension window.

The invariant that matters most: a calendar gate passing must never be readable
as "the evidence exists". STATION-PILOT-EXT-0001 is the case that proved they
can come apart entirely -- the gate passed on schedule while three consecutive
registered station-local dates recorded zero collection.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_weather.data_quality.gap_ledger import GapLedger
from kalshi_weather.research import extension_deployment as ed
from kalshi_weather.research import station_pilot_extension as ext
from kalshi_weather.research.extension_disposition import (
    FORBIDDEN_ANALYSIS_TOKENS,
    FORBIDDEN_STATION_VERDICTS,
    RE_REGISTRATION_PREREQUISITES,
    Disposition,
    DispositionError,
    DispositionLedger,
    DispositionRecord,
    StationDateCoverage,
    complete_date_count,
    evidence_is_sufficient,
    validate_record,
)

STATIONS = ("SEA", "PHX", "MIA")
GATE = datetime(2026, 8, 14, 4, 0, tzinfo=UTC)
VALIDATED = datetime(2026, 8, 6, 5, 2, 25, tzinfo=UTC)
RECORDED = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def cov(
    station: str, day: int, *, observed: int = 48, gaps: tuple[str, ...] = ()
) -> StationDateCoverage:
    d = date(2026, 8, day)
    start = datetime(2026, 8, day, 7, 0, tzinfo=UTC)
    return StationDateCoverage(
        station_code=station,
        local_date=d,
        utc_start=start,
        utc_end=start + timedelta(days=1),
        expected_cycles=48,
        observed_cycles=observed,
        recorded_attempts=observed * 2,
        intersecting_gap_ids=gaps,
    )


def full_coverage() -> tuple[StationDateCoverage, ...]:
    return tuple(cov(s, 6 + i) for s in STATIONS for i in range(7))


def record(**kw) -> DispositionRecord:
    base: dict = {
        "disposition_id": "TEST-DISP-0001",
        "extension_registration_id": ext.EXTENSION_REGISTRATION_ID,
        "registration_hash": ext.REGISTRATION.registration_hash(),
        "deployment_id": "SPX-EXT-DEPLOY-0001",
        "deployment_content_hash": "e" * 64,
        "deployment_validated_at": VALIDATED,
        "review_gate": GATE,
        "registered_dates": (("SEA", ("2026-08-06",)),),
        "coverage": full_coverage(),
        "required_complete_dates": 7,
        "disposition": Disposition.EXTENSION_EVIDENCE_SUFFICIENT,
        "evidence_complete": True,
        "station_verdicts_issued": False,
        "referenced_gap_ids": (),
        "unresolved_question": "n/a",
        "rationale": "test",
        "re_registration_prerequisites": RE_REGISTRATION_PREREQUISITES,
        "recorded_at": RECORDED,
        "recorded_by": "tester",
    }
    base.update(kw)
    return DispositionRecord(**base).with_hash()


def incomplete_coverage() -> tuple[StationDateCoverage, ...]:
    rows = []
    for s in STATIONS:
        rows.append(cov(s, 6))
        for i in range(1, 7):
            rows.append(cov(s, 6 + i, observed=0, gaps=("GAP-X",)))
    return tuple(rows)


def incomplete_record(**kw) -> DispositionRecord:
    defaults: dict = {
        "disposition": Disposition.EXTENSION_INCOMPLETE_COLLECTION,
        "evidence_complete": False,
        "coverage": incomplete_coverage(),
        "referenced_gap_ids": ("GAP-X",),
    }
    defaults.update(kw)
    return record(**defaults)


# --- sufficiency ---------------------------------------------------------------


def test_gate_passed_with_seven_complete_dates_is_review_eligible() -> None:
    assert evidence_is_sufficient(
        full_coverage(), station_codes=STATIONS, required_complete_dates=7
    )


def test_gate_passed_with_incomplete_dates_is_not_sufficient() -> None:
    assert not evidence_is_sufficient(
        incomplete_coverage(), station_codes=STATIONS, required_complete_dates=7
    )


def test_calendar_readiness_does_not_imply_evidence_readiness() -> None:
    """The core conflation this module exists to prevent."""
    state = ext.extension_state(now=GATE, deployment_validated_at=VALIDATED)
    assert state is ext.ExtensionState.READY_FOR_EXTENSION_REVIEW
    assert not evidence_is_sufficient(
        incomplete_coverage(), station_codes=STATIONS, required_complete_dates=7
    )


def test_a_date_with_full_counts_but_a_gap_is_incomplete() -> None:
    """Counts alone are insufficient: a ledger gap proves collection stopped."""
    assert not cov("SEA", 6, observed=48, gaps=("GAP-X",)).complete
    assert cov("SEA", 6, observed=48).complete


def test_station_with_no_coverage_rows_does_not_pass_vacuously() -> None:
    partial = tuple(c for c in full_coverage() if c.station_code != "MIA")
    assert complete_date_count(partial, "MIA") == 0
    assert not evidence_is_sufficient(
        partial, station_codes=STATIONS, required_complete_dates=7
    )


# --- record invariants ---------------------------------------------------------


def test_null_result_contains_no_station_verdict() -> None:
    rec = incomplete_record()
    assert validate_record(rec) == []
    assert rec.station_verdicts_issued is False
    blob = json.dumps(rec.to_dict()).upper()
    for verdict in FORBIDDEN_STATION_VERDICTS:
        assert verdict not in blob


def test_a_smuggled_station_verdict_is_refused() -> None:
    bad = incomplete_record(rationale="SEA should be KEEP")
    assert any("station verdict" in p for p in validate_record(bad))


def test_null_result_contains_no_performance_metrics() -> None:
    rec = incomplete_record()
    lowered = json.dumps(rec.to_dict()).lower()
    for token in FORBIDDEN_ANALYSIS_TOKENS:
        assert token not in lowered, token


def test_outcome_bearing_token_is_refused() -> None:
    bad = incomplete_record(rationale="brier score was better at SEA")
    assert any("outcome-bearing" in p for p in validate_record(bad))


def test_incomplete_disposition_requires_an_intersecting_gap() -> None:
    bad = incomplete_record(referenced_gap_ids=())
    assert any("collection gap" in p for p in validate_record(bad))


def test_disposition_must_agree_with_completeness() -> None:
    bad = record(disposition=Disposition.EXTENSION_INCOMPLETE_COLLECTION, evidence_complete=True)
    assert any("contradicts" in p for p in validate_record(bad))
    bad2 = record(
        disposition=Disposition.EXTENSION_EVIDENCE_SUFFICIENT,
        evidence_complete=False,
        coverage=incomplete_coverage(),
    )
    assert any("contradicts" in p for p in validate_record(bad2))


def test_disposition_before_the_gate_is_refused() -> None:
    bad = incomplete_record(recorded_at=GATE - timedelta(hours=1))
    assert any("precedes the review gate" in p for p in validate_record(bad))


def test_in_place_edit_detected() -> None:
    rec = incomplete_record()
    tampered = replace(rec, rationale="edited after the fact")
    assert any("content_hash mismatch" in p for p in validate_record(tampered))


def test_content_hash_is_deterministic() -> None:
    assert incomplete_record().content_hash == incomplete_record().content_hash


def test_no_force_or_override_surface() -> None:
    source = Path("src/kalshi_weather/research/extension_disposition.py").read_text()
    # Scan only the executable surface: parameter names and callables. Prose may
    # legitimately say "no override exists" -- what must not exist is an actual
    # escape hatch, so look for one being DEFINED or ACCEPTED.
    for token in ("force", "override", "skip_validation", "ignore_problems"):
        for shape in (f"{token}=", f"{token}:", f"def {token}", f"_{token}("):
            assert shape not in source.lower(), f"{token} appears as {shape!r}"


# --- ledger --------------------------------------------------------------------


def test_append_and_read_back(tmp_path: Path) -> None:
    p = tmp_path / "disp.jsonl"
    stamped = DispositionLedger().append(incomplete_record(), p)
    back = DispositionLedger.load(p)
    assert len(back.records) == 1
    assert back.records[0].content_hash == stamped.content_hash
    assert back.validate() == []


def test_duplicate_disposition_id_refused(tmp_path: Path) -> None:
    p = tmp_path / "disp.jsonl"
    DispositionLedger().append(incomplete_record(), p)
    with pytest.raises(DispositionError, match="already exists"):
        DispositionLedger.load(p).append(incomplete_record(), p)


def test_conflicting_disposition_for_same_registration_refused(tmp_path: Path) -> None:
    """A registration is disposed exactly once; a second, contradictory
    disposition must not silently supersede the first."""
    p = tmp_path / "disp.jsonl"
    DispositionLedger().append(incomplete_record(), p)
    conflicting = record(disposition_id="TEST-DISP-0002")
    with pytest.raises(DispositionError, match="disposed exactly once"):
        DispositionLedger.load(p).append(conflicting, p)


def test_invalid_record_never_written(tmp_path: Path) -> None:
    p = tmp_path / "disp.jsonl"
    with pytest.raises(DispositionError):
        DispositionLedger().append(incomplete_record(recorded_by=""), p)
    assert not p.exists()


def test_prior_bytes_unchanged_on_append(tmp_path: Path) -> None:
    p = tmp_path / "disp.jsonl"
    DispositionLedger().append(incomplete_record(), p)
    before = p.read_bytes()
    other = record(
        disposition_id="TEST-DISP-0003",
        extension_registration_id="STATION-PILOT-EXT-0002",
        disposition=Disposition.EXTENSION_INCOMPLETE_COLLECTION,
        evidence_complete=False,
        coverage=incomplete_coverage(),
        referenced_gap_ids=("GAP-X",),
    )
    DispositionLedger.load(p).append(other, p)
    assert p.read_bytes().startswith(before)


# --- state machine -------------------------------------------------------------


def test_recorded_disposition_makes_the_state_terminal() -> None:
    assert (
        ext.extension_state(
            now=GATE + timedelta(days=6),
            deployment_validated_at=VALIDATED,
            disposition_recorded=True,
        )
        is ext.ExtensionState.EXTENSION_DISPOSED
    )


def test_disposed_state_never_returns_to_collecting_or_review() -> None:
    for offset in (timedelta(days=-3), timedelta(0), timedelta(days=30)):
        state = ext.extension_state(
            now=GATE + offset,
            deployment_validated_at=VALIDATED,
            disposition_recorded=True,
        )
        assert state is ext.ExtensionState.EXTENSION_DISPOSED


def test_state_without_disposition_is_unchanged() -> None:
    """The calendar behaviour must not shift for undisposed windows."""
    assert (
        ext.extension_state(now=GATE, deployment_validated_at=VALIDATED)
        is ext.ExtensionState.READY_FOR_EXTENSION_REVIEW
    )
    assert (
        ext.extension_state(now=GATE - timedelta(days=1), deployment_validated_at=VALIDATED)
        is ext.ExtensionState.COLLECTING_EXTENSION
    )


# --- the real recorded disposition ---------------------------------------------


@pytest.fixture(scope="module")
def live() -> DispositionRecord:
    rec = DispositionLedger.load().for_registration(ext.EXTENSION_REGISTRATION_ID)
    assert rec is not None, "STATION-PILOT-EXT-0001 has no recorded disposition"
    return rec


def test_recorded_disposition_is_valid_and_incomplete(live: DispositionRecord) -> None:
    assert validate_record(live) == []
    assert live.disposition is Disposition.EXTENSION_INCOMPLETE_COLLECTION
    assert live.evidence_complete is False
    assert live.station_verdicts_issued is False


def test_recorded_disposition_pins_immutable_identities(live: DispositionRecord) -> None:
    assert live.registration_hash == ext.REGISTRATION.registration_hash()
    anchor = ed.DeploymentLedger.load().records[0]
    assert live.deployment_id == anchor.deployment_id
    assert live.deployment_content_hash == anchor.content_hash
    assert live.deployment_validated_at == anchor.deployment_validated_at
    assert live.review_gate == anchor.review_gate()


def test_recorded_dates_match_the_frozen_registration(live: DispositionRecord) -> None:
    """The disposition must not restate the window differently from the
    registration's own deterministic date arithmetic."""
    for code, tz in ext.STATION_TIMEZONES:
        expected = tuple(
            d.isoformat() for d in ext.included_local_dates(live.deployment_validated_at, tz)
        )
        recorded = dict(live.registered_dates)[code]
        assert recorded == expected, code


def test_referenced_gaps_exist_and_are_unchanged(live: DispositionRecord) -> None:
    gaps = GapLedger.load()
    assert gaps.validate() == []
    assert live.referenced_gap_ids, "an incomplete disposition must cite its gaps"
    for gap_id in live.referenced_gap_ids:
        gap = gaps.by_id(gap_id)
        assert gap is not None, gap_id
        assert gap.content_hash == gap.compute_hash(), gap_id


def test_no_station_reached_the_required_complete_dates(live: DispositionRecord) -> None:
    for code, _tz in ext.STATION_TIMEZONES:
        assert live.complete_dates(code) < live.required_complete_dates, code


def test_re_registration_is_not_created_automatically(live: DispositionRecord) -> None:
    ids = {r.extension_registration_id for r in DispositionLedger.load().records}
    assert ids == {ext.EXTENSION_REGISTRATION_ID}
    assert ext.EXTENSION_REGISTRATION_ID == "STATION-PILOT-EXT-0001"
    assert live.re_registration_prerequisites, "prerequisites must be recorded"


def test_frozen_review_is_not_invoked_by_this_module() -> None:
    """The disposition path must never call the station review."""
    source = Path("src/kalshi_weather/research/extension_disposition.py").read_text()
    assert "station_pilot_review" not in source
    assert "import" not in source.split("class DispositionError")[1]
