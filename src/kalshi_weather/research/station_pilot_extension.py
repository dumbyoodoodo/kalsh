"""SEA/PHX/MIA station-pilot EXTENSION registration — frozen prospectively.

Why this exists. The 2026-08-04 pilot review returned
``PILOT_COLLECTION_EXTENSION_REQUIRED`` for all three stations, driven by
platform evidence limits rather than station defects: cycle-level weather
counters carry no station dimension, so ``parser_failures`` and
``source_unavailable_attempts`` were ``evidence_unavailable`` and KEEP was
unreachable by construction. The extension exists to observe the same stations
once station-level attempt evidence exists.

What makes this registration trustworthy. It was frozen **before any
station-level attempt outcome existed anywhere** — at registration time the
``weather_collection_attempts`` table was not deployed and migration 0012 was
not applied, so no failure count could have influenced the window length,
start rule, or decision criteria. That is the whole point: a window chosen
after seeing outcomes is not evidence, it is selection.

The deployment anchor is deliberately **not a date**. Hard-coding a start date
before deployment would either exclude real collection or admit a
partially-instrumented day. Instead the start is a *rule* over
``deployment_validated_at``, a timestamp that must be recorded from deployment
evidence by a separate, separately-reviewed task — never chosen retroactively
here.

This module is pure: no database, no wall clock, no I/O. The original pilot is
untouched; this is a new observation period, not a replacement.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

#: Identity of this registration.
EXTENSION_REGISTRATION_ID = "STATION-PILOT-EXT-0001"
REGISTERED_AT = datetime(2026, 8, 4, 3, 34, 30, tzinfo=UTC)
REGISTERED_BY = "Wilson Tu"

#: Frozen: seven COMPLETE station-local dates per station. Chosen before any
#: station-level outcome existed; never re-derived from observed counts.
REQUIRED_COMPLETE_DATES = 7

#: The pilot stations and their authoritative zones (unchanged from ADR 0023;
#: PHX is fixed UTC-7 and never America/Denver).
STATION_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("SEA", "America/Los_Angeles"),
    ("PHX", "America/Phoenix"),
    ("MIA", "America/New_York"),
)
STATION_CODES: tuple[str, ...] = tuple(code for code, _ in STATION_TIMEZONES)

#: The immutable original-pilot artifact this extension follows. Verified, never
#: revised, superseded, regenerated, or reinterpreted.
#: Filename only. Research modules must not hardcode a research-docs
#: directory path (repo isolation invariant); the caller supplies it.
ORIGINAL_REVIEW_ARTIFACT_NAME = "station_pilot_review_2026-08-04.md"
ORIGINAL_REVIEW_ARTIFACT_SHA256 = "3df31e08ce12ec7fbe3113dde0ef98e7ed9d9d564685456bde08870f5b947c09"
ORIGINAL_REVIEW_VERDICT = "PILOT_COLLECTION_EXTENSION_REQUIRED"

START_RULE = (
    "first station-local calendar date whose LOCAL MIDNIGHT occurs strictly after "
    "deployment_validated_at; excludes a partially instrumented deployment date"
)
REVIEW_GATE_RULE = (
    "latest UTC end boundary among the three stations' seventh included local date "
    "(max over stations of local-midnight-ending-the-7th-date converted to UTC)"
)

#: Operational review dimensions, frozen. Performance analysis is forbidden.
REVIEW_DIMENSIONS: tuple[str, ...] = (
    "observation_completeness",
    "forecast_continuity",
    "station_level_request_outcomes",
    "parser_health",
    "source_availability",
    "persistence_integrity",
    "provenance_integrity",
    "timezone_and_local_date_correctness",
    "outage_adjusted_collection_reliability",
)
FORBIDDEN_ANALYSIS: tuple[str, ...] = (
    "forecast_error",
    "brier_score",
    "calibration",
    "market_prices",
    "liquidity",
    "pnl",
    "expected_value",
    "profitability",
    "station_ranking",
    "hypothesis_support",
)

#: Evidence states that are NOT acceptable inside the extension window. The
#: whole purpose of the extension is to eliminate them; tolerating them would
#: reproduce the condition that forced the extension.
UNACCEPTABLE_EXTENSION_EVIDENCE: tuple[str, ...] = ("LEGACY_UNKNOWN", "EVIDENCE_MISSING")


class ExtensionState(StrEnum):
    """Lifecycle state of the extension. No state exposes an outcome."""

    WAITING_FOR_ATTEMPT_ATTRIBUTION_DEPLOYMENT = "WAITING_FOR_ATTEMPT_ATTRIBUTION_DEPLOYMENT"
    COLLECTING_EXTENSION = "COLLECTING_EXTENSION"
    #: The seventh local date has elapsed. This is a CALENDAR fact only -- it
    #: says nothing about whether the evidence the review needs was collected.
    READY_FOR_EXTENSION_REVIEW = "READY_FOR_EXTENSION_REVIEW"
    #: Terminal. A disposition has been recorded for this registration; the
    #: window is closed and no further state change occurs.
    EXTENSION_DISPOSED = "EXTENSION_DISPOSED"


class ExtensionRegistrationError(ValueError):
    """A request violates the frozen extension registration."""


# --- deterministic date arithmetic -------------------------------------------


def first_included_local_date(deployment_validated_at: datetime, timezone_name: str) -> date:
    """First COMPLETE station-local date after deployment validation.

    A date is included only if its local midnight is strictly after
    ``deployment_validated_at``. The deployment day itself is therefore always
    excluded: it was only partially instrumented, and admitting it would let
    pre-deployment hours into the extension.
    """
    if deployment_validated_at.tzinfo is None:
        raise ExtensionRegistrationError("deployment_validated_at must be timezone-aware UTC")
    zone = ZoneInfo(timezone_name)
    local = deployment_validated_at.astimezone(zone)
    # Local midnight of the deployment day is at or before `local`, so the first
    # strictly-later local midnight always starts the NEXT local date.
    return local.date() + timedelta(days=1)


def included_local_dates(
    deployment_validated_at: datetime,
    timezone_name: str,
    *,
    count: int = REQUIRED_COMPLETE_DATES,
) -> tuple[date, ...]:
    """The ``count`` consecutive complete local dates for one station."""
    first = first_included_local_date(deployment_validated_at, timezone_name)
    return tuple(first + timedelta(days=i) for i in range(count))


def local_date_end_utc(local_day: date, timezone_name: str) -> datetime:
    """UTC instant at which ``local_day`` ends for that station (its local
    midnight boundary into the next day), DST-correct."""
    zone = ZoneInfo(timezone_name)
    return datetime.combine(
        local_day + timedelta(days=1), datetime.min.time(), tzinfo=zone
    ).astimezone(UTC)


def extension_review_gate(
    deployment_validated_at: datetime,
    *,
    station_timezones: tuple[tuple[str, str], ...] = STATION_TIMEZONES,
    count: int = REQUIRED_COMPLETE_DATES,
) -> datetime:
    """Earliest instant at which ALL stations have completed their seventh date.

    The max over stations, not the min: a gate at the earliest station would
    review a station whose seventh local date had not finished.
    """
    ends = []
    for _code, tz in station_timezones:
        seventh = included_local_dates(deployment_validated_at, tz, count=count)[-1]
        ends.append(local_date_end_utc(seventh, tz))
    return max(ends)


def extension_state(
    *,
    now: datetime,
    deployment_validated_at: datetime | None,
    disposition_recorded: bool = False,
) -> ExtensionState:
    """Lifecycle state. ``deployment_validated_at=None`` means the attempt
    evidence layer is not deployed, so the extension has no start and no gate.

    This function is deliberately CALENDAR-ONLY: given a deployment anchor and a
    clock it returns where the window sits in time. It has no access to
    collected evidence and therefore ``READY_FOR_EXTENSION_REVIEW`` must never
    be read as "enough evidence exists to issue a station verdict" -- for
    STATION-PILOT-EXT-0001 the gate passed while four registered dates had zero
    collection.

    ``disposition_recorded`` is the one non-calendar input, and it only ever
    moves the state to a terminal value: once a window has been formally
    disposed, continuing to report it as awaiting review would invite running
    the frozen review against evidence that was never acquired.
    """
    if deployment_validated_at is None:
        return ExtensionState.WAITING_FOR_ATTEMPT_ATTRIBUTION_DEPLOYMENT
    if now.tzinfo is None:
        raise ExtensionRegistrationError("now must be timezone-aware UTC")
    if disposition_recorded:
        return ExtensionState.EXTENSION_DISPOSED
    gate = extension_review_gate(deployment_validated_at)
    if now >= gate:
        return ExtensionState.READY_FOR_EXTENSION_REVIEW
    return ExtensionState.COLLECTING_EXTENSION


# --- the frozen registration object ------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtensionRegistration:
    """Immutable registration. ``deployment_validated_at`` is deliberately
    absent: it does not exist yet and must be supplied later from deployment
    evidence, never guessed here."""

    extension_registration_id: str = EXTENSION_REGISTRATION_ID
    registered_at: datetime = REGISTERED_AT
    registered_by: str = REGISTERED_BY
    required_complete_dates: int = REQUIRED_COMPLETE_DATES
    start_rule: str = START_RULE
    review_gate_rule: str = REVIEW_GATE_RULE
    station_codes: tuple[str, ...] = STATION_CODES
    station_timezones: tuple[tuple[str, str], ...] = STATION_TIMEZONES
    required_attempt_evidence: bool = True
    original_review_artifact_name: str = ORIGINAL_REVIEW_ARTIFACT_NAME
    original_review_artifact_hash: str = ORIGINAL_REVIEW_ARTIFACT_SHA256
    original_review_verdict: str = ORIGINAL_REVIEW_VERDICT
    review_dimensions: tuple[str, ...] = REVIEW_DIMENSIONS
    forbidden_analysis: tuple[str, ...] = FORBIDDEN_ANALYSIS
    unacceptable_extension_evidence: tuple[str, ...] = UNACCEPTABLE_EXTENSION_EVIDENCE
    #: Recorded fact, not an assertion of intent: no station-level attempt
    #: outcome existed anywhere when this was frozen.
    attempt_outcomes_existed_at_registration: bool = False

    def payload(self) -> dict[str, Any]:
        """Canonical hashable payload (every field except the hash itself)."""
        return {
            "extension_registration_id": self.extension_registration_id,
            "registered_at": self.registered_at.isoformat().replace("+00:00", "Z"),
            "registered_by": self.registered_by,
            "required_complete_dates": self.required_complete_dates,
            "start_rule": self.start_rule,
            "review_gate_rule": self.review_gate_rule,
            "station_codes": list(self.station_codes),
            "station_timezones": [list(p) for p in self.station_timezones],
            "required_attempt_evidence": self.required_attempt_evidence,
            "original_review_artifact_name": self.original_review_artifact_name,
            "original_review_artifact_hash": self.original_review_artifact_hash,
            "original_review_verdict": self.original_review_verdict,
            "review_dimensions": list(self.review_dimensions),
            "forbidden_analysis": list(self.forbidden_analysis),
            "unacceptable_extension_evidence": list(self.unacceptable_extension_evidence),
            "attempt_outcomes_existed_at_registration": (
                self.attempt_outcomes_existed_at_registration
            ),
        }

    def registration_hash(self) -> str:
        canonical = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        data = self.payload()
        data["registration_hash"] = self.registration_hash()
        return data


REGISTRATION = ExtensionRegistration()


def verify_original_artifact(artifact_bytes: bytes) -> bool:
    """Whether the referenced original pilot artifact is byte-identical.

    A mismatch means the immutable August 4 record changed, which invalidates
    the premise of this extension — fail closed rather than proceed.
    """
    return hashlib.sha256(artifact_bytes).hexdigest() == ORIGINAL_REVIEW_ARTIFACT_SHA256
