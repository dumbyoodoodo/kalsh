"""Station-level weather collection-attempt evidence: vocabulary, invariants,
reconciliation, and the pure station-summary API.

The defect this closes. ``WeatherCycleStats`` records only cycle-level scalars.
``invalid_items`` and ``errors`` have no station dimension, so a parser
rejection or an unavailable source could not be attributed to the station that
caused it -- which is why the 2026-08-04 SEA/PHX/MIA pilot review had to mark
``parser_failures`` and ``source_unavailable_attempts`` ``evidence_unavailable``
and could not issue KEEP for any station.

Note what was and was not missing. Successes were already attributable:
``weather_observations`` rows carry ``station_id``. It is FAILURES AND
NON-EVENTS that left no trace -- a request returning nothing, a source that
never answered, a body that failed to parse, a save that raised. Those produce
no row anywhere, so "no observation" and "never attempted" were
indistinguishable.

Legacy uncertainty is preserved, never invented. Attempts are PROSPECTIVE ONLY:
no historical row is ever backfilled, and any window predating the ledger is
``LEGACY_UNKNOWN`` -- never zero, never divided across stations. The evidence
state is deliberately four-valued because collapsing "we looked and found none"
into "we cannot know" is the original defect in miniature.

Everything here is pure: no database, no wall clock, no I/O.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

ATTEMPT_SCHEMA_VERSION = "1"

#: Prospective marker the fixed collector writes into collector_runs.stats_json.
#: Its presence is what makes a run attributable; no historical run has it, so
#: pre-fix runs are never judged as missing evidence they could not have had.
ATTEMPT_INSTRUMENTATION_KEY = "attempt_instrumentation_version"
ATTEMPT_INSTRUMENTATION_VERSION = "1"

#: Sanitizer bound for any error text persisted (matches the column width).
MAX_ERROR_DETAIL = 500

#: Substrings that must never reach a stored error message. Bodies and headers
#: can carry credentials; an evidence ledger must not become a secret store.
_SENSITIVE_TOKENS = (
    "authorization",
    "api-key",
    "api_key",
    "apikey",
    "bearer",
    "cookie",
    "set-cookie",
    "password",
    "secret",
    "token",
    "private_key",
    "x-amz-security",
    "signature",
)


class WeatherProductType(StrEnum):
    """Actual products the collector requests. Explicit per product -- never a
    generic value where the collector knows precisely what it asked for."""

    STATION_METADATA = "STATION_METADATA"
    CLI_OBSERVATIONS = "CLI_OBSERVATIONS"
    GRIDPOINT_FORECAST = "GRIDPOINT_FORECAST"


class AttemptStage(StrEnum):
    """Furthest successfully completed stage before the terminal outcome.

    Recorded alongside -- never folded into -- the outcome, so "source never
    answered" stays distinguishable from "source answered and the parser
    rejected the body".
    """

    REQUEST_STARTED = "REQUEST_STARTED"
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
    RAW_PAYLOAD_PERSISTED = "RAW_PAYLOAD_PERSISTED"
    PARSED = "PARSED"
    NORMALIZED_PERSISTED = "NORMALIZED_PERSISTED"
    TERMINAL = "TERMINAL"


class AttemptOutcome(StrEnum):
    """Terminal outcome of one logical attempt. Exactly one per attempt.

    Each value means one thing at one stage; none is overloaded. In particular
    ``SUCCEEDED_NO_DATA`` (source answered, nothing to report) is distinct from
    ``SOURCE_UNAVAILABLE`` (source never answered) -- conflating those is what
    made the legacy counters unattributable.
    """

    SUCCEEDED_NEW_DATA = "SUCCEEDED_NEW_DATA"
    SUCCEEDED_DUPLICATE = "SUCCEEDED_DUPLICATE"
    SUCCEEDED_NO_DATA = "SUCCEEDED_NO_DATA"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    REQUEST_FAILED = "REQUEST_FAILED"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    PARSER_REJECTED = "PARSER_REJECTED"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    UNSUPPORTED_PRODUCT = "UNSUPPORTED_PRODUCT"
    CANCELLED = "CANCELLED"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


class SourceAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    CONFIRMED_UNAVAILABLE = "CONFIRMED_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class AttemptEvidenceState(StrEnum):
    """Four-valued, deliberately. A boolean cannot tell "we looked and there
    were none" apart from "we have no way to know"."""

    KNOWN_ZERO = "KNOWN_ZERO"
    KNOWN_NONZERO = "KNOWN_NONZERO"
    LEGACY_UNKNOWN = "LEGACY_UNKNOWN"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"


