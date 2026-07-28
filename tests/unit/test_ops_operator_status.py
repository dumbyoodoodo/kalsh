"""Operator-status decision engine: per-checkpoint state mapping, injected-clock
boundaries, urgency ordering, next-action selection, forbidden-action list, and
JSON determinism. Pure synthetic inputs; no DB, no network, no clock."""

import json
from datetime import UTC, date, datetime

from kalshi_weather.ops.operator_status import (
    BANNERS,
    FORBIDDEN_ACTIONS,
    Checkpoint,
    CheckpointState,
    Dashboard,
    classify_e0002,
    classify_h0012r,
    classify_h0019,
    classify_h0020,
    classify_operational,
    classify_paper,
    classify_station_pilot,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


# --- paper -----------------------------------------------------------------

def test_paper_active_market_waits_for_evidence() -> None:
    c = classify_paper(
        has_open_position=True, terminal_result_available=False, reconcile_ok=True,
        kill_switch_active=False, close_time=datetime(2026, 7, 29, 7, 0, tzinfo=UTC), now=NOW,
    )
    assert c.state is CheckpointState.WAITING_FOR_EVIDENCE
    assert c.earliest_ts == "2026-07-29T07:00:00+00:00"
    assert c.auto_forbidden


def test_paper_terminal_result_ready_for_approval() -> None:
    c = classify_paper(
        has_open_position=True, terminal_result_available=True, reconcile_ok=True,
        kill_switch_active=False, close_time=None, now=NOW,
    )
    assert c.state is CheckpointState.READY_FOR_HUMAN_APPROVAL
    assert c.approval_required


def test_paper_reconcile_failure_is_action_required() -> None:
    c = classify_paper(
        has_open_position=True, terminal_result_available=True, reconcile_ok=False,
        kill_switch_active=False, close_time=None, now=NOW,
    )
    assert c.state is CheckpointState.ACTION_REQUIRED


def test_paper_kill_switch_is_action_required() -> None:
    c = classify_paper(
        has_open_position=True, terminal_result_available=False, reconcile_ok=True,
        kill_switch_active=True, close_time=None, now=NOW,
    )
    assert c.state is CheckpointState.ACTION_REQUIRED


def test_paper_no_position_complete() -> None:
    c = classify_paper(
        has_open_position=False, terminal_result_available=False, reconcile_ok=True,
        kill_switch_active=False, close_time=None, now=NOW,
    )
    assert c.state is CheckpointState.COMPLETE


# --- station pilot ---------------------------------------------------------

def test_pilot_before_review_waits_for_time() -> None:
    c = classify_station_pilot(
        review_ready=False, integrity_ok=True, earliest=date(2026, 8, 4), now=NOW
    )
    assert c.state is CheckpointState.WAITING_FOR_TIME
    assert c.earliest_ts == "2026-08-04"


def test_pilot_ready_for_approval() -> None:
    c = classify_station_pilot(
        review_ready=True, integrity_ok=True, earliest=date(2026, 8, 4),
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )
    assert c.state is CheckpointState.READY_FOR_HUMAN_APPROVAL


def test_pilot_integrity_failure_action_required() -> None:
    c = classify_station_pilot(review_ready=False, integrity_ok=False, earliest=None, now=NOW)
    assert c.state is CheckpointState.ACTION_REQUIRED


# --- E0002 -----------------------------------------------------------------

def test_e0002_before_14_days_waits_for_time() -> None:
    c = classify_e0002(
        complete_production_days=2, required_days=14, now=NOW, earliest=date(2026, 8, 10)
    )
    assert c.state is CheckpointState.WAITING_FOR_TIME
    assert c.detail["complete_production_days"] == 2


def test_e0002_ready_before_reserved_window() -> None:
    c = classify_e0002(
        complete_production_days=14, required_days=14,
        now=datetime(2026, 8, 10, tzinfo=UTC), earliest=date(2026, 8, 10),
    )
    assert c.state is CheckpointState.READY_FOR_HUMAN_APPROVAL


def test_e0002_reserved_window_blocks() -> None:
    c = classify_e0002(
        complete_production_days=20, required_days=14,
        now=datetime(2026, 8, 15, tzinfo=UTC), earliest=date(2026, 8, 10),
    )
    assert c.state is CheckpointState.BLOCKED


# --- research passthrough --------------------------------------------------

def test_h0012r_before_boundary_waits_for_time() -> None:
    c = classify_h0012r(readiness_state="CALENDAR_GATED", now=NOW)
    assert c.state is CheckpointState.WAITING_FOR_TIME
    assert c.earliest_ts == "2026-08-19"


def test_h0012r_after_boundary_collecting() -> None:
    c = classify_h0012r(readiness_state="COLLECTING", now=datetime(2026, 8, 20, tzinfo=UTC))
    assert c.state is CheckpointState.COLLECTING


def test_h0012r_after_boundary_ready() -> None:
    c = classify_h0012r(readiness_state="READY", now=datetime(2026, 8, 20, tzinfo=UTC))
    assert c.state is CheckpointState.READY_FOR_HUMAN_APPROVAL


def test_research_ready_label_before_boundary_never_ready() -> None:
    # Even a READY label cannot produce READY before the authoritative boundary.
    c = classify_h0012r(readiness_state="READY", now=NOW)
    assert c.state is CheckpointState.WAITING_FOR_TIME


def test_h0019_passthrough_not_ready() -> None:
    c = classify_h0019(readiness_state="NOT_READY", now=NOW)
    assert c.state is CheckpointState.WAITING_FOR_TIME
    assert c.detail["readiness_state"] == "NOT_READY"


def test_h0020_integrity_invalid_action_required() -> None:
    c = classify_h0020(readiness_state="INVALID", now=datetime(2026, 10, 7, tzinfo=UTC))
    assert c.state is CheckpointState.ACTION_REQUIRED


def test_h0020_deferred_blocks() -> None:
    c = classify_h0020(
        readiness_state="DEFERRED_INSUFFICIENT_DATA", now=datetime(2026, 10, 7, tzinfo=UTC)
    )
    assert c.state is CheckpointState.BLOCKED


def test_research_passthrough_never_exposes_performance() -> None:
    c = classify_h0020(readiness_state="COLLECTING_TRAIN", now=NOW)
    blob = json.dumps(c.to_dict()).lower()
    for forbidden in ("brier", "log_loss", "accuracy", "pnl", "return", "edge", "calibration"):
        assert forbidden not in blob


# --- operational -----------------------------------------------------------

def test_operational_healthy_and_degraded_and_unavailable() -> None:
    ok = classify_operational(
        "collector", healthy=True, detail_msg="ok", evidence="e", command="c"
    )
    assert ok.state is CheckpointState.HEALTHY
    bad = classify_operational(
        "collector", healthy=False, detail_msg="stale", evidence="e", command="c"
    )
    assert bad.state is CheckpointState.ACTION_REQUIRED
    na = classify_operational("collector", healthy=None, detail_msg="x", evidence="e", command="c")
    assert na.state is CheckpointState.NOT_APPLICABLE


# --- aggregation -----------------------------------------------------------

def _dash(states: list[CheckpointState]) -> Dashboard:
    cps = [
        Checkpoint(f"a{i}", s, "b", "e", "n", None, "cmd", False, True)
        for i, s in enumerate(states)
    ]
    return Dashboard(generated_at=NOW, git_commit="deadbeef", checkpoints=cps)


def test_ordering_by_urgency() -> None:
    d = _dash([
        CheckpointState.HEALTHY,
        CheckpointState.ACTION_REQUIRED,
        CheckpointState.WAITING_FOR_TIME,
        CheckpointState.READY_FOR_HUMAN_APPROVAL,
    ])
    order = [c.state for c in d.ordered()]
    assert order[0] is CheckpointState.ACTION_REQUIRED
    assert order[1] is CheckpointState.READY_FOR_HUMAN_APPROVAL
    assert order[-1] is CheckpointState.HEALTHY


def test_next_action_prefers_action_required_over_approval() -> None:
    d = _dash([CheckpointState.READY_FOR_HUMAN_APPROVAL, CheckpointState.ACTION_REQUIRED])
    nxt = d.next_action()
    assert nxt is not None and nxt.state is CheckpointState.ACTION_REQUIRED


def test_next_action_none_when_nothing_actionable() -> None:
    d = _dash([CheckpointState.WAITING_FOR_TIME, CheckpointState.HEALTHY])
    assert d.next_action() is None


def test_overall_state_is_most_urgent_non_na() -> None:
    d = _dash([CheckpointState.NOT_APPLICABLE, CheckpointState.WAITING_FOR_EVIDENCE])
    assert d.overall_state() is CheckpointState.WAITING_FOR_EVIDENCE


def test_forbidden_actions_and_banners_present_in_json() -> None:
    d = _dash([CheckpointState.HEALTHY])
    j = d.to_dict()
    assert j["forbidden_actions"] == list(FORBIDDEN_ACTIONS)
    assert j["banners"] == list(BANNERS)
    assert j["live_trading_enabled"] is False
    assert "submit any exchange order (live or demo)" in j["forbidden_actions"]


def test_json_stable_fields_present() -> None:
    d = _dash([CheckpointState.WAITING_FOR_TIME])
    j = d.to_dict()
    for field in (
        "generated_at", "git_commit", "overall_state", "next_action",
        "forbidden_actions", "paper", "station_pilot", "e0002", "h0012r",
        "h0019", "h0020", "backups", "restore_drill", "observatory", "storage",
    ):
        assert field in j


def test_json_determinism() -> None:
    d1 = _dash([CheckpointState.ACTION_REQUIRED, CheckpointState.HEALTHY])
    d2 = _dash([CheckpointState.HEALTHY, CheckpointState.ACTION_REQUIRED])
    # order of construction must not change the serialized dashboard structure
    assert json.dumps(d1.to_dict()["overall_state"]) == json.dumps(d2.to_dict()["overall_state"])
    a = json.dumps(d1.to_dict(), sort_keys=True)
    b = json.dumps(Dashboard(NOW, "deadbeef", list(d1.checkpoints)).to_dict(), sort_keys=True)
    assert a == b


def test_engine_has_no_execution_or_io_surface() -> None:
    import inspect

    from kalshi_weather.ops import operator_status as ost

    src = inspect.getsource(ost)
    for banned in ("asyncio", "subprocess", "create_engine", "requests", "httpx", "open("):
        assert banned not in src, f"pure engine references I/O: {banned}"
    for banned in ("datetime.now", "utcnow", "date.today"):
        assert banned not in src
