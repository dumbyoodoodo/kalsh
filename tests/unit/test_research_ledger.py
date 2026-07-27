"""Append-only research ledger: hash-chain integrity, no-overwrite,
frozen-success-rule, test-window-reuse acknowledgement, and family counts."""

import json
from pathlib import Path

import pytest

from kalshi_weather.research.ledger import (
    LedgerError,
    LedgerRecord,
    append_record,
    family_counts,
    read_ledger,
    verify_ledger,
)


def rec(hid: str = "H9001", **kw: object) -> LedgerRecord:
    base: dict[str, object] = {
        "hypothesis_id": hid,
        "title": f"{hid} synthetic hypothesis",
        "status": "registered",
        "created": "2026-07-27",
    }
    base.update(kw)
    return LedgerRecord(**base)  # type: ignore[arg-type]


def test_append_read_roundtrip_and_chain(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    r1 = append_record(path, rec("H9001"))
    r2 = append_record(path, rec("H9002"))
    assert (r1.sequence, r2.sequence) == (1, 2)
    assert r2.prev_hash == r1.record_hash
    loaded = read_ledger(path)
    assert [r.hypothesis_id for r in loaded] == ["H9001", "H9002"]
    assert verify_ledger(path) == 2


def test_tampering_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    append_record(path, rec("H9001", status="registered"))
    append_record(path, rec("H9002"))
    lines = path.read_text().splitlines()
    # rewrite history: flip H9001's status in place
    doctored = json.loads(lines[0])
    doctored["status"] = "supported"
    path.write_text("\n".join([json.dumps(doctored, sort_keys=True), lines[1]]) + "\n")
    with pytest.raises(LedgerError, match="hash mismatch"):
        read_ledger(path)
    # deleting a line breaks the chain too
    path.write_text(lines[1] + "\n")
    with pytest.raises(LedgerError, match=r"chain break|sequence"):
        read_ledger(path)


def test_no_overwrite_without_explicit_supersede(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    first = append_record(path, rec("H9001", status="registered"))
    with pytest.raises(LedgerError, match="never overwritten"):
        append_record(path, rec("H9001", status="supported"))
    # explicit supersede of the latest record is the sanctioned path
    updated = append_record(
        path, rec("H9001", status="not_supported", supersedes_sequence=first.sequence)
    )
    assert updated.sequence == 2
    # the failed outcome stays recorded; nothing was rewritten
    loaded = read_ledger(path)
    assert [r.status for r in loaded] == ["registered", "not_supported"]
    # superseding a stale (non-latest) record is refused
    with pytest.raises(LedgerError, match="supersedes_sequence=2"):
        append_record(path, rec("H9001", status="closed", supersedes_sequence=first.sequence))
    # supersede pointing at nothing is refused
    with pytest.raises(LedgerError, match="no prior record"):
        append_record(path, rec("H9003", supersedes_sequence=1))


def test_success_rule_frozen_after_test_access(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    r1 = append_record(
        path,
        rec("H9001", success_rule="CI above 0", test_data_accessed=True, status="not_supported"),
    )
    with pytest.raises(LedgerError, match="success rule is frozen"):
        append_record(
            path,
            rec(
                "H9001",
                success_rule="CI above -0.01",  # moved goalposts
                status="supported",
                supersedes_sequence=r1.sequence,
            ),
        )
    # same rule is fine (e.g. closing the hypothesis)
    append_record(
        path,
        rec(
            "H9001",
            success_rule="CI above 0",
            status="closed",
            test_data_accessed=True,
            supersedes_sequence=r1.sequence,
        ),
    )


def test_test_window_reuse_requires_acknowledgement(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    append_record(path, rec("H9001", test_window=("2026-08-12", "2026-08-25")))
    # silent overlap refused
    with pytest.raises(LedgerError, match="reuse must be acknowledged"):
        append_record(path, rec("H9002", test_window=("2026-08-20", "2026-09-02")))
    # acknowledged overlap allowed
    append_record(
        path,
        rec("H9002", test_window=("2026-08-20", "2026-09-02"), related=("H9001",)),
    )
    # disjoint window needs no acknowledgement
    append_record(path, rec("H9003", test_window=("2026-09-03", "2026-09-16")))


def test_family_counts_expose_multiple_testing(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    append_record(path, rec("H9001", test_data_accessed=True, status="not_supported"))
    append_record(path, rec("H9002"))
    r3 = append_record(path, rec("H9003"))
    append_record(
        path,
        rec("H9003", test_data_accessed=True, status="supported", supersedes_sequence=r3.sequence),
    )
    counts = family_counts(read_ledger(path))
    assert counts == {"hypotheses": 3, "test_data_accessed": 2}


def test_required_fields_and_missing_file(tmp_path: Path) -> None:
    assert read_ledger(tmp_path / "absent.jsonl") == []
    with pytest.raises(LedgerError, match="required"):
        append_record(tmp_path / "ledger.jsonl", rec("H9001", title=""))