SUCCESS_OUTCOMES = frozenset(
    {
        AttemptOutcome.SUCCEEDED_NEW_DATA,
        AttemptOutcome.SUCCEEDED_DUPLICATE,
        AttemptOutcome.SUCCEEDED_NO_DATA,
    }
)
FAILURE_OUTCOMES = frozenset(set(AttemptOutcome) - set(SUCCESS_OUTCOMES))
#: Outcomes that mean the parser saw a body and rejected it, or the body was
#: unusable. These map to the legacy ``invalid_items`` counter.
PARSER_OUTCOMES = frozenset({AttemptOutcome.PARSER_REJECTED, AttemptOutcome.MALFORMED_RESPONSE})

#: Reasons a window carries no station-level evidence. Surfaced verbatim so a
#: reader can never mistake one for a measured zero.
LEGACY_NO_ATTEMPT_LEDGER = "LEGACY_NO_ATTEMPT_LEDGER"
LEGACY_UNATTRIBUTABLE_CYCLE_AGGREGATE = "UNATTRIBUTABLE_CYCLE_AGGREGATE"
LEGACY_UNKNOWN_STATION_SCOPE = "UNKNOWN_STATION_SCOPE"
REASON_INCOMPLETE_ATTEMPTS = "INCOMPLETE_ATTEMPTS_IN_WINDOW"


class AttemptIntegrityError(ValueError):
    """An attempt record violates the evidence contract. Fail closed."""


def sanitize_error(text: str | None, *, limit: int = MAX_ERROR_DETAIL) -> str | None:
    """Bound and scrub error text before it is persisted.

    Provider errors embed response bodies and URLs, either of which can carry a
    credential. A line mentioning a sensitive token is dropped entirely rather
    than pattern-substituted -- partial redaction of an unknown format is a
    guess, and a guess is how secrets leak.
    """
    if text is None:
        return None
    kept = [
        line
        for line in str(text).splitlines()
        if not any(tok in line.lower() for tok in _SENSITIVE_TOKENS)
    ]
    cleaned = " ".join(" ".join(kept).split())
    return cleaned[:limit] if cleaned else None


def logical_request_key(
    *,
    collector_run_id: int,
    environment: str,
    station_code: str,
    product_type: WeatherProductType,
    target_window: str,
) -> str:
    """Deterministic identity of the INTENDED request.

    Deliberately excludes the retry number: retries are the same logical
    request and must collapse into one terminal record.
    """
    return f"{collector_run_id}|{environment}|{station_code}|{product_type}|{target_window}"


