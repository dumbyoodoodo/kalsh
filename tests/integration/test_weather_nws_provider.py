"""Opt-in integration test against the real NWS/IEM APIs.

Read-only; no credentials are required (NWS asks only for a descriptive
User-Agent). Skipped by default so normal test runs never depend on network
access -- set RUN_LIVE_WEATHER_TESTS=1 to enable.
"""

import os
from datetime import timedelta

import pytest

from kalshi_weather.domain.time import utc_now
from kalshi_weather.weather.provider import NwsProvider
from kalshi_weather.weather.stations import get_station

pytestmark = pytest.mark.integration

_RUN_LIVE = os.environ.get("RUN_LIVE_WEATHER_TESTS") == "1"


@pytest.mark.skipif(not _RUN_LIVE, reason="RUN_LIVE_WEATHER_TESTS=1 not set")
async def test_get_station_metadata_against_live_nws() -> None:
    station = get_station("NYC")
    async with NwsProvider(user_agent="kalshi-weather-research (integration-test)") as provider:
        metadata = await provider.get_station_metadata(station)

    assert metadata.office
    assert metadata.timezone == "America/New_York"


@pytest.mark.skipif(not _RUN_LIVE, reason="RUN_LIVE_WEATHER_TESTS=1 not set")
async def test_get_recent_observations_against_live_nws() -> None:
    station = get_station("NYC")
    async with NwsProvider(user_agent="kalshi-weather-research (integration-test)") as provider:
        today = utc_now().date()
        observations = await provider.get_observations(
            station, start=today - timedelta(days=2), end=today
        )

    assert isinstance(observations, list)
    for obs in observations:
        assert obs.variable in {"tmax_f", "tmin_f"}
        assert obs.raw_payload_id is None  # no sink configured in this test


@pytest.mark.skipif(not _RUN_LIVE, reason="RUN_LIVE_WEATHER_TESTS=1 not set")
async def test_get_forecast_against_live_nws() -> None:
    station = get_station("NYC")
    async with NwsProvider(user_agent="kalshi-weather-research (integration-test)") as provider:
        forecasts = await provider.get_forecast(station)

    assert len(forecasts) > 0
    assert all(f.variable == "temperature" for f in forecasts)
