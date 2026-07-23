"""Shared severity classification and finding type for the data quality
observatory.

Every observatory check -- new or adapted from an existing report --
reports through this one type, so the top-level report can roll findings
up into a single, deterministic status without each check inventing its
own scale.

This module computes no research statistic and makes no scientific claim.
It only classifies operational/data-quality conditions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """INFO: normal/expected state, recorded for trend visibility.
    WARNING: a condition worth an operator's attention, not yet data loss.
    CRITICAL: corruption, an integrity violation, or an unrecoverable gap
    (e.g. a missed forecast issuance -- ADR 0003: forecast history has no
    backfill source, so a capture gap is permanent)."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


_SEVERITY_RANK: dict[Severity, int] = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


def highest(severities: list[Severity]) -> Severity:
    """The most severe value in `severities`, or INFO if empty."""
    if not severities:
        return Severity.INFO
    return max(severities, key=lambda s: _SEVERITY_RANK[s])


#: The five collection streams this observatory monitors, plus "platform"
#: for cross-cutting checks (schema drift, station registry) and "backup"
#: for PostgreSQL backup/recovery health (docs/runbooks/backup_recovery.md).
Domain = str
DOMAINS: tuple[str, ...] = (
    "forecast",
    "observation",
    "market",
    "trade",
    "candle",
    "platform",
    "backup",
)


@dataclass(frozen=True, slots=True)
class Finding:
    domain: str
    check: str
    severity: Severity
    count: int
    message: str
    samples: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "check": self.check,
            "severity": self.severity.value,
            "count": self.count,
            "message": self.message,
            "samples": list(self.samples),
        }


def overall_severity(findings: list[Finding]) -> Severity:
    return highest([f.severity for f in findings])


def findings_by_severity(findings: list[Finding], severity: Severity) -> list[Finding]:
    return [f for f in findings if f.severity == severity]


def findings_by_domain(findings: list[Finding], domain: str) -> list[Finding]:
    return [f for f in findings if f.domain == domain]
