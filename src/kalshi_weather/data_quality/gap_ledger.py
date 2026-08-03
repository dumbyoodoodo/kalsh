"""Permanent collection-gap ledger: an append-only record of known data-loss
intervals, their causes, and the evidence supporting each classification.

Why this exists. Collected history is irreplaceable, but *absence* in that
history is ambiguous: a missing row can mean the source never issued the
product, the host was off, the upstream API was down, the parser rejected a
payload, or persistence failed. Those are different facts with different
research consequences, and the distinction is only cheaply recoverable while
the operational evidence is still at hand. This ledger captures it then --
before any experiment outcome that might be tempted to use it exists.

What it must never do. The ledger records **operational evidence**. It does
not decide whether excluding data improves a research result. There is no
field for metric impact, profitability, station ranking, hypothesis support,
or model improvement, and ``from_dict`` rejects any attempt to add one
(``ForbiddenFieldError``). An exclusion-oriented ``research_treatment`` may
only ever rest on prospective methodological rules plus the evidence recorded
here -- never on a measured effect.

Append-only. Records are frozen dataclasses; a correction is a *new* record
naming the one it ``supersedes_gap_id``. Nothing is edited in place, and the
loader verifies that every stored content hash still matches the record it
covers, so a silent edit of an earlier line is detectable.

The critical asymmetry (see ``GapKind``): **missing local rows never by
themselves prove the source was silent.** Four 2026-07 weather "source gaps"
were later disproved -- the source had issued every product and a parser
defect had dropped them. Only positive source evidence may justify
``SOURCE_GAP``; without it the honest classification is a collection gap or
``AMBIGUOUS_LEGACY``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

LEDGER_VERSION = "1"

#: Canonical version-controlled location (Option A: append-only JSONL).
DEFAULT_LEDGER_PATH = Path("data/quality/permanent_gap_ledger.jsonl")


class GapLedgerError(ValueError):
    """A ledger record or file violates the schema or append-only rules."""


class ForbiddenFieldError(GapLedgerError):
    """An outcome-, performance-, or preference-shaped field was supplied.

    The ledger justifies exclusion through operational evidence only. A field
    describing what exclusion *does to a result* would invert that, so it is
    rejected at parse time rather than merely discouraged.
    """


# --- controlled vocabularies -------------------------------------------------


class Classification(StrEnum):
    """Primary cause of the gap."""

    HOST_UNAVAILABLE = "HOST_UNAVAILABLE"
    COLLECTOR_STOPPED = "COLLECTOR_STOPPED"
    COLLECTOR_CRASH = "COLLECTOR_CRASH"
    UPSTREAM_KALSHI_OUTAGE = "UPSTREAM_KALSHI_OUTAGE"
    UPSTREAM_WEATHER_SOURCE_OUTAGE = "UPSTREAM_WEATHER_SOURCE_OUTAGE"
    SOURCE_PRODUCT_NOT_ISSUED = "SOURCE_PRODUCT_NOT_ISSUED"
    REQUEST_FAILED = "REQUEST_FAILED"
    PARSER_FAILURE = "PARSER_FAILURE"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"
    DISCOVERY_OMISSION = "DISCOVERY_OMISSION"
    RATE_LIMIT_TRANSIENT = "RATE_LIMIT_TRANSIENT"
    LEGACY_UNKNOWN = "LEGACY_UNKNOWN"
    PARTIAL_CURRENT_DAY = "PARTIAL_CURRENT_DAY"
    OTHER_CONFIRMED = "OTHER_CONFIRMED"


class GapKind(StrEnum):
    """Which *category* of absence this is -- deliberately separate from cause.

    ``SOURCE_GAP`` requires positive evidence that the authoritative source did
    not issue the product. Missing local rows are never sufficient.
    """

    SOURCE_GAP = "SOURCE_GAP"
    COLLECTION_GAP = "COLLECTION_GAP"
    HOST_GAP = "HOST_GAP"
    UPSTREAM_OUTAGE = "UPSTREAM_OUTAGE"
    AMBIGUOUS_LEGACY = "AMBIGUOUS_LEGACY"


class Subsystem(StrEnum):
    """Collection streams, kept separate so one stream's outage never implies
    another's (a Kalshi outage is not a weather failure)."""

    WEATHER_OBSERVATIONS = "WEATHER_OBSERVATIONS"
    WEATHER_FORECASTS = "WEATHER_FORECASTS"
    KALSHI_MARKET_DISCOVERY = "KALSHI_MARKET_DISCOVERY"
    KALSHI_MARKET_SNAPSHOTS = "KALSHI_MARKET_SNAPSHOTS"
    KALSHI_ORDER_BOOKS = "KALSHI_ORDER_BOOKS"
    TRADES = "TRADES"
    CANDLES = "CANDLES"
    PAPER_VALIDATION = "PAPER_VALIDATION"
    OPERATIONAL_MONITORING = "OPERATIONAL_MONITORING"


