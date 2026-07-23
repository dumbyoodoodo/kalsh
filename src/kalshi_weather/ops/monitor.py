"""Alert-state machine + history log for the data quality observatory's
scheduled monitor run (``ops monitor``; see
``docs/runbooks/monitoring_alerting.md``).

Three concerns, kept as separated as the rest of this codebase's ops/
observatory modules (pure decision logic vs. thin I/O):

1. **State machine** (`decide_alert`): given the observatory's current
   overall `Severity` and the last-persisted `MonitorState`, decide whether
   this run should alert, and what the next persisted state is. A pure
   function of ``(current, prior, now)`` -- independently testable and
   deterministic.
2. **Message formatting** (`format_alert_message`, `summarize_reason`): turn
   an `ObservatoryReport` + `AlertDecision` into text, for whichever
   transport `ops/alerting.py` is configured to use and for the history log.
3. **Persistence** (`load_state`/`save_state`/`append_history`,
   `try_acquire_lock`): a small JSON state file (atomic-rename write,
   matching `ops/restart_policy.py`'s convention) and an append-only JSONL
   alert-history log -- never overwritten, never truncated; this is the
   audit trail for "did an alert actually fire, and was it delivered." A
   non-blocking `flock` guard prevents two `ops monitor` runs from
   overlapping if one outlives the scheduling interval.

This module computes no research statistic and repairs nothing --
detection and notification only. A human decides what action, if any, to
take.
"""

from __future__ import annotations

import fcntl
import json
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

from kalshi_weather.observatory.severity import Severity

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from kalshi_weather.observatory.report import ObservatoryReport

STATE_SCHEMA = 1

#: Alert transitions, in the vocabulary the state machine, message
#: formatter, and history log all share.
TRANSITION_NEW_PROBLEM = "new_problem"
TRANSITION_SEVERITY_CHANGED = "severity_changed"
TRANSITION_RECOVERED = "recovered"
TRANSITION_NO_CHANGE = "no_change"

_MAX_FINDINGS_PER_SEVERITY = 10
_MAX_REASON_FINDINGS = 5


@dataclass(frozen=True, slots=True)
class MonitorState:
    severity: Severity
    since: str  # ISO-8601 timestamp this severity began
    last_alert_at: str | None  # ISO-8601 timestamp of the last alert sent for this severity


@dataclass(frozen=True, slots=True)
class AlertDecision:
    transition: str
    should_alert: bool
    new_state: MonitorState
    prior_severity: Severity | None


def decide_alert(current: Severity, prior: MonitorState | None, *, now: datetime) -> AlertDecision:
    """Pure decision: alert exactly once per severity transition, never
    while the severity is unchanged from the last-persisted state --
    HEALTHY->CRITICAL alerts once, repeated CRITICAL runs never repeat the
    alert, and CRITICAL->HEALTHY sends a recovery notification. A missing
    `prior` (first-ever run) establishes a baseline and alerts immediately
    only if that baseline is already unhealthy -- there is no "prior
    healthy" state to compare a fresh CRITICAL/WARNING against, but it is
    still worth surfacing right away rather than waiting for the next
    transition. Deterministic: depends only on its three arguments, never
    on any other process state."""
    now_iso = now.isoformat()

    if prior is None:
        should_alert = current != Severity.INFO
        transition = TRANSITION_NEW_PROBLEM if should_alert else TRANSITION_NO_CHANGE
        new_state = MonitorState(
            severity=current, since=now_iso, last_alert_at=now_iso if should_alert else None
        )
        return AlertDecision(
            transition=transition,
            should_alert=should_alert,
            new_state=new_state,
            prior_severity=None,
        )

    if current == prior.severity:
        return AlertDecision(
            transition=TRANSITION_NO_CHANGE,
            should_alert=False,
            new_state=prior,
            prior_severity=prior.severity,
        )

    if prior.severity == Severity.INFO:
        transition = TRANSITION_NEW_PROBLEM
    elif current == Severity.INFO:
        transition = TRANSITION_RECOVERED
    else:
        transition = TRANSITION_SEVERITY_CHANGED

    new_state = MonitorState(severity=current, since=now_iso, last_alert_at=now_iso)
    return AlertDecision(
        transition=transition,
        should_alert=True,
        new_state=new_state,
        prior_severity=prior.severity,
    )


