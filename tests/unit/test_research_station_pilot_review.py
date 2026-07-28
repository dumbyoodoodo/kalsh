"""Calendar-gated station-pilot review: gate behavior, KEEP/EXTEND/REMOVE
per-station criteria, overall verdict aggregation, determinism, and isolation
(no performance fields, no trading/model imports). Pure synthetic inputs."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from kalshi_weather.research.station_pilot_review import (
    BANNER,
    REVIEW_GATE,
    OverallVerdict,
    PilotReviewInputs,
    StationCounts,
    StationDecision,
    classify_station,
    gate_open,
    review,
)

BEFORE = datetime(2026, 8, 3, 23, 59, tzinfo=UTC)
AT_GATE = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
AFTER = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def counts(station: str = "SEA", **kw: object) -> StationCounts:
    base = dict(
        station=station,
        expected_dates=7,
        dates_with_tmax=7,
        dates_with_tmin=7,
        dates_with_both=7,
        missing_dates=(),
        partial_current_excluded=True,
        duplicate_observation_rows=0,
        backfill_only_dates=0,
        forecast_issuances=98,
        expected_forecast_issuances=98,
        longest_forecast_gap_hours=6.0,
        forecast_source_gap_windows=0,
        observations_missing_raw_payload=0,
        observations_missing_source_product=0,
        provenance_orphans=0,
        environment_inconsistencies=0,
        parser_failures=0,
        malformed_products=0,
        source_unavailable_attempts=0,
        timezone_date_mismatches=0,
        weather_gap_dates_outage_explained=0,
        weather_gap_dates_unexplained=0,
    )
    base.update(kw)
    return StationCounts(**base)  # type: ignore[arg-type]


def inputs(
    *station_counts: StationCounts, as_of: datetime = AFTER, **kw: object
) -> PilotReviewInputs:
    base = dict(
        as_of=as_of,
        stations=station_counts or (counts("SEA"), counts("PHX"), counts("MIA")),
        outages=(),
        specification_registered=True,
        observatory_critical_count=0,
        backup_healthy=True,
    )
    base.update(kw)
    return PilotReviewInputs(**base)  # type: ignore[arg-type]


# --- calendar gate -----------------------------------------------------------

def test_gate_closed_before_and_open_at_and_after() -> None:
    assert gate_open(BEFORE) is False
    assert gate_open(AT_GATE) is True  # exactly at gate is open
    assert gate_open(AFTER) is True


def test_before_gate_is_not_ready_no_decisions() -> None:
    r = review(inputs(as_of=BEFORE))
    assert r.overall_verdict is OverallVerdict.PILOT_REVIEW_NOT_READY
    assert r.gate_open is False
    assert all(s.decision is StationDecision.PENDING_GATE for s in r.stations)
    assert r.seconds_until_gate > 0


def test_exactly_at_gate_is_ready() -> None:
    r = review(inputs(as_of=AT_GATE))
    assert r.gate_open is True
    assert r.overall_verdict is not OverallVerdict.PILOT_REVIEW_NOT_READY
    assert r.seconds_until_gate == 0


def test_explicit_as_of_determinism() -> None:
    a = review(inputs(as_of=AFTER)).to_dict()
    b = review(inputs(as_of=AFTER)).to_dict()
    assert a == b


def test_review_gate_constant_is_registered_date() -> None:
    assert REVIEW_GATE.isoformat() == "2026-08-04T00:00:00+00:00"


# --- per-station decisions ---------------------------------------------------

def test_keep_when_complete_and_clean() -> None:
    assert classify_station(counts()).decision is StationDecision.KEEP


def test_extend_on_insufficient_complete_dates() -> None:
    d = classify_station(counts(dates_with_both=3, dates_with_tmax=3, dates_with_tmin=3))
    assert d.decision is StationDecision.EXTEND_COLLECTION


def test_host_outage_dates_drive_extend_not_remove() -> None:
    # gaps explained by a known host/upstream outage -> EXTEND, never REMOVE
    d = classify_station(
        counts(
            dates_with_both=5,
            missing_dates=("2026-07-30", "2026-07-31"),
            weather_gap_dates_outage_explained=2,
            weather_gap_dates_unexplained=0,
        )
    )
    assert d.decision is StationDecision.EXTEND_COLLECTION


def test_repeated_unexplained_gap_is_remove() -> None:
    d = classify_station(
        counts(missing_dates=("2026-07-30", "2026-07-31"), weather_gap_dates_unexplained=2)
    )
    assert d.decision is StationDecision.REMOVE_FOR_DATA_QUALITY
    assert any("unexplained" in r for r in d.reasons)


def test_single_parser_failure_extends_repeated_removes() -> None:
    assert classify_station(counts(parser_failures=1)).decision is StationDecision.EXTEND_COLLECTION
    assert (
        classify_station(counts(parser_failures=3)).decision
        is StationDecision.REMOVE_FOR_DATA_QUALITY
    )


def test_timezone_mismatch_is_remove() -> None:
    assert (
        classify_station(counts(timezone_date_mismatches=1)).decision
        is StationDecision.REMOVE_FOR_DATA_QUALITY
    )


def test_provenance_orphan_is_remove() -> None:
    assert (
        classify_station(counts(provenance_orphans=1)).decision
        is StationDecision.REMOVE_FOR_DATA_QUALITY
    )


def test_duplicate_observation_is_remove() -> None:
    assert (
        classify_station(counts(duplicate_observation_rows=2)).decision
        is StationDecision.REMOVE_FOR_DATA_QUALITY
    )


def test_isolated_source_gap_extends() -> None:
    d = classify_station(counts(source_unavailable_attempts=1, weather_gap_dates_unexplained=1,
                                missing_dates=("2026-07-30",)))
    assert d.decision is StationDecision.EXTEND_COLLECTION


# --- overall verdict aggregation --------------------------------------------

def test_overall_acceptable_when_all_keep() -> None:
    r = review(inputs(counts("SEA"), counts("PHX"), counts("MIA")))
    assert r.overall_verdict is OverallVerdict.PILOT_OPERATIONALLY_ACCEPTABLE


def test_overall_extension_when_any_extend_and_none_remove() -> None:
    r = review(inputs(counts("SEA"), counts("PHX", dates_with_both=3), counts("MIA")))
    assert r.overall_verdict is OverallVerdict.PILOT_COLLECTION_EXTENSION_REQUIRED


def test_overall_failure_when_any_remove() -> None:
    r = review(inputs(counts("SEA"), counts("PHX", timezone_date_mismatches=1), counts("MIA")))
    assert r.overall_verdict is OverallVerdict.PILOT_DATA_QUALITY_FAILURE


def test_specification_incomplete_blocks() -> None:
    r = review(inputs(as_of=AFTER, specification_registered=False))
    assert r.overall_verdict is OverallVerdict.PILOT_SPECIFICATION_INCOMPLETE


# --- isolation / banner / no-performance ------------------------------------

def test_banner_and_no_forbidden_fields_in_output() -> None:
    d = review(inputs(as_of=AFTER)).to_dict()
    assert d["banner"] == BANNER
    # The disclaimer fields intentionally NAME the excluded analyses; scan the
    # actual data (everything else) for forbidden performance metric fields.
    _disclaimers = ("forbidden_analysis", "operator_approval_required")
    data = {k: v for k, v in d.items() if k not in _disclaimers}
    blob = json.dumps(data).lower()
    for forbidden in (
        "forecast_error", "mae", "rmse", "brier", "calibration", "expected_value",
        "realized_value", "spread", "liquidity", "price", "profit", "ranking", "edge",
    ):
        assert forbidden not in blob, forbidden


def test_module_isolation_no_trading_model_experiment_imports() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "src/kalshi_weather/research/station_pilot_review.py"
    ).read_text()
    # No trading/model/experiment imports, no wall clock. (Metric WORDS appear
    # only in the module's exclusion disclaimer, which is intentional.)
    banned = re.compile(
        r"(?:from|import)\s+kalshi_weather\.(?:experiments|strategy|execution|backtest|models)"
        r"|datetime\.now|utcnow|date\.today",
        re.IGNORECASE,
    )
    m = banned.search(src)
    assert m is None, f"station_pilot_review references banned symbol: {m and m.group(0)!r}"


def test_pre_gate_to_dict_marks_not_ready_verdict() -> None:
    d = review(inputs(as_of=BEFORE)).to_dict()
    assert d["not_ready_verdict"] == "STATION PILOT REVIEW NOT READY — CALENDAR GATED"
    assert d["gate_open"] is False
