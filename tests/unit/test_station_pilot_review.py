"""Seven-day station-pilot review: window math, continuity, timezone
behavior, collector impact, decision gates, and isolation. Pure synthetic
fixtures with an injected clock -- no database, no production access."""

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from kalshi_weather.ops.station_pilot_review import (
    CONTINUE_PILOT,
    PILOT_COLLECTING,
    PILOT_REVIEW_READY,
    ROLLBACK_PILOT,
    SECOND_BATCH_ELIGIBLE,
    SECOND_BATCH_RANKING,
    SECOND_BATCH_RECOMMENDED,
    CycleLite,
    ForecastRowLite,
    GateInputs,
    ObservationRowLite,
    collector_impact,
    decide,
    observation_date_spillover,
    review_window,
    station_continuity,
    timezone_checks,
)

START = datetime(2026, 7, 27, 21, 55, tzinfo=UTC)


# --- review window -----------------------------------------------------------


def test_before_seven_days_is_collecting() -> None:
    w = review_window(datetime(2026, 7, 27, 23, 0, tzinfo=UTC), pilot_start=START)
    assert w.state == PILOT_COLLECTING and w.elapsed_full_days == 0
    w2 = review_window(datetime(2026, 8, 2, 12, 0, tzinfo=UTC), pilot_start=START)
    assert w2.state == PILOT_COLLECTING and w2.elapsed_full_days == 5
    assert str(w2.review_ready_at) == "2026-08-04"


def test_seven_complete_days_is_review_ready() -> None:
    w = review_window(datetime(2026, 8, 4, 0, 30, tzinfo=UTC), pilot_start=START)
    assert w.elapsed_full_days == 7 and w.state == PILOT_REVIEW_READY
    # 8-03 still collecting: only 6 complete days (current day excluded)
    w2 = review_window(datetime(2026, 8, 3, 23, 59, tzinfo=UTC), pilot_start=START)
    assert w2.elapsed_full_days == 6 and w2.state == PILOT_COLLECTING


def test_incomplete_current_day_excluded_from_continuity() -> None:
    w = review_window(datetime(2026, 7, 30, 6, 0, tzinfo=UTC), pilot_start=START)
    # complete days: 07-28, 07-29 -- today 07-30 excluded
    assert w.first_full_day == date(2026, 7, 28)
    assert w.last_complete_day == date(2026, 7, 29)
    c = station_continuity("SEA", [], [], w)
    assert list(c.forecast_rows_by_day) == ["2026-07-28", "2026-07-29"]


# --- continuity --------------------------------------------------------------


def fc(station: str, day: date, hour: int, *, minute: int = 0) -> ForecastRowLite:
    issue = datetime(day.year, day.month, day.day, hour, minute)
    return ForecastRowLite(
        station=station,
        issue_time=issue,
        observed_at=issue + timedelta(minutes=30),
        valid_start=issue + timedelta(hours=6),
    )


def obs(station: str, day: date, var: str) -> ObservationRowLite:
    return ObservationRowLite(station=station, variable=var, observation_date=day)


def full_days(window_end: datetime) -> tuple:  # type: ignore[type-arg]
    w = review_window(window_end, pilot_start=START)
    days = [w.first_full_day + timedelta(days=i) for i in range(w.elapsed_full_days)]
    return w, days


def test_forecast_continuity_and_expected_issuance() -> None:
    w, days = full_days(datetime(2026, 7, 30, 6, 0, tzinfo=UTC))
    rows = [fc("SEA", d, h) for d in days for h in (9, 21)]  # 2 issuances/day
    c = station_continuity("SEA", rows, [], w)
    assert all(v == 2 for v in c.issuances_by_day.values())
    assert c.issuance_shortfall_days == []
    # a day with a single issuance is a shortfall day
    rows2 = [fc("PHX", days[0], 9)] + [fc("PHX", days[1], h) for h in (9, 21)]
    c2 = station_continuity("PHX", rows2, [], w)
    assert c2.issuance_shortfall_days == [days[0].isoformat()]


def test_observation_completeness_and_missing_days() -> None:
    w, days = full_days(datetime(2026, 7, 30, 6, 0, tzinfo=UTC))
    good = [obs("MIA", d, v) for d in days for v in ("tmax_f", "tmin_f")]
    c = station_continuity("MIA", [], good, w)
    assert c.observation_days_complete == len(days) and c.observation_days_missing == []
    partial = [obs("SEA", days[0], "tmax_f")]  # missing tmin on day 1, all of day 2
    c2 = station_continuity("SEA", [], partial, w)
    assert c2.observation_days_complete == 0
    assert c2.observation_days_missing == [d.isoformat() for d in days]


def test_duplicate_detection_and_gap_measurement() -> None:
    w, days = full_days(datetime(2026, 7, 30, 6, 0, tzinfo=UTC))
    a = fc("SEA", days[0], 9)
    rows = [a, a, fc("SEA", days[1], 21)]  # duplicate logical row + 36h gap
    c = station_continuity("SEA", rows, [], w)
    assert c.duplicate_logical_rows == 1
    assert c.longest_forecast_gap_hours > 30


# --- timezone behavior -------------------------------------------------------


def test_phx_no_dst_and_not_denver() -> None:
    tz = timezone_checks(datetime(2026, 7, 28, tzinfo=UTC))
    phx = tz["PHX"]
    assert phx["summer_utc_offset"] == phx["winter_utc_offset"] == -7.0
    assert phx["denver_summer_offset"] == -6.0  # Denver shifts; Phoenix must not
    assert phx["ok"]


