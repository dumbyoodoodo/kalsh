"""Recovery watch: deterministic states, exactly-once notifications,
escalation, persistence, threshold reuse, and isolation. Injected clock and
disposable storage throughout."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_weather.ops.recovery_watch import (
    READY_MESSAGE,
    RecoveryHealthPolicy,
    WatchInputs,
    WatchState,
    WatchStateRecord,
    append_watch_history,
    classify,
    decide_notification,
    load_watch_state,
    save_watch_state,
)
from kalshi_weather.paper.engine import PaperRiskPolicy

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def inputs(**kw: object) -> WatchInputs:
    base: dict[str, object] = {
        "api_reachable": True,
        "success_run_age_seconds": 120.0,
        "latest_success_run_id": 1400,
        "latest_run_id": 1400,
        "latest_run_success": True,
        "snapshot_age_seconds": 120.0,
        "book_age_seconds": 100.0,
        "poll_evidence_age_seconds": 110.0,
        "collector_count": 1,
        "kill_switch_inactive": True,
        "reconcile_ok": True,
    }
    base.update(kw)
    return WatchInputs(**base)  # type: ignore[arg-type]


# --- classification ----------------------------------------------------------


def test_all_gates_pass_is_ready() -> None:
    state, reasons = classify(inputs())
    assert state is WatchState.PAPER_VALIDATION_READY and reasons == []


def test_outage_on_unreachable_api_or_stale_success() -> None:
    s1, r1 = classify(inputs(api_reachable=False))
    assert s1 is WatchState.OUTAGE_ACTIVE and "api_unreachable" in r1
    s2, r2 = classify(inputs(success_run_age_seconds=100_000.0))
    assert s2 is WatchState.OUTAGE_ACTIVE and "collector_success_stale" in r2
    s3, r3 = classify(inputs(success_run_age_seconds=None))
    assert s3 is WatchState.OUTAGE_ACTIVE and "collector_success_stale" in r3


def test_recovering_when_reachable_with_recent_success_but_stale_evidence() -> None:
    state, reasons = classify(inputs(book_age_seconds=10_000.0))
    assert state is WatchState.RECOVERING and reasons == ["stale_book_evidence"]
    state2, reasons2 = classify(
        inputs(snapshot_age_seconds=None, poll_evidence_age_seconds=100_000.0)
    )
    assert state2 is WatchState.RECOVERING
    assert set(reasons2) == {"stale_market_snapshot", "stale_poll_evidence"}


def test_healthy_when_data_fresh_but_paper_side_fails() -> None:
    state, reasons = classify(inputs(kill_switch_inactive=False))
    assert state is WatchState.HEALTHY and reasons == ["paper_kill_switch_active"]
    state2, reasons2 = classify(inputs(collector_count=2, reconcile_ok=False))
    assert state2 is WatchState.HEALTHY
    assert set(reasons2) == {"collector_count_not_one", "paper_reconcile_failed"}


def test_classify_uses_recovery_policy_not_paper_gate() -> None:
    # DQ-002: the recovery health decision uses cadence-aware thresholds, NOT
    # the strict 300s paper-fill gate.
    p = RecoveryHealthPolicy()
    assert p.max_book_age_seconds == 1200.0
    # a custom recovery policy changes the gate: 100s-old book fails a 50s bound
    tight = RecoveryHealthPolicy(max_book_age_seconds=50.0)
    state, reasons = classify(inputs(), tight)
    assert state is WatchState.RECOVERING and reasons == ["stale_book_evidence"]
    # inclusive bounds at the recovery thresholds
    edge = inputs(
        snapshot_age_seconds=p.max_snapshot_age_seconds,
        book_age_seconds=p.max_book_age_seconds,
        poll_evidence_age_seconds=p.max_poll_evidence_age_seconds,
        success_run_age_seconds=p.success_stale_seconds,
    )
    assert classify(edge)[0] is WatchState.PAPER_VALIDATION_READY


def test_paper_fill_gate_unchanged_at_300s() -> None:
    # The paper-execution book gate is untouched by DQ-002.
    assert PaperRiskPolicy().max_book_age_seconds == 300.0


def test_cadence_555s_book_does_not_flap_state() -> None:
    # A book aged at ~1x cadence (555s) -- stale for the 300s paper gate but
    # fresh for recovery health -> READY, not RECOVERING (no flap source).
    for age in (300.0, 555.0, 800.0, 1000.0, 1200.0):
        assert classify(inputs(book_age_seconds=age))[0] is WatchState.PAPER_VALIDATION_READY
    # only a genuine multi-cycle stall (>1200s) degrades
    assert classify(inputs(book_age_seconds=1300.0))[0] is WatchState.RECOVERING


def test_between_cycle_aging_does_not_leave_ready_or_notify() -> None:
    # READY, then normal between-cycle book aging (up to 1200s): stays READY,
    # no RECOVERING transition, no notification churn.
    prior = None
    notifs = 0
    for age in (100.0, 600.0, 900.0, 1200.0, 200.0, 700.0):
        state, reasons = classify(inputs(book_age_seconds=age))
        d = decide_notification(state, reasons, inputs(book_age_seconds=age), prior, now=NOW)
        assert state is WatchState.PAPER_VALIDATION_READY
        if d.should_notify:
            notifs += 1
        prior = d.new_state
    assert notifs == 1  # only the initial cold-start ready notification


# --- notification decisions --------------------------------------------------


def test_outage_notifies_once_then_deduplicates() -> None:
    i = inputs(api_reachable=False)
    state, reasons = classify(i)
    d1 = decide_notification(state, reasons, i, None, now=NOW)
    assert d1.should_notify and d1.transition == "entered_outage_active"
    assert "api_unreachable" in d1.message and NOW.isoformat() in d1.message
    d2 = decide_notification(state, reasons, i, d1.new_state, now=NOW + timedelta(minutes=5))
    assert not d2.should_notify and d2.transition == "no_change"
    d3 = decide_notification(state, reasons, i, d2.new_state, now=NOW + timedelta(minutes=10))
    assert not d3.should_notify  # repeated failed cycles: still silent


def test_escalation_fires_exactly_once_after_duration() -> None:
    i = inputs(api_reachable=False)
    state, reasons = classify(i)
    d1 = decide_notification(state, reasons, i, None, now=NOW)
    late = NOW + timedelta(hours=7)
    d2 = decide_notification(state, reasons, i, d1.new_state, now=late)
    assert d2.should_notify and d2.transition == "escalation"
    assert d2.message.startswith("ESCALATION")
    d3 = decide_notification(state, reasons, i, d2.new_state, now=late + timedelta(hours=1))
    assert not d3.should_notify  # escalation never repeats


def test_recovering_and_ready_each_notify_once() -> None:
    out_i = inputs(api_reachable=False)
    d_out = decide_notification(*classify(out_i), out_i, None, now=NOW)
    rec_i = inputs(book_age_seconds=10_000.0)
    d_rec = decide_notification(
        *classify(rec_i), rec_i, d_out.new_state, now=NOW + timedelta(hours=1)
    )
    assert d_rec.should_notify and d_rec.transition == "entered_recovering"
    d_rec2 = decide_notification(
        *classify(rec_i), rec_i, d_rec.new_state, now=NOW + timedelta(hours=1, minutes=5)
    )
    assert not d_rec2.should_notify
    ready_i = inputs()
    d_ready = decide_notification(
        *classify(ready_i), ready_i, d_rec2.new_state, now=NOW + timedelta(hours=2)
    )
    assert d_ready.should_notify and d_ready.transition == "entered_paper_validation_ready"
    assert d_ready.message.startswith(READY_MESSAGE)
    assert d_ready.new_state.outage_started_at is None  # recovery closes the window
    d_ready2 = decide_notification(
        *classify(ready_i), ready_i, d_ready.new_state, now=NOW + timedelta(hours=3)
    )
    assert not d_ready2.should_notify  # repeated ready checks stay silent


def test_regression_and_second_recovery_renotify() -> None:
    ready_i = inputs()
    d_ready = decide_notification(*classify(ready_i), ready_i, None, now=NOW)
    out_i = inputs(api_reachable=False)
    d_out = decide_notification(
        *classify(out_i), out_i, d_ready.new_state, now=NOW + timedelta(hours=1)
    )
    assert d_out.should_notify and d_out.transition == "entered_outage_active"
    d_ready2 = decide_notification(
        *classify(ready_i), ready_i, d_out.new_state, now=NOW + timedelta(hours=2)
    )
    assert d_ready2.should_notify  # a genuine new recovery re-notifies
    assert d_ready2.message.startswith(READY_MESSAGE)


def test_recovering_blip_from_healthy_is_silent_and_no_renotify() -> None:
    # A single stale-evidence blip out of a healthy state (NOT an outage) must
    # not notify, and returning to READY must not re-notify (kills DQ-002 flap).
    ready_i = inputs()
    d_ready = decide_notification(*classify(ready_i), ready_i, None, now=NOW)
    assert d_ready.should_notify  # cold-start ready
    blip_i = inputs(book_age_seconds=1300.0)  # > 1200s recovery threshold
    d_blip = decide_notification(
        *classify(blip_i), blip_i, d_ready.new_state, now=NOW + timedelta(minutes=16)
    )
    assert classify(blip_i)[0] is WatchState.RECOVERING
    assert not d_blip.should_notify  # silent: not out of an outage
    d_back = decide_notification(
        *classify(ready_i), ready_i, d_blip.new_state, now=NOW + timedelta(minutes=32)
    )
    assert d_back.new_state.state == WatchState.PAPER_VALIDATION_READY.value
    assert not d_back.should_notify  # no re-notification from aging


def test_sustained_stale_success_triggers_outage() -> None:
    # A collector success older than the recovery success-stale bound => OUTAGE.
    state, reasons = classify(inputs(success_run_age_seconds=1300.0))
    assert state is WatchState.OUTAGE_ACTIVE and "collector_success_stale" in reasons


def test_legacy_state_file_without_ready_pending_loads(tmp_path: Path) -> None:
    # A state file written before DQ-002 (no ready_pending field) loads with a
    # conservative default and does not crash / storm.
    import json as _json

    legacy = {
        "state": "PAPER_VALIDATION_READY",
        "since": NOW.isoformat(),
        "outage_started_at": None,
        "last_notification_at": NOW.isoformat(),
        "last_success_run_id": 1400,
        "transition_reason": "all_gates_pass",
        "escalated": False,
        # note: NO ready_pending, plus a stray unknown key
        "legacy_unknown_field": 123,
    }
    p = tmp_path / "state.json"
    p.write_text(_json.dumps(legacy))
    rec = load_watch_state(p)
    assert rec is not None
    assert rec.ready_pending is False
    assert rec.state == "PAPER_VALIDATION_READY"
    # first post-deploy check at READY: same state -> no_change -> no storm
    d = decide_notification(*classify(inputs()), inputs(), rec, now=NOW + timedelta(minutes=5))
    assert not d.should_notify and d.transition == "no_change"


def test_malformed_state_fails_closed(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{ this is not valid json ")
    assert load_watch_state(p) is None
    p2 = tmp_path / "wrongtype.json"
    p2.write_text("[1, 2, 3]")
    assert load_watch_state(p2) is None
    p3 = tmp_path / "missing_required.json"
    p3.write_text('{"escalated": true}')  # missing state/since -> fail closed
    assert load_watch_state(p3) is None


def test_ready_pending_round_trips(tmp_path: Path) -> None:
    rec = WatchStateRecord(
        state="OUTAGE_ACTIVE", since=NOW.isoformat(), outage_started_at=NOW.isoformat(),
        last_notification_at=NOW.isoformat(), last_success_run_id=1, transition_reason="x",
        escalated=False, ready_pending=True,
    )
    p = tmp_path / "s.json"
    save_watch_state(p, rec)
    assert load_watch_state(p) == rec


def test_healthy_transition_records_but_does_not_notify() -> None:
    i = inputs(kill_switch_inactive=False)
    state, reasons = classify(i)
    d = decide_notification(state, reasons, i, None, now=NOW)
    assert not d.should_notify and d.transition == "entered_healthy"
    assert d.new_state.transition_reason == "paper_kill_switch_active"


# --- persistence -------------------------------------------------------------


def test_state_persists_across_restart(tmp_path: Path) -> None:
    i = inputs(api_reachable=False)
    d = decide_notification(*classify(i), i, None, now=NOW)
    state_file = tmp_path / "watch" / "state.json"
    save_watch_state(state_file, d.new_state)
    reloaded = load_watch_state(state_file)  # a fresh process would do exactly this
    assert reloaded == d.new_state
    # dedup survives the restart: same state, no re-notification
    d2 = decide_notification(*classify(i), i, reloaded, now=NOW + timedelta(minutes=30))
    assert not d2.should_notify
    assert load_watch_state(tmp_path / "absent.json") is None


def test_history_is_append_only(tmp_path: Path) -> None:
    hist = tmp_path / "history.jsonl"
    for k in range(3):
        append_watch_history(
            hist,
            timestamp=(NOW + timedelta(minutes=k)).isoformat(),
            state="OUTAGE_ACTIVE",
            transition="no_change",
            reasons="api_unreachable",
            notified=None,
            detail="",
        )
    lines = hist.read_text().splitlines()
    assert len(lines) == 3  # appended, never truncated


# --- full synthetic transition scenario (section 10) -------------------------


def test_full_outage_to_ready_scenario() -> None:
    prior = None
    notifications: list[str] = []

    def step(i: WatchInputs, at: datetime) -> None:
        nonlocal prior
        d = decide_notification(*classify(i), i, prior, now=at)
        if d.should_notify:
            notifications.append(d.transition)
        prior = d.new_state

    t = NOW
    step(inputs(), t)  # 1. healthy... actually READY (all gates pass)
    step(inputs(api_reachable=False), t := t + timedelta(minutes=10))  # 2. outage
    for _ in range(3):  # 3. repeated failed cycles
        step(inputs(api_reachable=False), t := t + timedelta(minutes=10))
    # 4. endpoint back + success cycle happened, evidence still stale
    step(inputs(book_age_seconds=9_999.0), t := t + timedelta(minutes=10))
    # 5-6. evidence becomes fresh
    step(inputs(), t := t + timedelta(minutes=10))
    # 8. repeated healthy/ready checks
    for _ in range(2):
        step(inputs(), t := t + timedelta(minutes=10))
    assert notifications == [
        "entered_paper_validation_ready",
        "entered_outage_active",
        "entered_recovering",
        "entered_paper_validation_ready",
    ]


# --- isolation ---------------------------------------------------------------


def test_watch_module_isolation() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "src/kalshi_weather/ops/recovery_watch.py"
    ).read_text()
    banned = re.compile(
        r"h0019|h0020|kalshi_weather\.experiments|execute_paper_run|run_paper_session"
        r"|httpx|kalshi_weather\.kalshi|datetime\.now|utcnow",
        re.IGNORECASE,
    )
    match = banned.search(src)
    assert match is None, f"recovery_watch references banned symbol: {match.group(0)!r}"
