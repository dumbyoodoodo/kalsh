"""Uncovered-collection-gap attribution: interval arithmetic, ledger coverage
compatibility, and the station-pilot integration.

All fixtures synthetic, all clocks injected. No database, no wall clock, no
production read. The point of these tests is the defect they lock out: ledger
*confidence* must never stand in for ledger *coverage*.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_weather.research import gap_attribution as ga

# Pilot window anchors (synthetic, but matching the registered shape).
W0 = datetime(2026, 7, 27, 21, 55, tzinfo=UTC)
W1 = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
WINDOW = ga.Interval(W0, W1)


def iv(h0: float, h1: float, *, base: datetime = W0) -> ga.Interval:
    return ga.Interval(base + timedelta(hours=h0), base + timedelta(hours=h1))


def gap(interval: ga.Interval, subsystem: str = "WEATHER_OBSERVATIONS") -> ga.CollectorGap:
    return ga.CollectorGap(interval=interval, subsystem=subsystem, kind="no_runs")


def record(
    gap_id: str = "GAP-1",
    *,
    classification: str = "HOST_UNAVAILABLE",
    subsystems: tuple[str, ...] = ("WEATHER_OBSERVATIONS", "WEATHER_FORECASTS"),
    confidence: str = "CONFIRMED",
    interval: ga.Interval | None = None,
) -> ga.LedgerCoverageRecord:
    return ga.LedgerCoverageRecord(
        gap_id=gap_id,
        classification=classification,
        subsystems=subsystems,
        confidence=confidence,
        interval=interval if interval is not None else iv(0, 100),
    )


def runs(*specs: tuple[float, bool]) -> tuple[ga.CollectorRun, ...]:
    return tuple(ga.CollectorRun(started_at=W0 + timedelta(hours=h), success=ok) for h, ok in specs)


# --- interval arithmetic -----------------------------------------------------


def test_naive_interval_rejected() -> None:
    with pytest.raises(ga.GapAttributionError, match="timezone-aware"):
        ga.Interval(datetime(2026, 7, 27, 0, 0), W1)


def test_inverted_interval_rejected() -> None:
    with pytest.raises(ga.GapAttributionError, match="precedes"):
        ga.Interval(W1, W0)


def test_intersection_partial_nested_and_disjoint() -> None:
    a, b = iv(0, 10), iv(5, 20)
    assert ga.Interval(W0 + timedelta(hours=5), W0 + timedelta(hours=10)) == a.intersect(b)
    nested = iv(2, 4)
    assert a.intersect(nested) == nested
    assert a.intersect(iv(50, 60)) is None


def test_adjacent_intervals_do_not_overlap() -> None:
    """Touching at a boundary is adjacency, not overlap -- otherwise a record
    that ends exactly when a gap starts would falsely 'cover' it."""
    a, b = iv(0, 5), iv(5, 10)
    assert a.overlaps(b) is False
    assert a.intersect(b) is None


def test_exact_boundary_overlap_is_covered() -> None:
    base = iv(0, 5)
    assert ga.subtract(base, [iv(0, 5)]) == ()


def test_subtract_partial_leaves_remainder() -> None:
    assert ga.subtract(iv(0, 10), [iv(0, 4)]) == (iv(4, 10),)
    assert ga.subtract(iv(0, 10), [iv(6, 10)]) == (iv(0, 6),)


def test_subtract_nested_cut_splits_into_two() -> None:
    assert ga.subtract(iv(0, 10), [iv(3, 6)]) == (iv(0, 3), iv(6, 10))


def test_subtract_multiple_cuts_jointly_cover() -> None:
    assert ga.subtract(iv(0, 10), [iv(0, 5), iv(5, 10)]) == ()


def test_subtract_disjoint_cut_is_noop() -> None:
    assert ga.subtract(iv(0, 5), [iv(20, 30)]) == (iv(0, 5),)


def test_merge_unions_overlapping_and_adjacent() -> None:
    assert ga.merge([iv(0, 5), iv(4, 9)]) == (iv(0, 9),)
    assert ga.merge([iv(0, 5), iv(5, 9)]) == (iv(0, 9),)
    assert ga.merge([iv(0, 2), iv(6, 9)]) == (iv(0, 2), iv(6, 9))


# --- collector-gap derivation ------------------------------------------------


def test_no_gap_when_runs_are_dense() -> None:
    dense = runs(*[(h * 0.5, True) for h in range(0, 200)])
    assert (
        ga.derive_collector_gap_intervals(dense, window=ga.Interval(W0, W0 + timedelta(hours=99)))
        == ()
    )


def test_absence_of_runs_beyond_tolerance_is_a_gap() -> None:
    sparse = runs((0, True), (10, True))
    found = ga.derive_collector_gap_intervals(
        sparse, window=ga.Interval(W0, W0 + timedelta(hours=10))
    )
    assert len(found) == 1
    assert found[0].kind == "no_runs"
    assert found[0].interval.duration_hours == pytest.approx(10.0)


def test_gap_below_tolerance_is_ignored() -> None:
    close = runs((0, True), (1.5, True))
    assert (
        ga.derive_collector_gap_intervals(close, window=ga.Interval(W0, W0 + timedelta(hours=1.5)))
        == ()
    )


def test_consecutive_failed_cycles_are_a_continuity_break() -> None:
    """Runs kept starting but every one failed -- collection still stopped."""
    failing = runs((0, True), (1, False), (2, False), (3, False), (4, False), (5, True))
    found = ga.derive_collector_gap_intervals(
        failing, window=ga.Interval(W0, W0 + timedelta(hours=5))
    )
    kinds = {g.kind for g in found}
    assert "failed_cycles" in kinds


def test_successful_weather_cycles_never_form_a_gap() -> None:
    """Kalshi-only failures never reach here: the caller passes weather runs,
    and a successful weather cycle is never part of a gap."""
    ok = runs(*[(h, True) for h in range(0, 20)])
    found = ga.derive_collector_gap_intervals(ok, window=ga.Interval(W0, W0 + timedelta(hours=19)))
    assert found == ()


def test_gaps_are_clipped_to_the_window() -> None:
    sparse = runs((0, True), (50, True))
    window = ga.Interval(W0 + timedelta(hours=10), W0 + timedelta(hours=20))
    found = ga.derive_collector_gap_intervals(sparse, window=window)
    assert len(found) == 1
    assert found[0].interval.start == window.start
    assert found[0].interval.end == window.end


# --- ledger coverage ---------------------------------------------------------


def test_fully_covered_confirmed() -> None:
    cov = ga.compute_ledger_coverage(gap(iv(2, 6)), [record(interval=iv(0, 10))])
    assert cov.status is ga.CoverageStatus.COVERED_CONFIRMED
    assert cov.uncovered_intervals == ()
    assert cov.unclassified is False


def test_uncovered_when_no_record() -> None:
    cov = ga.compute_ledger_coverage(gap(iv(2, 6)), [])
    assert cov.status is ga.CoverageStatus.UNCOVERED
    assert cov.uncovered_hours == pytest.approx(4.0)
    assert cov.unclassified is True


def test_partial_coverage_reports_the_uncovered_segment() -> None:
    cov = ga.compute_ledger_coverage(gap(iv(0, 10)), [record(interval=iv(0, 4))])
    assert cov.status is ga.CoverageStatus.COVERED_PARTIAL
    assert cov.uncovered_intervals == (iv(4, 10),)
    assert cov.unclassified is True


def test_two_records_jointly_cover_one_gap() -> None:
    cov = ga.compute_ledger_coverage(
        gap(iv(0, 10)),
        [record("GAP-A", interval=iv(0, 5)), record("GAP-B", interval=iv(5, 10))],
    )
    assert cov.status is ga.CoverageStatus.COVERED_CONFIRMED
    assert cov.covering_gap_ids == ("GAP-A", "GAP-B")
    assert cov.unclassified is False


def test_kalshi_only_record_never_covers_a_weather_gap() -> None:
    kalshi = record(
        subsystems=("KALSHI_MARKET_SNAPSHOTS", "KALSHI_ORDER_BOOKS"), interval=iv(0, 100)
    )
    assert kalshi.can_cover(gap(iv(2, 6))) is False
    cov = ga.compute_ledger_coverage(gap(iv(2, 6)), [kalshi])
    assert cov.status is ga.CoverageStatus.UNCOVERED


def test_recovered_station_day_parser_record_does_not_cover_a_host_interval() -> None:
    """A parser failure means the collector RAN and mishandled a payload, so it
    cannot explain an absence of collector runs, however wide its interval."""
    parser = record(classification="PARSER_FAILURE", interval=iv(0, 200))
    assert parser.can_cover(gap(iv(2, 6))) is False
    assert ga.compute_ledger_coverage(gap(iv(2, 6)), [parser]).status is ga.CoverageStatus.UNCOVERED


def test_persistence_failure_also_does_not_cover_a_continuity_gap() -> None:
    persist = record(classification="PERSISTENCE_FAILURE", interval=iv(0, 200))
    assert persist.can_cover(gap(iv(2, 6))) is False


def test_low_confidence_full_coverage_is_ambiguous_not_confirmed() -> None:
    cov = ga.compute_ledger_coverage(
        gap(iv(2, 6)), [record(confidence="MEDIUM", interval=iv(0, 10))]
    )
    assert cov.status is ga.CoverageStatus.COVERAGE_AMBIGUOUS
    assert cov.unclassified is True


def test_broad_unrelated_record_cannot_mask_an_uncovered_interval() -> None:
    """A wide record with an incompatible cause must not swallow the gap."""
    broad_wrong_cause = record(classification="RATE_LIMIT_TRANSIENT", interval=iv(-50, 500))
    cov = ga.compute_ledger_coverage(gap(iv(0, 10)), [broad_wrong_cause])
    assert cov.status is ga.CoverageStatus.UNCOVERED
    assert cov.uncovered_intervals == (iv(0, 10),)


def test_subsystem_mismatch_within_weather_is_respected() -> None:
    """A forecast-only record does not cover an observations gap."""
    forecast_only = record(subsystems=("WEATHER_FORECASTS",), interval=iv(0, 100))
    assert forecast_only.can_cover(gap(iv(2, 6), subsystem="WEATHER_OBSERVATIONS")) is False


# --- affected local dates ----------------------------------------------------


COMPLETE = [date(2026, 7, 28) + timedelta(days=i) for i in range(7)]


@pytest.mark.parametrize(
    ("station", "tz"),
    [("SEA", "America/Los_Angeles"), ("PHX", "America/Phoenix"), ("MIA", "America/New_York")],
)
def test_affected_local_dates_per_station_timezone(station: str, tz: str) -> None:
    """Each station maps the same UTC interval through its OWN zone; PHX is
    fixed UTC-7 with no DST, SEA and MIA observe it."""
    interval = ga.Interval(
        datetime(2026, 7, 29, 3, 0, tzinfo=UTC), datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
    )
    dates = ga.affected_local_dates([interval], timezone_name=tz, complete_dates=COMPLETE)
    assert dates, station
    assert all(d in {c.isoformat() for c in COMPLETE} for d in dates)


def test_phx_is_utc_minus_7_year_round() -> None:
    """2026-07-29 04:00Z is 2026-07-28 21:00 in Phoenix (UTC-7), not the 29th."""
    interval = ga.Interval(
        datetime(2026, 7, 29, 4, 0, tzinfo=UTC), datetime(2026, 7, 29, 5, 0, tzinfo=UTC)
    )
    assert ga.affected_local_dates(
        [interval], timezone_name="America/Phoenix", complete_dates=COMPLETE
    ) == ("2026-07-28",)


def test_mia_dst_offset_differs_from_phx_for_the_same_instant() -> None:
    interval = ga.Interval(
        datetime(2026, 7, 29, 4, 0, tzinfo=UTC), datetime(2026, 7, 29, 5, 0, tzinfo=UTC)
    )
    mia = ga.affected_local_dates(
        [interval], timezone_name="America/New_York", complete_dates=COMPLETE
    )
    phx = ga.affected_local_dates(
        [interval], timezone_name="America/Phoenix", complete_dates=COMPLETE
    )
    assert mia == ("2026-07-29",)  # EDT = UTC-4
    assert phx == ("2026-07-28",)  # MST = UTC-7


def test_incomplete_dates_are_not_reported() -> None:
    interval = ga.Interval(
        datetime(2026, 9, 1, 0, 0, tzinfo=UTC), datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
    )
    assert (
        ga.affected_local_dates(
            [interval], timezone_name="America/New_York", complete_dates=COMPLETE
        )
        == ()
    )


# --- summary -----------------------------------------------------------------


def test_summary_no_gaps_is_not_unclassified() -> None:
    s = ga.summarize_unclassified_overlap([], [record()])
    assert s.unclassified_overlap is False
    assert s.total_uncovered_hours == 0.0


def test_summary_uncovered_gap_sets_ambiguity() -> None:
    s = ga.summarize_unclassified_overlap([gap(iv(0, 6))], [])
    assert s.unclassified_overlap is True
    assert s.total_uncovered_hours == pytest.approx(6.0)


def test_summary_fully_covered_gap_is_not_ambiguous() -> None:
    s = ga.summarize_unclassified_overlap([gap(iv(2, 6))], [record(interval=iv(0, 10))])
    assert s.unclassified_overlap is False
    assert s.total_uncovered_hours == 0.0


def test_total_uncovered_duration_is_deterministic() -> None:
    gaps = [gap(iv(0, 6)), gap(iv(20, 25))]
    a = ga.summarize_unclassified_overlap(gaps, [record(interval=iv(0, 3))])
    b = ga.summarize_unclassified_overlap(gaps, [record(interval=iv(0, 3))])
    assert a.total_uncovered_hours == b.total_uncovered_hours == pytest.approx(8.0)
    assert a.to_dict() == b.to_dict()


def test_summary_contains_no_performance_fields() -> None:
    import json

    blob = json.dumps(
        ga.summarize_unclassified_overlap([gap(iv(0, 6))], [record()]).to_dict()
    ).lower()
    parts = {p for p in "".join(c if c.isalnum() else " " for c in blob).split()}
    for token in ("brier", "calibration", "profit", "pnl", "ranking", "accuracy", "sharpe"):
        assert not any(p.startswith(token) for p in parts), token


def test_summary_reports_evidence_and_covering_ids() -> None:
    derived = ga.derive_collector_gap_intervals(
        runs((0, True), (10, True)), window=ga.Interval(W0, W0 + timedelta(hours=10))
    )
    s = ga.summarize_unclassified_overlap(derived, [record(interval=iv(0, 4))])
    payload = s.to_dict()
    assert payload["coverages"][0]["gap"]["evidence_refs"]
    assert payload["coverages"][0]["covering_gap_ids"] == ["GAP-1"]
    assert "ledger confidence alone never implies" in payload["note"]


# --- the defect this closes --------------------------------------------------


def test_all_confirmed_ledger_does_not_imply_complete_attribution() -> None:
    """The regression guard. Previously an all-CONFIRMED ledger produced
    unclassified_overlap=False no matter how many outages went unrecorded."""
    all_confirmed = [record("GAP-A", confidence="CONFIRMED", interval=iv(0, 1))]
    uncovered_gap = gap(iv(40, 60))
    s = ga.summarize_unclassified_overlap([uncovered_gap], all_confirmed)
    assert s.unclassified_overlap is True, "confidence must never stand in for coverage"


# --- isolation ---------------------------------------------------------------


def test_module_has_no_database_experiment_or_trading_imports() -> None:
    source = Path(ga.__file__).read_text()
    for line in source.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            continue
        for banned in (
            "sqlalchemy",
            "psycopg",
            "experiments",
            "h0019",
            "h0020",
            "paper",
            "broker",
            "httpx",
            "requests",
            "KalshiClient",
        ):
            assert banned not in stripped, f"gap_attribution imports {banned}: {stripped}"


def test_reuses_the_registered_cadence_tolerance() -> None:
    """The threshold must be the observatory's own constant, not a new one."""
    from kalshi_weather.ops.forecast_cadence import CadenceConfig

    assert ga.DEFAULT_OUTAGE_THRESHOLD_HOURS == CadenceConfig().outage_threshold_hours == 2.0


