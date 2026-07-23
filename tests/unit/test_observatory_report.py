import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.observatory.report import ObservatoryConfig, build_observatory_report
from kalshi_weather.observatory.severity import Severity
from kalshi_weather.ops.quality import EXPECTED_DB_REVISION
from kalshi_weather.storage.models import Base

CONFIG = ObservatoryConfig(
    kalshi_interval_seconds=1800,
    weather_interval_seconds=1800,
    price_sync_interval_seconds=1800,
    stale_after_intervals=3,
)


@pytest.fixture
async def session():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        await conn.execute(text(f"INSERT INTO alembic_version VALUES ('{EXPECTED_DB_REVISION}')"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_empty_store_report_is_json_serializable_and_has_findings(
    session: AsyncSession,
) -> None:
    report = await build_observatory_report(session, CONFIG)
    assert report.findings
    assert json.dumps(report.to_dict())


async def test_empty_store_report_status_matches_worst_finding(session: AsyncSession) -> None:
    report = await build_observatory_report(session, CONFIG)
    assert report.status == max(
        (f.severity for f in report.findings), key=lambda s: list(Severity).index(s)
    )


async def test_by_severity_and_by_domain_are_consistent_with_findings(
    session: AsyncSession,
) -> None:
    report = await build_observatory_report(session, CONFIG)
    all_by_severity = [f for sev in Severity for f in report.by_severity(sev)]
    assert sorted(id(f) for f in all_by_severity) == sorted(id(f) for f in report.findings)
    for domain in {f.domain for f in report.findings}:
        assert all(f.domain == domain for f in report.by_domain(domain))


async def test_report_reruns_deterministically_excluding_timestamp(
    session: AsyncSession,
) -> None:
    report1 = await build_observatory_report(session, CONFIG)
    report2 = await build_observatory_report(session, CONFIG)
    d1, d2 = report1.to_dict(), report2.to_dict()
    d1.pop("generated_at")
    d2.pop("generated_at")
    assert d1 == d2


async def test_to_dict_counts_by_severity_matches_findings(session: AsyncSession) -> None:
    report = await build_observatory_report(session, CONFIG)
    counts = report.to_dict()["counts_by_severity"]
    for sev in Severity:
        assert counts[sev.value] == len(report.by_severity(sev))


# --- Active vs historical severity (2026-07-23 production-gap fix) -----------


def _mk_run(collector: str, started_at, success: bool = True):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from kalshi_weather.storage.models import CollectorRun

    return CollectorRun(
        collector=collector,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=5),
        duration_seconds=5.0,
        success=success,
        requests_attempted=1,
        retries=0,
        stats_json={},
    )


def _finding(report, check: str):  # type: ignore[no-untyped-def]
    matches = [f for f in report.findings if f.check == check]
    assert len(matches) == 1, f"expected exactly one {check} finding"
    return matches[0]


async def test_missed_cycles_healed_gap_is_warning_not_critical(
    session: AsyncSession,
) -> None:
    """A gap that healed (runs resumed and are currently on schedule) is a
    recovered incident: WARNING, never CRITICAL."""
    from datetime import timedelta

    from kalshi_weather.domain.time import utc_now

    now = utc_now().replace(tzinfo=None)
    # a 4h hole ending 20h ago; healthy 30-min cadence since
    session.add(_mk_run("kalshi", now - timedelta(hours=25)))
    session.add(_mk_run("kalshi", now - timedelta(hours=21)))
    for m in range(0, 21 * 60, 30):
        session.add(_mk_run("kalshi", now - timedelta(minutes=m)))
    await session.commit()

    report = await build_observatory_report(session, CONFIG)
    finding = _finding(report, "missed_collection_cycles_kalshi")
    assert finding.severity == Severity.WARNING
    assert "healed" in finding.message
    assert "overdue" not in finding.message


async def test_missed_cycles_overdue_now_is_critical(session: AsyncSession) -> None:
    """The collector being overdue right now is an ACTIVE failure."""
    from datetime import timedelta

    from kalshi_weather.domain.time import utc_now

    now = utc_now().replace(tzinfo=None)
    session.add(_mk_run("kalshi", now - timedelta(hours=6)))
    session.add(_mk_run("kalshi", now - timedelta(hours=5)))  # newest run: 5h ago
    await session.commit()

    report = await build_observatory_report(session, CONFIG)
    finding = _finding(report, "missed_collection_cycles_kalshi")
    assert finding.severity == Severity.CRITICAL
    assert "overdue NOW" in finding.message


async def test_missed_cycles_gap_ages_out_of_lookback(session: AsyncSession) -> None:
    """A healed gap older than the continuity lookback disappears entirely
    (INFO) -- historical incidents age out on their own; no state is ever
    manually cleared."""
    from datetime import timedelta

    from kalshi_weather.domain.time import utc_now

    now = utc_now().replace(tzinfo=None)
    # the hole sits 20 days back -- outside the 14-day window
    session.add(_mk_run("kalshi", now - timedelta(days=21)))
    session.add(_mk_run("kalshi", now - timedelta(days=20)))
    for m in range(0, 12 * 60, 30):  # healthy recent cadence
        session.add(_mk_run("kalshi", now - timedelta(minutes=m)))
    await session.commit()

    report = await build_observatory_report(session, CONFIG)
    finding = _finding(report, "missed_collection_cycles_kalshi")
    assert finding.severity == Severity.INFO
    assert finding.count == 0


async def test_cadence_outage_healed_gap_is_warning_and_ages_out(
    session: AsyncSession,
) -> None:
    """The cadence collector-outage check: a healed in-window gap surfaces
    as the WARNING `collector_outage_recovered` kind; once the gap slides
    outside `cadence_run_window_hours` it vanishes entirely. This is the
    exact defect that latched the Jul 21 8.6h gap CRITICAL forever."""
    from datetime import timedelta
    from decimal import Decimal

    from kalshi_weather.domain.time import utc_now
    from kalshi_weather.storage.models import WeatherForecast, WeatherStation

    now = utc_now().replace(tzinfo=None)
    # cadence station reports only exist for stations with captures --
    # seed one NYC forecast so the outage analysis has a station to report.
    session.add(
        WeatherStation(
            station_id="NYC",
            provider="nws",
            source_location_code="KNYC",
            latitude=Decimal("40.78"),
            longitude=Decimal("-73.97"),
            name="Central Park",
            timezone="America/New_York",
            observed_at=now,
        )
    )
    session.add(
        WeatherForecast(
            station_id="NYC",
            provider="nws",
            variable="tmax",
            point_estimate=Decimal("85.00"),
            unit="F",
            issue_time=now - timedelta(hours=2),
            valid_start=now,
            valid_end=now + timedelta(hours=12),
            observed_at=now - timedelta(hours=2),
        )
    )
    # an 8h hole ending 30h ago, healthy 30-min cadence since -> inside the
    # default 72h window: recovered WARNING, and no CRITICAL outage.
    session.add(_mk_run("weather", now - timedelta(hours=38)))
    session.add(_mk_run("weather", now - timedelta(hours=30)))
    for m in range(0, 30 * 60, 30):
        session.add(_mk_run("weather", now - timedelta(minutes=m)))
    await session.commit()

    report = await build_observatory_report(session, CONFIG)
    outage = [f for f in report.findings if f.check == "cadence_collector_outage"]
    recovered = [f for f in report.findings if f.check == "cadence_collector_outage_recovered"]
    assert outage == []  # no station reports an ACTIVE outage
    assert recovered  # the healed gap is visible as a recovered incident
    assert all(f.severity == Severity.WARNING for f in recovered)

    # narrow the window below the gap's age: the recovered finding ages out
    aged_config = ObservatoryConfig(
        kalshi_interval_seconds=1800,
        weather_interval_seconds=1800,
        price_sync_interval_seconds=1800,
        stale_after_intervals=3,
        cadence_run_window_hours=24.0,
    )
    aged = await build_observatory_report(session, aged_config)
    assert [f for f in aged.findings if f.check == "cadence_collector_outage_recovered"] == []
    assert [f for f in aged.findings if f.check == "cadence_collector_outage"] == []
