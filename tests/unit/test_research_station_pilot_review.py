"""Calendar-gated station-pilot review: gate behavior, KEEP/EXTEND/REMOVE
per-station criteria, overall verdict aggregation, determinism, and isolation
(no performance fields, no trading/model imports). Pure synthetic inputs."""

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from kalshi_weather.research.station_pilot_review import (
    BANNER,
    REVIEW_GATE,
    GapAnnotation,
    ObsRowLite,
    OverallVerdict,
    PilotReviewInputs,
    StationCounts,
    StationDecision,
    build_station_counts,
    classify_station,
    gate_open,
    review,
    weather_gap_annotations,
)


def _forbidden_hits(blob: str, tokens: tuple[str, ...]) -> list[str]:
    """Forbidden tokens present as snake_case WORD PARTS, not raw substrings.

    A raw substring test flags innocent names -- "edge" inside "gap_l-edge-r"
    -- which would push the schema to rename honest keys instead of catching
    real performance fields."""
    parts = {p for p in "".join(c if c.isalnum() else " " for c in blob.lower()).split()}
    return [t for t in tokens if any(part.startswith(t) for part in parts)]


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
    d = classify_station(
        counts(
            source_unavailable_attempts=1,
            weather_gap_dates_unexplained=1,
            missing_dates=("2026-07-30",),
        )
    )
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
    hits = _forbidden_hits(
        blob,
        (
            "forecast_error",
            "mae",
            "rmse",
            "brier",
            "calibration",
            "expected_value",
            "realized_value",
            "spread",
            "liquidity",
            "price",
            "profit",
            "ranking",
            "edge",
        ),
    )
    assert hits == [], hits


def test_module_isolation_no_trading_model_experiment_imports() -> None:
    src = (
        Path(__file__).resolve().parents[2] / "src/kalshi_weather/research/station_pilot_review.py"
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


# --- evidence builder (from read-only rows) ---------------------------------

REVIEW_DATES = (date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30))


def obs(
    d: date = date(2026, 7, 28),
    variable: str = "tmax_f",
    raw_payload_id: int | None = 100,
    orphan: bool = False,
    issuance_time: datetime = datetime(2026, 7, 29, 6, 0),
    observed_at: datetime = datetime(2026, 7, 29, 6, 5),
    provider: str = "nws",
) -> ObsRowLite:
    return ObsRowLite(d, variable, raw_payload_id, orphan, issuance_time, observed_at, provider)


def _both(d: date) -> tuple[ObsRowLite, ...]:
    iss = datetime(d.year, d.month, d.day, 6, 0)
    return (
        obs(d, "tmax_f", issuance_time=iss, observed_at=iss),
        obs(d, "tmin_f", issuance_time=iss, observed_at=iss),
    )


def build(**kw: object):  # type: ignore[no-untyped-def]
    base: dict = dict(
        review_dates=REVIEW_DATES,
        observations=tuple(o for d in REVIEW_DATES for o in _both(d)),
        forecast_issue_times=(datetime(2026, 7, 28, 6), datetime(2026, 7, 28, 18)),
        window_weather_errors=0,
        window_weather_invalid=0,
    )
    base.update(kw)
    return build_station_counts("SEA", **base)


def test_build_completeness_and_missing_dates() -> None:
    c = build(observations=_both(date(2026, 7, 28)) + _both(date(2026, 7, 29)))
    assert c.dates_with_both == 2 and c.dates_with_tmax == 2 and c.dates_with_tmin == 2
    assert c.missing_dates == ("2026-07-30",)
    assert c.weather_gap_dates_unexplained == 1


def test_build_duplicate_rows_beyond_two_issuances() -> None:
    iss = datetime(2026, 7, 28, 6)
    dup = (
        _both(date(2026, 7, 28))
        + (obs(date(2026, 7, 28), "tmax_f", issuance_time=iss, observed_at=iss),) * 2
    )  # 3 tmax total -> 1 dup
    c = build(observations=dup, review_dates=(date(2026, 7, 28),))
    assert c.duplicate_observation_rows == 1


def test_build_provenance_orphan_and_missing_raw() -> None:
    rows = (obs(raw_payload_id=None), obs(variable="tmin_f", raw_payload_id=7, orphan=True))
    c = build(observations=rows, review_dates=(date(2026, 7, 28),))
    assert c.observations_missing_raw_payload == 1
    assert c.provenance_orphans == 1


def test_build_provider_mismatch_is_environment_inconsistency() -> None:
    c = build(observations=(obs(provider="iem"),), review_dates=(date(2026, 7, 28),))
    assert c.environment_inconsistencies == 1


def test_build_timezone_date_mismatch_detected() -> None:
    # observation_date 5 days off its issuance date -> tz/date defect
    bad = obs(d=date(2026, 7, 20), issuance_time=datetime(2026, 7, 28, 6))
    c = build(observations=(bad,), review_dates=(date(2026, 7, 20),))
    assert c.timezone_date_mismatches == 1


