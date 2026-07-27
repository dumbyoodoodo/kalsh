"""Reusable leakage linter for point-in-time research datasets.

Narrow, metadata-level rules distilled from the 2026-07-27 data-lineage
audit. Each rule operates on plain rows/frames or source text, computes no
predictive metric, fails closed on malformed input, and emits
machine-readable findings with a stable rule ID and severity — so every
future preregistration can run the same checks before freezing a design.

Temporal contract enforced here (documented in the audit):

- ``decision_time`` — the instant a research row claims to represent; every
  feature must be *available* at or before it.
- **Publication availability** (``issue_time``/``issuance_time``): the
  instant a source made a fact public. Legitimate for market-efficiency
  questions (the semantics the earlier registered experiments use).
- **Ingestion availability** (``observed_at``): the instant THIS system
  stored the fact. Required for tradability questions (the semantics the
  revision-underreaction registration froze). A design must declare which
  contract it uses; mixing them silently is a finding.
- **Label availability**: strictly after the feature cutoff — a label or
  terminal-state field appearing in a feature frame is always a finding.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo


class LintError(ValueError):
    """Malformed linter input -- fail closed rather than skip."""


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class LintFinding:
    rule_id: str
    severity: Severity
    message: str
    count: int
    samples: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "message": self.message,
            "count": self.count,
            "samples": list(self.samples),
        }


Row = Mapping[str, Any]


def _require(rows: Sequence[Row], cols: Sequence[str], rule: str) -> None:
    for r in rows:
        missing = [c for c in cols if c not in r]
        if missing:
            raise LintError(f"{rule}: row missing required fields {missing}")


def _sample(items: list[str], n: int = 5) -> tuple[str, ...]:
    return tuple(items[:n])


# --- R001: feature availability after decision -------------------------------


def check_feature_after_decision(
    rows: Sequence[Row], *, availability_field: str, rule_id: str = "R001"
) -> LintFinding | None:
    """Any feature row whose declared availability timestamp is after its
    decision_time is look-ahead by definition."""
    _require(rows, ["decision_time", availability_field], rule_id)
    bad = [
        r
        for r in rows
        if r[availability_field] is not None and r[availability_field] > r["decision_time"]
    ]
    if not bad:
        return None
    return LintFinding(
        rule_id=rule_id,
        severity=Severity.ERROR,
        message=f"{len(bad)} rows use a feature whose {availability_field!r} "
        "is after decision_time (look-ahead)",
        count=len(bad),
        samples=_sample([str(r.get("key", r[availability_field])) for r in bad]),
    )


def check_publication_after_decision(rows: Sequence[Row]) -> LintFinding | None:
    """R002: issue_time after decision_time -- forbidden under BOTH contracts."""
    return check_feature_after_decision(rows, availability_field="issue_time", rule_id="R002")


# --- R003: mixed availability contracts --------------------------------------


def check_contract_declared(
    declared: str, used_fields: Sequence[str]
) -> LintFinding | None:
    """A design declares 'publication' or 'ingestion'; using the other
    contract's field silently is a finding (the publication-vs-ingestion
    split between registered designs must always be explicit)."""
    if declared not in ("publication", "ingestion"):
        raise LintError(f"unknown availability contract {declared!r}")
    forbidden = {"publication": "observed_at", "ingestion": "issue_time"}[declared]
    misused = [f for f in used_fields if f == forbidden]
    if not misused:
        return None
    return LintFinding(
        rule_id="R003",
        severity=Severity.ERROR,
        message=f"design declares {declared!r} availability but uses {forbidden!r} "
        "as an availability field",
        count=len(misused),
    )


# --- R004: label availability vs feature cutoff ------------------------------


def check_label_after_cutoff(rows: Sequence[Row]) -> LintFinding | None:
    """A label's availability must be strictly AFTER the feature cutoff."""
    _require(rows, ["feature_cutoff", "label_available_at"], "R004")
    bad = [
        r
        for r in rows
        if r["label_available_at"] is not None
        and r["label_available_at"] <= r["feature_cutoff"]
    ]
    if not bad:
        return None
    return LintFinding(
        rule_id="R004",
        severity=Severity.ERROR,
        message=f"{len(bad)} rows have a label available at or before the feature cutoff",
        count=len(bad),
        samples=_sample([str(r.get("key", "?")) for r in bad]),
    )


# --- R005: terminal/label columns inside a feature frame ---------------------

TERMINAL_COLUMNS = ("result", "kalshi_result", "settlement_ts", "expiration_value", "settled")


def check_no_terminal_columns(feature_columns: Sequence[str]) -> LintFinding | None:
    hits = [c for c in feature_columns if c in TERMINAL_COLUMNS]
    if not hits:
        return None
    return LintFinding(
        rule_id="R005",
        severity=Severity.ERROR,
        message=f"feature frame exposes terminal/label columns: {hits}",
        count=len(hits),
        samples=tuple(hits),
    )


# --- R006: group leakage across partitions -----------------------------------


def check_group_partition_isolation(rows: Sequence[Row]) -> LintFinding | None:
    _require(rows, ["event_group", "partition"], "R006")
    seen: dict[Any, str] = {}
    bad: list[str] = []
    for r in rows:
        g, p = r["event_group"], r["partition"]
        if g in seen and seen[g] != p:
            bad.append(f"{g}:{seen[g]}+{p}")
        seen.setdefault(g, p)
    if not bad:
        return None
    return LintFinding(
        rule_id="R006",
        severity=Severity.ERROR,
        message=f"{len(bad)} event groups appear in more than one partition",
        count=len(bad),
        samples=_sample(bad),
    )


# --- R007: forbidden-window rows ---------------------------------------------


