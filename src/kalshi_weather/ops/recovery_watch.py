"""Kalshi production outage/recovery watch for the pending paper validation.

Answers one operational question deterministically: *is it safe to rerun the
pending one-contract paper-fill validation yet?* Four states:

    HEALTHY                 collection fresh, but a paper-side condition
                            (single collector / kill switch / reconcile)
                            fails -- data is fine, paper isn't ready
    OUTAGE_ACTIVE           the production API is unreachable or collector
                            success is stale -- collection is unavailable
    RECOVERING              endpoint reachable and a recent successful
                            collector cycle exists, but one or more evidence
                            freshness gates still fail
    PAPER_VALIDATION_READY  every gate passes -- the validation may be rerun

Freshness thresholds are **the paper engine's own** (`PaperRiskPolicy`):
this module deliberately owns no competing constants, so the watch can
never declare readiness the paper session would then reject.

Pure decision logic only (mirrors ``ops/monitor.py``'s separation): the
CLI layer gathers inputs and delivers notifications via the existing
``ops/alerting.py`` transport. Exactly-once notifications per state
transition, with one optional escalation for a long-running outage.
Nothing here runs a paper session, touches an experiment, or contacts the
exchange beyond an unauthenticated reachability probe performed by the
caller.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from kalshi_weather.paper.engine import PaperRiskPolicy

#: The exact ready notification (spec'd verbatim).
READY_MESSAGE = (
    "Kalshi production collection has recovered and all paper freshness gates pass. "
    "The pending one-contract paper-fill validation may now be rerun."
)
#: Escalate a still-active outage once after this long (existing monitor
#: convention: escalation is a single reminder, never a repeat-per-cycle).
DEFAULT_ESCALATE_AFTER_SECONDS = 6 * 3600.0


class WatchState(StrEnum):
    HEALTHY = "HEALTHY"
    OUTAGE_ACTIVE = "OUTAGE_ACTIVE"
    RECOVERING = "RECOVERING"
    PAPER_VALIDATION_READY = "PAPER_VALIDATION_READY"


@dataclass(frozen=True)
class WatchInputs:
    """Everything the classifier may know, gathered read-only by the caller."""

    api_reachable: bool
    success_run_age_seconds: float | None  # None = no successful run known
    latest_success_run_id: int | None
    latest_run_id: int | None
    latest_run_success: bool | None
    snapshot_age_seconds: float | None
    book_age_seconds: float | None
    poll_evidence_age_seconds: float | None
    collector_count: int
    kill_switch_inactive: bool
    reconcile_ok: bool


def classify(
    inputs: WatchInputs, thresholds: PaperRiskPolicy | None = None
) -> tuple[WatchState, list[str]]:
    """Deterministic state + machine-readable reason codes for failing gates.

    Gate values come from ``PaperRiskPolicy`` -- the same numbers the paper
    session enforces. "Recent successful collector run" reuses the snapshot
    threshold (a success necessarily refreshes snapshots).
    """
    t = thresholds or PaperRiskPolicy()
    reasons: list[str] = []

    if not inputs.api_reachable:
        reasons.append("api_unreachable")
    stale_success = (
        inputs.success_run_age_seconds is None
        or inputs.success_run_age_seconds > t.max_snapshot_age_seconds
    )
    if stale_success:
        reasons.append("collector_success_stale")
    if reasons:
        return WatchState.OUTAGE_ACTIVE, reasons

    if inputs.snapshot_age_seconds is None or (
        inputs.snapshot_age_seconds > t.max_snapshot_age_seconds
    ):
        reasons.append("stale_market_snapshot")
    if inputs.book_age_seconds is None or inputs.book_age_seconds > t.max_book_age_seconds:
        reasons.append("stale_book_evidence")
    if inputs.poll_evidence_age_seconds is None or (
        inputs.poll_evidence_age_seconds > t.max_poll_evidence_age_seconds
    ):
        reasons.append("stale_poll_evidence")
    if reasons:
        return WatchState.RECOVERING, reasons

    if inputs.collector_count != 1:
        reasons.append("collector_count_not_one")
    if not inputs.kill_switch_inactive:
        reasons.append("paper_kill_switch_active")
    if not inputs.reconcile_ok:
        reasons.append("paper_reconcile_failed")
    if reasons:
        return WatchState.HEALTHY, reasons

    return WatchState.PAPER_VALIDATION_READY, []


# --- exactly-once notification decisions ------------------------------------


@dataclass(frozen=True)
class WatchStateRecord:
    """Persisted watch state (JSON, atomic-rename write -- the ops/monitor
    convention). Survives restarts; the dedup key is (state, since)."""

    state: str
    since: str  # ISO timestamp the current state was entered
    outage_started_at: str | None
    last_notification_at: str | None
    last_success_run_id: int | None
    transition_reason: str
    escalated: bool = False


@dataclass(frozen=True)
class WatchDecision:
    transition: str  # entered_<state> | no_change | escalation
    should_notify: bool
    message: str
    new_state: WatchStateRecord


def _message_for(state: WatchState, reasons: list[str], inputs: WatchInputs, now: datetime) -> str:
    ages = (
        f"success_run_age_s={inputs.success_run_age_seconds} "
        f"snapshot_age_s={inputs.snapshot_age_seconds} "
        f"book_age_s={inputs.book_age_seconds} "
        f"poll_age_s={inputs.poll_evidence_age_seconds}"
    )
    if state is WatchState.PAPER_VALIDATION_READY:
        return f"{READY_MESSAGE} [{now.isoformat()}] {ages}"
    label = {
        WatchState.OUTAGE_ACTIVE: "Kalshi production collection OUTAGE",
        WatchState.RECOVERING: "Kalshi production collection RECOVERING (evidence still stale)",
        WatchState.HEALTHY: "Collection healthy; paper validation not ready",
    }[state]
    return f"{label} [{now.isoformat()}] reasons={','.join(reasons) or 'none'} {ages}"


def decide_notification(
    state: WatchState,
    reasons: list[str],
    inputs: WatchInputs,
    prior: WatchStateRecord | None,
    *,
    now: datetime,
    escalate_after_seconds: float = DEFAULT_ESCALATE_AFTER_SECONDS,
) -> WatchDecision:
    """Exactly-once per state transition; one optional outage escalation.

    Notifies on entering OUTAGE_ACTIVE, RECOVERING, or
    PAPER_VALIDATION_READY -- never on repeats of the same state, and never
    for HEALTHY (recorded in history only). A later regression and
    re-recovery is a new transition, so it re-notifies. Pure and
    deterministic given its arguments (injectable clock).
    """
    now_iso = now.isoformat()
    reason_text = ",".join(reasons) or "all_gates_pass"

    if prior is not None and prior.state == state.value:
        # Same state: maybe escalate a long outage, exactly once.
        if (
            state is WatchState.OUTAGE_ACTIVE
            and not prior.escalated
            and prior.outage_started_at is not None
            and (now - datetime.fromisoformat(prior.outage_started_at)).total_seconds()
            > escalate_after_seconds
        ):
            new = WatchStateRecord(
                **{**asdict(prior), "escalated": True, "last_notification_at": now_iso}
            )
            return WatchDecision(
                transition="escalation",
                should_notify=True,
                message="ESCALATION: " + _message_for(state, reasons, inputs, now),
                new_state=new,
            )
        return WatchDecision(
            transition="no_change", should_notify=False, message="", new_state=prior
        )

    outage_started = (
        (prior.outage_started_at if prior is not None else None)
        if state is not WatchState.OUTAGE_ACTIVE
        else (
            prior.outage_started_at
            if prior is not None and prior.state == WatchState.OUTAGE_ACTIVE.value
            else now_iso
        )
    )
    if state in (WatchState.PAPER_VALIDATION_READY, WatchState.HEALTHY):
        outage_started = None  # a full recovery closes the outage window
    should = state is not WatchState.HEALTHY
    new = WatchStateRecord(
        state=state.value,
        since=now_iso,
        outage_started_at=outage_started,
        last_notification_at=now_iso if should else (
            prior.last_notification_at if prior is not None else None
        ),
        last_success_run_id=inputs.latest_success_run_id,
        transition_reason=reason_text,
        escalated=False,
    )
    return WatchDecision(
        transition=f"entered_{state.value.lower()}",
        should_notify=should,
        message=_message_for(state, reasons, inputs, now) if should else "",
        new_state=new,
    )


# --- persistence (ops/monitor conventions: atomic JSON + append-only JSONL) --


def load_watch_state(path: Path) -> WatchStateRecord | None:
    if not path.exists():
        return None
    return WatchStateRecord(**json.loads(path.read_text()))


def save_watch_state(path: Path, record: WatchStateRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(record), indent=2, sort_keys=True))
    tmp.replace(path)  # atomic rename, never a partial state file


def append_watch_history(
    path: Path,
    *,
    timestamp: str,
    state: str,
    transition: str,
    reasons: str,
    notified: bool | None,
    detail: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "timestamp": timestamp,
            "state": state,
            "transition": transition,
            "reasons": reasons,
            "notified": notified,
            "detail": detail,
        },
        sort_keys=True,
    )
    with path.open("a") as fh:  # append-only; never truncated
        fh.write(line + "\n")
