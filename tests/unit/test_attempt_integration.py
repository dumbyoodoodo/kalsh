"""Integration wiring: reconcile row-loading and main-observatory inclusion.

Synthetic only. The production database is never opened — the loaders are
exercised through their pure inputs, and the wiring itself is asserted from
source so this cannot pass while the CLI still stubs its rows.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from kalshi_weather.ingestion import weather_attempts as wa
from kalshi_weather.observatory import attempts as oa
from kalshi_weather.observatory.severity import Severity

NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
T0 = NOW - timedelta(minutes=30)
STATIONS = frozenset({"SEA", "PHX", "MIA"})
TZ = {"SEA": "America/Los_Angeles", "PHX": "America/Phoenix", "MIA": "America/New_York"}
PAIRS = tuple(
    [(s, "CLI_OBSERVATIONS") for s in sorted(STATIONS)]
    + [(s, "GRIDPOINT_FORECAST") for s in sorted(STATIONS)]
)


def orow(station: str, product: str, **kw: object) -> oa.AttemptRow:
    base: dict[str, object] = {
        "attempt_id": f"{station}-{product}",
        "collector_run_id": 1,
        "environment": "production",
        "station_code": station,
        "product_type": product,
        "logical_request_key": f"1|production|{station}|{product}|2026-08-12",
        "stage": str(wa.AttemptStage.NORMALIZED_PERSISTED),
        "outcome": str(wa.AttemptOutcome.SUCCEEDED_NEW_DATA),
        "source_availability": str(wa.SourceAvailability.AVAILABLE),
        "requested_at": T0,
        "completed_at": T0 + timedelta(seconds=5),
        "target_station_local_date": date(2026, 8, 12),
        "raw_payload_id": 7,
        "parsed_entity_count": 2,
        "persisted_entity_count": 2,
        "duplicate_entity_count": 0,
    }
    base.update(kw)
    return oa.AttemptRow(**base)  # type: ignore[arg-type]


def full_set() -> list[oa.AttemptRow]:
    return [orow(s, p) for s, p in PAIRS]


def run_ctx(**kw: object) -> oa.RunContext:
    base: dict[str, object] = {
        "collector_run_id": 1,
        "environment": "production",
        "started_at": T0,
        "finished_at": T0 + timedelta(seconds=20),
        "expected_pairs": PAIRS,
        "attempt_instrumented": True,
    }
    base.update(kw)
    return oa.RunContext(**base)  # type: ignore[arg-type]


def summarize(rows, runs, **kw):
    defaults = dict(
        now=NOW,
        known_stations=STATIONS,
        station_timezones=TZ,
        schema_deployed=True,
        db_revision="0012",
        expected_revision="0012",
    )
    defaults.update(kw)
    return oa.summarize(rows, runs, **defaults)


# --- reconcile row loading ---------------------------------------------------


def test_cli_reconcile_loads_real_rows_not_a_stub() -> None:
    """Regression guard: the CLI previously passed attempts=[]."""
    source = Path("src/kalshi_weather/cli.py").read_text()
    body = source.split("def weather_attempts_reconcile", 1)[1].split("\ndef ", 1)[0]
    assert "from weather_collection_attempts where collector_run_id=:i" in body
    assert "attempts=attempts" in body
    assert "attempts=[]" not in body, "reconcile must not stub its attempt rows"
    assert "cycle_stats=stats" in body, "legacy counters must be loaded and compared"


def test_cli_reconcile_does_not_duplicate_rules() -> None:
    source = Path("src/kalshi_weather/cli.py").read_text()
    body = source.split("def weather_attempts_reconcile", 1)[1].split("\ndef ", 1)[0]
    assert "wa.reconcile_collector_run(" in body
    for restated in ("missing_pairs =", "def reconcile", "duplicate_keys ="):
        assert restated not in body, "reconciliation rules live in the evidence layer"


def test_cli_reconcile_defers_active_run() -> None:
    source = Path("src/kalshi_weather/cli.py").read_text()
    body = source.split("def weather_attempts_reconcile", 1)[1].split("\ndef ", 1)[0]
    assert "_RECON_DEFERRED" in body
    assert "has not completed" in body


def test_cli_reconcile_refuses_unknown_run_and_reports_not_deployed() -> None:
    source = Path("src/kalshi_weather/cli.py").read_text()
    body = source.split("def weather_attempts_reconcile", 1)[1].split("\ndef ", 1)[0]
    assert "no collector run with id" in body
    assert "_ATTEMPT_NOT_DEPLOYED" in body


def test_cli_reconcile_performs_no_writes() -> None:
    source = Path("src/kalshi_weather/cli.py").read_text()
    body = source.split("def weather_attempts_reconcile", 1)[1].split("\ndef ", 1)[0]
    for banned in ("insert", "update ", "delete", "commit()"):
        assert banned not in body.lower(), banned


def test_exact_run_passes_and_missing_attempt_fails() -> None:
    ok = wa.reconcile_collector_run(
        collector_run_id=1,
        environment="production",
        attempts=[
            wa.AttemptRecord(
                attempt_id=f"{s}-{p}",
                collector_run_id=1,
                environment="production",
                station_code=s,
                product_type=wa.WeatherProductType(p),
                logical_request_key=f"1|production|{s}|{p}|2026-08-12",
                source_endpoint="nws",
                stage=wa.AttemptStage.NORMALIZED_PERSISTED,
                outcome=wa.AttemptOutcome.SUCCEEDED_NEW_DATA,
                source_availability=wa.SourceAvailability.AVAILABLE,
                requested_at=T0,
                completed_at=T0,
                observed_at=T0,
                created_at=T0,
                target_station_local_date=date(2026, 8, 12),
                raw_payload_id=7,
                parsed_entity_count=2,
                persisted_entity_count=2,
            )
            for s, p in PAIRS
        ],
        expected_pairs=[(s, wa.WeatherProductType(p)) for s, p in PAIRS],
    )
    assert ok.ok, ok.problems
    short = wa.reconcile_collector_run(
        collector_run_id=1,
        environment="production",
        attempts=[],
        expected_pairs=[(s, wa.WeatherProductType(p)) for s, p in PAIRS],
    )
    assert not short.ok and short.missing_pairs


# --- main observatory integration --------------------------------------------


def test_attempt_findings_are_wired_into_the_main_report() -> None:
    source = Path("src/kalshi_weather/observatory/report.py").read_text()
    assert "_build_attempt_findings" in source
    assert "from kalshi_weather.observatory import attempts as oa" in source
    # Contributed before the report is returned, not after.
    assert source.index("_build_attempt_findings(session") < source.index(
        "return ObservatoryReport(generated_at"
    )


def test_pre_deployment_absence_contributes_only_info() -> None:
    findings = summarize([], [], schema_deployed=False, db_revision="0011")
    assert findings and all(f.severity is Severity.INFO for f in findings)


def test_completed_exact_run_produces_no_integrity_finding() -> None:
    findings = summarize(full_set(), [run_ctx()])
    assert not [f for f in findings if f.severity is Severity.CRITICAL]


def test_missing_expected_attempt_surfaces() -> None:
    findings = summarize(full_set()[:-1], [run_ctx()])
    assert [f for f in findings if f.check == "missing_expected_attempt"]


def test_provenance_defect_surfaces() -> None:
    rows = full_set()
    rows[0] = orow(
        "SEA",
        "CLI_OBSERVATIONS",
        outcome=str(wa.AttemptOutcome.PARSER_REJECTED),
        raw_payload_id=None,
        parsed_entity_count=0,
        persisted_entity_count=0,
        stage=str(wa.AttemptStage.PARSED),
    )
    findings = summarize(rows, [run_ctx()])
    assert [f for f in findings if f.check == "parser_rejection_missing_raw_payload"]


def test_active_run_not_falsely_flagged_in_main_report() -> None:
    findings = summarize([], [run_ctx(finished_at=None)])
    assert not [f for f in findings if f.check == "missing_expected_attempt"]
    assert [f for f in findings if f.check == "incomplete_terminal_attempt_set"]


def test_counter_mismatch_surfaces() -> None:
    findings = summarize(full_set(), [run_ctx(legacy_invalid_items=2)])
    assert [f for f in findings if f.check == "collector_run_counter_mismatch"]


def test_output_is_deterministic() -> None:
    a = [f.to_dict() for f in summarize(full_set(), [run_ctx()])]
    b = [f.to_dict() for f in summarize(full_set(), [run_ctx()])]
    assert a == b


def test_no_fourth_severity_introduced() -> None:
    assert {s.value for s in Severity} == {"info", "warning", "critical"}


# --- reconcile SQL is checked against the REAL schema -------------------------


def _selected_columns(body: str, table: str) -> set[str]:
    """Column names in the ``select ... from <table>`` inside ``body``.

    The statement is assembled from adjacent string literals, so the literals
    are concatenated before parsing -- exactly as Python sees them.
    """
    import re

    literals = re.findall(r'"([^"]*)"', body)
    sql = "".join(literals)
    # Anchor on the LAST ``select`` before this table's ``from``: the literals
    # of several statements are concatenated, so a plain non-greedy match would
    # start at an earlier statement's ``select``.
    match = re.search(
        r"select\s+((?:(?!select).)*?)\s+from\s+" + re.escape(table),
        sql,
    )
    assert match is not None, f"no select against {table} found in the reconcile body"
    return {c.strip() for c in match.group(1).split(",") if c.strip()}


def test_reconcile_selects_only_columns_that_exist() -> None:
    """Regression guard for the 2026-08-06 deployment defect.

    ``reconcile`` selected ``collector_runs.environment``, which has never
    existed. Every source-level assertion above still passed, because none of
    them compared the SQL against the schema -- so the command was unusable
    against production and nothing caught it until deployment day. This
    compares the selected columns to the mapped models directly.
    """
    from kalshi_weather.storage.models import CollectorRun, WeatherCollectionAttempt

    source = Path("src/kalshi_weather/cli.py").read_text()
    body = source.split("def weather_attempts_reconcile", 1)[1].split("\ndef ", 1)[0]

    for table, model in (
        ("collector_runs", CollectorRun),
        ("weather_collection_attempts", WeatherCollectionAttempt),
    ):
        real = {c.name for c in model.__table__.columns}
        selected = _selected_columns(body, table)
        assert selected, f"expected a non-empty column list for {table}"
        missing = selected - real
        assert not missing, f"{table}: reconcile selects non-existent column(s) {sorted(missing)}"


def test_reconcile_environment_is_not_read_from_collector_runs() -> None:
    """``collector_runs`` stores no environment; the expected value comes from
    the collector's own default instead of a column that cannot be read."""
    source = Path("src/kalshi_weather/cli.py").read_text()
    body = source.split("def weather_attempts_reconcile", 1)[1].split("\ndef ", 1)[0]
    assert "environment" not in _selected_columns(body, "collector_runs")
    assert 'environment = "production"' in body
