from datetime import date, datetime

import polars as pl
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.observatory.pit_consistency import (
    adapt_completeness,
    load_pit_frames,
    summarize_forecast_eligibility,
    summarize_point_in_time_integrity,
)
from kalshi_weather.observatory.severity import Severity

OBS_SCHEMA = {
    "station_id": pl.Utf8,
    "variable": pl.Utf8,
    "observation_date": pl.Date,
    "issuance_time": pl.Datetime,
    "value": pl.Float64,
}
FC_SCHEMA = {
    "station_id": pl.Utf8,
    "variable": pl.Utf8,
    "target_date": pl.Date,
    "issue_time": pl.Datetime,
    "valid_start": pl.Datetime,
    "forecast_value": pl.Float64,
}


def _obs_frame(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=OBS_SCHEMA, orient="row")


def _fc_frame(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=FC_SCHEMA, orient="row")


def test_summarize_forecast_eligibility_empty_frame_is_info() -> None:
    finding = summarize_forecast_eligibility(_obs_frame([]))
    assert finding.severity == Severity.INFO
    assert finding.count == 0


def test_summarize_forecast_eligibility_all_eligible() -> None:
    # Final issuance well after local midnight of the following day -- eligible.
    obs = _obs_frame(
        [
            ("NYC", "tmax_f", date(2026, 7, 19), datetime(2026, 7, 20, 10, 0, 0), 75.0),
            ("NYC", "tmin_f", date(2026, 7, 19), datetime(2026, 7, 20, 10, 0, 0), 60.0),
        ]
    )
    finding = summarize_forecast_eligibility(obs)
    assert finding.severity == Severity.INFO
    assert finding.count == 2
    assert "2/2" in finding.message


def test_summarize_forecast_eligibility_not_yet_final() -> None:
    # Same-day issuance -- not yet past the local-midnight boundary.
    obs = _obs_frame([("NYC", "tmax_f", date(2026, 7, 22), datetime(2026, 7, 22, 12, 0, 0), 75.0)])
    finding = summarize_forecast_eligibility(obs)
    assert finding.severity == Severity.INFO  # not_yet_final alone isn't a data-quality warning
    assert "1 not yet final" in finding.message


def test_summarize_forecast_eligibility_unknown_station_is_warning() -> None:
    obs = _obs_frame([("ZZZ", "tmax_f", date(2026, 7, 19), datetime(2026, 7, 20, 10, 0, 0), 75.0)])
    finding = summarize_forecast_eligibility(obs)
    assert finding.severity == Severity.WARNING
    assert "1 unknown-station" in finding.message


def test_summarize_point_in_time_integrity_clean_join() -> None:
    obs = _obs_frame([("NYC", "tmax_f", date(2026, 7, 19), datetime(2026, 7, 20, 10, 0, 0), 75.0)])
    fc = _fc_frame(
        [
            (
                "NYC",
                "tmax_f",
                date(2026, 7, 19),
                datetime(2026, 7, 19, 0, 0, 0),
                datetime(2026, 7, 19, 12, 0, 0),
                74.0,
            )
        ]
    )
    finding = summarize_point_in_time_integrity(fc, obs)
    assert finding.severity == Severity.INFO
    assert finding.count == 0


def test_summarize_point_in_time_integrity_detects_violation() -> None:
    # Observation finalizes at 2026-07-21T10:00; forecast issued afterwards
    # for the same target date -- a leakage risk masquerading as a join.
    obs = _obs_frame([("NYC", "tmax_f", date(2026, 7, 20), datetime(2026, 7, 21, 10, 0, 0), 75.0)])
    fc = _fc_frame(
        [
            (
                "NYC",
                "tmax_f",
                date(2026, 7, 20),
                datetime(2026, 7, 21, 11, 0, 0),
                datetime(2026, 7, 21, 17, 0, 0),
                74.0,
            )
        ]
    )
    finding = summarize_point_in_time_integrity(fc, obs)
    assert finding.severity == Severity.CRITICAL
    assert finding.count == 1
    assert finding.samples == ("NYC/tmax_f/2026-07-20",)


def test_adapt_completeness_high_coverage_is_info() -> None:
    findings = adapt_completeness(
        {"NYC": {"coverage": 0.99, "observation_days": 100, "range": "x"}}
    )
    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO
    assert findings[0].check == "observation_completeness_NYC"


def test_adapt_completeness_moderate_coverage_is_warning() -> None:
    findings = adapt_completeness(
        {"NYC": {"coverage": 0.92, "observation_days": 100, "range": "x"}}
    )
    assert findings[0].severity == Severity.WARNING


def test_adapt_completeness_low_coverage_is_critical() -> None:
    findings = adapt_completeness({"NYC": {"coverage": 0.5, "observation_days": 100, "range": "x"}})
    assert findings[0].severity == Severity.CRITICAL


def test_adapt_completeness_none_coverage_is_info() -> None:
    findings = adapt_completeness({"NYC": {"coverage": None, "observation_days": 0, "range": None}})
    assert findings[0].severity == Severity.INFO


def test_adapt_completeness_sorted_by_station() -> None:
    findings = adapt_completeness(
        {
            "NYC": {"coverage": 0.99, "observation_days": 1, "range": "x"},
            "CHI": {"coverage": 0.99, "observation_days": 1, "range": "x"},
        }
    )
    assert [f.check for f in findings] == [
        "observation_completeness_CHI",
        "observation_completeness_NYC",
    ]


@pytest.fixture
async def session():  # type: ignore[no-untyped-def]
    from kalshi_weather.storage.models import Base

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


async def test_load_pit_frames_empty_store_returns_empty_frames(session: AsyncSession) -> None:
    forecasts, observations = await load_pit_frames(session, window_days=10)
    assert forecasts.is_empty()
    assert observations.is_empty()
    assert set(forecasts.columns) >= {
        "station_id",
        "target_date",
        "issue_time",
        "valid_start",
        "variable",
        "forecast_value",
    }