def test_build_backfill_only_date() -> None:
    # observed_at 10 days after observation_date -> collected via backfill only
    bf = obs(d=date(2026, 7, 28), observed_at=datetime(2026, 8, 8, 6))
    c = build(observations=(bf,), review_dates=(date(2026, 7, 28),))
    assert c.backfill_only_dates == 1


def test_build_forecast_gap_and_source_gap_windows() -> None:
    issues = (datetime(2026, 7, 28, 0), datetime(2026, 7, 28, 6), datetime(2026, 7, 29, 12))
    c = build(forecast_issue_times=issues)  # 6h then 30h gap
    assert c.longest_forecast_gap_hours == 30.0
    assert c.forecast_source_gap_windows == 1  # 30h > 18h threshold


# --- window-gated aggregate fields (parser/malformed/source) -----------------


def test_clean_window_supports_zero_not_unavailable() -> None:
    c = build(window_weather_errors=0, window_weather_invalid=0)
    assert c.parser_failures == 0 and c.malformed_products == 0
    assert c.evidence_unavailable == ()
    # a fully clean station is KEEP-eligible only after enough complete dates
    assert classify_station(c).decision in (StationDecision.KEEP, StationDecision.EXTEND_COLLECTION)


def test_parser_failure_outside_window_does_not_flag() -> None:
    # The SQL scopes cycles to started_at >= first_full; an out-of-window parser
    # error therefore never enters window_weather_errors -> stays 0, no flag.
    c = build(window_weather_errors=0)
    assert "parser_failures" not in c.evidence_unavailable


def test_window_errors_mark_parser_and_source_unavailable() -> None:
    c = build(window_weather_errors=4, window_weather_invalid=0)
    assert "parser_failures" in c.evidence_unavailable
    assert "source_unavailable_attempts" in c.evidence_unavailable
    assert "malformed_products" not in c.evidence_unavailable


def test_window_invalid_marks_malformed_unavailable() -> None:
    c = build(window_weather_invalid=3)
    assert "malformed_products" in c.evidence_unavailable


def test_unavailable_evidence_cannot_be_keep() -> None:
    # A window-error-flagged station (otherwise complete & clean) must NOT KEEP.
    rdates = tuple(date(2026, 7, 28) + timedelta(days=i) for i in range(7))
    c = build_station_counts(
        "SEA",
        review_dates=rdates,
        observations=tuple(o for d in rdates for o in _both(d)),
        forecast_issue_times=(datetime(2026, 7, 28, 6), datetime(2026, 7, 28, 18)),
        window_weather_errors=2,
        window_weather_invalid=0,
    )
    assert c.dates_with_both == 7  # complete
    d = classify_station(c)
    assert d.decision is StationDecision.EXTEND_COLLECTION  # NOT keep
    assert any("evidence incomplete" in r for r in d.reasons)


def test_remove_still_wins_over_unavailable() -> None:
    rdates = tuple(date(2026, 7, 28) + timedelta(days=i) for i in range(7))
    c = build_station_counts(
        "SEA",
        review_dates=rdates,
        observations=(
            *(o for d in rdates for o in _both(d)),
            obs(d=date(2026, 7, 1), issuance_time=datetime(2026, 7, 28, 6)),  # tz mismatch
        ),
        forecast_issue_times=(datetime(2026, 7, 28, 6),),
        window_weather_errors=2,  # also unavailable
        window_weather_invalid=0,
    )
    assert c.timezone_date_mismatches == 1 and c.evidence_unavailable
    assert classify_station(c).decision is StationDecision.REMOVE_FOR_DATA_QUALITY


# --- gap-ledger annotation boundary (added 2026-08-03) -----------------------


def annot(
    gap_id: str = "GAP-X",
    *,
    subsystems: tuple[str, ...] = ("WEATHER_OBSERVATIONS",),
    confidence: str = "CONFIRMED",
    treatment: str = "EXCLUDE_FOR_MISSING_COLLECTION",
    start: datetime = datetime(2026, 7, 28, 0, 0, tzinfo=UTC),
    end: datetime = datetime(2026, 7, 29, 0, 0, tzinfo=UTC),
) -> GapAnnotation:
    return GapAnnotation(
        gap_id=gap_id,
        classification="HOST_UNAVAILABLE",
        subsystems=subsystems,
        confidence=confidence,
        research_treatment=treatment,
        start_at=start,
        end_at=end,
        evidence_refs=("run:1",),
    )


def test_kalshi_only_annotation_is_never_weather_relevant() -> None:
    """A Kalshi outage must never be read as a weather-collection failure."""
    kalshi = annot(subsystems=("KALSHI_MARKET_SNAPSHOTS", "KALSHI_ORDER_BOOKS"))
    assert kalshi.weather_relevant is False
    assert (
        weather_gap_annotations(
            (kalshi,),
            window_start=datetime(2026, 7, 27, 21, 55, tzinfo=UTC),
            window_end=AFTER,
        )
        == ()
    )