def check_excluded_window(
    rows: Sequence[Row], *, start: date, end: date, window_name: str
) -> LintFinding | None:
    _require(rows, ["target_date"], "R007")
    bad = [r for r in rows if start <= r["target_date"] <= end]
    if not bad:
        return None
    return LintFinding(
        rule_id="R007",
        severity=Severity.ERROR,
        message=f"{len(bad)} rows fall inside the excluded window {window_name} "
        f"({start}..{end})",
        count=len(bad),
        samples=_sample([str(r["target_date"]) for r in bad]),
    )


# --- R008: backfilled rows treated as contemporaneous ------------------------


def check_backfill_flagged(
    rows: Sequence[Row], *, max_ingest_delay_hours: float, contract: str
) -> LintFinding | None:
    """Under the *ingestion* contract late arrival is handled by construction;
    under the *publication* contract, rows ingested far after issuance must be
    explicitly marked (``backfilled``) or they silently import an
    execution-realism gap."""
    if contract == "ingestion":
        return None
    _require(rows, ["issue_time", "observed_at"], "R008")
    bad = []
    for r in rows:
        if r["observed_at"] is None or r["issue_time"] is None:
            continue
        delay_h = (r["observed_at"] - r["issue_time"]).total_seconds() / 3600
        if delay_h > max_ingest_delay_hours and not r.get("backfilled", False):
            bad.append(str(r.get("key", r["issue_time"])))
    if not bad:
        return None
    return LintFinding(
        rule_id="R008",
        severity=Severity.WARNING,
        message=f"{len(bad)} publication-contract rows were ingested more than "
        f"{max_ingest_delay_hours}h after issuance without a backfilled marker",
        count=len(bad),
        samples=_sample(bad),
    )


# --- R009: provenance completeness -------------------------------------------


def check_provenance(
    rows: Sequence[Row], *, id_field: str = "raw_payload_id"
) -> LintFinding | None:
    bad = [str(r.get("key", "?")) for r in rows if not r.get(id_field)]
    if not bad:
        return None
    return LintFinding(
        rule_id="R009",
        severity=Severity.WARNING,
        message=f"{len(bad)} rows lack a {id_field!r} provenance link",
        count=len(bad),
        samples=_sample(bad),
    )


# --- R010: station-local date derivation -------------------------------------


def check_local_date_derivation(rows: Sequence[Row]) -> LintFinding | None:
    """Recompute the station-local date from the aware instant + IANA tz; a
    stored local date that disagrees indicates truncation before conversion
    (the classic UTC-date bug)."""
    _require(rows, ["instant_utc", "timezone", "local_date"], "R010")
    bad = []
    for r in rows:
        instant: datetime = r["instant_utc"]
        if instant.tzinfo is None:
            raise LintError("R010: instant_utc must be timezone-aware")
        expected = instant.astimezone(ZoneInfo(r["timezone"])).date()
        if r["local_date"] != expected:
            bad.append(f"{r.get('key','?')}: stored={r['local_date']} expected={expected}")
    if not bad:
        return None
    return LintFinding(
        rule_id="R010",
        severity=Severity.ERROR,
        message=f"{len(bad)} rows derive the station-local date incorrectly",
        count=len(bad),
        samples=_sample(bad),
    )


# --- R011: current-universe survivorship -------------------------------------


def check_universe_source(universe_source: str) -> LintFinding | None:
    """A historical dataset built from the CURRENT active-market universe
    silently drops delisted/expired markets (survivorship). The universe must
    come from append-only listings/snapshots."""
    if universe_source in ("append_only_snapshots", "append_only_listings"):
        return None
    return LintFinding(
        rule_id="R011",
        severity=Severity.ERROR,
        message=f"historical universe built from {universe_source!r}; must be an "
        "append-only history source",
        count=1,
    )


# --- R012: latest-state source patterns (static text scan) -------------------

#: Query/code patterns that select CURRENT state; safe in live-ops code,
#: findings inside research dataset builders unless allowlisted with a
#: documented invariant.
LATEST_STATE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"order_by\([^)]*\.desc\(\)\)\s*\.limit\(1\)", "ORM latest-row pattern"),
    (r"order by [\w.]+ desc\s+limit 1", "SQL latest-row pattern"),
    (r"distinct on \([^)]*\)[^;]*order by[^;]*desc", "SQL distinct-on-latest pattern"),
    (r"\.drop_nulls\(\)\.first\(\)", "first-non-null identity pattern"),
)


def scan_latest_state(
    source_text: str, *, path: str, allowlist: Sequence[str] = ()
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for pattern, label in LATEST_STATE_PATTERNS:
        for m in re.finditer(pattern, source_text, re.IGNORECASE):
            line = source_text.count("\n", 0, m.start()) + 1
            key = f"{path}:{line}"
            if any(a in key or a in m.group(0) for a in allowlist):
                continue
            findings.append(
                LintFinding(
                    rule_id="R012",
                    severity=Severity.WARNING,
                    message=f"latest-state pattern ({label}) in research path",
                    count=1,
                    samples=(key,),
                )
            )
    return findings


# --- runner ------------------------------------------------------------------


@dataclass
class LintReport:
    findings: list[LintFinding] = field(default_factory=list)

    def add(self, finding: LintFinding | None) -> None:
        if finding is not None:
            self.findings.append(finding)

    def extend(self, findings: Sequence[LintFinding]) -> None:
        self.findings.extend(findings)

    @property
    def errors(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    def verdict(self, *, fail_on: Severity = Severity.ERROR) -> str:
        order = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}
        worst = max((order[f.severity] for f in self.findings), default=-1)
        if worst >= order[fail_on]:
            return "FAIL"
        return "PASS_WITH_WARNINGS" if worst >= order[Severity.WARNING] else "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict(),
            "findings": [f.to_dict() for f in self.findings],
        }
