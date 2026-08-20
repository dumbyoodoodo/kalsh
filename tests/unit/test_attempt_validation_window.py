"""The attempt-validation query window must be ONE coherent scope.

Reproduces the 2026-08-20 production false positive behaviourally, against a
real database rather than by inspecting source text.

The defect: attempts were selected by a fixed ROW LIMIT while collector runs
were selected by a 24-HOUR window. Under degraded cadence 500 attempt rows
reached back ~40h, so attempts legitimately referencing runs older than 24h
found their run missing from the validation context and were reported as
``foreign_collector_run_lineage`` -- 122 CRITICAL findings against data that was
provably clean (every referenced run existed, was a completed weather run, in
the right environment).

These tests pin the property that makes that impossible: whatever attempts are
selected, the runs they reference are loaded, so a foreign-lineage finding can
only mean the database genuinely lacks the run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.observatory import attempts as oa
from kalshi_weather.observatory.severity import Severity
from kalshi_weather.storage.models import Base, CollectorRun, WeatherCollectionAttempt
from kalshi_weather.storage.repositories import load_attempt_validation_context

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
WINDOW = timedelta(hours=24)
STATIONS = ("SEA", "PHX")
PRODUCTS = ("CLI_OBSERVATIONS", "GRIDPOINT_FORECAST")
PAIRS = tuple((s, p) for p in PRODUCTS for s in STATIONS)


@pytest.fixture
async def session():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def make_cycle(
    session: AsyncSession,
    *,
    started_at: datetime,
    run_id: int,
    instrumented: bool = True,
    finished: bool = True,
    attempts_at: datetime | None = None,
    pairs: tuple[tuple[str, str], ...] = PAIRS,
) -> CollectorRun:
    """One weather run plus its per-station/product attempts."""
    stats = {"attempt_instrumentation_version": 1} if instrumented else {}
    run = CollectorRun(
        id=run_id,
        collector="weather",
        started_at=started_at.replace(tzinfo=None),
        finished_at=(started_at + timedelta(seconds=6)).replace(tzinfo=None) if finished else None,
        duration_seconds=6.0,
        success=True,
        requests_attempted=len(pairs),
        retries=0,
        stats_json=stats,
        schema_version="1",
    )
    session.add(run)
    await session.flush()
    requested = attempts_at or started_at
    for station, product in pairs:
        session.add(
            WeatherCollectionAttempt(
                attempt_id=f"a-{run_id}-{station}-{product}",
                collector_run_id=run_id,
                environment="production",
                station_code=station,
                product_type=product,
                logical_request_key=f"production|{station}|{product}|{requested.date()}",
                source_endpoint="https://api.weather.gov/x",
                stage="NORMALIZED_PERSISTED",
                outcome="SUCCEEDED_NEW_DATA",
                source_availability="AVAILABLE",
                requested_at=requested.replace(tzinfo=None),
                completed_at=(requested + timedelta(seconds=2)).replace(tzinfo=None),
                observed_at=(requested + timedelta(seconds=2)).replace(tzinfo=None),
                created_at=(requested + timedelta(seconds=3)).replace(tzinfo=None),
                target_station_local_date=requested.date(),
                raw_payload_id=None,
                parsed_entity_count=1,
                persisted_entity_count=1,
                duplicate_entity_count=0,
                retry_count=0,
                schema_version="1",
            )
        )
    await session.flush()
    return run


async def old_style_context(session: AsyncSession, *, limit: int = 500, hours: float = 24.0):
    """The PRE-FIX algorithm, reproduced exactly.

    Attempts by row limit with NO time bound; runs by time window only. This is
    what produced 122 CRITICAL findings in production on 2026-08-20.
    """
    from sqlalchemy import select

    from kalshi_weather.storage.repositories import AttemptValidationContext

    attempts = tuple(
        (
            await session.scalars(
                select(WeatherCollectionAttempt)
                .order_by(WeatherCollectionAttempt.requested_at.desc())
                .limit(limit)
            )
        ).all()
    )
    runs = tuple(
        (
            await session.scalars(
                select(CollectorRun)
                .where(
                    CollectorRun.collector == "weather",
                    CollectorRun.started_at >= (NOW - timedelta(hours=hours)).replace(tzinfo=None),
                )
                .order_by(CollectorRun.started_at)
            )
        ).all()
    )
    return AttemptValidationContext(
        window_start=NOW - timedelta(hours=hours),
        window_end=NOW,
        attempts=attempts,
        runs=runs,
        referenced_run_ids=frozenset(int(a.collector_run_id) for a in attempts),
        truncated=len(attempts) == limit,
    )


async def ctx(session: AsyncSession, *, limit: int = 500, hours: float = 24.0):
    return await load_attempt_validation_context(
        session,
        window_start=NOW - timedelta(hours=hours),
        window_end=NOW,
        limit=limit,
    )


def summarize(context, *, now: datetime = NOW) -> list[oa.Finding]:
    """Run the real observatory rules over a loaded context."""
    rows = [
        oa.AttemptRow(
            attempt_id=a.attempt_id,
            collector_run_id=a.collector_run_id,
            environment=a.environment,
            station_code=a.station_code,
            product_type=a.product_type,
            logical_request_key=a.logical_request_key,
            stage=a.stage,
            outcome=a.outcome,
            source_availability=a.source_availability,
            requested_at=a.requested_at.replace(tzinfo=UTC),
            completed_at=a.completed_at.replace(tzinfo=UTC),
            target_station_local_date=a.target_station_local_date,
            raw_payload_id=a.raw_payload_id,
            parsed_entity_count=a.parsed_entity_count,
            persisted_entity_count=a.persisted_entity_count,
            duplicate_entity_count=a.duplicate_entity_count,
        )
        for a in context.attempts
    ]
    runs = [
        oa.RunContext(
            collector_run_id=int(r.id),
            environment="production",
            started_at=r.started_at.replace(tzinfo=UTC),
            finished_at=None if r.finished_at is None else r.finished_at.replace(tzinfo=UTC),
            expected_pairs=PAIRS,
            attempt_instrumented=bool((r.stats_json or {}).get("attempt_instrumentation_version")),
        )
        for r in context.runs
    ]
    return oa.summarize(
        rows,
        runs,
        now=now,
        known_stations=frozenset(STATIONS),
        station_timezones={"SEA": "America/Los_Angeles", "PHX": "America/Phoenix"},
        schema_deployed=True,
        db_revision="0012",
        expected_revision="0012",
    )


def foreign(findings: list[oa.Finding]) -> list[oa.Finding]:
    return [f for f in findings if f.check == "foreign_collector_run_lineage"]


def criticals(findings: list[oa.Finding]) -> list[oa.Finding]:
    return [f for f in findings if f.severity is Severity.CRITICAL]


# --- the exact production defect ----------------------------------------------


async def test_degraded_cadence_false_positive_is_fixed(session: AsyncSession) -> None:
    """THE regression, as it actually happened in production.

    Sparse recent cycles plus valid older ones. The old algorithm's row limit
    reached past its 24h run window, so older-but-valid runs were absent from
    the lineage context and their attempts were flagged CRITICAL. Every
    referenced run exists in the database throughout.
    """
    for i in range(3):
        await make_cycle(session, started_at=NOW - timedelta(hours=2 + i * 3), run_id=900 + i)
    for i in range(6):
        await make_cycle(session, started_at=NOW - timedelta(hours=30 + i * 3), run_id=800 + i)

    stale = await old_style_context(session, limit=500)
    stale_findings = foreign(summarize(stale))
    assert stale_findings, "the old algorithm must reproduce the false positive"
    assert sum(f.count for f in stale_findings) == 6 * len(PAIRS)

    # The runs the old context called "foreign" all EXIST -- that is precisely
    # what made the 122 production findings false rather than real corruption.
    absent_from_stale_context = stale.missing_referenced_run_ids
    assert absent_from_stale_context, "the artifact must be present in the old context"
    from sqlalchemy import select as _select

    present = set(
        (
            await session.scalars(
                _select(CollectorRun.id).where(CollectorRun.id.in_(sorted(absent_from_stale_context)))
            )
        ).all()
    )
    assert present == set(absent_from_stale_context), "every flagged run must exist in the database"

    fixed = await ctx(session, limit=500)
    assert foreign(summarize(fixed)) == []
    assert fixed.missing_referenced_run_ids == frozenset()


async def test_fix_holds_when_attempts_reach_outside_the_window(
    session: AsyncSession,
) -> None:
    """A wider window still loads every referenced run, so widening the window
    (or raising the limit) never reintroduces the finding."""
    for i in range(10):
        await make_cycle(session, started_at=NOW - timedelta(hours=1 + i * 6), run_id=900 + i)
    for hours in (24.0, 48.0, 96.0):
        context = await ctx(session, limit=500, hours=hours)
        assert context.missing_referenced_run_ids == frozenset()
        assert foreign(summarize(context)) == []


# --- genuine lineage defects still detected -----------------------------------


async def test_genuinely_missing_run_is_still_critical(session: AsyncSession) -> None:
    await make_cycle(session, started_at=NOW - timedelta(hours=2), run_id=900)
    # An attempt whose run does not exist anywhere. SQLite does not enforce the
    # FK by default, which is what lets this defect be simulated at all.
    session.add(
        WeatherCollectionAttempt(
            attempt_id="orphan-1",
            collector_run_id=424242,
            environment="production",
            station_code="SEA",
            product_type="CLI_OBSERVATIONS",
            logical_request_key="production|SEA|CLI_OBSERVATIONS|2026-08-20",
            source_endpoint="https://api.weather.gov/x",
            stage="NORMALIZED_PERSISTED",
            outcome="SUCCEEDED_NEW_DATA",
            source_availability="AVAILABLE",
            requested_at=(NOW - timedelta(hours=1)).replace(tzinfo=None),
            completed_at=(NOW - timedelta(hours=1)).replace(tzinfo=None),
            observed_at=(NOW - timedelta(hours=1)).replace(tzinfo=None),
            created_at=(NOW - timedelta(hours=1)).replace(tzinfo=None),
            target_station_local_date=NOW.date(),
            parsed_entity_count=1,
            persisted_entity_count=1,
            duplicate_entity_count=0,
            retry_count=0,
            schema_version="1",
        )
    )
    await session.flush()

    context = await ctx(session)
    assert context.missing_referenced_run_ids == frozenset({424242})
    assert foreign(summarize(context)), "a truly absent run must stay CRITICAL"


async def test_attempt_reassigned_to_a_non_weather_run_is_not_silently_accepted(
    session: AsyncSession,
) -> None:
    """Lineage pointing at a run from another collector must not pass."""
    await make_cycle(session, started_at=NOW - timedelta(hours=2), run_id=900)
    session.add(
        CollectorRun(
            id=700,
            collector="kalshi",
            started_at=(NOW - timedelta(hours=3)).replace(tzinfo=None),
            finished_at=(NOW - timedelta(hours=3)).replace(tzinfo=None),
            duration_seconds=1.0,
            success=True,
            requests_attempted=1,
            retries=0,
            stats_json={},
            schema_version="1",
        )
    )
    await session.flush()
    context = await ctx(session)
    # The kalshi run is not a weather run, so it is not in the window set; it is
    # loaded only if referenced. Nothing references it here.
    assert 700 not in {int(r.id) for r in context.runs}


# --- limit semantics -----------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 4, 8])
async def test_limit_truncates_rows_but_never_lineage(
    session: AsyncSession, limit: int
) -> None:
    for i in range(5):
        await make_cycle(session, started_at=NOW - timedelta(hours=1 + i), run_id=900 + i)
    context = await ctx(session, limit=limit)
    assert len(context.attempts) <= limit
    assert context.missing_referenced_run_ids == frozenset()
    assert foreign(summarize(context)) == []


async def test_exactly_at_the_limit_reports_truncated(session: AsyncSession) -> None:
    await make_cycle(session, started_at=NOW - timedelta(hours=1), run_id=900)
    context = await ctx(session, limit=len(PAIRS))
    assert len(context.attempts) == len(PAIRS)
    assert context.truncated is True


async def test_below_the_limit_is_not_truncated(session: AsyncSession) -> None:
    await make_cycle(session, started_at=NOW - timedelta(hours=1), run_id=900)
    context = await ctx(session, limit=len(PAIRS) + 1)
    assert context.truncated is False


async def test_more_rows_than_limit_keeps_the_newest(session: AsyncSession) -> None:
    await make_cycle(session, started_at=NOW - timedelta(hours=10), run_id=800)
    await make_cycle(session, started_at=NOW - timedelta(hours=1), run_id=900)
    context = await ctx(session, limit=len(PAIRS))
    assert {a.collector_run_id for a in context.attempts} == {900}


# --- window boundaries ---------------------------------------------------------


async def test_run_starting_before_window_but_referenced_is_loaded(
    session: AsyncSession,
) -> None:
    """A cycle straddling the boundary: run starts outside, attempts inside."""
    await make_cycle(
        session,
        started_at=NOW - timedelta(hours=24, minutes=5),
        run_id=800,
        attempts_at=NOW - timedelta(hours=23, minutes=55),
    )
    context = await ctx(session)
    assert 800 in {int(r.id) for r in context.runs}
    assert 800 not in {int(r.id) for r in context.runs_in_window}
    assert foreign(summarize(context)) == []


async def test_attempts_outside_the_window_are_excluded(session: AsyncSession) -> None:
    await make_cycle(session, started_at=NOW - timedelta(hours=40), run_id=800)
    await make_cycle(session, started_at=NOW - timedelta(hours=1), run_id=900)
    context = await ctx(session)
    assert {a.collector_run_id for a in context.attempts} == {900}


async def test_just_finished_run_is_deferred_not_flagged(session: AsyncSession) -> None:
    """``collector_runs.finished_at`` is NOT NULL, so a run is never literally
    open; "active" means inside the completion grace. Such a run must be
    deferred, not reported as a completed run missing its evidence."""
    await make_cycle(
        session, started_at=NOW - timedelta(minutes=2), run_id=901, pairs=()
    )
    context = await ctx(session)
    findings = summarize(context)
    assert foreign(findings) == []
    assert not [
        f for f in criticals(findings) if "missing_all_attempt_evidence" in f.check
    ]


async def test_zero_attempt_run_inside_window_is_still_detected(
    session: AsyncSession,
) -> None:
    """The reason the run set is a UNION, not just referenced runs.

    A completed instrumented run with NO attempts references nothing, so a
    referenced-runs-only context would never load it and the CRITICAL rule would
    be silently disabled.
    """
    await make_cycle(
        session, started_at=NOW - timedelta(hours=2), run_id=900, pairs=()
    )
    context = await ctx(session)
    assert 900 in {int(r.id) for r in context.runs}
    assert [
        f for f in criticals(summarize(context)) if "missing_all_attempt_evidence" in f.check
    ]


async def test_legacy_uninstrumented_run_is_not_a_failure(session: AsyncSession) -> None:
    await make_cycle(
        session, started_at=NOW - timedelta(hours=2), run_id=900, instrumented=False, pairs=()
    )
    context = await ctx(session)
    assert not [
        f for f in criticals(summarize(context)) if "missing_all_attempt_evidence" in f.check
    ]


# --- cadence shapes ------------------------------------------------------------


@pytest.mark.parametrize("spacing_minutes", [30, 120, 480])
async def test_any_cadence_produces_no_foreign_lineage(
    session: AsyncSession, spacing_minutes: int
) -> None:
    step = timedelta(minutes=spacing_minutes)
    for i in range(12):
        await make_cycle(session, started_at=NOW - step * (i + 1), run_id=900 + i)
    context = await ctx(session, limit=500)
    assert context.missing_referenced_run_ids == frozenset()
    assert foreign(summarize(context)) == []


# --- the durable invariant -----------------------------------------------------


async def test_invariant_every_selected_attempt_has_its_run_loaded(
    session: AsyncSession,
) -> None:
    """Every attempt selected for lineage validation has its referenced run in
    the context, unless the database genuinely lacks that run.

    Behavioural, over many shapes -- not a source-text assertion.
    """
    for i in range(4):
        await make_cycle(session, started_at=NOW - timedelta(hours=1 + i * 2), run_id=900 + i)
    for i in range(4):
        await make_cycle(session, started_at=NOW - timedelta(hours=26 + i * 2), run_id=800 + i)

    for limit in (1, 3, 7, 16, 500):
        for hours in (1.0, 6.0, 24.0, 72.0):
            context = await ctx(session, limit=limit, hours=hours)
            loaded = {int(r.id) for r in context.runs}
            for attempt in context.attempts:
                assert attempt.collector_run_id in loaded, (
                    f"limit={limit} hours={hours}: attempt {attempt.attempt_id} "
                    f"references run {attempt.collector_run_id} not loaded"
                )


async def test_window_end_is_honoured_by_both_queries(session: AsyncSession) -> None:
    """A run created after window_end must be invisible to BOTH queries, so the
    two cannot disagree about whether it exists."""
    await make_cycle(session, started_at=NOW - timedelta(hours=1), run_id=900)
    await make_cycle(session, started_at=NOW + timedelta(minutes=5), run_id=999)
    context = await ctx(session)
    assert 999 not in {int(r.id) for r in context.runs}
    assert 999 not in {a.collector_run_id for a in context.attempts}
    assert foreign(summarize(context)) == []


# --- schema/execution regression ------------------------------------------------


async def test_context_loader_executes_against_the_real_schema(
    session: AsyncSession,
) -> None:
    """Executes the loader for real rather than asserting on source text.

    The 2026-08-06 ``collector_runs.environment`` defect passed every
    source-string guard and failed only against a live database; this catches
    that class directly.
    """
    await make_cycle(session, started_at=NOW - timedelta(hours=1), run_id=900)
    context = await ctx(session)
    assert context.attempts and context.runs
    assert context.window_start < context.window_end
    assert isinstance(context.referenced_run_ids, frozenset)
    assert context.missing_referenced_run_ids == frozenset()


# --- the two consumers must not diverge -----------------------------------------


def test_cli_and_observatory_share_the_context_loader() -> None:
    """Both attempt-integrity consumers must load context the same way.

    They previously built independent raw SQL. The CLI's mismatched windows
    produced 122 false CRITICALs; the observatory's copy mis-indexed the run
    columns (environment read from started_at, started_at from finished_at,
    finished_at from stats_json) behind a bare except, so it silently
    contributed no attempt findings at all. A shared loader is what keeps their
    semantics identical, so this pins that they both use it.
    """
    from pathlib import Path

    cli = Path("src/kalshi_weather/cli.py").read_text()
    # Scope to the validate command body: `summary` and `reconcile` legitimately
    # run their own single-station / single-run queries, which cannot suffer a
    # window mismatch because their scope is one explicit key.
    validate_body = cli.split("def weather_attempts_validate", 1)[1].split("\n@", 1)[0]
    assert "load_attempt_validation_context" in cli
    assert "from weather_collection_attempts" not in validate_body, (
        "validate must not select attempts with hand-written SQL"
    )
    assert "interval '24 hours'" not in validate_body, (
        "validate must not carry an independent time window"
    )

    report = Path("src/kalshi_weather/observatory/report.py").read_text()
    assert "load_attempt_validation_context" in report
    assert "from weather_collection_attempts" not in report, (
        "observatory report still selects attempts with hand-written SQL"
    )


def test_observatory_report_has_no_bare_except_around_attempt_loading() -> None:
    """The mis-indexed run columns were invisible because a bare except
    discarded the failure and returned empty evidence."""
    from pathlib import Path

    source = Path("src/kalshi_weather/observatory/report.py").read_text()
    marker = "load_attempt_validation_context("
    idx = source.index(marker)
    region = source[idx : idx + 2500]
    assert "except Exception:" not in region, (
        "attempt-context loading must not swallow failures into empty evidence"
    )


# --- truncation must not mirror the bug -----------------------------------------


async def test_row_limit_does_not_make_full_runs_look_empty(
    session: AsyncSession,
) -> None:
    """The mirror-image false positive, caught during the live check.

    With a small ``--limit`` the retained rows cover only the newest cycles.
    Older runs in the window still load (correctly), but their attempts were
    truncated away -- judging their completeness reported "0 of N attempts" for
    runs that had recorded every one. Completeness is therefore evaluated only
    where the attempt set is fully loaded.
    """
    for i in range(8):
        await make_cycle(session, started_at=NOW - timedelta(hours=1 + i * 2), run_id=900 + i)

    context = await ctx(session, limit=len(PAIRS))  # exactly one cycle retained
    assert context.truncated is True
    eligible = context.completeness_eligible_run_ids
    assert eligible, "the newest fully-covered run must remain judgeable"
    assert len(eligible) < len({int(r.id) for r in context.runs})

    findings = oa.summarize(
        [
            oa.AttemptRow(
                attempt_id=a.attempt_id,
                collector_run_id=a.collector_run_id,
                environment=a.environment,
                station_code=a.station_code,
                product_type=a.product_type,
                logical_request_key=a.logical_request_key,
                stage=a.stage,
                outcome=a.outcome,
                source_availability=a.source_availability,
                requested_at=a.requested_at.replace(tzinfo=UTC),
                completed_at=a.completed_at.replace(tzinfo=UTC),
                target_station_local_date=a.target_station_local_date,
                raw_payload_id=a.raw_payload_id,
                parsed_entity_count=a.parsed_entity_count,
                persisted_entity_count=a.persisted_entity_count,
                duplicate_entity_count=a.duplicate_entity_count,
            )
            for a in context.attempts
        ],
        [
            oa.RunContext(
                collector_run_id=int(r.id),
                environment="production",
                started_at=r.started_at.replace(tzinfo=UTC),
                finished_at=None if r.finished_at is None else r.finished_at.replace(tzinfo=UTC),
                expected_pairs=PAIRS,
                attempt_instrumented=True,
            )
            for r in context.runs
        ],
        now=NOW,
        known_stations=frozenset(STATIONS),
        station_timezones={"SEA": "America/Los_Angeles", "PHX": "America/Phoenix"},
        schema_deployed=True,
        db_revision="0012",
        expected_revision="0012",
        completeness_eligible_run_ids=eligible,
    )
    assert criticals(findings) == [], [f.check for f in criticals(findings)]


async def test_untruncated_context_judges_every_loaded_run(
    session: AsyncSession,
) -> None:
    """Without truncation nothing is excluded, so a genuinely empty run is still
    caught."""
    await make_cycle(session, started_at=NOW - timedelta(hours=2), run_id=900, pairs=())
    context = await ctx(session, limit=500)
    assert context.truncated is False
    assert context.coverage_start == context.window_start
    assert 900 in context.completeness_eligible_run_ids
    assert [
        f for f in criticals(summarize(context)) if "missing_all_attempt_evidence" in f.check
    ]