class RecoveryState(StrEnum):
    NOT_RECOVERABLE = "NOT_RECOVERABLE"
    RECOVERABLE_NOT_YET_RECOVERED = "RECOVERABLE_NOT_YET_RECOVERED"
    PARTIALLY_RECOVERED = "PARTIALLY_RECOVERED"
    FULLY_RECOVERED = "FULLY_RECOVERED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


class Recoverability(StrEnum):
    """Whether the data *could* be recovered -- distinct from whether it was."""

    RECOVERABLE = "RECOVERABLE"
    NOT_RECOVERABLE = "NOT_RECOVERABLE"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ResearchTreatment(StrEnum):
    """Evidence-quality guidance only. Never a performance decision.

    ``RECOVERED_NOT_CONTEMPORANEOUS`` is the load-bearing one: a row recovered
    later carries a historical issuance time but was NOT knowable at that time,
    so it is valid for settlement reconstruction and invalid for
    point-in-time feature generation.
    """

    INCLUDE_WITH_ANNOTATION = "INCLUDE_WITH_ANNOTATION"
    EXCLUDE_FOR_MISSING_COLLECTION = "EXCLUDE_FOR_MISSING_COLLECTION"
    EXCLUDE_FOR_SOURCE_UNAVAILABILITY = "EXCLUDE_FOR_SOURCE_UNAVAILABILITY"
    RECOVERED_USE_AS_OBSERVED_AT = "RECOVERED_USE_AS_OBSERVED_AT"
    RECOVERED_NOT_CONTEMPORANEOUS = "RECOVERED_NOT_CONTEMPORANEOUS"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Confidence(StrEnum):
    CONFIRMED = "CONFIRMED"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SourceAvailability(StrEnum):
    """What is known about the upstream source during the interval."""

    CONFIRMED_ISSUED = "CONFIRMED_ISSUED"
    CONFIRMED_NOT_ISSUED = "CONFIRMED_NOT_ISSUED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidenceKind(StrEnum):
    COLLECTOR_RUN = "COLLECTOR_RUN"
    POLL_ATTEMPT = "POLL_ATTEMPT"
    RAW_PAYLOAD = "RAW_PAYLOAD"
    OBSERVATION_ROW = "OBSERVATION_ROW"
    SERVICE_LOG = "SERVICE_LOG"
    OBSERVATORY_FINDING = "OBSERVATORY_FINDING"
    SOURCE_ARCHIVE = "SOURCE_ARCHIVE"
    HTTP_STATUS = "HTTP_STATUS"
    PARSER_EXCEPTION = "PARSER_EXCEPTION"
    PERSISTENCE_EXCEPTION = "PERSISTENCE_EXCEPTION"
    HOST_EVIDENCE = "HOST_EVIDENCE"
    AUDIT_REPORT = "AUDIT_REPORT"


#: Evidence kinds that count as *collector* evidence -- anything describing the
#: platform's own state or processing. Parser and persistence exceptions belong
#: here: a payload the platform received and then failed to handle is
#: collector-side proof, not source-side.
_COLLECTOR_EVIDENCE = {
    EvidenceKind.COLLECTOR_RUN,
    EvidenceKind.POLL_ATTEMPT,
    EvidenceKind.SERVICE_LOG,
    EvidenceKind.HOST_EVIDENCE,
    EvidenceKind.OBSERVATORY_FINDING,
    EvidenceKind.PARSER_EXCEPTION,
    EvidenceKind.PERSISTENCE_EXCEPTION,
}
#: Evidence kinds that can speak to what the *source* did.
_SOURCE_EVIDENCE = {
    EvidenceKind.SOURCE_ARCHIVE,
    EvidenceKind.RAW_PAYLOAD,
    EvidenceKind.HTTP_STATUS,
    EvidenceKind.OBSERVATION_ROW,
}
#: Evidence kinds that can demonstrate a recovery actually happened.
_RECOVERY_EVIDENCE = {
    EvidenceKind.OBSERVATION_ROW,
    EvidenceKind.RAW_PAYLOAD,
    EvidenceKind.SOURCE_ARCHIVE,
    EvidenceKind.AUDIT_REPORT,
}

