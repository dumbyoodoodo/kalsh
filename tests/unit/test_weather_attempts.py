"""Station-level weather collection-attempt evidence: vocabulary, invariants,
reconciliation, summary, and disposable-DB schema/repository behaviour.

Synthetic fixtures and an in-memory SQLite database only. Nothing here touches
production, and the guards at the end assert it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kalshi_weather.ingestion import weather_attempts as wa
from kalshi_weather.ingestion.weather_attempts import (
    AttemptEvidenceState as ES,
)
from kalshi_weather.ingestion.weather_attempts import (
    AttemptOutcome as O,
)
from kalshi_weather.ingestion.weather_attempts import (
    AttemptStage as S,
)
from kalshi_weather.ingestion.weather_attempts import (
    SourceAvailability as A,
)
from kalshi_weather.ingestion.weather_attempts import (
    WeatherProductType as P,
)
from kalshi_weather.storage.models import Base, WeatherCollectionAttempt
from kalshi_weather.storage.repositories import (
    AttemptConflictError,
    append_terminal_attempt,
    detect_duplicate_logical_attempts,
    find_attempt_by_id,
    list_attempts_for_collector_run,
    list_attempts_for_product_window,
    list_attempts_for_station_window,
)

T0 = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
T1 = T0 + timedelta(seconds=8)


def rec(
    station: str = "SEA",
    product: P = P.CLI_OBSERVATIONS,
    outcome: O = O.SUCCEEDED_NEW_DATA,
    **kw: object,
) -> wa.AttemptRecord:
    run_id = int(kw.pop("collector_run_id", 1))  # type: ignore[arg-type]
    env = str(kw.pop("environment", "production"))
    key = wa.logical_request_key(
        collector_run_id=run_id,
        environment=env,
        station_code=station,
        product_type=product,
        target_window="2026-08-11",
    )
    base: dict[str, object] = {
        "attempt_id": wa.attempt_id_for(key),
        "collector_run_id": run_id,
        "environment": env,
        "station_code": station,
        "product_type": product,
        "logical_request_key": key,
        "source_endpoint": "nws:cli-observations",
        "stage": S.NORMALIZED_PERSISTED,
        "outcome": outcome,
        "source_availability": A.AVAILABLE,
        "requested_at": T0,
        "completed_at": T1,
        "observed_at": T1,
        "created_at": T1,
        "target_station_local_date": date(2026, 8, 11),
        "parsed_entity_count": 4,
        "persisted_entity_count": 4,
        "raw_payload_id": 99,
    }
    base.update(kw)
    return wa.AttemptRecord(**base)  # type: ignore[arg-type]


# --- vocabularies ------------------------------------------------------------


def test_all_required_vocabulary_values_exist() -> None:
    for name in (
        "REQUEST_STARTED",
        "RESPONSE_RECEIVED",
        "RAW_PAYLOAD_PERSISTED",
        "PARSED",
        "NORMALIZED_PERSISTED",
        "TERMINAL",
    ):
        assert hasattr(S, name), name
    for name in (
        "SUCCEEDED_NEW_DATA",
        "SUCCEEDED_DUPLICATE",
        "SUCCEEDED_NO_DATA",
        "SOURCE_UNAVAILABLE",
        "REQUEST_FAILED",
        "MALFORMED_RESPONSE",
        "PARSER_REJECTED",
        "PERSISTENCE_FAILED",
        "UNSUPPORTED_PRODUCT",
        "CANCELLED",
        "UNKNOWN_FAILURE",
    ):
        assert hasattr(O, name), name
    assert {str(x) for x in A} == {
        "AVAILABLE",
        "CONFIRMED_UNAVAILABLE",
        "UNKNOWN",
        "NOT_APPLICABLE",
    }
    assert {str(x) for x in ES} == {
        "KNOWN_ZERO",
        "KNOWN_NONZERO",
        "LEGACY_UNKNOWN",
        "EVIDENCE_MISSING",
    }


def test_products_are_explicit_not_generic() -> None:
    assert P.CLI_OBSERVATIONS is not P.GRIDPOINT_FORECAST
    assert "GENERIC" not in {str(p) for p in P}


# --- valid outcomes ----------------------------------------------------------


def test_succeeded_new_data() -> None:
    assert rec().validate() == []


def test_succeeded_duplicate() -> None:
    r = rec(outcome=O.SUCCEEDED_DUPLICATE, persisted_entity_count=0, duplicate_entity_count=4)
    assert r.validate() == []


def test_succeeded_no_data() -> None:
    r = rec(
        outcome=O.SUCCEEDED_NO_DATA,
        parsed_entity_count=0,
        persisted_entity_count=0,
        stage=S.PARSED,
    )
    assert r.validate() == []


def test_source_unavailable() -> None:
    r = rec(
        outcome=O.SOURCE_UNAVAILABLE,
        source_availability=A.CONFIRMED_UNAVAILABLE,
        parsed_entity_count=0,
        persisted_entity_count=0,
        raw_payload_id=None,
        stage=S.REQUEST_STARTED,
    )
    assert r.validate() == []


def test_request_failed() -> None:
    r = rec(
        outcome=O.REQUEST_FAILED,
        source_availability=A.UNKNOWN,
        parsed_entity_count=0,
        persisted_entity_count=0,
        raw_payload_id=None,
        stage=S.REQUEST_STARTED,
    )
    assert r.validate() == []


def test_malformed_response_with_body() -> None:
    r = rec(
        outcome=O.MALFORMED_RESPONSE,
        parsed_entity_count=0,
        persisted_entity_count=0,
        stage=S.RAW_PAYLOAD_PERSISTED,
        raw_payload_id=7,
    )
    assert r.validate() == []


def test_parser_rejected_with_raw_payload() -> None:
    r = rec(
        outcome=O.PARSER_REJECTED,
        parsed_entity_count=0,
        persisted_entity_count=0,
        stage=S.PARSED,
        raw_payload_id=7,
        parser_error_type="MalformedPayloadError",
    )
    assert r.validate() == []


def test_parser_rejected_without_raw_payload_fails() -> None:
    r = rec(
        outcome=O.PARSER_REJECTED,
        parsed_entity_count=0,
        persisted_entity_count=0,
        stage=S.PARSED,
        raw_payload_id=None,
        parser_error_type="X",
    )
    assert any("raw_payload_id" in p for p in r.validate())


def test_persistence_failed_preserves_provenance() -> None:
    r = rec(
        outcome=O.PERSISTENCE_FAILED,
        parsed_entity_count=4,
        persisted_entity_count=0,
        persistence_error_type="IntegrityError",
        raw_payload_id=7,
    )
    assert r.validate() == []


def test_persistence_failed_requires_error_type_and_parsed() -> None:
    assert any(
        "persistence_error_type" in p
        for p in rec(
            outcome=O.PERSISTENCE_FAILED, parsed_entity_count=4, persisted_entity_count=0
        ).validate()
    )


def test_unsupported_and_cancelled_are_valid() -> None:
    for o in (O.UNSUPPORTED_PRODUCT, O.CANCELLED):
        r = rec(outcome=o, parsed_entity_count=0, persisted_entity_count=0, raw_payload_id=None)
        assert r.validate() == [], o


def test_unknown_failure_requires_a_reason() -> None:
    bare = rec(outcome=O.UNKNOWN_FAILURE, parsed_entity_count=0, persisted_entity_count=0)
    assert any("sanitized failure reason" in p for p in bare.validate())
    ok = rec(
        outcome=O.UNKNOWN_FAILURE,
        parsed_entity_count=0,
        persisted_entity_count=0,
        parser_error_message="something odd",
    )
    assert ok.validate() == []


def test_no_data_must_not_be_confirmed_unavailable() -> None:
    """The source answered; it simply had nothing. Conflating these is the
    original defect."""
    r = rec(
        outcome=O.SUCCEEDED_NO_DATA,
        parsed_entity_count=0,
        persisted_entity_count=0,
        source_availability=A.CONFIRMED_UNAVAILABLE,
    )
    assert any("CONFIRMED_UNAVAILABLE" in p for p in r.validate())


def test_source_unavailable_must_not_link_a_payload() -> None:
    r = rec(
        outcome=O.SOURCE_UNAVAILABLE,
        source_availability=A.CONFIRMED_UNAVAILABLE,
        parsed_entity_count=0,
        persisted_entity_count=0,
        raw_payload_id=5,
    )
    assert any("must not link a raw payload" in p for p in r.validate())


# --- structural invariants ---------------------------------------------------


def test_naive_timestamps_rejected() -> None:
    r = rec(requested_at=datetime(2026, 8, 11, 15, 0))
    assert any("timezone-aware" in p for p in r.validate())


def test_completed_before_requested_rejected() -> None:
    assert any("precedes" in p for p in rec(completed_at=T0 - timedelta(seconds=5)).validate())


def test_negative_counts_rejected() -> None:
    assert any("retry_count" in p for p in rec(retry_count=-1).validate())
    assert any(">= 0" in p for p in rec(parsed_entity_count=-1).validate())


def test_persisted_cannot_exceed_parsed() -> None:
    assert any(
        "persisted_entity_count exceeds" in p
        for p in rec(parsed_entity_count=2, persisted_entity_count=5).validate()
    )


def test_duplicate_cannot_exceed_parsed() -> None:
    r = rec(
        outcome=O.SUCCEEDED_DUPLICATE,
        parsed_entity_count=2,
        persisted_entity_count=0,
        duplicate_entity_count=5,
    )
    assert any("duplicate_entity_count exceeds" in p for p in r.validate())


def test_persisted_plus_duplicate_cannot_exceed_parsed() -> None:
    r = rec(parsed_entity_count=4, persisted_entity_count=3, duplicate_entity_count=3)
    assert any("persisted + duplicate exceeds" in p for p in r.validate())


def test_missing_logical_key_and_station_rejected() -> None:
    assert any("logical_request_key" in p for p in rec(logical_request_key=" ").validate())
    assert any("station_code" in p for p in rec(station_code=" ").validate())


def test_require_valid_raises() -> None:
    with pytest.raises(wa.AttemptIntegrityError):
        rec(parsed_entity_count=1, persisted_entity_count=9).require_valid()


# --- sanitization ------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "Authorization: Bearer abc123",
        "api_key=SUPERSECRET",
        "Cookie: session=xyz",
        "password=hunter2",
        "X-Amz-Security-Token: zzz",
    ],
)
def test_sensitive_error_text_is_dropped(secret: str) -> None:
    assert wa.sanitize_error(secret) is None


def test_sanitizer_keeps_benign_text_and_bounds_length() -> None:
    assert wa.sanitize_error("MalformedPayloadError: bad temp") == "MalformedPayloadError: bad temp"
    assert len(wa.sanitize_error("x" * 2000) or "") == wa.MAX_ERROR_DETAIL


def test_record_with_unsanitized_secret_is_rejected() -> None:
    r = rec(
        outcome=O.PARSER_REJECTED,
        parsed_entity_count=0,
        persisted_entity_count=0,
        raw_payload_id=7,
        parser_error_type="X",
        parser_error_message="Bearer abc",
    )
    assert any("sensitive material" in p for p in r.validate())


# --- logical identity --------------------------------------------------------


def test_logical_key_excludes_retry_number() -> None:
    k = wa.logical_request_key(
        collector_run_id=1,
        environment="production",
        station_code="SEA",
        product_type=P.CLI_OBSERVATIONS,
        target_window="2026-08-11",
    )
    assert "retry" not in k.lower()
    assert wa.attempt_id_for(k) == wa.attempt_id_for(k)


def test_different_products_get_different_keys() -> None:
    a = wa.logical_request_key(
        collector_run_id=1,
        environment="production",
        station_code="SEA",
        product_type=P.CLI_OBSERVATIONS,
        target_window="2026-08-11",
    )
    b = wa.logical_request_key(
        collector_run_id=1,
        environment="production",
        station_code="SEA",
        product_type=P.GRIDPOINT_FORECAST,
        target_window="2026-08-11",
    )
    assert a != b and wa.attempt_id_for(a) != wa.attempt_id_for(b)


# --- reconciliation ----------------------------------------------------------

PAIRS = [("SEA", P.CLI_OBSERVATIONS), ("PHX", P.CLI_OBSERVATIONS), ("MIA", P.CLI_OBSERVATIONS)]


def test_reconciliation_exact_match() -> None:
    attempts = [rec(s) for s, _ in PAIRS]
    r = wa.reconcile_collector_run(
        collector_run_id=1, environment="production", attempts=attempts, expected_pairs=PAIRS
    )
    assert r.ok, r.problems


def test_reconciliation_missing_pair() -> None:
    r = wa.reconcile_collector_run(
        collector_run_id=1,
        environment="production",
        attempts=[rec("SEA"), rec("PHX")],
        expected_pairs=PAIRS,
    )
    assert not r.ok and r.missing_pairs == (("MIA", "CLI_OBSERVATIONS"),)


def test_reconciliation_unexpected_pair() -> None:
    r = wa.reconcile_collector_run(
        collector_run_id=1,
        environment="production",
        attempts=[rec(s) for s, _ in PAIRS] + [rec("DEN")],
        expected_pairs=PAIRS,
    )
    assert not r.ok and ("DEN", "CLI_OBSERVATIONS") in r.unexpected_pairs


def test_reconciliation_foreign_run_and_environment() -> None:
    bad = rec("SEA", collector_run_id=999)
    r = wa.reconcile_collector_run(
        collector_run_id=1, environment="production", attempts=[bad], expected_pairs=[PAIRS[0]]
    )
    assert any("foreign collector_run_id" in p for p in r.problems)
    bad_env = rec("SEA", environment="demo")
    r2 = wa.reconcile_collector_run(
        collector_run_id=1,
        environment="production",
        attempts=[bad_env],
        expected_pairs=[PAIRS[0]],
    )
    assert any("environment" in p for p in r2.problems)


def test_reconciliation_legacy_invalid_items_mismatch() -> None:
    attempts = [rec(s) for s, _ in PAIRS]
    r = wa.reconcile_collector_run(
        collector_run_id=1,
        environment="production",
        attempts=attempts,
        expected_pairs=PAIRS,
        cycle_stats={"invalid_items": 3, "errors": 0},
    )
    assert any("invalid_items" in p for p in r.problems)


def test_reconciliation_legacy_errors_lower_bound() -> None:
    """`errors` counts stations while attempts count station/product pairs, so
    it is checked as a lower bound, not equality."""
    attempts = [rec(s) for s, _ in PAIRS]
    r = wa.reconcile_collector_run(
        collector_run_id=1,
        environment="production",
        attempts=attempts,
        expected_pairs=PAIRS,
        cycle_stats={"invalid_items": 0, "errors": 2},
    )
    assert any("exceeds attempt-derived failures" in p for p in r.problems)
    assert "errors" in wa.LEGACY_COUNTER_MAPPING


# --- station summary ---------------------------------------------------------


def _summary(station: str, attempts: list[wa.AttemptRecord], **kw: object):
    return wa.summarize_station_window(
        station,
        attempts,
        window_start=T0,
        window_end=T1 + timedelta(days=7),
        ledger_available=True,
        **kw,  # type: ignore[arg-type]
    )


def test_summary_known_zero() -> None:
    s = _summary("SEA", [rec("SEA")])
    assert s.parser_failures is ES.KNOWN_ZERO and s.parser_failure_count == 0


def test_summary_known_nonzero() -> None:
    bad = rec(
        "PHX",
        outcome=O.PARSER_REJECTED,
        parsed_entity_count=0,
        persisted_entity_count=0,
        stage=S.PARSED,
        raw_payload_id=7,
        parser_error_type="X",
    )
    s = _summary("PHX", [bad])
    assert s.parser_failures is ES.KNOWN_NONZERO and s.parser_failure_count == 1


def test_summary_legacy_unknown_never_zero() -> None:
    s = wa.summarize_station_window(
        "SEA",
        [],
        window_start=T0,
        window_end=T1,
        ledger_available=False,
        unattributable_cycle_aggregate=4,
    )
    assert s.parser_failures is ES.LEGACY_UNKNOWN
    assert s.parser_failure_count is None
    assert s.evidence_reason == wa.LEGACY_NO_ATTEMPT_LEDGER
    assert s.unattributable_cycle_aggregate == 4


def test_summary_evidence_missing_when_station_absent() -> None:
    s = _summary("DEN", [rec("SEA")])
    assert s.parser_failures is ES.EVIDENCE_MISSING
    assert s.evidence_reason == wa.LEGACY_UNKNOWN_STATION_SCOPE


def test_summary_incomplete_attempts_is_not_known_zero() -> None:
    s = _summary("SEA", [rec("SEA")], expected_attempts=2)
    assert s.parser_failures is ES.EVIDENCE_MISSING
    assert s.evidence_reason == wa.REASON_INCOMPLETE_ATTEMPTS
    assert s.problems


def test_station_isolation() -> None:
    """A PHX parser rejection must not touch SEA or MIA."""
    phx_bad = rec(
        "PHX",
        outcome=O.PARSER_REJECTED,
        parsed_entity_count=0,
        persisted_entity_count=0,
        stage=S.PARSED,
        raw_payload_id=7,
        parser_error_type="X",
    )
    attempts = [rec("SEA"), phx_bad, rec("MIA")]
    assert _summary("SEA", attempts).parser_failures is ES.KNOWN_ZERO
    assert _summary("PHX", attempts).parser_failures is ES.KNOWN_NONZERO
    assert _summary("MIA", attempts).parser_failures is ES.KNOWN_ZERO


def test_source_outage_is_not_platform_wide() -> None:
    sea_out = rec(
        "SEA",
        outcome=O.SOURCE_UNAVAILABLE,
        source_availability=A.CONFIRMED_UNAVAILABLE,
        parsed_entity_count=0,
        persisted_entity_count=0,
        raw_payload_id=None,
        stage=S.REQUEST_STARTED,
    )
    attempts = [sea_out, rec("PHX"), rec("MIA")]
    assert _summary("SEA", attempts).source_unavailable is ES.KNOWN_NONZERO
    assert _summary("PHX", attempts).source_unavailable is ES.KNOWN_ZERO


def test_product_isolation_and_filter() -> None:
    obs = rec("SEA", P.CLI_OBSERVATIONS)
    fc_bad = rec(
        "SEA",
        P.GRIDPOINT_FORECAST,
        outcome=O.PARSER_REJECTED,
        parsed_entity_count=0,
        persisted_entity_count=0,
        stage=S.PARSED,
        raw_payload_id=7,
        parser_error_type="X",
    )
    both = _summary("SEA", [obs, fc_bad])
    assert {p.product_type for p in both.products} == {"CLI_OBSERVATIONS", "GRIDPOINT_FORECAST"}
    obs_only = _summary("SEA", [obs, fc_bad], product_filter=P.CLI_OBSERVATIONS)
    assert obs_only.parser_failures is ES.KNOWN_ZERO


def test_summary_coverage_and_dates() -> None:
    s = _summary("SEA", [rec("SEA")])
    assert s.raw_payload_coverage == (1, 1)
    assert s.collector_runs_covered == 1
    assert s.affected_local_dates == ("2026-08-11",)


def test_summary_has_no_performance_fields() -> None:
    import json

    blob = json.dumps(_summary("SEA", [rec("SEA")]).to_dict()).lower()
    parts = {p for p in "".join(c if c.isalnum() else " " for c in blob).split()}
    for tok in ("brier", "calibration", "profit", "pnl", "rank", "accuracy", "sharpe", "price"):
        assert not any(p.startswith(tok) for p in parts), tok


# --- timezone semantics ------------------------------------------------------


def test_phx_is_fixed_utc_minus_7() -> None:
    moment = datetime(2026, 8, 12, 4, 0, tzinfo=UTC)
    assert moment.astimezone(ZoneInfo("America/Phoenix")).date() == date(2026, 8, 11)
    assert datetime(2026, 12, 12, 4, 0, tzinfo=UTC).astimezone(
        ZoneInfo("America/Phoenix")
    ).date() == date(2026, 12, 11)


def test_sea_and_mia_dst_differ_from_phx_for_same_instant() -> None:
    moment = datetime(2026, 8, 12, 4, 0, tzinfo=UTC)
    assert moment.astimezone(ZoneInfo("America/New_York")).date() == date(2026, 8, 12)  # EDT
    assert moment.astimezone(ZoneInfo("America/Los_Angeles")).date() == date(2026, 8, 11)


def test_target_date_is_local_not_utc() -> None:
    from kalshi_weather.ingestion.weather_collector import _station_local_date
    from kalshi_weather.weather.stations import STATIONS

    moment = datetime(2026, 8, 12, 4, 0, tzinfo=UTC)
    assert _station_local_date(STATIONS["PHX"], moment) == date(2026, 8, 11)
    assert _station_local_date(STATIONS["MIA"], moment) == date(2026, 8, 12)


# --- disposable-DB schema and repository -------------------------------------


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def test_table_has_required_columns_and_constraints() -> None:
    cols = set(WeatherCollectionAttempt.__table__.columns.keys())
    for required in (
        "attempt_id",
        "collector_run_id",
        "environment",
        "station_code",
        "wfo",
        "product_type",
        "logical_request_key",
        "source_request_id",
        "source_product_id",
        "requested_at",
        "completed_at",
        "observed_at",
        "created_at",
        "target_station_local_date",
        "source_endpoint",
        "http_status",
        "retry_count",
        "stage",
        "outcome",
        "source_availability",
        "parser_name",
        "parser_version",
        "parser_error_type",
        "parser_error_message",
        "raw_payload_id",
        "parsed_entity_count",
        "persisted_entity_count",
        "duplicate_entity_count",
        "persistence_error_type",
        "persistence_error_message",
    ):
        assert required in cols, required
    idx = {i.name for i in WeatherCollectionAttempt.__table__.indexes}
    for name in (
        "ix_weather_attempts_station_time",
        "ix_weather_attempts_run",
        "ix_weather_attempts_product_outcome",
        "ix_weather_attempts_target_date",
        "ix_weather_attempts_env_time",
    ):
        assert name in idx, name
    uq = {
        c.name
        for c in WeatherCollectionAttempt.__table__.constraints
        if c.name == "uq_weather_attempt_logical"
    }
    assert uq, "logical uniqueness constraint missing"
    fk_cols = {fk.parent.name for fk in WeatherCollectionAttempt.__table__.foreign_keys}
    assert {"collector_run_id", "raw_payload_id"} <= fk_cols, fk_cols


@pytest.mark.asyncio
async def test_append_and_read_back(session) -> None:
    result = await append_terminal_attempt(session, rec("SEA"))
    assert result.already_recorded is False
    rows = await list_attempts_for_collector_run(session, 1)
    assert len(rows) == 1 and rows[0].station_code == "SEA"
    assert await find_attempt_by_id(session, rows[0].attempt_id) is not None


@pytest.mark.asyncio
async def test_identical_append_is_idempotent(session) -> None:
    a = rec("SEA")
    await append_terminal_attempt(session, a)
    again = await append_terminal_attempt(session, a)
    assert again.already_recorded is True
    assert len(await list_attempts_for_collector_run(session, 1)) == 1


@pytest.mark.asyncio
async def test_conflicting_same_attempt_id_refused(session) -> None:
    a = rec("SEA")
    await append_terminal_attempt(session, a)
    conflicting = rec(
        "SEA",
        outcome=O.PARSER_REJECTED,
        parsed_entity_count=0,
        persisted_entity_count=0,
        stage=S.PARSED,
        raw_payload_id=7,
        parser_error_type="X",
    )
    with pytest.raises(AttemptConflictError, match="already recorded"):
        await append_terminal_attempt(session, conflicting)
    assert len(await list_attempts_for_collector_run(session, 1)) == 1


@pytest.mark.asyncio
async def test_invalid_attempt_never_persisted(session) -> None:
    with pytest.raises(wa.AttemptIntegrityError):
        await append_terminal_attempt(
            session, rec("SEA", parsed_entity_count=1, persisted_entity_count=9)
        )
    assert await list_attempts_for_collector_run(session, 1) == []


@pytest.mark.asyncio
async def test_station_and_product_window_queries(session) -> None:
    await append_terminal_attempt(session, rec("SEA"))
    await append_terminal_attempt(session, rec("PHX"))
    await append_terminal_attempt(
        session, rec("SEA", P.GRIDPOINT_FORECAST, source_endpoint="nws:gridpoint-forecast")
    )
    sea = await list_attempts_for_station_window(
        session, "SEA", start=T0 - timedelta(hours=1), end=T1 + timedelta(hours=1)
    )
    assert len(sea) == 2
    obs = await list_attempts_for_product_window(
        session, "CLI_OBSERVATIONS", start=T0 - timedelta(hours=1), end=T1 + timedelta(hours=1)
    )
    assert len(obs) == 2
    assert await detect_duplicate_logical_attempts(session, 1) == []


def test_repository_exposes_no_update_or_delete() -> None:
    from kalshi_weather.storage import repositories as r

    names = [n for n in dir(r) if "attempt" in n.lower()]
    for banned in ("update_attempt", "delete_attempt", "upsert_attempt", "overwrite_attempt"):
        assert banned not in names, banned


# --- isolation guards --------------------------------------------------------


def test_no_experiment_paper_or_exchange_imports() -> None:
    for path in (
        Path(wa.__file__),
        Path("src/kalshi_weather/ingestion/weather_collector.py"),
    ):
        for line in path.read_text().splitlines():
            s = line.strip()
            if not (s.startswith("import ") or s.startswith("from ")):
                continue
            for banned in ("experiments", "h0019", "h0020", "paper", "broker", "KalshiClient"):
                assert banned not in s, f"{path.name} imports {banned}: {s}"


_GUARD_MARKER = "# --- isolation " + "guards"


def test_no_production_database_in_tests() -> None:
    """Scans only the region ABOVE the guard header, so the guard's own
    forbidden-token literals do not trip it."""
    region = Path(__file__).read_text().split(_GUARD_MARKER)[0]
    assert len(region) > 5_000, "guard region split failed"
    for banned in ("get_settings", "postgresql", "psycopg", "kalsh-postgres"):
        assert banned not in region, banned