def test_weather_annotation_in_window_is_surfaced() -> None:
    weather = annot(subsystems=("WEATHER_OBSERVATIONS", "WEATHER_FORECASTS"))
    kept = weather_gap_annotations(
        (weather,),
        window_start=datetime(2026, 7, 27, 21, 55, tzinfo=UTC),
        window_end=AFTER,
    )
    assert [a.gap_id for a in kept] == ["GAP-X"]


def test_annotation_outside_window_is_dropped() -> None:
    old = annot(start=datetime(2026, 7, 1, tzinfo=UTC), end=datetime(2026, 7, 2, tzinfo=UTC))
    assert (
        weather_gap_annotations(
            (old,),
            window_start=datetime(2026, 7, 27, 21, 55, tzinfo=UTC),
            window_end=AFTER,
        )
        == ()
    )


def test_recovered_not_contemporaneous_is_flagged_not_treated_as_collected() -> None:
    a = annot(treatment="RECOVERED_NOT_CONTEMPORANEOUS")
    assert a.recovered_not_contemporaneous is True


def test_unclassified_gap_demotes_remove_to_extend() -> None:
    """An unclassified collection gap makes attribution impossible, so a
    missing-date REMOVE must become EXTEND rather than blaming the station."""
    c = counts(weather_gap_dates_unexplained=3, dates_with_both=4)
    assert classify_station(c).decision is StationDecision.REMOVE_FOR_DATA_QUALITY
    demoted = classify_station(c, unclassified_gap_overlap=True)
    assert demoted.decision is StationDecision.EXTEND_COLLECTION
    assert any("UNCLASSIFIED" in r for r in demoted.reasons)


def test_unclassified_gap_does_not_demote_integrity_defects() -> None:
    """No outage can produce a wrong local date or an orphaned payload, so
    integrity defects stay REMOVE even when attribution is unavailable."""
    for field_name in (
        "timezone_date_mismatches",
        "provenance_orphans",
        "environment_inconsistencies",
        "duplicate_observation_rows",
    ):
        c = counts(**{field_name: 1})
        assert (
            classify_station(c, unclassified_gap_overlap=True).decision
            is StationDecision.REMOVE_FOR_DATA_QUALITY
        ), field_name


def test_invalid_gap_ledger_blocks_every_station() -> None:
    inputs = PilotReviewInputs(
        as_of=AFTER,
        stations=(counts("SEA"), counts("PHX"), counts("MIA")),
        outages=(),
        specification_registered=True,
        observatory_critical_count=0,
        backup_healthy=True,
        gap_ledger_valid=False,
    )
    report = review(inputs)
    assert report.overall_verdict is OverallVerdict.PILOT_REVIEW_BLOCKED
    assert all(s.decision is StationDecision.REVIEW_BLOCKED for s in report.stations)
    assert all("untrustworthy" in " ".join(s.reasons) for s in report.stations)


def test_review_blocked_outranks_remove() -> None:
    blocked = classify_station(
        counts(timezone_date_mismatches=5), blocked_reasons=("integrity failure",)
    )
    assert blocked.decision is StationDecision.REVIEW_BLOCKED


def test_gap_ledger_block_is_reported_in_json() -> None:
    inputs = PilotReviewInputs(
        as_of=AFTER,
        stations=(counts("SEA"),),
        outages=(),
        specification_registered=True,
        observatory_critical_count=0,
        backup_healthy=True,
        gap_annotations=(annot(),),
        unclassified_gap_overlap=True,
        gap_ledger_valid=True,
    )
    payload = review(inputs).to_dict()
    assert payload["gap_ledger"]["unclassified_gap_overlap"] is True
    assert payload["gap_ledger"]["valid"] is True
    assert payload["gap_ledger"]["annotations"][0]["gap_id"] == "GAP-X"
    assert "never appends" in payload["gap_ledger"]["usage"]


def test_gap_annotations_do_not_leak_performance_fields() -> None:
    inputs = PilotReviewInputs(
        as_of=AFTER,
        stations=(counts("SEA"),),
        outages=(),
        specification_registered=True,
        observatory_critical_count=0,
        backup_healthy=True,
        gap_annotations=(annot(),),
    )
    data = {
        k: v
        for k, v in review(inputs).to_dict().items()
        if k not in ("forbidden_analysis", "operator_approval_required")
    }
    blob = json.dumps(data).lower()
    hits = _forbidden_hits(
        blob, ("brier", "calibration", "profit", "pnl", "ranking", "expected_value", "edge")
    )
    assert hits == [], hits


def test_pre_gate_still_issues_no_decision_with_annotations() -> None:
    inputs = PilotReviewInputs(
        as_of=BEFORE,
        stations=(counts("SEA"),),
        outages=(),
        specification_registered=True,
        observatory_critical_count=0,
        backup_healthy=True,
        gap_annotations=(annot(),),
        unclassified_gap_overlap=True,
        gap_ledger_valid=False,
    )
    report = review(inputs)
    assert report.overall_verdict is OverallVerdict.PILOT_REVIEW_NOT_READY
    assert all(s.decision is StationDecision.PENDING_GATE for s in report.stations)
