"""Append-only deployment-anchor ledger for the SEA/PHX/MIA pilot extension.

``deployment_validated_at`` is the hinge the whole extension hangs on: it fixes
the first included local date and therefore the review gate. If it can be
guessed, backdated, or half-satisfied, the extension window is not evidence --
it is a preference. So every condition ADR 0024 registered is checked here, and
there is deliberately **no override, force, backdate, or manual-anchor path**.

Three insufficiencies are called out explicitly because each is a plausible
shortcut: a migration timestamp alone is not an anchor, a collector start
timestamp alone is not an anchor, and a partially completed cycle is not an
anchor. The anchor is the moment the LAST required condition became true.

Append-only, mirroring the permanent gap ledger: frozen records, deterministic
content hash, prior bytes never rewritten, duplicate ids refused, and a
malformed existing ledger blocks any new append.

Pure except for reading/appending its own JSONL file. It queries no attempt
outcome and no research data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from kalshi_weather.research.station_pilot_extension import (
    EXTENSION_REGISTRATION_ID,
    REQUIRED_COMPLETE_DATES,
    STATION_TIMEZONES,
    ExtensionState,
    extension_review_gate,
    included_local_dates,
    local_date_end_utc,
)
from kalshi_weather.research.station_pilot_extension import (
    REGISTRATION as EXTENSION_REGISTRATION,
)

#: Canonical location. Distinct from the research ledger and the permanent gap
#: ledger: this records an OPERATIONAL deployment fact, not a research result
#: or a data-loss classification, and forcing it into either would blur both.
DEFAULT_DEPLOYMENT_LEDGER = Path("data/operations/station_pilot_extension_deployments.jsonl")

REQUIRED_MIGRATION_REVISION = "0012"


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class DeploymentLedgerError(ValueError):
    """A deployment record or ledger violates the append-only contract."""


@dataclass(frozen=True, slots=True)
class StationCalendar:
    """One station's derived extension calendar."""

    station_code: str
    timezone_name: str
    first_included_date: date
    included_dates: tuple[date, ...]
    seventh_date_end_utc: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "station_code": self.station_code,
            "timezone": self.timezone_name,
            "first_included_date": self.first_included_date.isoformat(),
            "included_dates": [d.isoformat() for d in self.included_dates],
            "seventh_date_end_utc": _iso(self.seventh_date_end_utc),
        }


def derive_calendars(deployment_validated_at: datetime) -> tuple[StationCalendar, ...]:
    """Per-station calendars, delegating entirely to the frozen ADR 0024 logic.

    No timezone or gate arithmetic is duplicated here -- the registration module
    is the single source of truth, so a change there cannot silently diverge
    from what a recorded anchor says.
    """
    calendars = []
    for code, tz in STATION_TIMEZONES:
        dates = included_local_dates(deployment_validated_at, tz, count=REQUIRED_COMPLETE_DATES)
        calendars.append(
            StationCalendar(
                station_code=code,
                timezone_name=tz,
                first_included_date=dates[0],
                included_dates=dates,
                seventh_date_end_utc=local_date_end_utc(dates[-1], tz),
            )
        )
    return tuple(calendars)