_RECOVERED_STATES = {RecoveryState.FULLY_RECOVERED, RecoveryState.PARTIALLY_RECOVERED}
_EXCLUSION_TREATMENTS = {
    ResearchTreatment.EXCLUDE_FOR_MISSING_COLLECTION,
    ResearchTreatment.EXCLUDE_FOR_SOURCE_UNAVAILABILITY,
}

#: Stems that mark a field as outcome-, performance-, or preference-driven.
#: Matched against snake_case *word parts* by prefix, not as raw substrings --
#: a substring test would flag innocent names ("edge" inside "l-edge-r_version")
#: and teach operators to route around the guard.
FORBIDDEN_FIELD_TOKENS = (
    "accuracy",
    "alpha",
    "brier",
    "calibration",
    "edge",
    "favorable",
    "improve",
    "loss",
    "metric",
    "outcome",
    "performance",
    "pnl",
    "preferred",
    "profit",
    "rank",
    "return",
    "score",
    "sharpe",
    "sharpness",
    "significan",
    "support",
    "verdict",
    "win",
)


def _forbidden_token(key: str) -> str | None:
    """The stem that makes ``key`` outcome-shaped, or ``None``.

    A key is forbidden when any of its snake_case parts *begins with* a stem,
    so ``model_improvement`` and ``station_ranking`` are caught while
    ``ledger_version`` and ``scope`` are not.
    """
    parts = [p for p in "".join(c if c.isalnum() else "_" for c in key.lower()).split("_") if p]
    for part in parts:
        for token in FORBIDDEN_FIELD_TOKENS:
            if part.startswith(token):
                return token
    return None


# --- records -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Evidence:
    """One reproducible pointer supporting the classification.

    ``reference`` must be a stable identifier or a re-runnable query -- a human
    narrative alone can never raise a record to ``CONFIRMED``.
    """

    kind: EvidenceKind
    reference: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": str(self.kind), "reference": self.reference, "detail": self.detail}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Evidence:
        unknown = set(raw) - {"kind", "reference", "detail"}
        if unknown:
            raise GapLedgerError(f"unknown evidence field(s): {sorted(unknown)}")
        try:
            kind = EvidenceKind(raw["kind"])
        except (KeyError, ValueError) as exc:
            raise GapLedgerError(f"invalid evidence kind: {raw.get('kind')!r}") from exc
        reference = str(raw.get("reference", "")).strip()
        if not reference:
            raise GapLedgerError("evidence requires a non-empty reference")
        return cls(kind=kind, reference=reference, detail=str(raw.get("detail", "")))


