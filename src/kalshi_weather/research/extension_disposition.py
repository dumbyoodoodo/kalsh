"""Terminal disposition for a station-pilot extension window.

Why this exists. ``station_pilot_extension.extension_state`` is purely a
CALENDAR function: it takes ``now`` and ``deployment_validated_at`` and nothing
else, so ``READY_FOR_EXTENSION_REVIEW`` means only "the seventh local date has
elapsed". It cannot and does not mean "the evidence the review needs was
collected". For STATION-PILOT-EXT-0001 those two came apart completely: the gate
passed on 2026-08-14T04:00:00Z while four consecutive registered dates had ZERO
weather collection, caused by host unavailability.

Leaving the state at ``READY_FOR_EXTENSION_REVIEW`` would invite exactly the
wrong action -- running the frozen KEEP/EXTEND/REMOVE review against evidence
that was never acquired, and thereby converting *host downtime* into a
*station* verdict. This module records the third possibility the state machine
lacked: the window ended, the evidence was not collected, and no station was
evaluated.

What this is NOT. This is not KEEP, EXTEND_COLLECTION, or
REMOVE_FOR_DATA_QUALITY -- those are station verdicts produced by the frozen
review, and none is issued here. It is not a negative or failed experimental
result either: nothing about the stations was measured. It is an
evidence-ACQUISITION failure, recorded so the pilot question is visibly
unresolved rather than silently answered.

The frozen registration, its dates, the deployment anchor, and the permanent gap
records are all immutable and are referenced by hash, never rewritten.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

DEFAULT_DISPOSITION_LEDGER = Path("data/operations/station_pilot_extension_dispositions.jsonl")

LEDGER_VERSION = "1"

#: Station verdicts from the frozen review. Named here ONLY so a disposition
#: record can be checked never to contain one.
FORBIDDEN_STATION_VERDICTS: tuple[str, ...] = (
    "KEEP",
    "EXTEND_COLLECTION",
    "REMOVE_FOR_DATA_QUALITY",
    "PILOT_OPERATIONALLY_ACCEPTABLE",
    "PILOT_DATA_QUALITY_FAILURE",
)

#: Outcome-bearing analysis that must never appear in an operational
#: disposition. Mirrors the registration's own forbidden list.
FORBIDDEN_ANALYSIS_TOKENS: tuple[str, ...] = (
    "forecast_error",
    "brier",
    "calibration",
    "market_price",
    "liquidity",
    "pnl",
    "expected_value",
    "profitability",
    "station_ranking",
    "hypothesis_support",
    "accuracy",
    "sharpe",
)

#: Minimum prerequisites before ANY further prospective extension may be
#: registered. Deliberately about host availability, not about a specific
#: machine or vendor -- choosing the infrastructure is a separate decision.
RE_REGISTRATION_PREREQUISITES: tuple[str, ...] = (
    "collector hosted on a platform intended to remain continuously available",
    "exactly one collector process",
    "service survives unattended operation across a bounded validation period",
    "no recurring host-sleep collection gaps during that validation period",
    "weather-attempt evidence complete for every instrumented cycle",
    "observatory reports no CRITICAL findings",
)


class DispositionError(ValueError):
    """A disposition record or append violates the recorded rules."""


class Disposition(StrEnum):
    """Terminal disposition of a registered extension window.

    Deliberately disjoint from the station-verdict vocabulary: neither value
    says anything about a station.
    """

    #: Window ended; required prospective evidence was not collected; no station
    #: was evaluated; the pilot question remains unresolved.
    EXTENSION_INCOMPLETE_COLLECTION = "EXTENSION_INCOMPLETE_COLLECTION"
    #: Window ended with sufficient evidence; the frozen review may now run.
    #: Recording this does NOT run the review and issues no verdict.
    EXTENSION_EVIDENCE_SUFFICIENT = "EXTENSION_EVIDENCE_SUFFICIENT"


@dataclass(frozen=True, slots=True)
class StationDateCoverage:
    """Factual collection coverage of ONE registered station-local date.

    Counts only. There is no field here that could carry a performance measure.
    """

    station_code: str
    local_date: date
    utc_start: datetime
    utc_end: datetime
    expected_cycles: int
    observed_cycles: int
    recorded_attempts: int
    intersecting_gap_ids: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """A date is complete only with full expected coverage and no gap.

        Both conditions, because either alone is insufficient: a date could show
        full counts while a ledger gap proves collection stopped, and a date
        with no recorded gap could still be short.
        """
        return self.observed_cycles >= self.expected_cycles and not self.intersecting_gap_ids

    @property
    def coverage_fraction(self) -> float:
        if self.expected_cycles <= 0:
            return 0.0
        return min(1.0, self.observed_cycles / self.expected_cycles)

    def to_dict(self) -> dict[str, Any]:
        return {
            "station_code": self.station_code,
            "local_date": self.local_date.isoformat(),
            "utc_start": _iso(self.utc_start),
            "utc_end": _iso(self.utc_end),
            "expected_cycles": self.expected_cycles,
            "observed_cycles": self.observed_cycles,
            "recorded_attempts": self.recorded_attempts,
            "intersecting_gap_ids": list(self.intersecting_gap_ids),
            "complete": self.complete,
            "coverage_fraction": round(self.coverage_fraction, 4),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StationDateCoverage:
        return cls(
            station_code=str(raw["station_code"]),
            local_date=date.fromisoformat(str(raw["local_date"])),
            utc_start=_parse(raw["utc_start"], "utc_start"),
            utc_end=_parse(raw["utc_end"], "utc_end"),
            expected_cycles=int(raw["expected_cycles"]),
            observed_cycles=int(raw["observed_cycles"]),
            recorded_attempts=int(raw["recorded_attempts"]),
            intersecting_gap_ids=tuple(str(g) for g in raw.get("intersecting_gap_ids", ())),
        )


def complete_date_count(coverage: tuple[StationDateCoverage, ...], station_code: str) -> int:
    return sum(1 for c in coverage if c.station_code == station_code and c.complete)


def evidence_is_sufficient(
    coverage: tuple[StationDateCoverage, ...],
    *,
    station_codes: tuple[str, ...],
    required_complete_dates: int,
) -> bool:
    """Every station must reach the frozen required number of complete dates.

    Applies the registered rule as-is. No threshold is introduced here, and a
    station with no coverage rows at all counts as zero rather than passing
    vacuously.
    """
    return all(
        complete_date_count(coverage, code) >= required_complete_dates for code in station_codes
    )


@dataclass(frozen=True, slots=True)
class DispositionRecord:
    """One immutable terminal disposition for one registered extension."""

    disposition_id: str
    extension_registration_id: str
    registration_hash: str
    deployment_id: str
    deployment_content_hash: str
    deployment_validated_at: datetime
    review_gate: datetime
    registered_dates: tuple[tuple[str, tuple[str, ...]], ...]
    coverage: tuple[StationDateCoverage, ...]
    required_complete_dates: int
    disposition: Disposition
    evidence_complete: bool
    station_verdicts_issued: bool
    referenced_gap_ids: tuple[str, ...]
    unresolved_question: str
    rationale: str
    re_registration_prerequisites: tuple[str, ...]
    recorded_at: datetime
    recorded_by: str
    ledger_version: str = LEDGER_VERSION
    content_hash: str = ""

    def complete_dates(self, station_code: str) -> int:
        return complete_date_count(self.coverage, station_code)

    def incomplete_dates(self, station_code: str) -> tuple[date, ...]:
        return tuple(
            c.local_date
            for c in self.coverage
            if c.station_code == station_code and not c.complete
        )

    def _payload(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("content_hash", None)
        data["deployment_validated_at"] = _iso(self.deployment_validated_at)
        data["review_gate"] = _iso(self.review_gate)
        data["recorded_at"] = _iso(self.recorded_at)
        data["disposition"] = str(self.disposition)
        data["registered_dates"] = [[code, list(days)] for code, days in self.registered_dates]
        data["coverage"] = [c.to_dict() for c in self.coverage]
        data["referenced_gap_ids"] = list(self.referenced_gap_ids)
        data["re_registration_prerequisites"] = list(self.re_registration_prerequisites)
        return data

    def compute_hash(self) -> str:
        canonical = json.dumps(self._payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def with_hash(self) -> DispositionRecord:
        return replace(self, content_hash=self.compute_hash())

    def to_dict(self) -> dict[str, Any]:
        data = self._payload()
        data["content_hash"] = self.content_hash or self.compute_hash()
        return data

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DispositionRecord:
        try:
            return cls(
                disposition_id=str(raw["disposition_id"]),
                extension_registration_id=str(raw["extension_registration_id"]),
                registration_hash=str(raw["registration_hash"]),
                deployment_id=str(raw["deployment_id"]),
                deployment_content_hash=str(raw["deployment_content_hash"]),
                deployment_validated_at=_parse(
                    raw["deployment_validated_at"], "deployment_validated_at"
                ),
                review_gate=_parse(raw["review_gate"], "review_gate"),
                registered_dates=tuple(
                    (str(code), tuple(str(d) for d in days))
                    for code, days in raw["registered_dates"]
                ),
                coverage=tuple(StationDateCoverage.from_dict(c) for c in raw["coverage"]),
                required_complete_dates=int(raw["required_complete_dates"]),
                disposition=Disposition(raw["disposition"]),
                evidence_complete=bool(raw["evidence_complete"]),
                station_verdicts_issued=bool(raw["station_verdicts_issued"]),
                referenced_gap_ids=tuple(str(g) for g in raw["referenced_gap_ids"]),
                unresolved_question=str(raw["unresolved_question"]),
                rationale=str(raw["rationale"]),
                re_registration_prerequisites=tuple(
                    str(p) for p in raw["re_registration_prerequisites"]
                ),
                recorded_at=_parse(raw["recorded_at"], "recorded_at"),
                recorded_by=str(raw["recorded_by"]),
                ledger_version=str(raw.get("ledger_version", LEDGER_VERSION)),
                content_hash=str(raw.get("content_hash", "")),
            )
        except KeyError as exc:
            raise DispositionError(f"missing required field: {exc}") from exc
        except ValueError as exc:
            if isinstance(exc, DispositionError):
                raise
            raise DispositionError(f"invalid disposition record: {exc}") from exc


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise DispositionError(f"{field_name} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise DispositionError(f"{field_name} must be timezone-aware UTC")
    return parsed.astimezone(UTC)


def validate_record(record: DispositionRecord) -> list[str]:
    """Every invariant. Empty list == valid. Fails closed; no override exists."""
    p: list[str] = []
    rid = record.disposition_id

    if not rid.strip():
        p.append("disposition_id is required")
    if not record.extension_registration_id.strip():
        p.append(f"{rid}: extension_registration_id is required")
    if len(record.registration_hash) != 64:
        p.append(f"{rid}: registration_hash must be a sha256 hex digest")
    if len(record.deployment_content_hash) != 64:
        p.append(f"{rid}: deployment_content_hash must be a sha256 hex digest")
    if not record.recorded_by.strip():
        p.append(f"{rid}: recorded_by is required")
    if not record.rationale.strip():
        p.append(f"{rid}: rationale is required")
    if not record.unresolved_question.strip():
        p.append(f"{rid}: unresolved_question is required")

    # The gate must actually have passed: a disposition before it would close a
    # window that was still collecting.
    if record.recorded_at < record.review_gate:
        p.append(f"{rid}: recorded_at precedes the review gate")
    if record.review_gate <= record.deployment_validated_at:
        p.append(f"{rid}: review_gate must follow deployment_validated_at")

    # No station verdict may ever ride along in a disposition.
    if record.station_verdicts_issued:
        p.append(f"{rid}: station_verdicts_issued must be false -- no verdict is produced here")
    blob = json.dumps(record._payload(), sort_keys=True).upper()
    for verdict in FORBIDDEN_STATION_VERDICTS:
        if verdict in blob:
            p.append(f"{rid}: contains station verdict {verdict!r}")
    lowered = blob.lower()
    for token in FORBIDDEN_ANALYSIS_TOKENS:
        if token in lowered:
            p.append(f"{rid}: contains outcome-bearing token {token!r}")

    # Disposition must agree with the computed completeness.
    if record.disposition is Disposition.EXTENSION_INCOMPLETE_COLLECTION:
        if record.evidence_complete:
            p.append(f"{rid}: INCOMPLETE disposition contradicts evidence_complete=True")
        if not record.coverage:
            p.append(f"{rid}: coverage rows are required to justify an incomplete disposition")
        if not record.referenced_gap_ids:
            p.append(
                f"{rid}: at least one documented collection gap must intersect the "
                "registered dates"
            )
        if not any(not c.complete for c in record.coverage):
            p.append(f"{rid}: INCOMPLETE disposition but every registered date is complete")
    elif record.disposition is Disposition.EXTENSION_EVIDENCE_SUFFICIENT and (
        not record.evidence_complete
    ):
        p.append(f"{rid}: SUFFICIENT disposition contradicts evidence_complete=False")

    if record.required_complete_dates <= 0:
        p.append(f"{rid}: required_complete_dates must be positive")
    for cov in record.coverage:
        if cov.observed_cycles < 0 or cov.expected_cycles <= 0:
            p.append(f"{rid}: implausible cycle counts for {cov.station_code} {cov.local_date}")
        if cov.utc_end <= cov.utc_start:
            p.append(f"{rid}: {cov.station_code} {cov.local_date} ends before it starts")

    stored = record.content_hash
    if stored and stored != record.compute_hash():
        p.append(f"{rid}: content_hash mismatch -- the record was edited in place")
    return p


@dataclass(frozen=True)
class DispositionLedger:
    """Append-only view over the disposition ledger."""

    records: tuple[DispositionRecord, ...] = field(default_factory=tuple)

    @classmethod
    def load(cls, path: Path = DEFAULT_DISPOSITION_LEDGER) -> DispositionLedger:
        if not path.exists():
            return cls(records=())
        out: list[DispositionRecord] = []
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise DispositionError(f"{path}:{lineno} is not valid JSON: {exc}") from exc
            try:
                out.append(DispositionRecord.from_dict(raw))
            except DispositionError as exc:
                raise DispositionError(f"{path}:{lineno}: {exc}") from exc
        return cls(records=tuple(out))

    def validate(self) -> list[str]:
        problems: list[str] = []
        seen: set[str] = set()
        by_registration: dict[str, list[Disposition]] = {}
        for record in self.records:
            problems.extend(validate_record(record))
            if record.disposition_id in seen:
                problems.append(f"duplicate disposition_id: {record.disposition_id}")
            seen.add(record.disposition_id)
            by_registration.setdefault(record.extension_registration_id, []).append(
                record.disposition
            )
        for reg, dispositions in by_registration.items():
            if len(dispositions) > 1:
                problems.append(
                    f"{len(dispositions)} dispositions for {reg} -- a registration has exactly "
                    "one terminal disposition"
                )
        return problems

    def for_registration(self, registration_id: str) -> DispositionRecord | None:
        for record in self.records:
            if record.extension_registration_id == registration_id:
                return record
        return None

    def by_id(self, disposition_id: str) -> DispositionRecord | None:
        for record in self.records:
            if record.disposition_id == disposition_id:
                return record
        return None

    def append(
        self, record: DispositionRecord, path: Path = DEFAULT_DISPOSITION_LEDGER
    ) -> DispositionRecord:
        """Append ONE validated terminal disposition. Never rewrites a line."""
        existing = self.validate()
        if existing:
            raise DispositionError(
                f"refusing to append to a ledger that does not validate: {existing[:3]}"
            )
        stamped = record if record.content_hash else record.with_hash()
        problems = validate_record(stamped)
        if problems:
            raise DispositionError(f"record is invalid: {problems}")
        if self.by_id(stamped.disposition_id) is not None:
            raise DispositionError(f"disposition_id {stamped.disposition_id!r} already exists")
        prior = self.for_registration(stamped.extension_registration_id)
        if prior is not None:
            raise DispositionError(
                f"{stamped.extension_registration_id!r} already has terminal disposition "
                f"{prior.disposition} -- a registration is disposed exactly once and a "
                "conflicting disposition is refused"
            )
        candidate = DispositionLedger(records=(*self.records, stamped))
        problems = candidate.validate()
        if problems:
            raise DispositionError(f"append would invalidate the ledger: {problems}")

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(stamped.to_json_line() + "\n")
        return stamped


__all__ = [
    "DEFAULT_DISPOSITION_LEDGER",
    "FORBIDDEN_STATION_VERDICTS",
    "RE_REGISTRATION_PREREQUISITES",
    "Disposition",
    "DispositionError",
    "DispositionLedger",
    "DispositionRecord",
    "StationDateCoverage",
    "complete_date_count",
    "evidence_is_sufficient",
    "validate_record",
]
