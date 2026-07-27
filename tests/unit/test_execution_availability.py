"""Observed-availability, availability-aware passive fills, and coverage-preset
tests (all synthetic, deterministic). Availability construction against a real
PostgreSQL database is covered in tests/integration/.
"""

from datetime import UTC, datetime

from kalshi_weather.execution.availability import (
    EvidenceType,
    IntervalState,
    ObservationAvailabilityPolicy,
    ObservationInterval,
    TickerAvailability,
    _build_ticker,
    _Cycle,
    reconstruct_from_rows,
)
from kalshi_weather.execution.coverage import (
    ReplayCoveragePolicy,
    exploratory_preset,
    get_preset,
    passive_research_preset,
    strict_marketable_preset,
)
from kalshi_weather.execution.fills import simulate_passive
from kalshi_weather.execution.models import Action, OrderIntent, OrderType, Side, TimeInForce
from kalshi_weather.execution.policy import ExecutionPolicy


def T(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 7, 26, h, m, s, tzinfo=UTC)


def _cyc(rid: int, h1: int, m1: int, h2: int, m2: int, ok: bool = True) -> _Cycle:
    return _Cycle(rid, T(h1, m1), T(h2, m2), ok)


POL = ObservationAvailabilityPolicy()


# --- availability construction ---------------------------------------------


def test_fully_observed_between_two_witnessed_points() -> None:
    # collector up across two cycles, direct obs in each -> OBSERVED throughout
    cycles = [_cyc(1, 10, 0, 10, 5), _cyc(2, 10, 10, 10, 15)]
    obs = [(T(10, 2), "orderbook_snapshots:1"), (T(10, 12), "orderbook_snapshots:2")]
    ta = _build_ticker("A", T(10, 0), T(10, 15), cycles, obs, POL)
    assert ta.state_at(T(10, 2)) is IntervalState.OBSERVED
    assert ta.state_at(T(10, 7)) is IntervalState.OBSERVED  # deduped gap bridged
    assert ta.state_at(T(10, 12)) is IntervalState.OBSERVED


def test_collector_restart_gap_is_unavailable() -> None:
    # a >20min gap between cycles -> COLLECTOR_UNAVAILABLE
    cycles = [_cyc(1, 10, 0, 10, 5), _cyc(2, 10, 40, 10, 45)]
    obs = [(T(10, 2), "orderbook_snapshots:1"), (T(10, 42), "orderbook_snapshots:2")]
    ta = _build_ticker("A", T(10, 0), T(10, 45), cycles, obs, POL)
    assert ta.state_at(T(10, 20)) is IntervalState.COLLECTOR_UNAVAILABLE


def test_failed_cycle_is_unavailable() -> None:
    cycles = [_cyc(1, 10, 0, 10, 5), _cyc(2, 10, 10, 10, 15, ok=False)]
    obs = [(T(10, 2), "orderbook_snapshots:1")]
    ta = _build_ticker("A", T(10, 0), T(10, 15), cycles, obs, POL)
    assert ta.state_at(T(10, 12)) is IntervalState.COLLECTOR_UNAVAILABLE


def test_no_collector_run_is_unknown_not_healthy() -> None:
    ta = _build_ticker("A", T(10, 0), T(11, 0), [], [], POL)
    assert all(iv.state is IntervalState.UNKNOWN for iv in ta.intervals)
    assert ta.state_at(T(10, 30)) is IntervalState.UNKNOWN  # missing != observed


def test_no_per_market_proof_is_not_eligible_not_observed() -> None:
    # collector up but ticker never observed -> MARKET_NOT_ELIGIBLE (not OBSERVED)
    cycles = [_cyc(1, 10, 0, 10, 5)]
    ta = _build_ticker("A", T(10, 0), T(10, 5), cycles, [], POL)
    assert ta.state_at(T(10, 2)) is IntervalState.MARKET_NOT_ELIGIBLE


def test_intervals_are_sorted_nonoverlapping_and_timezone_aware() -> None:
    cycles = [_cyc(1, 10, 0, 10, 5), _cyc(2, 10, 40, 10, 45, ok=False)]
    obs = [(T(10, 2), "orderbook_snapshots:1")]
    ta = _build_ticker("A", T(10, 0), T(10, 45), cycles, obs, POL)
    for a, b in zip(ta.intervals, ta.intervals[1:], strict=False):
        assert a.end == b.start  # contiguous, non-overlapping
        assert a.start < a.end
        assert a.start.tzinfo is UTC and a.end.tzinfo is UTC