def test_sea_and_mia_dst_offsets() -> None:
    tz = timezone_checks(datetime(2026, 7, 28, tzinfo=UTC))
    assert tz["SEA"]["ok"] and tz["SEA"]["summer_utc_offset"] == -7.0
    assert tz["MIA"]["ok"] and tz["MIA"]["winter_utc_offset"] == -5.0


def test_observation_spillover_detection() -> None:
    w = review_window(datetime(2026, 7, 30, 6, 0, tzinfo=UTC), pilot_start=START)
    # 06:00Z on 07-30 is still 07-29 in PHX (UTC-7): a 07-30-dated PHX
    # observation would be FUTURE-dated locally -> spillover anomaly
    bad = [obs("PHX", date(2026, 7, 30), "tmax_f")]
    ok = [obs("PHX", date(2026, 7, 29), "tmax_f")]
    assert observation_date_spillover(bad, w)["PHX"] is False
    assert observation_date_spillover(ok, w)["PHX"] is True


# --- collector impact --------------------------------------------------------


def cyc(rid: int, minutes_after: int, dur: float, *, ok: bool = True) -> CycleLite:
    return CycleLite(
        run_id=rid,
        started_at=START + timedelta(minutes=minutes_after),
        duration_seconds=dur,
        requests=50,
        success=ok,
    )


def test_first_backfill_cycle_excluded_from_steady_state() -> None:
    pre = [cyc(1, -120, 4.0), cyc(2, -60, 3.5)]
    post = [cyc(3, 1, 44.7), cyc(4, 45, 9.0), cyc(5, 90, 10.0), cyc(6, 135, 8.5)]
    impact = collector_impact(pre, post, cadence_seconds=2600)
    assert impact.post_first_backfill_s == 44.7
    assert impact.post_median_s <= 10.0  # backfill not in steady state
    assert impact.cycles_exceeding_half_cadence == 0


def test_capacity_regression_detected() -> None:
    pre = [cyc(1, -60, 4.0)]
    post = [cyc(2, 1, 40.0), cyc(3, 45, 1500.0), cyc(4, 90, 1400.0)]
    impact = collector_impact(pre, post, cadence_seconds=2600)
    assert impact.cycles_exceeding_half_cadence == 2


# --- decision gates ----------------------------------------------------------


def gate_inputs(**kw: object) -> GateInputs:
    base: dict[str, object] = {
        "review_state": PILOT_REVIEW_READY,
        "timezone_ok": True,
        "spillover_ok": True,
        "mapping_ok": True,
        "pilot_critical_findings": 0,
        "stations_with_stable_issuance": 3,
        "pilot_observation_completeness": 0.95,
        "duplicate_rows": 0,
        "collector_capacity_ok": True,
        "baseline_stations_healthy": True,
        "settlement_no_regression": True,
        "settlement_unresolved_new": 0,
        "storage_growth_mb_per_day": 1.0,
    }
    base.update(kw)
    return GateInputs(**base)  # type: ignore[arg-type]


def test_all_gates_pass_second_batch_eligible_with_human_approval_flag() -> None:
    rec, reasons = decide(gate_inputs())
    assert rec == SECOND_BATCH_ELIGIBLE
    assert "human_approval_still_required" in reasons


def test_before_seven_days_never_recommends_expansion() -> None:
    rec, reasons = decide(gate_inputs(review_state=PILOT_COLLECTING))
    assert rec == CONTINUE_PILOT and "seven_days_not_elapsed" in reasons


def test_timezone_anomaly_forces_rollback() -> None:
    rec, reasons = decide(gate_inputs(timezone_ok=False))
    assert rec == ROLLBACK_PILOT and "timezone_or_target_date_error" in reasons
    rec2, _ = decide(gate_inputs(spillover_ok=False))
    assert rec2 == ROLLBACK_PILOT


def test_mapping_capacity_duplication_regression_force_rollback() -> None:
    assert decide(gate_inputs(mapping_ok=False))[0] == ROLLBACK_PILOT
    assert decide(gate_inputs(collector_capacity_ok=False))[0] == ROLLBACK_PILOT
    assert decide(gate_inputs(duplicate_rows=3))[0] == ROLLBACK_PILOT
    assert decide(gate_inputs(baseline_stations_healthy=False))[0] == ROLLBACK_PILOT
    assert decide(gate_inputs(settlement_no_regression=False))[0] == ROLLBACK_PILOT


def test_soft_shortfalls_continue_pilot() -> None:
    rec, reasons = decide(gate_inputs(pilot_observation_completeness=0.7))
    assert rec == CONTINUE_PILOT and "observation_completeness_below_0.85" in reasons
    rec2, _ = decide(gate_inputs(stations_with_stable_issuance=1))
    assert rec2 == CONTINUE_PILOT


def test_second_batch_ranking_static_and_capped() -> None:
    cities = [c for c, _ in SECOND_BATCH_RANKING]
    assert len(cities) == len(set(cities)) == 11
    assert set(SECOND_BATCH_RECOMMENDED) <= set(cities)
    assert len(SECOND_BATCH_RECOMMENDED) <= 4
    assert "SEA" not in cities and "PHX" not in cities and "MIA" not in cities


# --- isolation ---------------------------------------------------------------


def test_module_isolation() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "src/kalshi_weather/ops/station_pilot_review.py"
    ).read_text()
    banned = re.compile(
        r"kalshi_weather\.experiments|kalshi_weather\.paper|kalshi_weather\.kalshi"
        r"|h0019|h0020|brier|logit|fit_|datetime\.now|utcnow|create_async_engine"
        r"|INSERT|UPDATE|DELETE",
        re.IGNORECASE,
    )
    m = banned.search(src)
    assert m is None, f"pilot review references banned symbol: {m.group(0)!r}"