# --- CLI future as-of guard --------------------------------------------------


def test_future_as_of_refused_before_any_query() -> None:
    """The refusal must fire before the loader touches production. Pointing the
    ledger path at nothing and passing an unreachable future time: if the guard
    did not fire first, the command would fail on the database instead."""
    from typer.testing import CliRunner

    from kalshi_weather.cli import app

    result = CliRunner().invoke(
        app, ["research", "station-pilot-review", "--as-of", "2099-01-01T00:00:00Z"]
    )
    assert result.exit_code == 2
    assert "REFUSED: --as-of cannot be later than the current UTC time." in result.stdout


def test_future_as_of_refusal_writes_no_artifact(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from kalshi_weather.cli import app

    out = tmp_path / "artifact.md"
    result = CliRunner().invoke(
        app,
        [
            "research",
            "station-pilot-review",
            "--as-of",
            "2099-01-01T00:00:00Z",
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 2
    assert not out.exists()


def test_no_clock_bypass_option_exists() -> None:
    from kalshi_weather.cli import research_app

    cmd = next(c for c in research_app.registered_commands if c.name == "station-pilot-review")
    names = set(cmd.callback.__annotations__)
    for bypass in ("force", "ignore_clock", "allow_future", "skip_gate", "no_gate"):
        assert bypass not in names, f"bypass option {bypass} must not exist"