def test_direct_poll_failure_downgrades_to_unknown_and_breaks_continuity() -> None:
    # collector up; a direct per-ticker poll FAILED in the 10:10 cycle (no success)
    cycles = [_cyc(1, 10, 0, 10, 5), _cyc(2, 10, 10, 10, 15)]
    obs = [(T(10, 2), "market_poll_attempts:1")]  # success in cycle 1
    failures = [T(10, 12)]  # failure in cycle 2
    ta = _build_ticker("A", T(10, 0), T(10, 15), cycles, obs, POL, failures)
    assert ta.state_at(T(10, 2)) is IntervalState.OBSERVED
    assert ta.state_at(T(10, 12)) is IntervalState.UNKNOWN  # failed poll != observed
    # a passive order at 10:2 loses continuity at the failed poll
    brk = ta.first_break_after(T(10, 2), allow_likely=True)
    assert brk is not None and brk <= T(10, 15)


def test_direct_success_never_overridden_by_a_failure_elsewhere() -> None:
    cycles = [_cyc(1, 10, 0, 10, 5), _cyc(2, 10, 10, 10, 15)]
    obs = [(T(10, 2), "mpa:1"), (T(10, 12), "mpa:2")]  # both cycles witnessed
    failures = [T(10, 12)]  # a failure coincides with a witnessed success cycle
    ta = _build_ticker("A", T(10, 0), T(10, 15), cycles, obs, POL, failures)
    # the witnessed cycle stays OBSERVED (success is not overridden)
    assert ta.state_at(T(10, 12)) is IntervalState.OBSERVED


def test_reconstruct_round_trip() -> None:
    cycles = [_cyc(1, 10, 0, 10, 5)]
    obs = [(T(10, 2), "orderbook_snapshots:1")]
    ta = _build_ticker("A", T(10, 0), T(10, 5), cycles, obs, POL)
    rows = [iv.to_dict() for iv in ta.intervals]
    back = reconstruct_from_rows(rows)["A"]
    assert [iv.state for iv in back.intervals] == [iv.state for iv in ta.intervals]
    assert [iv.start for iv in back.intervals] == [iv.start for iv in ta.intervals]


# --- availability-aware passive fills --------------------------------------


def _ta(*intervals: tuple[str, datetime, datetime, IntervalState]) -> TickerAvailability:
    ivs = tuple(
        ObservationInterval("M", a, b, st, EvidenceType.CYCLE_ELIGIBLE, "high", "test", (), (), "v")
        for _, a, b, st in intervals
    )
    return TickerAvailability("M", ivs)


def _passive(side: Side = Side.YES, action: Action = Action.BUY, price: int = 40) -> OrderIntent:
    return OrderIntent(
        order_id="p",
        strategy_id="s",
        ticker="M",
        side=side,
        action=action,
        order_type=OrderType.PASSIVE_LIMIT,
        limit_price_cents=price,
        quantity=5,
        submitted_at=T(10, 0),
        time_in_force=TimeInForce.GTC,
    )


def _trade(hh: int, mm: int, qty: int, taker: Side = Side.NO):  # type: ignore[no-untyped-def]
    from kalshi_weather.execution.market import Trade

    return Trade("M", T(hh, mm), yes_price_cents=39, quantity=qty, taker_side=taker)


def test_passive_fills_within_observed_interval() -> None:
    avail = _ta(("M", T(10, 0), T(11, 0), IntervalState.OBSERVED))
    fills = simulate_passive(_passive(), [_trade(10, 10, 8)], ExecutionPolicy(), availability=avail)
    assert fills and fills[0].quantity == 5


def test_later_trade_does_not_bridge_an_outage() -> None:
    # observed until 10:15, then COLLECTOR_UNAVAILABLE; a trade at 10:30 must not fill
    avail = _ta(
        ("M", T(10, 0), T(10, 15), IntervalState.OBSERVED),
        ("M", T(10, 15), T(11, 0), IntervalState.COLLECTOR_UNAVAILABLE),
    )
    fills = simulate_passive(_passive(), [_trade(10, 30, 8)], ExecutionPolicy(), availability=avail)
    assert fills == []  # trade after the availability break is excluded


