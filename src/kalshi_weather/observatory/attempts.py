"""Observatory checks over station-level weather collection attempts.

Reports through the existing ``Finding``/``Severity`` contract; it defines no
new vocabulary and re-uses the shipped evidence layer
(``ingestion.weather_attempts``) rather than restating any of its types.

Severity mapping. The hardening spec names four levels (CRITICAL / ERROR /
WARNING / INFO) but this repository's observatory is deliberately three-valued,
and the roll-up in ``severity.overall_severity`` depends on that. Rather than
fork the scale repo-wide, the spec's **ERROR maps onto CRITICAL** here: both
denote "an integrity violation an operator must resolve", and collapsing them
preserves the existing status roll-up. WARNING and INFO are unchanged.

Pre-deployment quiet. Before migration 0012 exists there are no attempt rows,
and that is the expected state -- not corruption. The absence is reported at
INFO and never pages, because the schema drift itself is already visible as one
canonical ``unexpected_schema_change`` finding; duplicating it here would train
operators to ignore both.

Pure: every function takes already-loaded rows and returns findings. No
database, no wall clock beyond an injected ``now``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from kalshi_weather.ingestion.weather_attempts import (
    AttemptOutcome,
    AttemptStage,
    SourceAvailability,
    WeatherProductType,
)
from kalshi_weather.observatory.severity import Finding, Severity

DOMAIN = "observation"

#: A run is only judged once it has completed. An active run has not yet had
#: the chance to write its attempts, and flagging it would manufacture a
#: finding out of normal operation.
DEFAULT_COMPLETION_GRACE = timedelta(minutes=5)

#: All product types the collector may legitimately record.
KNOWN_PRODUCTS: frozenset[str] = frozenset(str(product) for product in WeatherProductType)


@dataclass(frozen=True, slots=True)
class AttemptRow:
    """One persisted attempt, reduced to what integrity checking needs.

    Mirrors the stored columns rather than the domain record, because the
    observatory's job is to catch rows that should never have been written.
    """

    attempt_id: str
    collector_run_id: int | None
    environment: str
    station_code: str
    product_type: str
    logical_request_key: str
    stage: str
    outcome: str
    source_availability: str
    requested_at: datetime
    completed_at: datetime
    target_station_local_date: date | None
    raw_payload_id: int | None
    parsed_entity_count: int
    persisted_entity_count: int
    duplicate_entity_count: int
    parser_error_type: str | None = None
    persistence_error_type: str | None = None


@dataclass(frozen=True, slots=True)
class RunContext:
    """A weather collector run and the scope it was expected to cover."""

    collector_run_id: int
    environment: str
    started_at: datetime
    finished_at: datetime | None
    expected_pairs: tuple[tuple[str, str], ...]
    legacy_invalid_items: int | None = None
    legacy_errors: int | None = None
    #: True only when the run was produced by an attribution-instrumented
    #: collector. Pre-fix runs are LEGACY_UNKNOWN, never integrity failures.
    attempt_instrumented: bool = False

    def is_complete(self, now: datetime, grace: timedelta = DEFAULT_COMPLETION_GRACE) -> bool:
        """Completed runs only. Uses the run's own completion marker; the grace
        period covers the write of the run record itself and is NOT a new
        timeout invented to silence findings."""
        return self.finished_at is not None and now >= self.finished_at + grace


def _f(
    check: str, severity: Severity, count: int, message: str, samples: Sequence[str] = ()
) -> Finding:
    return Finding(
        domain=DOMAIN,
        check=check,
        severity=severity,
        count=count,
        message=message,
        samples=tuple(samples[:10]),
    )


def legacy_absence_finding(*, schema_deployed: bool) -> Finding:
    """Pre-deployment absence is INFO and must never page.

    The canonical ``unexpected_schema_change`` finding already reports that
    production lags the code; a second CRITICAL here would be duplicate noise.
    """
    if schema_deployed:
        return _f(
            "attempt_schema_missing_when_expected",
            Severity.INFO,
            0,
            "attempt-attribution schema present",
        )
    return _f(
        "attempt_schema_missing_when_expected",
        Severity.INFO,
        1,
        "station-level attempt evidence is not deployed yet (migration 0012 pending); "
        "pre-deployment absence is expected, not corruption — see the canonical "
        "unexpected_schema_change finding for the drift itself",
    )


def schema_revision_finding(*, db_revision: str, expected_revision: str) -> Finding:
    """Reported at INFO while the drift is intentional and already canonical."""
    if db_revision == expected_revision:
        return _f("attempt_schema_revision_mismatch", Severity.INFO, 0, "schema revision matches")
    return _f(
        "attempt_schema_revision_mismatch",
        Severity.INFO,
        1,
        f"database revision {db_revision!r} lags expected {expected_revision!r}; "
        "attempt evidence is unavailable until the migration is deployed",
    )


def check_attempt_integrity(
    rows: Sequence[AttemptRow],
    *,
    now: datetime,
    known_stations: frozenset[str],
    known_products: frozenset[str] = KNOWN_PRODUCTS,
) -> list[Finding]:
    """Per-row integrity. Rows that should never have been written."""
    findings: list[Finding] = []
    valid_outcomes = {str(o) for o in AttemptOutcome}
    valid_stages = {str(s) for s in AttemptStage}
    valid_availability = {str(a) for a in SourceAvailability}

    def collect(check: str, severity: Severity, message: str, samples: list[str]) -> None:
        if samples:
            findings.append(_f(check, severity, len(samples), message, samples))

    missing_outcome = [
        r.attempt_id
        for r in rows
        if r.outcome not in valid_outcomes
        or r.stage not in valid_stages
        or r.source_availability not in valid_availability
    ]
    collect(
        "terminal_outcome_missing",
        Severity.CRITICAL,
        "attempt row carries an unrecognized stage/outcome/availability value",
        missing_outcome,
    )

    collect(
        "invalid_station_identity",
        Severity.CRITICAL,
        "attempt row references a station outside the registry",
        [r.attempt_id for r in rows if r.station_code not in known_stations],
    )
    collect(
        "invalid_product_identity",
        Severity.CRITICAL,
        "attempt row references an unknown product type",
        [r.attempt_id for r in rows if r.product_type not in known_products],
    )
    collect(
        "missing_collector_run_lineage",
        Severity.CRITICAL,
        "attempt row has no collector_run_id",
        [r.attempt_id for r in rows if r.collector_run_id is None],
    )
    collect(
        "future_attempt_timestamp",
        Severity.CRITICAL,
        "attempt timestamp lies in the future",
        [r.attempt_id for r in rows if r.requested_at > now or r.completed_at > now],
    )

    impossible = [
        r.attempt_id
        for r in rows
        if r.persisted_entity_count > r.parsed_entity_count
        or r.duplicate_entity_count > r.parsed_entity_count
        or r.persisted_entity_count + r.duplicate_entity_count > r.parsed_entity_count
        or (r.outcome == str(AttemptOutcome.SUCCEEDED_NEW_DATA) and r.persisted_entity_count <= 0)
        or (r.outcome == str(AttemptOutcome.SUCCEEDED_DUPLICATE) and r.duplicate_entity_count <= 0)
        or (
            r.outcome == str(AttemptOutcome.SOURCE_UNAVAILABLE)
            and (r.parsed_entity_count or r.persisted_entity_count)
        )
    ]
    collect(
        "impossible_outcome_count_combination",
        Severity.CRITICAL,
        "attempt counts contradict the recorded outcome",
        impossible,
    )

    # Duplicate logical identity is CRITICAL only when the duplicates disagree;
    # identical repeats are an idempotency artifact, not corruption.
    by_key: dict[str, list[AttemptRow]] = {}
    for r in rows:
        by_key.setdefault(f"{r.collector_run_id}|{r.logical_request_key}", []).append(r)
    conflicting = [
        k for k, group in by_key.items() if len(group) > 1 and len({g.outcome for g in group}) > 1
    ]
    identical = [
        k for k, group in by_key.items() if len(group) > 1 and len({g.outcome for g in group}) == 1
    ]
    collect(
        "duplicate_logical_attempt",
        Severity.CRITICAL,
        "the same logical attempt was recorded more than once with CONFLICTING outcomes",
        conflicting,
    )
    collect(
        "duplicate_logical_attempt",
        Severity.WARNING,
        "the same logical attempt appears more than once with identical outcomes",
        identical,
    )
    return findings


def check_environment_and_lineage(
    rows: Sequence[AttemptRow], runs: Sequence[RunContext]
) -> list[Finding]:
    """Attempts must belong to their run and match its environment."""
    findings: list[Finding] = []
    by_run = {r.collector_run_id: r for r in runs}
    foreign = [
        a.attempt_id
        for a in rows
        if a.collector_run_id is not None and a.collector_run_id not in by_run
    ]
    mismatched = [
        a.attempt_id
        for a in rows
        if a.collector_run_id in by_run and a.environment != by_run[a.collector_run_id].environment
    ]
    if foreign:
        findings.append(
            _f(
                "foreign_collector_run_lineage",
                Severity.CRITICAL,
                len(foreign),
                "attempt references a collector run outside the inspected window",
                foreign,
            )
        )
    if mismatched:
        findings.append(
            _f(
                "environment_mismatch",
                Severity.CRITICAL,
                len(mismatched),
                "attempt environment differs from its collector run's environment",
                mismatched,
            )
        )
    return findings


def check_target_local_dates(
    rows: Sequence[AttemptRow], *, station_timezones: dict[str, str], tolerance_days: int = 2
) -> list[Finding]:
    """A target date must be the station-LOCAL day near its request instant.

    A UTC-day mistake shows up here as an off-by-one for exactly the stations
    whose offset crosses midnight — which is why PHX (fixed UTC-7) and MIA
    (DST) are checked through their own zones rather than a shared one.
    """
    bad: list[str] = []
    for r in rows:
        if r.target_station_local_date is None:
            continue
        tz = station_timezones.get(r.station_code)
        if tz is None:
            continue
        local_day = r.requested_at.astimezone(ZoneInfo(tz)).date()
        if abs((r.target_station_local_date - local_day).days) > tolerance_days:
            bad.append(r.attempt_id)
    if not bad:
        return []
    return [
        _f(
            "invalid_target_local_date",
            Severity.CRITICAL,
            len(bad),
            "target_station_local_date is not the station-local day of the request",
            bad,
        )
    ]


def check_provenance(rows: Sequence[AttemptRow]) -> list[Finding]:
    """A body the platform received must remain linkable.

    Losing the payload behind a rejection destroys the only evidence of what
    was refused, which is precisely the gap this whole layer exists to close.
    """
    findings: list[Finding] = []
    saw_body = {str(AttemptStage.RAW_PAYLOAD_PERSISTED), str(AttemptStage.PARSED)}

    def collect(check: str, samples: list[str], message: str) -> None:
        if samples:
            findings.append(_f(check, Severity.CRITICAL, len(samples), message, samples))

    collect(
        "parser_rejection_missing_raw_payload",
        [
            r.attempt_id
            for r in rows
            if r.outcome == str(AttemptOutcome.PARSER_REJECTED) and r.raw_payload_id is None
        ],
        "PARSER_REJECTED without raw_payload_id — the rejected body was not preserved",
    )
    collect(
        "raw_payload_missing_for_malformed_response",
        [
            r.attempt_id
            for r in rows
            if r.outcome == str(AttemptOutcome.MALFORMED_RESPONSE)
            and r.stage in saw_body
            and r.raw_payload_id is None
        ],
        "MALFORMED_RESPONSE with a received body but no raw_payload_id",
    )
    collect(
        "raw_payload_missing_for_parser_input",
        [
            r.attempt_id
            for r in rows
            if r.stage == str(AttemptStage.PARSED)
            and r.parsed_entity_count > 0
            and r.raw_payload_id is None
        ],
        "parsed entities exist but the source body was not preserved",
    )
    collect(
        "persistence_failure_missing_provenance",
        [
            r.attempt_id
            for r in rows
            if r.outcome == str(AttemptOutcome.PERSISTENCE_FAILED)
            and (r.raw_payload_id is None or not r.persistence_error_type)
        ],
        "PERSISTENCE_FAILED without payload lineage or an error type",
    )
    return findings


def check_run_reconciliation(
    run: RunContext, rows: Sequence[AttemptRow], *, now: datetime
) -> list[Finding]:
    """Reconcile ONE completed, instrumented run.

    An active run is reported, never judged. An UNinstrumented run is skipped
    entirely: it predates attribution and legitimately has no attempts.
    """
    if not run.attempt_instrumented:
        return []
    if not run.is_complete(now):
        return [
            _f(
                "incomplete_terminal_attempt_set",
                Severity.INFO,
                1,
                f"collector run {run.collector_run_id} has not completed; "
                "reconciliation deferred rather than reported as passing",
            )
        ]

    mine = [r for r in rows if r.collector_run_id == run.collector_run_id]
    seen = {(r.station_code, r.product_type) for r in mine}
    expected = set(run.expected_pairs)
    missing = sorted(expected - seen)
    unexpected = sorted(seen - expected)

    findings: list[Finding] = []
    if missing:
        findings.append(
            _f(
                "missing_expected_attempt",
                Severity.CRITICAL,
                len(missing),
                f"collector run {run.collector_run_id} is missing attempts for expected pairs",
                [f"{s}/{p}" for s, p in missing],
            )
        )
    if unexpected:
        findings.append(
            _f(
                "collector_run_pair_mismatch",
                Severity.CRITICAL,
                len(unexpected),
                f"collector run {run.collector_run_id} recorded unexpected station/product pairs",
                [f"{s}/{p}" for s, p in unexpected],
            )
        )
    if len(mine) != len(expected):
        findings.append(
            _f(
                "collector_run_attempt_count_mismatch",
                Severity.CRITICAL,
                abs(len(mine) - len(expected)),
                f"collector run {run.collector_run_id}: {len(mine)} attempts != "
                f"{len(expected)} expected pairs",
            )
        )

    if run.legacy_invalid_items is not None:
        derived = sum(
            1
            for r in mine
            if r.outcome
            in {str(AttemptOutcome.PARSER_REJECTED), str(AttemptOutcome.MALFORMED_RESPONSE)}
        )
        if derived != run.legacy_invalid_items:
            findings.append(
                _f(
                    "collector_run_counter_mismatch",
                    Severity.CRITICAL,
                    abs(derived - run.legacy_invalid_items),
                    f"collector run {run.collector_run_id}: legacy invalid_items "
                    f"{run.legacy_invalid_items} != attempt-derived {derived}",
                )
            )
    return findings


def check_operational_outcomes(rows: Sequence[AttemptRow]) -> list[Finding]:
    """Bounded operational failures with intact evidence: WARNING, not CRITICAL.

    These are the conditions the pilot review needs attributed to a station.
    They are real, but they are collection facts — not corruption.
    """
    findings: list[Finding] = []
    for check, outcome, severity, message in (
        (
            "bounded_source_unavailable",
            AttemptOutcome.SOURCE_UNAVAILABLE,
            Severity.WARNING,
            "source confirmed unavailable for a station/product",
        ),
        (
            "bounded_request_failed",
            AttemptOutcome.REQUEST_FAILED,
            Severity.WARNING,
            "request failed with provenance intact",
        ),
        (
            "station_parser_failure",
            AttemptOutcome.PARSER_REJECTED,
            Severity.WARNING,
            "station-specific parser rejection (body preserved)",
        ),
        (
            "successful_no_data",
            AttemptOutcome.SUCCEEDED_NO_DATA,
            Severity.INFO,
            "source answered with no data — distinct from unavailable",
        ),
    ):
        samples = [f"{r.station_code}/{r.product_type}" for r in rows if r.outcome == str(outcome)]
        if samples:
            findings.append(_f(check, severity, len(samples), message, samples))
    return findings


def summarize(
    rows: Sequence[AttemptRow],
    runs: Sequence[RunContext],
    *,
    now: datetime,
    known_stations: frozenset[str],
    station_timezones: dict[str, str],
    schema_deployed: bool,
    db_revision: str,
    expected_revision: str,
) -> list[Finding]:
    """Every attempt-attribution finding, in deterministic order."""
    findings = [
        legacy_absence_finding(schema_deployed=schema_deployed),
        schema_revision_finding(db_revision=db_revision, expected_revision=expected_revision),
    ]
    if not schema_deployed:
        return findings
    findings.extend(check_attempt_integrity(rows, now=now, known_stations=known_stations))
    findings.extend(check_environment_and_lineage(rows, runs))
    findings.extend(check_target_local_dates(rows, station_timezones=station_timezones))
    findings.extend(check_provenance(rows))
    for run in runs:
        findings.extend(check_run_reconciliation(run, rows, now=now))
    findings.extend(completed_runs_missing_all_evidence(runs, rows, now=now))
    findings.extend(check_operational_outcomes(rows))
    return findings


#: Deployment boundary: a run is judged only if it carries the prospective
#: instrumentation marker. Never inferred from timestamps. That
#: instant is supplied by the caller from operational evidence (the first run
#: created after the schema existed) -- never guessed from the wall clock, and
#: never assumed for historical pre-0012 runs.
def completed_runs_missing_all_evidence(
    runs: Sequence[RunContext],
    rows: Sequence[AttemptRow],
    *,
    now: datetime,
) -> list[Finding]:
    """The rule that would have caught production run 3290.

    A COMPLETED weather run that started after attribution went live, with
    expected pairs > 0 and ZERO terminal attempts, is an integrity failure --
    not a clean zero and not informational absence. Reporting that state as
    valid is exactly how an inert deployment looked healthy.

    Pre-boundary runs are excluded: they legitimately predate the evidence and
    are LEGACY_UNKNOWN, not failures.
    """
    offenders: list[str] = []
    partial: list[str] = []
    for run in runs:
        if not run.attempt_instrumented or not run.is_complete(now):
            continue
        if not run.expected_pairs:
            continue
        mine = [r for r in rows if r.collector_run_id == run.collector_run_id]
        if not mine:
            offenders.append(f"run {run.collector_run_id}: 0/{len(run.expected_pairs)}")
        elif len(mine) < len(run.expected_pairs):
            partial.append(f"run {run.collector_run_id}: {len(mine)}/{len(run.expected_pairs)}")

    findings: list[Finding] = []
    if offenders:
        findings.append(
            _f(
                "completed_weather_run_missing_all_attempt_evidence",
                Severity.CRITICAL,
                len(offenders),
                "a completed post-deployment weather run recorded NONE of its expected "
                "attempts — attribution is deployed but inert",
                offenders,
            )
        )
    if partial:
        findings.append(
            _f(
                "completed_weather_run_partial_attempt_evidence",
                Severity.CRITICAL,
                len(partial),
                "a completed post-deployment weather run recorded only part of its "
                "expected attempts",
                partial,
            )
        )
    return findings


def to_dicts(findings: Sequence[Finding]) -> list[dict[str, Any]]:
    return [f.to_dict() for f in findings]