def derive_review_gate(deployment_validated_at: datetime) -> datetime:
    """The registered gate: the LATEST seventh-date end across stations."""
    return extension_review_gate(deployment_validated_at)


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    """One immutable deployment-anchor record."""

    deployment_id: str
    extension_registration_id: str
    registration_hash: str
    deployed_commit: str
    migration_revision: str
    collector_pid: int
    collector_loaded_commit: str
    collector_git_dirty: bool
    single_collector_confirmed: bool
    migration_applied_at: datetime
    collector_started_at: datetime
    validated_cycle_id: int
    validated_cycle_started_at: datetime
    validated_cycle_completed_at: datetime
    expected_attempts: int
    actual_attempts: int
    reconciliation_status: CheckStatus
    raw_payload_provenance_status: CheckStatus
    critical_observatory_findings: int
    final_condition_confirmed_at: datetime
    deployment_validated_at: datetime
    recorded_at: datetime
    recorded_by: str
    evidence_references: tuple[str, ...] = ()
    content_hash: str = ""

    # -- derived calendar -----------------------------------------------------

    def calendars(self) -> tuple[StationCalendar, ...]:
        return derive_calendars(self.deployment_validated_at)

    def review_gate(self) -> datetime:
        return derive_review_gate(self.deployment_validated_at)

    def resulting_extension_state(self) -> ExtensionState:
        return ExtensionState.COLLECTING_EXTENSION

    # -- serialization --------------------------------------------------------

    def _payload(self) -> dict[str, Any]:
        cals = self.calendars()
        return {
            "deployment_id": self.deployment_id,
            "extension_registration_id": self.extension_registration_id,
            "registration_hash": self.registration_hash,
            "deployed_commit": self.deployed_commit,
            "migration_revision": self.migration_revision,
            "collector_pid": self.collector_pid,
            "collector_loaded_commit": self.collector_loaded_commit,
            "collector_git_dirty": self.collector_git_dirty,
            "single_collector_confirmed": self.single_collector_confirmed,
            "migration_applied_at": _iso(self.migration_applied_at),
            "collector_started_at": _iso(self.collector_started_at),
            "validated_cycle_id": self.validated_cycle_id,
            "validated_cycle_started_at": _iso(self.validated_cycle_started_at),
            "validated_cycle_completed_at": _iso(self.validated_cycle_completed_at),
            "expected_attempts": self.expected_attempts,
            "actual_attempts": self.actual_attempts,
            "reconciliation_status": str(self.reconciliation_status),
            "raw_payload_provenance_status": str(self.raw_payload_provenance_status),
            "critical_observatory_findings": self.critical_observatory_findings,
            "final_condition_confirmed_at": _iso(self.final_condition_confirmed_at),
            "deployment_validated_at": _iso(self.deployment_validated_at),
            "recorded_at": _iso(self.recorded_at),
            "recorded_by": self.recorded_by,
            "evidence_references": list(self.evidence_references),
            "per_station_first_included_date": {
                c.station_code: c.first_included_date.isoformat() for c in cals
            },
            "per_station_included_dates": {
                c.station_code: [d.isoformat() for d in c.included_dates] for c in cals
            },
            "per_station_seventh_date_end_utc": {
                c.station_code: _iso(c.seventh_date_end_utc) for c in cals
            },
            "extension_review_gate": _iso(self.review_gate()),
            "resulting_extension_state": str(self.resulting_extension_state()),
        }

    def compute_hash(self) -> str:
        canonical = json.dumps(self._payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def with_hash(self) -> DeploymentRecord:
        from dataclasses import replace

        return replace(self, content_hash=self.compute_hash())

    def to_dict(self) -> dict[str, Any]:
        data = self._payload()
        data["content_hash"] = self.content_hash or self.compute_hash()
        return data

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DeploymentRecord:
        try:
            return cls(
                deployment_id=str(raw["deployment_id"]),
                extension_registration_id=str(raw["extension_registration_id"]),
                registration_hash=str(raw["registration_hash"]),
                deployed_commit=str(raw["deployed_commit"]),
                migration_revision=str(raw["migration_revision"]),
                collector_pid=int(raw["collector_pid"]),
                collector_loaded_commit=str(raw["collector_loaded_commit"]),
                collector_git_dirty=bool(raw["collector_git_dirty"]),
                single_collector_confirmed=bool(raw.get("single_collector_confirmed", False)),
                migration_applied_at=_parse(raw["migration_applied_at"], "migration_applied_at"),
                collector_started_at=_parse(raw["collector_started_at"], "collector_started_at"),
                validated_cycle_id=int(raw["validated_cycle_id"]),
                validated_cycle_started_at=_parse(
                    raw["validated_cycle_started_at"], "validated_cycle_started_at"
                ),
                validated_cycle_completed_at=_parse(
                    raw["validated_cycle_completed_at"], "validated_cycle_completed_at"
                ),
                expected_attempts=int(raw["expected_attempts"]),
                actual_attempts=int(raw["actual_attempts"]),
                reconciliation_status=CheckStatus(raw["reconciliation_status"]),
                raw_payload_provenance_status=CheckStatus(raw["raw_payload_provenance_status"]),
                critical_observatory_findings=int(raw["critical_observatory_findings"]),
                final_condition_confirmed_at=_parse(
                    raw["final_condition_confirmed_at"], "final_condition_confirmed_at"
                ),
                deployment_validated_at=_parse(
                    raw["deployment_validated_at"], "deployment_validated_at"
                ),
                recorded_at=_parse(raw["recorded_at"], "recorded_at"),
                recorded_by=str(raw["recorded_by"]),
                evidence_references=tuple(str(x) for x in raw.get("evidence_references", ())),
                content_hash=str(raw.get("content_hash", "")),
            )
        except KeyError as exc:
            raise DeploymentLedgerError(f"missing required field: {exc}") from exc
        except ValueError as exc:
            if isinstance(exc, DeploymentLedgerError):
                raise
            raise DeploymentLedgerError(f"invalid deployment record: {exc}") from exc


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise DeploymentLedgerError(f"{field_name} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise DeploymentLedgerError(f"{field_name} must be timezone-aware UTC")
    return parsed.astimezone(UTC)


# --- invariants --------------------------------------------------------------


def validate_record(record: DeploymentRecord) -> list[str]:
    """Every registered condition. Empty list == a valid anchor.

    Fails closed: a condition that cannot be evaluated is a failure, never a
    pass. There is no override.
    """
    p: list[str] = []
    rid = record.deployment_id

    if not rid.strip():
        p.append("deployment_id is required")
    if record.extension_registration_id != EXTENSION_REGISTRATION_ID:
        p.append(f"{rid}: extension_registration_id must be {EXTENSION_REGISTRATION_ID!r}")
    expected_hash = EXTENSION_REGISTRATION.registration_hash()
    if record.registration_hash != expected_hash:
        p.append(f"{rid}: registration_hash does not match the frozen registration")
    if record.migration_revision != REQUIRED_MIGRATION_REVISION:
        p.append(f"{rid}: migration_revision must be {REQUIRED_MIGRATION_REVISION!r}")
    if record.deployed_commit != record.collector_loaded_commit:
        p.append(
            f"{rid}: collector is not running the deployed commit "
            "(deployed_commit != collector_loaded_commit)"
        )
    if record.collector_git_dirty:
        p.append(f"{rid}: collector_git_dirty must be false")
    if record.collector_pid <= 0:
        p.append(f"{rid}: a collector PID is required")
    if not record.single_collector_confirmed:
        p.append(f"{rid}: exactly one collector must be confirmed in supporting evidence")
    if record.expected_attempts <= 0:
        p.append(f"{rid}: expected_attempts must be positive")
    if record.actual_attempts != record.expected_attempts:
        p.append(
            f"{rid}: actual_attempts {record.actual_attempts} != expected "
            f"{record.expected_attempts}"
        )
    if record.reconciliation_status is not CheckStatus.PASS:
        p.append(f"{rid}: reconciliation_status must be PASS")
    if record.raw_payload_provenance_status is not CheckStatus.PASS:
        p.append(f"{rid}: raw_payload_provenance_status must be PASS")
    if record.critical_observatory_findings != 0:
        p.append(f"{rid}: critical_observatory_findings must be 0")

    # Ordering. A migration timestamp alone, a collector start alone, or a
    # partial cycle are each insufficient -- the anchor is the moment the LAST
    # condition became true.
    if record.validated_cycle_completed_at < record.validated_cycle_started_at:
        p.append(f"{rid}: validated cycle completed before it started")
    if record.final_condition_confirmed_at < record.validated_cycle_completed_at:
        p.append(
            f"{rid}: final_condition_confirmed_at precedes cycle completion — a partial "
            "cycle is not an anchor"
        )
    if record.deployment_validated_at != record.final_condition_confirmed_at:
        p.append(
            f"{rid}: deployment_validated_at must EQUAL final_condition_confirmed_at "
            "(no backdating, no independent choice)"
        )
    if record.deployment_validated_at < record.migration_applied_at:
        p.append(f"{rid}: deployment_validated_at precedes migration_applied_at")
    if record.deployment_validated_at < record.collector_started_at:
        p.append(f"{rid}: deployment_validated_at precedes collector_started_at")
    if record.recorded_at < record.deployment_validated_at:
        p.append(f"{rid}: recorded_at precedes deployment_validated_at (backdated record)")
    if not record.recorded_by.strip():
        p.append(f"{rid}: recorded_by is required")
    if not record.evidence_references:
        p.append(f"{rid}: at least one evidence reference is required")

    stored = record.content_hash
    if stored and stored != record.compute_hash():
        p.append(f"{rid}: content_hash mismatch — the record was edited in place")
    return p


# --- append-only ledger ------------------------------------------------------


@dataclass(frozen=True)
class DeploymentLedger:
    """Immutable view over the append-only deployment ledger."""

    records: tuple[DeploymentRecord, ...] = field(default_factory=tuple)

    @classmethod
    def load(cls, path: Path = DEFAULT_DEPLOYMENT_LEDGER) -> DeploymentLedger:
        if not path.exists():
            return cls(records=())
        records: list[DeploymentRecord] = []
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise DeploymentLedgerError(f"{path}:{lineno} is not valid JSON: {exc}") from exc
            try:
                records.append(DeploymentRecord.from_dict(raw))
            except DeploymentLedgerError as exc:
                raise DeploymentLedgerError(f"{path}:{lineno}: {exc}") from exc
        return cls(records=tuple(records))

    def verify_ledger(self) -> list[str]:
        problems: list[str] = []
        seen: set[str] = set()
        for record in self.records:
            problems.extend(validate_record(record))
            if record.deployment_id in seen:
                problems.append(f"duplicate deployment_id: {record.deployment_id}")
            seen.add(record.deployment_id)
        # ADR 0024 registers ONE extension; a second anchor for it would silently
        # re-date the window, so it is refused rather than treated as an amendment.
        by_registration: dict[str, int] = {}
        for record in self.records:
            key = record.extension_registration_id
            by_registration[key] = by_registration.get(key, 0) + 1
        for key, count in by_registration.items():
            if count > 1:
                problems.append(
                    f"{count} anchors for registration {key} — ADR 0024 registers one "
                    "extension; a redeployment amendment is not permitted"
                )
        return problems

    def find_by_deployment_id(self, deployment_id: str) -> DeploymentRecord | None:
        for record in self.records:
            if record.deployment_id == deployment_id:
                return record
        return None

    def find_for_extension_registration(self, registration_id: str) -> tuple[DeploymentRecord, ...]:
        return tuple(r for r in self.records if r.extension_registration_id == registration_id)

    def list_records(self) -> tuple[DeploymentRecord, ...]:
        return self.records

    def append_record(
        self, record: DeploymentRecord, path: Path = DEFAULT_DEPLOYMENT_LEDGER
    ) -> DeploymentRecord:
        """Append ONE validated record. Never rewrites an existing line."""
        existing_problems = self.verify_ledger()
        if existing_problems:
            raise DeploymentLedgerError(
                f"refusing to append to a ledger that does not verify: {existing_problems[:3]}"
            )
        stamped = record if record.content_hash else record.with_hash()
        problems = validate_record(stamped)
        if problems:
            raise DeploymentLedgerError(f"record is invalid: {problems}")
        if self.find_by_deployment_id(stamped.deployment_id) is not None:
            raise DeploymentLedgerError(f"deployment_id {stamped.deployment_id!r} already exists")
        if self.find_for_extension_registration(stamped.extension_registration_id):
            raise DeploymentLedgerError(
                f"an anchor already exists for {stamped.extension_registration_id!r}; "
                "ADR 0024 registers one extension and permits no redeployment amendment"
            )
        candidate = DeploymentLedger(records=(*self.records, stamped))
        problems = candidate.verify_ledger()
        if problems:
            raise DeploymentLedgerError(f"append would invalidate the ledger: {problems}")

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(stamped.to_json_line() + "\n")
        return stamped


__all__ = [
    "DEFAULT_DEPLOYMENT_LEDGER",
    "REQUIRED_MIGRATION_REVISION",
    "CheckStatus",
    "DeploymentLedger",
    "DeploymentLedgerError",
    "DeploymentRecord",
    "ExtensionState",
    "StationCalendar",
    "derive_calendars",
    "derive_review_gate",
    "validate_record",
]
