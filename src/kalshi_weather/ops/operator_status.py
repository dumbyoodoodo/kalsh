"""Unified operator-status decision engine (pure, read-only).

Maps already-gathered checkpoint inputs -- research-readiness state strings,
calendar boundaries, coverage counts, operational health flags -- to a small
stable status vocabulary, orders them by urgency, and derives the single next
permitted action plus the standing forbidden-action list. This module executes
NOTHING: no experiment, no settlement, no service, no network, no database, no
wall clock (the caller injects ``now``). It computes no performance metric and
never inspects H0019/H0020 outcomes -- research checkpoints carry only their
counts-only readiness *state label*, passed through verbatim.

READY_FOR_HUMAN_APPROVAL means the structural/time gates are clear and a human
may now decide -- it is NOT automatic authorization, and scientific readiness
is never scientific support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

BANNERS: tuple[str, ...] = (
    "COUNTS ONLY — NOT AN EXPERIMENT RESULT",
    "HUMAN APPROVAL REQUIRED for any execution",
    "DO NOT RUN EARLY — calendar and evidence gates bind",
    "NO EXCHANGE ORDERS — LIVE TRADING DISABLED",
)

#: Standing prohibitions, always shown. These never depend on the clock.
FORBIDDEN_ACTIONS: tuple[str, ...] = (
    "submit any exchange order (live or demo)",
    "enable live trading (ENABLE_LIVE_TRADING stays false)",
    "run any experiment before its gates pass AND a human approves",
    "settle the paper position outside a dedicated approved settlement task",
    "add or change stations",
    "restart the collector or any service",
    "inspect H0019/H0020 partial test performance",
    "auto-chain readiness into execution",
)

#: Authoritative calendar boundaries (UTC dates). Not tunable here.
PILOT_REVIEW_BOUNDARY = date(2026, 8, 4)
E0002_REMEASURE_BOUNDARY = date(2026, 8, 10)
H0012R_BOUNDARY = date(2026, 8, 19)
H0019_TEST_END_BOUNDARY = date(2026, 8, 25)
H0020_ABSOLUTE_TEST_END = date(2026, 10, 6)
#: E0002 must not consume any reserved confirmatory window.
RESERVED_WINDOW = (date(2026, 8, 12), date(2026, 10, 6))


class CheckpointState(StrEnum):
    ACTION_REQUIRED = "ACTION_REQUIRED"
    READY_FOR_HUMAN_APPROVAL = "READY_FOR_HUMAN_APPROVAL"
    WAITING_FOR_EVIDENCE = "WAITING_FOR_EVIDENCE"
    WAITING_FOR_TIME = "WAITING_FOR_TIME"
    COLLECTING = "COLLECTING"
    HEALTHY = "HEALTHY"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


#: Display / next-action urgency order (most urgent first).
URGENCY: tuple[CheckpointState, ...] = (
    CheckpointState.ACTION_REQUIRED,
    CheckpointState.READY_FOR_HUMAN_APPROVAL,
    CheckpointState.WAITING_FOR_EVIDENCE,
    CheckpointState.WAITING_FOR_TIME,
    CheckpointState.COLLECTING,
    CheckpointState.HEALTHY,
    CheckpointState.COMPLETE,
    CheckpointState.BLOCKED,
    CheckpointState.NOT_APPLICABLE,
)

#: States a human should act on (next-action candidates), most urgent first.
_ACTIONABLE = (
    CheckpointState.ACTION_REQUIRED,
    CheckpointState.READY_FOR_HUMAN_APPROVAL,
)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    area: str
    state: CheckpointState
    blocker: str
    evidence_source: str
    next_action: str
    earliest_ts: str | None
    command: str
    approval_required: bool
    auto_forbidden: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "area": self.area,
            "state": self.state.value,
            "blocker": self.blocker,
            "evidence_source": self.evidence_source,
            "next_action": self.next_action,
            "earliest_ts": self.earliest_ts,
            "command": self.command,
            "approval_required": self.approval_required,
            "auto_forbidden": self.auto_forbidden,
            "detail": self.detail,
        }


def _iso(d: date | datetime | None) -> str | None:
    return d.isoformat() if d is not None else None


# --------------------------------------------------------------------------
# Per-checkpoint pure classifiers. Each takes minimal inputs + injected `now`.
# --------------------------------------------------------------------------

def classify_paper(
    *,
    has_open_position: bool,
    terminal_result_available: bool,
    reconcile_ok: bool,
    kill_switch_active: bool,
    close_time: datetime | None,
    now: datetime,
) -> Checkpoint:
    cmd = "uv run kalshi-weather paper settle  # only via a dedicated approved task"
    if not reconcile_ok:
        return Checkpoint(
            "paper", CheckpointState.ACTION_REQUIRED,
            "paper ledger does not reconcile", "paper reconcile",
            "STOP: investigate the ledger mismatch read-only before anything else.",
            None, "uv run kalshi-weather paper reconcile", True, True,
        )
    if kill_switch_active:
        return Checkpoint(
            "paper", CheckpointState.ACTION_REQUIRED, "kill switch active",
            "paper forward-status", "Investigate why the kill switch tripped.",
            None, "uv run kalshi-weather paper forward-status", True, True,
        )
    if not has_open_position:
        return Checkpoint(
            "paper", CheckpointState.COMPLETE, "no open position",
            "paper forward-status", "None — no open paper position.",
            None, "uv run kalshi-weather paper forward-status", False, True,
        )
    if terminal_result_available:
        return Checkpoint(
            "paper", CheckpointState.READY_FOR_HUMAN_APPROVAL,
            "authoritative terminal result available",
            "market_snapshots (finalized result + settlement_ts)",
            "Human-approve the dedicated settlement task, then paper settle once.",
            _iso(now), cmd, True, True,
        )
    return Checkpoint(
        "paper", CheckpointState.WAITING_FOR_EVIDENCE,
        "market still active; no authoritative Kalshi result yet",
        "market_snapshots (status/result)",
        "Wait for a finalized Kalshi result after market close; do not infer it.",
        _iso(close_time), "uv run kalshi-weather paper forward-status", True, True,
    )


def classify_station_pilot(
    *, review_ready: bool, integrity_ok: bool, earliest: date | None, now: datetime
) -> Checkpoint:
    cmd = "uv run kalshi-weather ops station-pilot-review"
    if not integrity_ok:
        return Checkpoint(
            "station_pilot", CheckpointState.ACTION_REQUIRED,
            "pilot integrity failure", "ops station-pilot-review",
            "Investigate the pilot integrity failure read-only.", None, cmd, True, True,
        )
    if not review_ready:
        return Checkpoint(
            "station_pilot", CheckpointState.WAITING_FOR_TIME,
            "seven-day review window not elapsed", "ops station-pilot-review",
            "Wait for the review window; keep collecting.", _iso(earliest), cmd, False, True,
        )
    return Checkpoint(
        "station_pilot", CheckpointState.READY_FOR_HUMAN_APPROVAL,
        "review window elapsed and gates pass", "ops station-pilot-review",
        "Human-review continue/expand/rollback; expansion is not automatic.",
        _iso(now), cmd, True, True,
    )


def classify_e0002(
    *, complete_production_days: int, required_days: int, now: datetime, earliest: date | None
) -> Checkpoint:
    cmd = "uv run kalshi-weather research exploratory e0002 --json"
    in_reserved = RESERVED_WINDOW[0] <= now.date() <= RESERVED_WINDOW[1]
    if complete_production_days < required_days:
        return Checkpoint(
            "e0002", CheckpointState.WAITING_FOR_TIME,
            f"{complete_production_days}/{required_days} complete production days",
            "orderbook_snapshots (production complete-day count)",
            "Wait for 14 full production days, then re-run the exploratory remeasure.",
            _iso(earliest), cmd, False, True,
            detail={"complete_production_days": complete_production_days},
        )
    if in_reserved:
        return Checkpoint(
            "e0002", CheckpointState.BLOCKED,
            "reserved confirmatory window active — remeasure would risk contamination",
            "reserved-window calendar",
            "Do not run inside the reserved window; schedule after it.",
            _iso(RESERVED_WINDOW[1]), cmd, True, True,
        )
    return Checkpoint(
        "e0002", CheckpointState.READY_FOR_HUMAN_APPROVAL,
        "14 production days available, outside reserved windows",
        "orderbook_snapshots (production complete-day count)",
        "Run the exploratory remeasurement (descriptive only; not confirmatory).",
        _iso(now), cmd, True, True,
        detail={"complete_production_days": complete_production_days},
    )


def _passthrough_research(
    area: str,
    *,
    readiness_state: str,
    boundary: date,
    command: str,
    now: datetime,
    ready_tokens: tuple[str, ...] = ("READY_FOR_FINAL_TEST", "READY"),
    invalid_tokens: tuple[str, ...] = ("INVALID", "INTEGRITY_BLOCKED"),
    deferred_tokens: tuple[str, ...] = ("DEFERRED", "DEFERRED_INSUFFICIENT_DATA"),
    collecting_tokens: tuple[str, ...] = ("COLLECTING",),
) -> Checkpoint:
    """Map a counts-only readiness *state label* to a dashboard state. Never
    reads counts or outcomes -- pure string passthrough plus the calendar
    boundary. Fails safe: an unrecognized label is WAITING_FOR_EVIDENCE."""
    s = readiness_state.upper()
    before_boundary = now.date() < boundary
    if any(t in s for t in invalid_tokens):
        return Checkpoint(
            area, CheckpointState.ACTION_REQUIRED, f"integrity/invalid state: {readiness_state}",
            f"experiment readiness {area}", "STOP: investigate the integrity failure read-only.",
            None, command, True, True, detail={"readiness_state": readiness_state},
        )
    if any(t in s for t in deferred_tokens):
        return Checkpoint(
            area, CheckpointState.BLOCKED, f"deferred (insufficient data): {readiness_state}",
            f"experiment readiness {area}", "Final: record and propose a new hypothesis.",
            None, command, True, True, detail={"readiness_state": readiness_state},
        )
    if any(t in s for t in ready_tokens):
        # Never READY before the authoritative boundary, even if the label says so.
        if before_boundary:
            return Checkpoint(
                area, CheckpointState.WAITING_FOR_TIME,
                f"calendar boundary {boundary.isoformat()} not reached",
                f"experiment readiness {area}",
                "Wait for the boundary; readiness is not support.",
                _iso(boundary), command, True, True,
                detail={"readiness_state": readiness_state},
            )
        return Checkpoint(
            area, CheckpointState.READY_FOR_HUMAN_APPROVAL,
            "counts-only readiness satisfied; human approval + gate sequence required",
            f"experiment readiness {area}",
            "Follow the runbook gate sequence, then a single approved run. Readiness ≠ support.",
            _iso(now), command, True, True, detail={"readiness_state": readiness_state},
        )
    if before_boundary:
        return Checkpoint(
            area, CheckpointState.WAITING_FOR_TIME,
            f"calendar boundary {boundary.isoformat()} not reached ({readiness_state})",
            f"experiment readiness {area}", "Wait for the boundary; keep collecting.",
            _iso(boundary), command, True, True, detail={"readiness_state": readiness_state},
        )
    if any(t in s for t in collecting_tokens) or "COLLECTING" in s:
        return Checkpoint(
            area, CheckpointState.COLLECTING, f"accumulating data ({readiness_state})",
            f"experiment readiness {area}", "Keep collecting; re-run the counts-only readiness.",
            None, command, True, True, detail={"readiness_state": readiness_state},
        )
    return Checkpoint(
        area, CheckpointState.WAITING_FOR_EVIDENCE, f"gates unmet ({readiness_state})",
        f"experiment readiness {area}", "Keep collecting; re-run the counts-only readiness.",
        None, command, True, True, detail={"readiness_state": readiness_state},
    )


def classify_h0012r(*, readiness_state: str, now: datetime) -> Checkpoint:
    return _passthrough_research(
        "h0012r", readiness_state=readiness_state, boundary=H0012R_BOUNDARY,
        command="uv run kalshi-weather experiment readiness h0012r", now=now,
    )


def classify_h0019(*, readiness_state: str, now: datetime) -> Checkpoint:
    return _passthrough_research(
        "h0019", readiness_state=readiness_state, boundary=H0019_TEST_END_BOUNDARY,
        command="uv run kalshi-weather experiment readiness h0019", now=now,
    )


def classify_h0020(*, readiness_state: str, now: datetime) -> Checkpoint:
    return _passthrough_research(
        "h0020", readiness_state=readiness_state, boundary=H0020_ABSOLUTE_TEST_END,
        command="uv run kalshi-weather experiment readiness h0020", now=now,
    )


def classify_operational(
    area: str,
    *,
    healthy: bool | None,
    detail_msg: str,
    evidence: str,
    command: str,
    unavailable: bool = False,
) -> Checkpoint:
    """Ops health checkpoints (collector, backups, restore-drill, recovery
    watch, storage, observatory). ``healthy=None`` or ``unavailable`` -> data
    could not be read this run (partial-data posture)."""
    if unavailable or healthy is None:
        return Checkpoint(
            area, CheckpointState.NOT_APPLICABLE, "status unavailable this run",
            evidence, f"Run {command} directly.", None, command, False, True,
        )
    if healthy:
        return Checkpoint(
            area, CheckpointState.HEALTHY, "nominal", evidence, detail_msg or "None.",
            None, command, False, True,
        )
    return Checkpoint(
        area, CheckpointState.ACTION_REQUIRED, detail_msg or "degraded", evidence,
        f"Investigate: {command}", None, command, True, True,
    )


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

@dataclass
class Dashboard:
    generated_at: datetime
    git_commit: str
    checkpoints: list[Checkpoint]

    def ordered(self) -> list[Checkpoint]:
        rank = {s: i for i, s in enumerate(URGENCY)}
        return sorted(
            self.checkpoints, key=lambda c: (rank.get(c.state, 99), c.area)
        )

    def overall_state(self) -> CheckpointState:
        present = {c.state for c in self.checkpoints}
        for s in URGENCY:
            if s in present and s not in (CheckpointState.NOT_APPLICABLE,):
                return s
        return CheckpointState.HEALTHY

    def next_action(self) -> Checkpoint | None:
        for s in _ACTIONABLE:
            for c in self.ordered():
                if c.state is s:
                    return c
        return None

    def to_dict(self) -> dict[str, Any]:
        by_area = {c.area: c.to_dict() for c in self.checkpoints}
        nxt = self.next_action()
        return {
            "banners": list(BANNERS),
            "generated_at": self.generated_at.isoformat(),
            "git_commit": self.git_commit,
            "overall_state": self.overall_state().value,
            "next_action": (
                {"area": nxt.area, "action": nxt.next_action, "command": nxt.command}
                if nxt
                else None
            ),
            "forbidden_actions": list(FORBIDDEN_ACTIONS),
            "live_trading_enabled": False,
            **{
                key: by_area.get(key)
                for key in (
                    "collector", "paper", "station_pilot", "e0002", "h0012r",
                    "h0019", "h0020", "backups", "restore_drill", "observatory", "storage",
                )
            },
            "checkpoints": [c.to_dict() for c in self.ordered()],
        }