def test_unknown_gap_resets_queue_evidence() -> None:
    avail = _ta(
        ("M", T(10, 0), T(10, 5), IntervalState.OBSERVED),
        ("M", T(10, 5), T(11, 0), IntervalState.UNKNOWN),
    )
    # a trade before the break fills; one after does not
    ok = simulate_passive(_passive(), [_trade(10, 2, 8)], ExecutionPolicy(), availability=avail)
    assert ok and ok[0].quantity == 5
    after = simulate_passive(_passive(), [_trade(10, 30, 8)], ExecutionPolicy(), availability=avail)
    assert after == []


def test_likely_observed_allowed_by_one_policy_rejected_by_another() -> None:
    avail = _ta(
        ("M", T(10, 0), T(10, 5), IntervalState.OBSERVED),
        ("M", T(10, 5), T(11, 0), IntervalState.LIKELY_OBSERVED),
    )
    trades = [_trade(10, 30, 8)]
    strict = simulate_passive(
        _passive(), trades, ExecutionPolicy(), availability=avail, allow_likely_observed=False
    )
    lenient = simulate_passive(
        _passive(), trades, ExecutionPolicy(), availability=avail, allow_likely_observed=True
    )
    assert strict == []  # LIKELY_OBSERVED breaks continuity by default
    assert lenient and lenient[0].quantity == 5


def test_availability_overrides_trade_gap_safeguard() -> None:
    # trades are dense (no trade-gap break) but an outage still truncates
    avail = _ta(
        ("M", T(10, 0), T(10, 6), IntervalState.OBSERVED),
        ("M", T(10, 6), T(11, 0), IntervalState.COLLECTOR_UNAVAILABLE),
    )
    trades = [_trade(10, 3, 2), _trade(10, 10, 8)]  # 2nd is past the outage
    fills = simulate_passive(_passive(), trades, ExecutionPolicy(), availability=avail)
    assert fills and fills[0].quantity == 2  # only the pre-outage volume


def test_direction_and_availability_together() -> None:
    avail = _ta(("M", T(10, 0), T(11, 0), IntervalState.OBSERVED))
    # incompatible taker side excluded even though availability is fine
    incompat = simulate_passive(
        _passive(), [_trade(10, 10, 8, taker=Side.YES)], ExecutionPolicy(), availability=avail
    )
    assert incompat == []


def test_no_side_passive_symmetry() -> None:
    avail = _ta(("M", T(10, 0), T(11, 0), IntervalState.OBSERVED))
    from kalshi_weather.execution.market import Trade

    # resting NO BUY @ 40 filled by taker buying YES (taker_side YES); price: NO
    # price = 100 - yes; a yes trade at 65 -> no price 35 <= 40 crosses.
    intent = _passive(side=Side.NO, action=Action.BUY, price=40)
    t = Trade("M", T(10, 10), yes_price_cents=65, quantity=8, taker_side=Side.YES)
    fills = simulate_passive(intent, [t], ExecutionPolicy(), availability=avail)
    assert fills and fills[0].quantity == 5


# --- coverage presets ------------------------------------------------------


def test_presets_serialize_every_threshold_and_name() -> None:
    for name in ("strict-marketable", "passive-research", "exploratory"):
        p = get_preset(name)
        man = p.to_manifest()
        assert man["preset_name"] and man["version"]
        # every field is present in the manifest (no hidden threshold)
        assert set(man.keys()) == set(ReplayCoveragePolicy.__dataclass_fields__.keys())


def test_strict_marketable_disallows_passive() -> None:
    assert strict_marketable_preset().disallow_passive is True
    assert passive_research_preset().disallow_passive is False


def test_passive_research_requires_continuity_and_trades() -> None:
    p = passive_research_preset()
    assert p.require_availability_continuity is True
    assert p.passive_requires_trade_coverage is True
    assert p.min_trades_per_market >= 1
    assert p.min_observed_pct_required is True


def test_exploratory_never_relaxes_fatal_gates() -> None:
    p = exploratory_preset()
    assert p.max_conflicts == 0  # conflict gate not relaxed
    assert p.required_production_pct == 100.0  # provenance gate not relaxed
    assert "LOW FIDELITY" in p.preset_name


def test_preset_version_change_alters_manifest() -> None:
    a = strict_marketable_preset()
    b = passive_research_preset()
    assert a.to_manifest()["version"] != b.to_manifest()["version"]


def test_no_preset_uses_unsafe_override() -> None:
    # presets are pure policies; none carries an override flag (override is a
    # separate explicit CLI action).
    for name in ("strict-marketable", "passive-research", "exploratory"):
        assert "override" not in " ".join(get_preset(name).to_manifest().keys())