def format_alert_message(report: ObservatoryReport, decision: AlertDecision) -> str:
    """Human-readable message body for whichever transport is configured.
    Deterministic given the same report + decision (no wall-clock reads
    beyond what `report.generated_at` already carries)."""
    lines = [f"[{decision.transition.upper()}] observatory status: {report.status.value.upper()}"]
    if decision.prior_severity is not None:
        lines.append(
            f"previous: {decision.prior_severity.value.upper()} -> "
            f"now: {report.status.value.upper()}"
        )
    lines.append(f"generated: {report.generated_at}")

    if decision.transition == TRANSITION_RECOVERED:
        lines.append("all findings back to INFO -- no action needed.")
        return "\n".join(lines)

    for severity in (Severity.CRITICAL, Severity.WARNING):
        findings = report.by_severity(severity)
        if not findings:
            continue
        lines.append(f"-- {severity.value.upper()} ({len(findings)}) --")
        for f in findings[:_MAX_FINDINGS_PER_SEVERITY]:
            lines.append(f"{f.domain}/{f.check}: {f.message}")
        if len(findings) > _MAX_FINDINGS_PER_SEVERITY:
            lines.append(f"... and {len(findings) - _MAX_FINDINGS_PER_SEVERITY} more")
    return "\n".join(lines)


def summarize_reason(report: ObservatoryReport, decision: AlertDecision) -> str:
    """Short, single-line summary for the history log's `reason` field."""
    if decision.transition == TRANSITION_NO_CHANGE:
        return f"severity unchanged ({report.status.value})"
    if decision.transition == TRANSITION_RECOVERED:
        return "all findings returned to INFO"
    bad = [f for sev in (Severity.CRITICAL, Severity.WARNING) for f in report.by_severity(sev)]
    if not bad:
        return "no findings"
    checks = ", ".join(f"{f.domain}/{f.check}" for f in bad[:_MAX_REASON_FINDINGS])
    if len(bad) > _MAX_REASON_FINDINGS:
        checks += f", +{len(bad) - _MAX_REASON_FINDINGS} more"
    return checks


def load_state(path: Path) -> MonitorState | None:
    """`None` means no prior run has ever persisted state -- see
    `decide_alert`'s baseline handling."""
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return MonitorState(
        severity=Severity(raw["severity"]),
        since=raw["since"],
        last_alert_at=raw.get("last_alert_at"),
    )


def save_state(path: Path, state: MonitorState) -> None:
    """Atomic-rename write (matches `ops/restart_policy.write_manifest`) --
    a crash mid-write must never leave a corrupt/partial state file, which
    would otherwise make the next run re-derive a wrong transition."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": STATE_SCHEMA,
        "severity": state.severity.value,
        "since": state.since,
        "last_alert_at": state.last_alert_at,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


def append_history(
    path: Path,
    *,
    timestamp: str,
    severity: Severity,
    transition: str,
    reason: str,
    delivered: bool | None,
    detail: str,
) -> None:
    """Append one line to the alert-history JSONL log -- append-only, never
    truncated or rewritten (the audit trail for whether an alert actually
    fired and was delivered). `delivered=None` means no delivery was even
    attempted (a suppressed, no-change run)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": timestamp,
        "severity": severity.value,
        "transition": transition,
        "reason": reason,
        "delivered": delivered,
        "detail": detail,
    }
    with path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def try_acquire_lock(lock_path: Path) -> IO[str] | None:
    """Non-blocking exclusive lock so two `ops monitor` runs never overlap
    (e.g. a slow observatory query outliving the launchd scheduling
    interval). Returns the open file handle (keep it referenced for the
    run's duration; the kernel releases the lock the instant the process
    exits, however it exits) or `None` if another instance already holds
    it -- the caller should skip this cycle rather than wait, since the
    next scheduled run is minutes away, not retry-and-block like the
    collector's own single-instance guard (`scripts/service/launch.py`)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")  # noqa: SIM115 -- held for the run's duration
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh
