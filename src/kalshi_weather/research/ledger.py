"""Append-only, hash-chained research ledger.

``HYPOTHESES.md`` remains the narrative record humans read; this ledger is
its machine-readable companion: one JSONL record per hypothesis state, with
each line carrying a SHA-256 over its own canonical content plus the
previous line's hash. Any edit, deletion, or reordering of history breaks
the chain and is detected by ``verify_ledger`` -- immutability is checked,
not assumed.

Enforced invariants (the multiple-testing discipline, made mechanical):

- **No overwriting.** A hypothesis id may only gain a new record when the
  new record explicitly supersedes the latest prior record for that id.
  Prior records are never rewritten; corrections are new records.
- **No moving the goalposts.** Once any record for an id has
  ``test_data_accessed=True``, later records for that id may not change the
  success rule.
- **No silent test-window reuse.** A record whose test window overlaps
  another hypothesis's recorded test window is rejected unless it names
  that hypothesis in ``related`` -- reuse must be acknowledged, on the
  record, at registration time.
- **Explicit multiple-testing count.** ``family_counts`` reports how many
  hypotheses exist and how many have accessed test data, so any
  significance claim can state its family size.

Failed hypotheses stay recorded forever; a negative result is a completed
row, not a deleted one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

#: Chain anchor for the first record in a ledger file.
GENESIS_HASH = "0" * 64


class LedgerError(ValueError):
    """A ledger operation that would violate append-only research discipline."""


@dataclass(frozen=True)
class LedgerRecord:
    """One immutable ledger line. Windows are (start, end) ISO-date pairs."""

    hypothesis_id: str
    title: str
    status: str
    created: str  # ISO date the record was authored (caller-supplied, no wall clock here)
    rationale: str | None = None
    dataset_version: str | None = None
    train_window: tuple[str, str] | None = None
    validation_window: tuple[str, str] | None = None
    test_window: tuple[str, str] | None = None
    feature_family: str | None = None
    model_family: str | None = None
    benchmark: str | None = None
    primary_metric: str | None = None
    success_rule: str | None = None
    result: str | None = None
    related: tuple[str, ...] = ()
    test_data_accessed: bool = False
    artifact_hashes: tuple[tuple[str, str], ...] = ()
    supersedes_sequence: int | None = None
    source: str | None = None  # e.g. "HYPOTHESES.md backfill"
    # Assigned by append_record; 0 means "not yet appended".
    sequence: int = 0
    record_hash: str = ""
    prev_hash: str = ""


def _canonical_payload(record: LedgerRecord) -> str:
    """Canonical JSON of everything the hash covers (all fields except the
    hash itself)."""
    payload = asdict(record)
    payload.pop("record_hash")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _hash_record(record: LedgerRecord) -> str:
    return hashlib.sha256(_canonical_payload(record).encode()).hexdigest()


def _tuplify(record: LedgerRecord) -> LedgerRecord:
    """JSON round-trips tuples as lists; normalize back for equality/hashing."""

    def pair(v: object) -> tuple[str, str] | None:
        if v is None:
            return None
        seq = list(v)  # type: ignore[call-overload]
        if len(seq) != 2:
            raise LedgerError(f"window must be a (start, end) pair, got {v!r}")
        return (str(seq[0]), str(seq[1]))

    return LedgerRecord(
        **{
            **asdict(record),
            "train_window": pair(record.train_window),
            "validation_window": pair(record.validation_window),
            "test_window": pair(record.test_window),
            "related": tuple(record.related),
            "artifact_hashes": tuple((str(k), str(v)) for k, v in record.artifact_hashes),
        }
    )


def read_ledger(path: Path) -> list[LedgerRecord]:
    """Load and chain-verify the ledger. Raises LedgerError on any tampering."""
    if not path.exists():
        return []
    records: list[LedgerRecord] = []
    prev = GENESIS_HASH
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"line {lineno}: not valid JSON ({exc})") from exc
        record = _tuplify(LedgerRecord(**raw))
        if record.prev_hash != prev:
            raise LedgerError(
                f"line {lineno}: chain break (prev_hash {record.prev_hash[:12]}... "
                f"!= expected {prev[:12]}...)"
            )
        if record.record_hash != _hash_record(record):
            raise LedgerError(f"line {lineno}: content hash mismatch -- record was modified")
        if record.sequence != lineno:
            raise LedgerError(f"line {lineno}: sequence {record.sequence} out of order")
        records.append(record)
        prev = record.record_hash
    return records


def verify_ledger(path: Path) -> int:
    """Chain-verify; returns the number of valid records."""
    return len(read_ledger(path))


def _windows_overlap(a: tuple[str, str], b: tuple[str, str]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]  # ISO dates compare lexicographically


def append_record(path: Path, record: LedgerRecord) -> LedgerRecord:
    """Validate against history, assign sequence + hashes, and append.

    The file is only ever opened in append mode -- existing bytes cannot be
    rewritten through this API.
    """
    if not record.hypothesis_id or not record.title or not record.status or not record.created:
        raise LedgerError("hypothesis_id, title, status, and created are required")
    record = _tuplify(record)
    existing = read_ledger(path)  # verifies chain before any append

    prior_same_id = [r for r in existing if r.hypothesis_id == record.hypothesis_id]
    if prior_same_id:
        latest = prior_same_id[-1]
        if record.supersedes_sequence != latest.sequence:
            raise LedgerError(
                f"{record.hypothesis_id} already has record #{latest.sequence}; a new record "
                f"must set supersedes_sequence={latest.sequence} (got "
                f"{record.supersedes_sequence!r}) -- prior outcomes are never overwritten"
            )
        if any(r.test_data_accessed for r in prior_same_id):
            frozen_rules = {r.success_rule for r in prior_same_id if r.test_data_accessed}
            if record.success_rule not in frozen_rules:
                raise LedgerError(
                    f"{record.hypothesis_id} has accessed test data; its success rule is frozen "
                    f"and cannot change"
                )
    elif record.supersedes_sequence is not None:
        raise LedgerError(
            f"supersedes_sequence set but no prior record exists for {record.hypothesis_id}"
        )

    if record.test_window is not None:
        for other in existing:
            if other.hypothesis_id == record.hypothesis_id or other.test_window is None:
                continue
            if _windows_overlap(record.test_window, other.test_window) and (
                other.hypothesis_id not in record.related
            ):
                raise LedgerError(
                    f"test window {record.test_window} overlaps {other.hypothesis_id}'s "
                    f"{other.test_window}; reuse must be acknowledged by listing "
                    f"{other.hypothesis_id!r} in related"
                )

    prev = existing[-1].record_hash if existing else GENESIS_HASH
    sequenced = LedgerRecord(
        **{**asdict(record), "sequence": len(existing) + 1, "prev_hash": prev, "record_hash": ""}
    )
    sequenced = _tuplify(sequenced)
    final = LedgerRecord(**{**asdict(sequenced), "record_hash": _hash_record(sequenced)})
    final = _tuplify(final)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(asdict(final), sort_keys=True) + "\n")
    return final


def family_counts(records: list[LedgerRecord]) -> dict[str, int]:
    """The explicit multiple-testing accounting: how big is the family?"""
    ids = {r.hypothesis_id for r in records}
    tested = {r.hypothesis_id for r in records if r.test_data_accessed}
    return {"hypotheses": len(ids), "test_data_accessed": len(tested)}