@dataclass(frozen=True, slots=True)
class GapRecord:
    """One immutable ledger entry. Corrections append a superseding record."""

    gap_id: str
    created_at: datetime
    created_by: str
    classification: Classification
    gap_kind: GapKind
    subsystems: tuple[Subsystem, ...]
    environment: str
    start_at: datetime
    end_at: datetime
    source_availability: SourceAvailability
    recovery_state: RecoveryState
    recoverability: Recoverability
    research_treatment: ResearchTreatment
    confidence: Confidence
    reason_code: str
    human_note: str
    evidence: tuple[Evidence, ...]
    ledger_version: str = LEDGER_VERSION
    scope: str = ""
    affected_entity_count: int = 0
    collector_run_refs: tuple[str, ...] = ()
    poll_attempt_refs: tuple[str, ...] = ()
    raw_payload_refs: tuple[str, ...] = ()
    instantaneous: bool = False
    exclusion_eligible: bool = False
    supersedes_gap_id: str | None = None
    related_gap_ids: tuple[str, ...] = ()
    content_hash: str = ""

    # -- serialization --------------------------------------------------------

    def _payload(self) -> dict[str, Any]:
        """Canonical hashable payload: every field except ``content_hash``."""
        data = asdict(self)
        data.pop("content_hash", None)
        data["created_at"] = _iso(self.created_at)
        data["start_at"] = _iso(self.start_at)
        data["end_at"] = _iso(self.end_at)
        data["subsystems"] = [str(s) for s in self.subsystems]
        data["evidence"] = [e.to_dict() for e in self.evidence]
        for key in (
            "classification",
            "gap_kind",
            "source_availability",
            "recovery_state",
            "recoverability",
            "research_treatment",
            "confidence",
        ):
            data[key] = str(data[key])
        for key in (
            "collector_run_refs",
            "poll_attempt_refs",
            "raw_payload_refs",
            "related_gap_ids",
        ):
            data[key] = list(data[key])
        return data

    def compute_hash(self) -> str:
        """Deterministic sha256 over the canonical payload (sorted keys)."""
        canonical = json.dumps(self._payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def with_hash(self) -> GapRecord:
        return replace(self, content_hash=self.compute_hash())

    def to_dict(self) -> dict[str, Any]:
        data = self._payload()
        data["content_hash"] = self.content_hash or self.compute_hash()
        return data

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GapRecord:
        for key in raw:
            token = _forbidden_token(key)
            if token is not None:
                raise ForbiddenFieldError(
                    f"field {key!r} contains forbidden token {token!r}: the gap ledger "
                    "records operational evidence only, never outcome or performance "
                    "information"
                )
        allowed = set(cls.__dataclass_fields__)
        unknown = set(raw) - allowed
        if unknown:
            raise GapLedgerError(f"unknown field(s): {sorted(unknown)}")
        missing = {
            "gap_id",
            "created_at",
            "created_by",
            "classification",
            "gap_kind",
            "subsystems",
            "environment",
            "start_at",
            "end_at",
            "source_availability",
            "recovery_state",
            "recoverability",
            "research_treatment",
            "confidence",
            "reason_code",
            "evidence",
        } - set(raw)
        if missing:
            raise GapLedgerError(f"missing required field(s): {sorted(missing)}")
        try:
            record = cls(
                gap_id=str(raw["gap_id"]),
                created_at=_parse_dt(raw["created_at"], "created_at"),
                created_by=str(raw["created_by"]),
                classification=Classification(raw["classification"]),
                gap_kind=GapKind(raw["gap_kind"]),
                subsystems=tuple(Subsystem(s) for s in raw["subsystems"]),
                environment=str(raw["environment"]),
                start_at=_parse_dt(raw["start_at"], "start_at"),
                end_at=_parse_dt(raw["end_at"], "end_at"),
                source_availability=SourceAvailability(raw["source_availability"]),
                recovery_state=RecoveryState(raw["recovery_state"]),
                recoverability=Recoverability(raw["recoverability"]),
                research_treatment=ResearchTreatment(raw["research_treatment"]),
                confidence=Confidence(raw["confidence"]),
                reason_code=str(raw["reason_code"]),
                human_note=str(raw.get("human_note", "")),
                evidence=tuple(Evidence.from_dict(e) for e in raw["evidence"]),
                ledger_version=str(raw.get("ledger_version", LEDGER_VERSION)),
                scope=str(raw.get("scope", "")),
                affected_entity_count=int(raw.get("affected_entity_count", 0)),
                collector_run_refs=tuple(str(x) for x in raw.get("collector_run_refs", ())),
                poll_attempt_refs=tuple(str(x) for x in raw.get("poll_attempt_refs", ())),
                raw_payload_refs=tuple(str(x) for x in raw.get("raw_payload_refs", ())),
                instantaneous=bool(raw.get("instantaneous", False)),
                exclusion_eligible=bool(raw.get("exclusion_eligible", False)),
                supersedes_gap_id=(
                    str(raw["supersedes_gap_id"]) if raw.get("supersedes_gap_id") else None
                ),
                related_gap_ids=tuple(str(x) for x in raw.get("related_gap_ids", ())),
                content_hash=str(raw.get("content_hash", "")),
            )
        except ValueError as exc:
            if isinstance(exc, GapLedgerError):
                raise
            raise GapLedgerError(f"invalid record {raw.get('gap_id')!r}: {exc}") from exc
        return record


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_dt(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise GapLedgerError(f"{field_name} is not a valid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise GapLedgerError(f"{field_name} must be timezone-aware UTC: {value!r}")
    return parsed.astimezone(UTC)


# --- per-record validation ---------------------------------------------------


def validate_record(record: GapRecord) -> list[str]:
    """Structural and semantic problems with one record (empty == valid)."""
    problems: list[str] = []
    rid = record.gap_id

    if not rid.strip():
        problems.append("gap_id must be non-empty")
    if not record.environment.strip():
        problems.append(f"{rid}: environment is required")
    if not record.subsystems:
        problems.append(f"{rid}: at least one subsystem is required")
    if not record.reason_code.strip():
        problems.append(f"{rid}: reason_code is required")
    if not record.created_by.strip():
        problems.append(f"{rid}: created_by is required")

    if record.instantaneous:
        if record.end_at < record.start_at:
            problems.append(f"{rid}: instantaneous event may not end before it starts")
    elif record.start_at >= record.end_at:
        problems.append(
            f"{rid}: start_at must precede end_at "
            "(set instantaneous=true for a point-in-time event)"
        )

    if not record.evidence:
        problems.append(f"{rid}: at least one evidence reference is required")

    kinds = {e.kind for e in record.evidence}

    # A narrative alone can never be CONFIRMED.
    if record.confidence is Confidence.CONFIRMED and not kinds:
        problems.append(f"{rid}: CONFIRMED requires at least one evidence reference")

    # Recovery claims need recovery evidence.
    if record.recovery_state in _RECOVERED_STATES and not (kinds & _RECOVERY_EVIDENCE):
        problems.append(
            f"{rid}: recovery_state={record.recovery_state} requires recovery evidence "
            f"(one of {sorted(str(k) for k in _RECOVERY_EVIDENCE)})"
        )

    # Source-gap claims need positive source evidence -- missing local rows are
    # never sufficient (four 2026-07 'source gaps' were disproved this way).
    if record.gap_kind is GapKind.SOURCE_GAP:
        if not (kinds & _SOURCE_EVIDENCE):
            problems.append(f"{rid}: SOURCE_GAP requires source evidence")
        if record.source_availability is not SourceAvailability.CONFIRMED_NOT_ISSUED:
            problems.append(f"{rid}: SOURCE_GAP requires source_availability=CONFIRMED_NOT_ISSUED")

    # Collection/host/upstream gaps need platform-side evidence.
    if record.gap_kind in (
        GapKind.COLLECTION_GAP,
        GapKind.HOST_GAP,
        GapKind.UPSTREAM_OUTAGE,
    ) and not (kinds & _COLLECTOR_EVIDENCE):
        problems.append(f"{rid}: {record.gap_kind} requires collector-side evidence")

    # Classification / recovery-state compatibility.
    if (
        record.classification is Classification.SOURCE_PRODUCT_NOT_ISSUED
        and record.recovery_state in _RECOVERED_STATES
    ):
        problems.append(
            f"{rid}: SOURCE_PRODUCT_NOT_ISSUED cannot be recovered -- if data was later "
            "obtained, the source did issue it and the cause is not source silence"
        )
    if (
        record.recovery_state is RecoveryState.FULLY_RECOVERED
        and record.recoverability is Recoverability.NOT_RECOVERABLE
    ):
        problems.append(f"{rid}: FULLY_RECOVERED contradicts recoverability=NOT_RECOVERABLE")
    if record.recovery_state in _RECOVERED_STATES and record.research_treatment in (
        ResearchTreatment.EXCLUDE_FOR_MISSING_COLLECTION,
        ResearchTreatment.EXCLUDE_FOR_SOURCE_UNAVAILABILITY,
    ):
        problems.append(
            f"{rid}: a recovered gap should carry a RECOVERED_* treatment, not an exclusion"
        )

    # LOW confidence can never authorize exclusion.
    if record.exclusion_eligible and record.confidence is Confidence.LOW:
        problems.append(f"{rid}: LOW-confidence records cannot authorize exclusion")
    if record.research_treatment in _EXCLUSION_TREATMENTS and record.confidence is Confidence.LOW:
        problems.append(
            f"{rid}: exclusion treatment {record.research_treatment} requires confidence above LOW"
        )

    if record.affected_entity_count < 0:
        problems.append(f"{rid}: affected_entity_count must be >= 0")

    stored = record.content_hash
    if stored and stored != record.compute_hash():
        problems.append(
            f"{rid}: content_hash mismatch -- record was edited in place "
            f"(stored {stored[:12]}..., computed {record.compute_hash()[:12]}...)"
        )
    return problems


# --- ledger ------------------------------------------------------------------


@dataclass(frozen=True)
class GapLedger:
    """An immutable in-memory view over the append-only ledger file.

    Pure: construction and every query below are side-effect free and touch no
    database. This is the interface future experiments may consult; it
    deliberately exposes no way to mutate anything.
    """

    records: tuple[GapRecord, ...] = field(default_factory=tuple)

    # -- integrity ------------------------------------------------------------

    def validate(self) -> list[str]:
        """All problems across the whole ledger (empty == valid)."""
        problems: list[str] = []
        seen: dict[str, GapRecord] = {}
        for record in self.records:
            problems.extend(validate_record(record))
            if record.gap_id in seen:
                problems.append(f"duplicate gap_id: {record.gap_id}")
            seen[record.gap_id] = record

        for record in self.records:
            target = record.supersedes_gap_id
            if target is None:
                continue
            if target not in seen:
                problems.append(f"{record.gap_id}: supersedes unknown gap_id {target!r}")
            elif target == record.gap_id:
                problems.append(f"{record.gap_id}: cannot supersede itself")
            for related in record.related_gap_ids:
                if related not in seen:
                    problems.append(f"{record.gap_id}: unknown related gap_id {related!r}")

        problems.extend(self._supersede_cycles(seen))
        problems.extend(self._unlinked_duplicates())
        return problems

    def _supersede_cycles(self, index: dict[str, GapRecord]) -> list[str]:
        problems: list[str] = []
        for start in index:
            seen: set[str] = set()
            cursor: str | None = start
            while cursor is not None and cursor in index:
                if cursor in seen:
                    problems.append(f"supersede cycle involving {sorted(seen)}")
                    break
                seen.add(cursor)
                cursor = index[cursor].supersedes_gap_id
        # one message per distinct cycle
        return sorted(set(problems))

    def _unlinked_duplicates(self) -> list[str]:
        """Two records with identical scope, cause, and overlapping interval must
        be explicitly linked (superseding or related), else one is a stray copy."""
        problems: list[str] = []
        records = list(self.records)
        for i, a in enumerate(records):
            for b in records[i + 1 :]:
                same = (
                    a.classification is b.classification
                    and a.scope == b.scope
                    and set(a.subsystems) == set(b.subsystems)
                    and a.environment == b.environment
                )
                overlaps = a.start_at < b.end_at and b.start_at < a.end_at
                linked = (
                    b.supersedes_gap_id == a.gap_id
                    or a.supersedes_gap_id == b.gap_id
                    or b.gap_id in a.related_gap_ids
                    or a.gap_id in b.related_gap_ids
                )
                if same and overlaps and not linked:
                    problems.append(
                        f"unlinked duplicate: {a.gap_id} and {b.gap_id} share cause, scope, "
                        "and interval but neither supersedes nor references the other"
                    )
        return problems

    # -- pure query interface (research integration boundary) -----------------

    def superseded_ids(self) -> frozenset[str]:
        return frozenset(r.supersedes_gap_id for r in self.records if r.supersedes_gap_id)

    def active_records(self) -> tuple[GapRecord, ...]:
        """Records not replaced by a later amendment."""
        dead = self.superseded_ids()
        return tuple(r for r in self.records if r.gap_id not in dead)

    def by_id(self, gap_id: str) -> GapRecord | None:
        for record in self.records:
            if record.gap_id == gap_id:
                return record
        return None

    def overlapping(
        self,
        moment: datetime,
        *,
        subsystem: Subsystem | None = None,
        min_confidence: Confidence | None = Confidence.CONFIRMED,
    ) -> tuple[GapRecord, ...]:
        """Active records whose interval contains ``moment``.

        ``subsystem`` scopes the answer, so a Kalshi outage never reports
        against a weather query. ``min_confidence=None`` disables the filter.
        """
        if moment.tzinfo is None:
            raise GapLedgerError("moment must be timezone-aware UTC")
        moment = moment.astimezone(UTC)
        ranked = {
            Confidence.CONFIRMED: 3,
            Confidence.HIGH: 2,
            Confidence.MEDIUM: 1,
            Confidence.LOW: 0,
        }
        floor = ranked[min_confidence] if min_confidence is not None else -1
        out = []
        for record in self.active_records():
            if not (record.start_at <= moment <= record.end_at):
                continue
            if subsystem is not None and subsystem not in record.subsystems:
                continue
            if ranked[record.confidence] < floor:
                continue
            out.append(record)
        return tuple(out)

    def contemporaneous_use_allowed(self, moment: datetime, subsystem: Subsystem) -> bool:
        """Whether data at ``moment`` may be used as point-in-time evidence.

        False whenever a confirmed gap covers the moment and its treatment says
        the data either was not collected or was only recovered afterwards --
        a recovered row's historical issuance time never makes it
        contemporaneously knowable.
        """
        blocking = {
            ResearchTreatment.EXCLUDE_FOR_MISSING_COLLECTION,
            ResearchTreatment.EXCLUDE_FOR_SOURCE_UNAVAILABILITY,
            ResearchTreatment.RECOVERED_NOT_CONTEMPORANEOUS,
            ResearchTreatment.INSUFFICIENT_EVIDENCE,
        }
        return not any(
            r.research_treatment in blocking for r in self.overlapping(moment, subsystem=subsystem)
        )

    def annotations_for(self, moment: datetime, subsystem: Subsystem) -> tuple[str, ...]:
        """Human-readable annotations applying at ``moment`` for ``subsystem``."""
        return tuple(
            f"{r.gap_id}: {r.classification} / {r.research_treatment} — {r.reason_code}"
            for r in self.overlapping(moment, subsystem=subsystem)
        )

    # -- persistence ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path = DEFAULT_LEDGER_PATH) -> GapLedger:
        """Read the append-only JSONL. A missing file is an empty ledger."""
        if not path.exists():
            return cls(records=())
        records: list[GapRecord] = []
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise GapLedgerError(f"{path}:{lineno} is not valid JSON: {exc}") from exc
            if not isinstance(raw, dict):
                raise GapLedgerError(f"{path}:{lineno} is not a JSON object")
            try:
                records.append(GapRecord.from_dict(raw))
            except GapLedgerError as exc:
                raise GapLedgerError(f"{path}:{lineno}: {exc}") from exc
        return cls(records=tuple(records))

    def append(self, record: GapRecord, path: Path = DEFAULT_LEDGER_PATH) -> GapRecord:
        """Append one validated record. Never rewrites an existing line.

        Refuses on: a duplicate ``gap_id``, a record that fails validation, or a
        ledger whose existing lines no longer verify (which would mean an
        earlier record had been edited in place).
        """
        existing_problems = self.validate()
        if existing_problems:
            raise GapLedgerError(
                f"refusing to append to a ledger that does not validate: {existing_problems[:3]}"
            )
        stamped = record if record.content_hash else record.with_hash()
        problems = validate_record(stamped)
        if problems:
            raise GapLedgerError(f"record is invalid: {problems}")
        if self.by_id(stamped.gap_id) is not None:
            raise GapLedgerError(
                f"gap_id {stamped.gap_id!r} already exists -- corrections must append a "
                "NEW record with supersedes_gap_id, never reuse an id"
            )
        if stamped.supersedes_gap_id and self.by_id(stamped.supersedes_gap_id) is None:
            raise GapLedgerError(f"supersedes unknown gap_id {stamped.supersedes_gap_id!r}")
        candidate = GapLedger(records=(*self.records, stamped))
        problems = candidate.validate()
        if problems:
            raise GapLedgerError(f"append would invalidate the ledger: {problems}")

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(stamped.to_json_line() + "\n")
        return stamped

    def summary(self) -> dict[str, Any]:
        """Counts only -- no outcome, metric, or ranking information."""
        active = self.active_records()
        by_field: dict[str, dict[str, int]] = {}
        getters: tuple[tuple[str, Callable[[GapRecord], str]], ...] = (
            ("classification", lambda r: str(r.classification)),
            ("gap_kind", lambda r: str(r.gap_kind)),
            ("recovery_state", lambda r: str(r.recovery_state)),
            ("research_treatment", lambda r: str(r.research_treatment)),
            ("confidence", lambda r: str(r.confidence)),
        )
        for name, getter in getters:
            counts: dict[str, int] = {}
            for record in active:
                counts[getter(record)] = counts.get(getter(record), 0) + 1
            by_field[name] = dict(sorted(counts.items()))
        subsystem_counts: dict[str, int] = {}
        for record in active:
            for subsystem in record.subsystems:
                subsystem_counts[str(subsystem)] = subsystem_counts.get(str(subsystem), 0) + 1
        return {
            "total_records": len(self.records),
            "active_records": len(active),
            "superseded_records": len(self.superseded_ids()),
            "by": by_field,
            "by_subsystem": dict(sorted(subsystem_counts.items())),
            "earliest_start": _iso(min(r.start_at for r in active)) if active else None,
            "latest_end": _iso(max(r.end_at for r in active)) if active else None,
        }
