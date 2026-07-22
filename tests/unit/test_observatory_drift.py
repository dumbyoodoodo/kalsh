from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.observatory.drift import (
    adapt_cadence_alert,
    adapt_quality_finding,
    find_orphan_stations,
    load_forecast_station_ids,
    load_observation_station_ids,
    orphan_station_finding,
    registry_station_ids,
)
from kalshi_weather.observatory.severity import Severity
from kalshi_weather.ops.forecast_cadence import CadenceAlert
from kalshi_weather.ops.quality import QualityFinding
from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import (
    save_weather_forecast,
    save_weather_observation,
    save_weather_station,
)


def test_find_orphan_stations_no_orphans() -> None:
    assert find_orphan_stations({"NYC", "CHI"}, {"NYC"}) == set()


def test_find_orphan_stations_detects_unregistered_station() -> None:
    assert find_orphan_stations({"NYC", "CHI"}, {"NYC", "ZZZ"}) == {"ZZZ"}


def test_find_orphan_stations_empty_data_is_no_orphans() -> None:
    assert find_orphan_stations({"NYC"}, set()) == set()


@pytest.mark.parametrize(
    ("severity", "expected"),
    [("error", Severity.CRITICAL), ("warning", Severity.WARNING), ("info", Severity.INFO)],
)
def test_adapt_quality_finding_maps_severity(severity: str, expected: Severity) -> None:
    qf = QualityFinding(check="missing_forecasts", severity=severity, count=3, message="m")
    finding = adapt_quality_finding(qf, domain="forecast")
    assert finding.severity == expected
    assert finding.domain == "forecast"
    assert finding.check == "missing_forecasts"
    assert finding.count == 3
    assert finding.message == "m"


def test_adapt_quality_finding_stringifies_samples() -> None:
    qf = QualityFinding(
        check="x", severity="info", count=1, message="m", samples=[date(2026, 1, 1)]
    )
    finding = adapt_quality_finding(qf, domain="platform")
    assert finding.samples == ("2026-01-01",)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("missing_issuance", Severity.WARNING),
        ("duplicate_issuance", Severity.WARNING),
        ("abnormal_cadence", Severity.WARNING),
        ("issuance_gap", Severity.WARNING),
        ("collector_outage", Severity.CRITICAL),
        ("stalled_updates", Severity.CRITICAL),
    ],
)
def test_adapt_cadence_alert_maps_severity(kind: str, expected: Severity) -> None:
    alert = CadenceAlert(
        station_id="NYC",
        kind=kind,
        expected="2 issuances/window",
        observed="0 issuances",
        recommended_action="check logs",
    )
    finding = adapt_cadence_alert(alert)
    assert finding.severity == expected
    assert finding.domain == "forecast"
    assert finding.check == f"cadence_{kind}"
    assert "NYC" in finding.message
    assert finding.samples == ("check logs",)


def test_adapt_cadence_alert_unknown_kind_defaults_to_warning() -> None:
    alert = CadenceAlert(
        station_id="NYC", kind="something_new", expected="e", observed="o", recommended_action="a"
    )
    assert adapt_cadence_alert(alert).severity == Severity.WARNING


def test_orphan_station_finding_no_orphans_is_info() -> None:
    finding = orphan_station_finding(set(), domain="observation", source="observations")
    assert finding.severity == Severity.INFO
    assert finding.count == 0


def test_orphan_station_finding_with_orphans_is_warning_and_sorted() -> None:
    finding = orphan_station_finding({"ZZZ", "AAA"}, domain="observation", source="observations")
    assert finding.severity == Severity.WARNING
    assert finding.count == 2
    assert finding.samples == ("AAA", "ZZZ")


def test_registry_station_ids_includes_known_stations() -> None:
    ids = registry_station_ids()
    assert "NYC" in ids
    assert "CHI" in ids


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


async def _seed_station(session: AsyncSession, station_id: str) -> None:
    await save_weather_station(
        session,
        station_id=station_id,
        provider="nws",
        source_location_code=station_id,
        office="OKX",
        latitude=Decimal("40.7829"),
        longitude=Decimal("-73.9654"),
        name=station_id,
        timezone="America/New_York",
    )


async def test_load_observation_station_ids_distinct(session: AsyncSession) -> None:
    await _seed_station(session, "NYC")
    await save_weather_observation(
        session,
        station_id="NYC",
        provider="nws",
        variable="tmax_f",
        value=Decimal("72.0"),
        unit="F",
        observation_date=date(2026, 7, 21),
        issuance_time=datetime(2026, 7, 22, 6, 0, 0),
        source_product_id="CLINYC",
        raw_payload_id=None,
    )
    ids = await load_observation_station_ids(session)
    assert ids == {"NYC"}


async def test_load_observation_station_ids_empty_store(session: AsyncSession) -> None:
    assert await load_observation_station_ids(session) == set()


async def test_load_forecast_station_ids_distinct(session: AsyncSession) -> None:
    await _seed_station(session, "NYC")
    await save_weather_forecast(
        session,
        station_id="NYC",
        provider="nws",
        variable="tmax_f",
        point_estimate=Decimal("75.0"),
        unit="F",
        issue_time=datetime(2026, 7, 21, 0, 0, 0),
        valid_start=datetime(2026, 7, 21, 12, 0, 0),
        valid_end=datetime(2026, 7, 22, 0, 0, 0),
        raw_payload_id=None,
    )
    ids = await load_forecast_station_ids(session)
    assert ids == {"NYC"}
