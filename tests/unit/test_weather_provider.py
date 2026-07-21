from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from kalshi_weather.domain.time import utc_now
from kalshi_weather.weather.provider import LIVE_RETENTION_DAYS, NwsProvider
from kalshi_weather.weather.stations import get_station

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "weather"
STATION = get_station("NYC")

POINTS_PAYLOAD = {
    "properties": {
        "cwa": "OKX",
        "gridId": "OKX",
        "gridX": 34,
        "gridY": 45,
        "forecast": "https://api.weather.gov/gridpoints/OKX/34,45/forecast",
        "timeZone": "America/New_York",
    }
}

FORECAST_PAYLOAD = {
    "properties": {
        "updateTime": "2026-07-20T18:49:29+00:00",
        "periods": [
            {
                "number": 1,
                "name": "Tonight",
                "startTime": "2026-07-20T18:00:00-04:00",
                "endTime": "2026-07-21T06:00:00-04:00",
                "isDaytime": False,
                "temperature": 70,
                "temperatureUnit": "F",
            },
            {
                "number": 2,
                "name": "Tuesday",
                "startTime": "2026-07-21T06:00:00-04:00",
                "endTime": "2026-07-21T18:00:00-04:00",
                "isDaytime": True,
                "temperature": 80,
                "temperatureUnit": "F",
            },
        ],
    }
}


def _cli_text() -> str:
    return (FIXTURES / "cli_nyc_today.txt").read_text()


def _make_sink() -> tuple[list[tuple[str, str, str, int, Any]], Any]:
    calls: list[tuple[str, str, str, int, Any]] = []
    next_id = iter(range(1, 10_000))

    async def sink(
        source: str, endpoint: str, request_key: str, status: int, payload: Any
    ) -> int:
        calls.append((source, endpoint, request_key, status, payload))
        return next(next_id)

    return calls, sink


async def test_get_station_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/points/" in str(request.url)
        return httpx.Response(200, json=POINTS_PAYLOAD)

    async with NwsProvider(
        user_agent="test-agent", transport=httpx.MockTransport(handler)
    ) as provider:
        meta = await provider.get_station_metadata(STATION)

    assert meta.station_id == "NYC"
    assert meta.office == "OKX"
    assert meta.timezone == "America/New_York"


async def test_get_forecast_shares_one_raw_payload_id_across_periods() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/points/" in url:
            return httpx.Response(200, json=POINTS_PAYLOAD)
        if "/gridpoints/" in url:
            return httpx.Response(200, json=FORECAST_PAYLOAD)
        raise AssertionError(f"unexpected url {url}")

    calls, sink = _make_sink()
    async with NwsProvider(
        user_agent="test-agent", raw_payload_sink=sink, transport=httpx.MockTransport(handler)
    ) as provider:
        forecasts = await provider.get_forecast(STATION)

    assert len(forecasts) == 2
    assert forecasts[0].point_estimate == 70
    assert forecasts[1].point_estimate == 80
    # both periods came from the single forecast request -> same raw_payload_id
    assert forecasts[0].raw_payload_id == forecasts[1].raw_payload_id
    assert forecasts[0].raw_payload_id is not None
    # two requests happened: /points then /gridpoints/.../forecast
    assert len(calls) == 2


