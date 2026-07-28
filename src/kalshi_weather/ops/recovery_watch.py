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
    PAPER_VALIDATION_READY  collection has recovered and evidence was fresh
                            enough at transition time -- the operator may now
                            perform the (separately gated) paper validation

Recovery-HEALTH freshness (``RecoveryHealthPolicy``) is deliberately
SEPARATE from the paper-EXECUTION fill gate (``PaperRiskPolicy``, 300s book).
The collector polls thousands of tickers on a proactive throttle, so a full
cycle takes ~555s and a perfectly healthy book is routinely 300-900s old.
Gating the watch on the strict 300s fill threshold made it flap
RECOVERING <-> PAPER_VALIDATION_READY every cycle and re-notify each time
(DQ-002). The recovery thresholds here tolerate normal between-cycle aging
(~2x cadence) while still catching a real multi-cycle stall promptly. They
NEVER permit a paper fill: PAPER_VALIDATION_READY means "recovered, go run
the validation", and that run re-checks the strict 300s gate at execution
time. See docs/runbooks/data_quality_exceptions.md (DQ-002).

Pure decision logic only (mirrors ``ops/monitor.py``'s separation): the
CLI layer gathers inputs and delivers notifications via the existing
``ops/alerting.py`` transport. Notifications are episode-scoped: one per
outage episode, one per recovery, at most one PAPER_VALIDATION_READY per
recovery episode -- normal evidence aging between healthy cycles never
re-notifies. Nothing here runs a paper session, touches an experiment, or
contacts the exchange beyond an unauthenticated reachability probe performed
by the caller.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from enum import StrEnum
from pathlib import Path

#: The exact ready notification (spec'd verbatim).
READY_MESSAGE = (
    "Kalshi production collection has recovered and all recovery-health gates pass. "
    "The pending one-contract paper-fill validation may now be rerun "
    "(its own 300s freshness gate is re-checked at run time)."
)
#: Escalate a still-active outage once after this long (existing monitor
#: convention: escalation is a single reminder, never a repeat-per-cycle).
DEFAULT_ESCALATE_AFTER_SECONDS = 6 * 3600.0


@dataclass(frozen=True)
class RecoveryHealthPolicy:
    """Cadence-aware freshness for the recovery-HEALTH decision only.

    Independent of ``PaperRiskPolicy`` (the 300s paper-execution fill gate,
    which is unchanged and still enforced at fill time). Defaults are ~2x the
    measured ~555s collector cadence, so a healthy book that is merely
    between cycles never trips RECOVERING, while a genuine multi-cycle stall
    (book/snapshot/poll evidence older than ~2 cycles) still does. Raising
    these values NEVER loosens any paper trading gate.
    """

    max_book_age_seconds: float = 1200.0
    max_snapshot_age_seconds: float = 1200.0
    max_poll_evidence_age_seconds: float = 1200.0
    #: A successful collector run older than this => OUTAGE (collection down).
    success_stale_seconds: float = 1200.0


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
    inputs: WatchInputs, policy: RecoveryHealthPolicy | None = None
) -> tuple[WatchState, list[str]]:
    """Deterministic state + machine-readable reason codes for failing gates.

    Gate values come from ``RecoveryHealthPolicy`` (cadence-aware), NOT the
    paper-execution fill gate: the watch judges collection HEALTH, not
    instantaneous fill-eligibility. "Recent successful collector run" uses the
    recovery success-stale threshold.
    """
    t = policy or RecoveryHealthPolicy()
    reasons: list[str] = []

    if not inputs.api_reachable:
        reasons.append("api_unreachable")
    stale_success = (
        inputs.success_run_age_seconds is None
        or inputs.success_run_age_seconds > t.success_stale_seconds
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
    convention). Survives restarts. Dedup is episode-scoped: ``ready_pending``
    is armed by a genuine degradation (outage, or a recovering step out of an
    outage) and disarmed when a PAPER_VALIDATION_READY notification fires, so a
    recovery is announced exactly once per episode and normal between-cycle
    aging never re-notifies."""

    state: str
    since: str  # ISO timestamp the current state was entered
    outage_started_at: str | None
    last_notification_at: str | None
    last_success_run_id: int | None
    transition_reason: str
    escalated: bool = False
    #: True when a degradation has occurred and the next full recovery should
    #: notify. New field (defaults False) -- legacy state files load safely.
    ready_pending: bool = False


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
    """Episode-scoped notification: one per outage, one per recovery, at most
    one PAPER_VALIDATION_READY per recovery episode; one optional outage
    escalation. Normal evidence aging between healthy cycles never re-notifies.

    - OUTAGE_ACTIVE (enter): notify; arm ``ready_pending``.
    - RECOVERING (enter): notify only out of an outage (or cold start); a
      RECOVERING blip from a previously-healthy state is silent (kills the
      DQ-002 flap). Arms ``ready_pending`` only when it notifies.
    - PAPER_VALIDATION_READY (enter): notify only on cold start or when
      ``ready_pending`` (a real degradation happened); then disarm.
    - HEALTHY (enter): never notifies; carries ``ready_pending``.

    Pure and deterministic given its arguments (injectable clock).
    """
    now_iso = now.isoformat()
    reason_text = ",".join(reasons) or "all_gates_pass"
    prior_ready_pending = prior.ready_pending if prior is not None else False

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

    # --- transition to a new state: decide notify + episode bookkeeping.
    cold_start = prior is None
    prior_state = prior.state if prior is not None else None
    if state is WatchState.OUTAGE_ACTIVE:
        should = True
        ready_pending = True
        outage_started = (
            prior.outage_started_at
            if prior is not None and prior_state == WatchState.OUTAGE_ACTIVE.value
            else now_iso
        )
    elif state is WatchState.RECOVERING:
        # Notify a recovery-in-progress only when it follows an outage (or on a
        # cold start into RECOVERING); a stale-evidence blip from a healthy
        # state is silent and does not arm the recovery notification.
        from_outage = cold_start or prior_state == WatchState.OUTAGE_ACTIVE.value
        should = from_outage
        ready_pending = True if from_outage else prior_ready_pending
        outage_started = prior.outage_started_at if prior is not None else None
    elif state is WatchState.PAPER_VALIDATION_READY:
        should = cold_start or prior_ready_pending
        ready_pending = False
        outage_started = None  # a full recovery closes the outage window
    else:  # HEALTHY: data recovered but a paper-side gate fails; never notifies
        should = False
        ready_pending = prior_ready_pending
        outage_started = None

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
        ready_pending=ready_pending,
    )
    return WatchDecision(
        transition=f"entered_{state.value.lower()}",
        should_notify=should,
        message=_message_for(state, reasons, inputs, now) if should else "",
        new_state=new,
    )


# --- persistence (ops/monitor conventions: atomic JSON + append-only JSONL) --


def load_watch_state(path: Path) -> WatchStateRecord | None:
    """Read the persisted state, tolerating legacy files. Unknown keys are
    dropped and missing new fields (e.g. ``ready_pending``) take their
    conservative defaults, so a file written before DQ-002 loads without a
    notification storm. A malformed/unreadable file fails closed (returns
    ``None`` = treated as first run) rather than raising."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return None
        known = {f.name for f in fields(WatchStateRecord)}
        filtered = {k: v for k, v in raw.items() if k in known}
        # Missing required fields (state/since/...) raise TypeError below and
        # are caught -> None (fail closed); missing optionals take defaults.
        return WatchStateRecord(**filtered)
    except (json.JSONDecodeError, TypeError, ValueError, OSError):
        return None


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