def attempt_id_for(logical_key: str) -> str:
    """Stable id derived from the logical key, generated before network I/O."""
    return hashlib.sha256(logical_key.encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One logical station/product collection attempt, terminal by construction."""

    attempt_id: str
    collector_run_id: int
    environment: str
    station_code: str
    product_type: WeatherProductType
    logical_request_key: str
    source_endpoint: str
    stage: AttemptStage
    outcome: AttemptOutcome
    source_availability: SourceAvailability
    requested_at: datetime
    completed_at: datetime
    observed_at: datetime
    created_at: datetime
    wfo: str | None = None
    source_request_id: str | None = None
    source_product_id: str | None = None
    target_station_local_date: date | None = None
    http_status: int | None = None
    retry_count: int = 0
    parser_name: str | None = None
    parser_version: str | None = None
    parser_error_type: str | None = None
    parser_error_message: str | None = None
    raw_payload_id: int | None = None
    parsed_entity_count: int = 0
    persisted_entity_count: int = 0
    duplicate_entity_count: int = 0
    persistence_error_type: str | None = None
    persistence_error_message: str | None = None
    schema_version: str = ATTEMPT_SCHEMA_VERSION

    def validate(self) -> list[str]:
        """Contract violations (empty == valid)."""
        p: list[str] = []
        if not self.attempt_id.strip():
            p.append("attempt_id is required")
        if not self.logical_request_key.strip():
            p.append("logical_request_key is required")
        if not self.station_code.strip():
            p.append("station_code is required — identity must be known at request time")
        if not self.environment.strip():
            p.append("environment is required")

        naive = [
            name
            for name in ("requested_at", "completed_at", "observed_at", "created_at")
            if getattr(self, name).tzinfo is None
        ]
        for name in naive:
            p.append(f"{name} must be timezone-aware UTC")
        # Ordering is only comparable once every timestamp is aware; comparing a
        # naive against an aware datetime raises, which would turn a reportable
        # contract violation into a crash.
        if not naive:
            if self.completed_at < self.requested_at:
                p.append("completed_at precedes requested_at")
            if self.observed_at < self.requested_at:
                p.append("observed_at precedes requested_at")
            if self.created_at < self.requested_at:
                p.append("created_at precedes requested_at")

        if self.retry_count < 0:
            p.append("retry_count must be >= 0")
        for name in (
            "parsed_entity_count",
            "persisted_entity_count",
            "duplicate_entity_count",
        ):
            if getattr(self, name) < 0:
                p.append(f"{name} must be >= 0")
        if self.persisted_entity_count > self.parsed_entity_count:
            p.append("persisted_entity_count exceeds parsed_entity_count")
        if self.duplicate_entity_count > self.parsed_entity_count:
            p.append("duplicate_entity_count exceeds parsed_entity_count")
        # Every parsed entity is either newly persisted or a duplicate; the sum
        # may be < parsed when items were validation-rejected mid-batch.
        if self.persisted_entity_count + self.duplicate_entity_count > self.parsed_entity_count:
            p.append("persisted + duplicate exceeds parsed_entity_count")

        for name in ("parser_error_message", "persistence_error_message"):
            value = getattr(self, name)
            if value is not None and any(t in value.lower() for t in _SENSITIVE_TOKENS):
                p.append(f"{name} contains sensitive material — must be sanitized")

        p.extend(self._outcome_invariants())
        return p

    def _outcome_invariants(self) -> list[str]:
        o, p = self.outcome, []
        avail = self.source_availability

        if o is AttemptOutcome.SUCCEEDED_NEW_DATA:
            if avail is not SourceAvailability.AVAILABLE:
                p.append("SUCCEEDED_NEW_DATA requires source_availability=AVAILABLE")
            if self.parsed_entity_count <= 0 or self.persisted_entity_count <= 0:
                p.append("SUCCEEDED_NEW_DATA requires parsed>0 and persisted>0")
            if self.parser_error_type or self.persistence_error_type:
                p.append("SUCCEEDED_NEW_DATA must carry no parser/persistence error")
        elif o is AttemptOutcome.SUCCEEDED_DUPLICATE:
            if avail is not SourceAvailability.AVAILABLE:
                p.append("SUCCEEDED_DUPLICATE requires source_availability=AVAILABLE")
            if self.parsed_entity_count <= 0 or self.duplicate_entity_count <= 0:
                p.append("SUCCEEDED_DUPLICATE requires parsed>0 and duplicate>0")
            if self.parser_error_type or self.persistence_error_type:
                p.append("SUCCEEDED_DUPLICATE must carry no parser/persistence error")
        elif o is AttemptOutcome.SUCCEEDED_NO_DATA:
            if avail is SourceAvailability.CONFIRMED_UNAVAILABLE:
                p.append(
                    "SUCCEEDED_NO_DATA must not be CONFIRMED_UNAVAILABLE — the source "
                    "answered; it simply had nothing to report"
                )
            if self.persisted_entity_count or self.duplicate_entity_count:
                p.append("SUCCEEDED_NO_DATA requires zero persisted and duplicate counts")
            if self.parser_error_type or self.persistence_error_type:
                p.append("SUCCEEDED_NO_DATA must carry no parser/persistence error")
        elif o is AttemptOutcome.SOURCE_UNAVAILABLE:
            if avail is not SourceAvailability.CONFIRMED_UNAVAILABLE:
                p.append("SOURCE_UNAVAILABLE requires source_availability=CONFIRMED_UNAVAILABLE")
            if self.raw_payload_id is not None:
                p.append("SOURCE_UNAVAILABLE must not link a raw payload — no body was received")
            if self.parsed_entity_count or self.persisted_entity_count:
                p.append("SOURCE_UNAVAILABLE must have no parsed or persisted entities")
        elif o is AttemptOutcome.REQUEST_FAILED:
            if avail is SourceAvailability.AVAILABLE:
                p.append("REQUEST_FAILED must not claim source_availability=AVAILABLE")
            if self.parsed_entity_count or self.persisted_entity_count:
                p.append("REQUEST_FAILED must have no parsed or persisted entities")
        elif o is AttemptOutcome.MALFORMED_RESPONSE:
            if self.stage in (AttemptStage.RAW_PAYLOAD_PERSISTED, AttemptStage.PARSED) and (
                self.raw_payload_id is None
            ):
                p.append("MALFORMED_RESPONSE with a stored body requires raw_payload_id")
            if self.persisted_entity_count:
                p.append("MALFORMED_RESPONSE must not report persisted entities")
        elif o is AttemptOutcome.PARSER_REJECTED:
            if self.raw_payload_id is None:
                p.append(
                    "PARSER_REJECTED requires raw_payload_id — the rejected body is the "
                    "only evidence of what was refused"
                )
            if not self.parser_error_type:
                p.append("PARSER_REJECTED requires parser_error_type")
            if self.persisted_entity_count:
                p.append("PARSER_REJECTED must not report persisted entities")
        elif o is AttemptOutcome.PERSISTENCE_FAILED:
            if self.parsed_entity_count <= 0:
                p.append("PERSISTENCE_FAILED requires parsed_entity_count > 0")
            if not self.persistence_error_type:
                p.append("PERSISTENCE_FAILED requires persistence_error_type")
        elif o is AttemptOutcome.UNKNOWN_FAILURE and not (
            self.parser_error_message or self.persistence_error_message
        ):
            p.append(
                "UNKNOWN_FAILURE requires a sanitized failure reason — use a precise "
                "outcome whenever one applies"
            )
        return p

    def require_valid(self) -> AttemptRecord:
        problems = self.validate()
        if problems:
            raise AttemptIntegrityError(
                f"attempt {self.attempt_id} ({self.station_code}/{self.product_type}): {problems}"
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "collector_run_id": self.collector_run_id,
            "environment": self.environment,
            "station_code": self.station_code,
            "wfo": self.wfo,
            "product_type": str(self.product_type),
            "logical_request_key": self.logical_request_key,
            "source_request_id": self.source_request_id,
            "source_product_id": self.source_product_id,
            "source_endpoint": self.source_endpoint,
            "stage": str(self.stage),
            "outcome": str(self.outcome),
            "source_availability": str(self.source_availability),
            "requested_at": self.requested_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "target_station_local_date": (
                self.target_station_local_date.isoformat()
                if self.target_station_local_date
                else None
            ),
            "http_status": self.http_status,
            "retry_count": self.retry_count,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "parser_error_type": self.parser_error_type,
            "parser_error_message": self.parser_error_message,
            "raw_payload_id": self.raw_payload_id,
            "parsed_entity_count": self.parsed_entity_count,
            "persisted_entity_count": self.persisted_entity_count,
            "duplicate_entity_count": self.duplicate_entity_count,
            "persistence_error_type": self.persistence_error_type,
            "persistence_error_message": self.persistence_error_message,
            "schema_version": self.schema_version,
        }


# --- reconciliation ----------------------------------------------------------

#: How attempt outcomes map onto the legacy WeatherCycleStats scalars. Recorded
#: explicitly so the two systems cannot drift silently.
LEGACY_COUNTER_MAPPING = {
    "invalid_items": "PARSER_REJECTED + MALFORMED_RESPONSE",
    "errors": (
        "station-level exceptions only: REQUEST_FAILED + SOURCE_UNAVAILABLE + "
        "PERSISTENCE_FAILED + UNKNOWN_FAILURE. NOTE: the legacy counter increments "
        "once per STATION, while attempts are per station/product, so this maps "
        "one-to-many and is checked as a lower bound, not equality."
    ),
    "observations_saved / forecasts_saved": (
        "sum of persisted_entity_count over the matching product; exact"
    ),
    "observations_duplicate / forecasts_duplicate": (
        "sum of duplicate_entity_count over the matching product; exact"
    ),
    "stations_processed": "count of distinct station_code with any non-CANCELLED attempt",
}


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    collector_run_id: int
    expected_pairs: int
    actual_attempts: int
    by_outcome: tuple[tuple[str, int], ...]
    missing_pairs: tuple[tuple[str, str], ...]
    unexpected_pairs: tuple[tuple[str, str], ...]
    duplicate_keys: tuple[str, ...]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "collector_run_id": self.collector_run_id,
            "expected_pairs": self.expected_pairs,
            "actual_attempts": self.actual_attempts,
            "by_outcome": dict(self.by_outcome),
            "missing_pairs": [list(x) for x in self.missing_pairs],
            "unexpected_pairs": [list(x) for x in self.unexpected_pairs],
            "duplicate_logical_keys": list(self.duplicate_keys),
            "problems": list(self.problems),
            "ok": self.ok,
            "legacy_counter_mapping": LEGACY_COUNTER_MAPPING,
        }


def reconcile_collector_run(
    *,
    collector_run_id: int,
    environment: str,
    attempts: Sequence[AttemptRecord],
    expected_pairs: Sequence[tuple[str, WeatherProductType]],
    cycle_stats: dict[str, int] | None = None,
) -> ReconciliationResult:
    """Hard invariant: exactly one terminal attempt per expected pair.

    A mismatch is returned as an explicit failure. Legacy cycle counters are
    cross-checked but never rewritten.
    """
    problems: list[str] = []
    counts: dict[str, int] = {}
    seen_keys: dict[str, int] = {}
    seen_pairs: set[tuple[str, str]] = set()

    for a in attempts:
        counts[str(a.outcome)] = counts.get(str(a.outcome), 0) + 1
        seen_keys[a.logical_request_key] = seen_keys.get(a.logical_request_key, 0) + 1
        seen_pairs.add((a.station_code, str(a.product_type)))
        if a.collector_run_id != collector_run_id:
            problems.append(f"{a.station_code}/{a.product_type}: foreign collector_run_id")
        if a.environment != environment:
            problems.append(
                f"{a.station_code}/{a.product_type}: environment {a.environment!r} != "
                f"run environment {environment!r}"
            )
        problems.extend(f"{a.station_code}/{a.product_type}: {x}" for x in a.validate())

    expected = {(s, str(p)) for s, p in expected_pairs}
    missing = tuple(sorted(expected - seen_pairs))
    unexpected = tuple(sorted(seen_pairs - expected))
    duplicates = tuple(sorted(k for k, n in seen_keys.items() if n > 1))

    if missing:
        problems.append(f"missing expected station/product pair(s): {list(missing)}")
    if unexpected:
        problems.append(f"unexpected station/product pair(s): {list(unexpected)}")
    if duplicates:
        problems.append(f"duplicate logical_request_key(s): {list(duplicates)}")
    if len(attempts) != len(expected):
        problems.append(
            f"actual terminal attempts {len(attempts)} != expected pairs {len(expected)}"
        )
    if sum(counts.values()) != len(attempts):
        problems.append("outcome totals do not sum to the attempt count")

    if cycle_stats is not None:
        derived_invalid = sum(n for o, n in counts.items() if AttemptOutcome(o) in PARSER_OUTCOMES)
        legacy_invalid = cycle_stats.get("invalid_items")
        if legacy_invalid is not None and derived_invalid != legacy_invalid:
            problems.append(
                f"legacy invalid_items {legacy_invalid} != attempt-derived "
                f"parser/malformed {derived_invalid}"
            )
        # `errors` counts STATIONS, attempts count station/product pairs, so the
        # legacy scalar can only be a lower bound on attempt-level failures.
        derived_failures = sum(
            n for o, n in counts.items() if AttemptOutcome(o) in FAILURE_OUTCOMES
        )
        legacy_errors = cycle_stats.get("errors")
        if legacy_errors is not None and derived_failures < legacy_errors:
            problems.append(
                f"legacy errors {legacy_errors} exceeds attempt-derived failures "
                f"{derived_failures} — evidence is missing for a counted failure"
            )

    return ReconciliationResult(
        collector_run_id=collector_run_id,
        expected_pairs=len(expected),
        actual_attempts=len(attempts),
        by_outcome=tuple(sorted(counts.items())),
        missing_pairs=missing,
        unexpected_pairs=unexpected,
        duplicate_keys=duplicates,
        problems=tuple(problems),
    )


# --- station summary ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProductBreakdown:
    product_type: str
    recorded_attempts: int
    new_data: int
    duplicates: int
    no_data: int
    source_unavailable: int
    request_failed: int
    malformed_response: int
    parser_rejected: int
    persistence_failed: int
    unsupported: int
    cancelled: int
    unknown_failure: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_type": self.product_type,
            "recorded_attempts": self.recorded_attempts,
            "new_data": self.new_data,
            "duplicates": self.duplicates,
            "no_data": self.no_data,
            "source_unavailable": self.source_unavailable,
            "request_failed": self.request_failed,
            "malformed_response": self.malformed_response,
            "parser_rejected": self.parser_rejected,
            "persistence_failed": self.persistence_failed,
            "unsupported": self.unsupported,
            "cancelled": self.cancelled,
            "unknown_failure": self.unknown_failure,
        }


@dataclass(frozen=True, slots=True)
class StationAttemptSummary:
    """Per-station operational rollup. Counts and evidence states only -- no
    forecast error, calibration, price, or any performance quantity."""

    station_code: str
    window_start: datetime
    window_end: datetime
    ledger_available: bool
    expected_attempts: int
    recorded_attempts: int
    products: tuple[ProductBreakdown, ...]
    parser_failures: AttemptEvidenceState
    parser_failure_count: int | None
    source_unavailable: AttemptEvidenceState
    source_unavailable_count: int | None
    persistence_failures: AttemptEvidenceState
    persistence_failure_count: int | None
    raw_payload_coverage: tuple[int, int]
    collector_runs_covered: int
    affected_local_dates: tuple[str, ...]
    evidence_reason: str | None
    unattributable_cycle_aggregate: int | None = None
    problems: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "station_code": self.station_code,
            "window": [self.window_start.isoformat(), self.window_end.isoformat()],
            "ledger_available": self.ledger_available,
            "expected_attempts": self.expected_attempts,
            "recorded_attempts": self.recorded_attempts,
            "products": [p.to_dict() for p in self.products],
            "evidence": {
                "parser_failures": {
                    "state": str(self.parser_failures),
                    "count": self.parser_failure_count,
                },
                "source_unavailable": {
                    "state": str(self.source_unavailable),
                    "count": self.source_unavailable_count,
                },
                "persistence_failures": {
                    "state": str(self.persistence_failures),
                    "count": self.persistence_failure_count,
                },
            },
            "raw_payload_coverage": {
                "linked": self.raw_payload_coverage[0],
                "expected": self.raw_payload_coverage[1],
            },
            "collector_runs_covered": self.collector_runs_covered,
            "affected_local_dates": list(self.affected_local_dates),
            "evidence_reason": self.evidence_reason,
            "unattributable_cycle_aggregate": self.unattributable_cycle_aggregate,
            "problems": list(self.problems),
        }


def _legacy_summary(
    station_code: str,
    window_start: datetime,
    window_end: datetime,
    *,
    reason: str,
    state: AttemptEvidenceState,
    unattributable: int | None,
) -> StationAttemptSummary:
    return StationAttemptSummary(
        station_code=station_code,
        window_start=window_start,
        window_end=window_end,
        ledger_available=False,
        expected_attempts=0,
        recorded_attempts=0,
        products=(),
        parser_failures=state,
        parser_failure_count=None,
        source_unavailable=state,
        source_unavailable_count=None,
        persistence_failures=state,
        persistence_failure_count=None,
        raw_payload_coverage=(0, 0),
        collector_runs_covered=0,
        affected_local_dates=(),
        evidence_reason=reason,
        unattributable_cycle_aggregate=unattributable,
    )


def summarize_station_window(
    station_code: str,
    attempts: Sequence[AttemptRecord],
    *,
    window_start: datetime,
    window_end: datetime,
    ledger_available: bool,
    expected_attempts: int = 0,
    product_filter: WeatherProductType | None = None,
    unattributable_cycle_aggregate: int | None = None,
) -> StationAttemptSummary:
    """Roll one station's attempts up over a window.

    ``ledger_available=False`` means the window predates station-level evidence:
    every state becomes ``LEGACY_UNKNOWN`` with a ``None`` count. It never
    becomes ``KNOWN_ZERO`` -- absence of a ledger is not a measured zero, and
    cycle aggregates are never divided across stations to manufacture one.
    """
    if not ledger_available:
        return _legacy_summary(
            station_code,
            window_start,
            window_end,
            reason=LEGACY_NO_ATTEMPT_LEDGER,
            state=AttemptEvidenceState.LEGACY_UNKNOWN,
            unattributable=unattributable_cycle_aggregate,
        )

    mine = [
        a
        for a in attempts
        if a.station_code == station_code
        and (product_filter is None or a.product_type is product_filter)
    ]
    if not mine:
        # The ledger exists but this station has no rows: missing evidence for
        # the station, not a clean zero.
        return _legacy_summary(
            station_code,
            window_start,
            window_end,
            reason=LEGACY_UNKNOWN_STATION_SCOPE,
            state=AttemptEvidenceState.EVIDENCE_MISSING,
            unattributable=unattributable_cycle_aggregate,
        )

    per_product: dict[str, dict[str, int]] = {}
    for a in mine:
        b = per_product.setdefault(str(a.product_type), {})
        b[str(a.outcome)] = b.get(str(a.outcome), 0) + 1

    def n(bucket: dict[str, int], outcome: AttemptOutcome) -> int:
        return bucket.get(str(outcome), 0)

    products = tuple(
        ProductBreakdown(
            product_type=name,
            recorded_attempts=sum(b.values()),
            new_data=n(b, AttemptOutcome.SUCCEEDED_NEW_DATA),
            duplicates=n(b, AttemptOutcome.SUCCEEDED_DUPLICATE),
            no_data=n(b, AttemptOutcome.SUCCEEDED_NO_DATA),
            source_unavailable=n(b, AttemptOutcome.SOURCE_UNAVAILABLE),
            request_failed=n(b, AttemptOutcome.REQUEST_FAILED),
            malformed_response=n(b, AttemptOutcome.MALFORMED_RESPONSE),
            parser_rejected=n(b, AttemptOutcome.PARSER_REJECTED),
            persistence_failed=n(b, AttemptOutcome.PERSISTENCE_FAILED),
            unsupported=n(b, AttemptOutcome.UNSUPPORTED_PRODUCT),
            cancelled=n(b, AttemptOutcome.CANCELLED),
            unknown_failure=n(b, AttemptOutcome.UNKNOWN_FAILURE),
        )
        for name, b in sorted(per_product.items())
    )

    parser_n = sum(p.parser_rejected + p.malformed_response for p in products)
    source_n = sum(p.source_unavailable + p.request_failed for p in products)
    persist_n = sum(p.persistence_failed for p in products)

    # Incomplete evidence is never a clean zero.
    incomplete = expected_attempts > 0 and len(mine) < expected_attempts
    problems: tuple[str, ...] = ()
    if incomplete:
        problems = (
            f"{len(mine)} recorded attempt(s) < {expected_attempts} expected — evidence incomplete",
        )

    def state(count: int) -> tuple[AttemptEvidenceState, int | None]:
        if incomplete:
            return AttemptEvidenceState.EVIDENCE_MISSING, None
        return (
            AttemptEvidenceState.KNOWN_NONZERO if count else AttemptEvidenceState.KNOWN_ZERO
        ), count

    expect_payload = [a for a in mine if a.stage is not AttemptStage.REQUEST_STARTED]
    linked = sum(1 for a in expect_payload if a.raw_payload_id is not None)
    parser_state, parser_c = state(parser_n)
    source_state, source_c = state(source_n)
    persist_state, persist_c = state(persist_n)

    return StationAttemptSummary(
        station_code=station_code,
        window_start=window_start,
        window_end=window_end,
        ledger_available=True,
        expected_attempts=expected_attempts,
        recorded_attempts=len(mine),
        products=products,
        parser_failures=parser_state,
        parser_failure_count=parser_c,
        source_unavailable=source_state,
        source_unavailable_count=source_c,
        persistence_failures=persist_state,
        persistence_failure_count=persist_c,
        raw_payload_coverage=(linked, len(expect_payload)),
        collector_runs_covered=len({a.collector_run_id for a in mine}),
        affected_local_dates=tuple(
            sorted(
                {
                    a.target_station_local_date.isoformat()
                    for a in mine
                    if a.target_station_local_date
                }
            )
        ),
        evidence_reason=REASON_INCOMPLETE_ATTEMPTS if incomplete else None,
        unattributable_cycle_aggregate=unattributable_cycle_aggregate,
        problems=problems,
    )


@dataclass(frozen=True, slots=True)
class PendingWeatherCollectionAttempt:
    """A terminal attempt captured DURING the cycle, before its run id exists.

    ``collector_run_id`` is genuinely unavailable at collection time: the
    collector-run record is written after the cycle, in a separate session
    scope. Rather than carry a placeholder (0, "", a sentinel UUID) that could
    leak into the database and silently mis-attribute evidence, the field is
    simply ABSENT here. It becomes an ``AttemptRecord`` only via
    ``materialize`` with a real, persisted id.

    Immutable, and fully validated before it leaves the cycle -- a pending
    attempt that could never be persisted is caught while the collector still
    has the context to report it.
    """

    attempt_id: str
    environment: str
    station_code: str
    product_type: WeatherProductType
    logical_request_key: str
    source_endpoint: str
    stage: AttemptStage
    outcome: AttemptOutcome
    source_availability: SourceAvailability
    requested_at: datetime
    completed_at: datetime
    observed_at: datetime
    wfo: str | None = None
    source_request_id: str | None = None
    source_product_id: str | None = None
    target_station_local_date: date | None = None
    http_status: int | None = None
    retry_count: int = 0
    parser_name: str | None = None
    parser_version: str | None = None
    parser_error_type: str | None = None
    parser_error_message: str | None = None
    raw_payload_id: int | None = None
    parsed_entity_count: int = 0
    persisted_entity_count: int = 0
    duplicate_entity_count: int = 0
    persistence_error_type: str | None = None
    persistence_error_message: str | None = None

    @property
    def logical_pair(self) -> tuple[str, str]:
        return (self.station_code, str(self.product_type))

    def materialize(self, collector_run_id: int, *, created_at: datetime) -> AttemptRecord:
        """Bind this pending attempt to a REAL persisted collector-run id.

        Purely additive: no outcome, count, stage, availability, identity, or
        timestamp is altered, so materialization cannot change what the cycle
        observed. ``attempt_id`` and ``logical_request_key`` are carried through
        unchanged, preserving idempotency across a retried persistence pass.
        """
        if collector_run_id is None or collector_run_id <= 0:
            raise AttemptIntegrityError(
                f"cannot materialize attempt {self.attempt_id!r} without a real "
                f"collector_run_id (got {collector_run_id!r})"
            )
        return AttemptRecord(
            attempt_id=self.attempt_id,
            collector_run_id=collector_run_id,
            environment=self.environment,
            station_code=self.station_code,
            product_type=self.product_type,
            logical_request_key=self.logical_request_key,
            source_endpoint=self.source_endpoint,
            stage=self.stage,
            outcome=self.outcome,
            source_availability=self.source_availability,
            requested_at=self.requested_at,
            completed_at=self.completed_at,
            observed_at=self.observed_at,
            created_at=created_at,
            wfo=self.wfo,
            source_request_id=self.source_request_id,
            source_product_id=self.source_product_id,
            target_station_local_date=self.target_station_local_date,
            http_status=self.http_status,
            retry_count=self.retry_count,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            parser_error_type=self.parser_error_type,
            parser_error_message=self.parser_error_message,
            raw_payload_id=self.raw_payload_id,
            parsed_entity_count=self.parsed_entity_count,
            persisted_entity_count=self.persisted_entity_count,
            duplicate_entity_count=self.duplicate_entity_count,
            persistence_error_type=self.persistence_error_type,
            persistence_error_message=self.persistence_error_message,
        )

    def validate_pending(self) -> list[str]:
        """Validate everything checkable without a run id, by materializing
        against a probe id and dropping any problem that mentions lineage."""
        probe = self.materialize(1, created_at=self.completed_at)
        return [p for p in probe.validate() if "collector_run" not in p]
