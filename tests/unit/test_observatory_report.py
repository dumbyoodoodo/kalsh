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
