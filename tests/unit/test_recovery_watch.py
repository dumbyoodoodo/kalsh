"""Recovery watch: deterministic states, exactly-once notifications,
escalation, persistence, threshold reuse, and isolation. Injected clock and
disposable storage throughout."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_weather.ops.recovery_watch import (
    READY_MESSAGE,
    WatchInputs,
    WatchState,
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


def test_thresholds_come_from_paper_policy() -> None:
    # a custom policy changes the gate: 100s-old book fails a 50s threshold
    tight = PaperRiskPolicy(max_book_age_seconds=50.0)
    state, reasons = classify(inputs(), tight)
    assert state is WatchState.RECOVERING and reasons == ["stale_book_evidence"]
    # and the defaults are literally the paper policy's numbers
    p = PaperRiskPolicy()
    edge = inputs(
        snapshot_age_seconds=p.max_snapshot_age_seconds,
        book_age_seconds=p.max_book_age_seconds,
        poll_evidence_age_seconds=p.max_poll_evidence_age_seconds,
        success_run_age_seconds=p.max_snapshot_age_seconds,
    )
    assert classify(edge)[0] is WatchState.PAPER_VALIDATION_READY  # inclusive bounds


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