async def test_get_observations_live_path_assigns_distinct_raw_payload_ids() -> None:
    """Two products fetched in one call -> each observation pair must carry
    the raw_payload_id of *its own* detail request, not the last one made."""
    product_ids = ["prod-1", "prod-2"]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/products") or "/products?" in url:
            return httpx.Response(
                200,
                json={
                    "@graph": [
                        {
                            "id": pid,
                            "issuingOffice": "KOKX",
                            "issuanceTime": "2026-07-20T20:37:00+00:00",
                            "productCode": "CLI",
                        }
                        for pid in product_ids
                    ]
                },
            )
        for pid in product_ids:
            if url.endswith(f"/products/{pid}"):
                return httpx.Response(
                    200,
                    json={
                        "id": pid,
                        "issuanceTime": "2026-07-20T20:37:00+00:00",
                        "productCode": "CLI",
                        "productText": _cli_text(),
                    },
                )
        raise AssertionError(f"unexpected url {url}")

    _calls, sink = _make_sink()
    async with NwsProvider(
        user_agent="test-agent", raw_payload_sink=sink, transport=httpx.MockTransport(handler)
    ) as provider:
        today = utc_now().date()
        observations = await provider.get_observations(
            STATION, start=today - timedelta(days=1), end=today
        )

    # 2 products * 2 variables (tmax_f, tmin_f) = 4 records
    assert len(observations) == 4
    ids_by_product = {
        obs.source_product_id: obs.raw_payload_id for obs in observations
    }
    assert ids_by_product["prod-1"] != ids_by_product["prod-2"]
    assert all(v is not None for v in ids_by_product.values())


async def test_get_observations_historical_path_parses_iem_text_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/nws/afos/list.json" in url:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "entered": "2026-06-15T06:20:00Z",
                            "pil": "CLINYC",
                            "product_id": "202606150620-KOKX-CDUS41-CLINYC",
                            "cccc": "KOKX",
                            "text_link": (
                                "https://mesonet.agron.iastate.edu/api/1/nwstext/"
                                "202606150620-KOKX-CDUS41-CLINYC"
                            ),
                        }
                    ]
                },
            )
        if "/nwstext/" in url:
            return httpx.Response(200, text=(FIXTURES / "cli_nyc_yesterday.txt").read_text())
        raise AssertionError(f"unexpected url {url}")

    calls, sink = _make_sink()
    async with NwsProvider(
        user_agent="test-agent", raw_payload_sink=sink, transport=httpx.MockTransport(handler)
    ) as provider:
        old_day = utc_now().date() - timedelta(days=LIVE_RETENTION_DAYS + 10)
        observations = await provider.get_observations(STATION, start=old_day, end=old_day)

    assert len(observations) == 2
    variables = {obs.variable: obs.value for obs in observations}
    assert variables["tmax_f"] == 87
    assert variables["tmin_f"] == 70
    # the raw text payload is wrapped as {"text": ...} for JSON storage
    text_calls = [c for c in calls if "nwstext" in c[1]]
    assert len(text_calls) == 1
    assert "text" in text_calls[0][4]


async def test_get_observations_straddling_range_queries_both_backends() -> None:
    live_calls: list[str] = []
    historical_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.weather.gov" in url:
            live_calls.append(url)
            return httpx.Response(200, json={"@graph": []})
        if "mesonet.agron.iastate.edu" in url:
            historical_calls.append(url)
            return httpx.Response(200, json={"data": []})
        raise AssertionError(f"unexpected url {url}")

    async with NwsProvider(
        user_agent="test-agent", transport=httpx.MockTransport(handler)
    ) as provider:
        today = utc_now().date()
        await provider.get_observations(
            STATION, start=today - timedelta(days=LIVE_RETENTION_DAYS + 3), end=today
        )

    assert live_calls, "expected at least one live api.weather.gov call"
    assert historical_calls, "expected at least one historical IEM call"


async def test_malformed_product_text_is_skipped_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/products") or "/products?" in url:
            return httpx.Response(
                200,
                json={
                    "@graph": [
                        {
                            "id": "bad-product",
                            "issuingOffice": "KOKX",
                            "issuanceTime": "2026-07-20T20:37:00+00:00",
                            "productCode": "CLI",
                        }
                    ]
                },
            )
        if url.endswith("/products/bad-product"):
            return httpx.Response(
                200,
                json={
                    "id": "bad-product",
                    "issuanceTime": "2026-07-20T20:37:00+00:00",
                    "productCode": "CLI",
                    "productText": "not a real climate report",
                },
            )
        raise AssertionError(f"unexpected url {url}")

    async with NwsProvider(
        user_agent="test-agent", transport=httpx.MockTransport(handler)
    ) as provider:
        today = utc_now().date()
        observations = await provider.get_observations(
            STATION, start=today - timedelta(days=1), end=today
        )

    assert observations == []
