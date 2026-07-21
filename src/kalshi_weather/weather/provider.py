"""NWS (National Weather Service) weather data provider.

# CONFIRMED (docs/adr/0003-weather-data-source.md): hosts, endpoints, and
# query parameters below were verified against live api.weather.gov and
# mesonet.agron.iastate.edu (IEM) responses during Phase 3 research, not
# guessed.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

import httpx

from kalshi_weather.domain.time import parse_iso8601_utc, utc_now
from kalshi_weather.logging import get_logger
from kalshi_weather.weather.cli_parser import CliParseError, parse_cli_temperatures
from kalshi_weather.weather.models import (
    ForecastRecord,
    ForecastResponse,
    ObservationRecord,
    PointsResponse,
    ProductDetail,
    ProductListResponse,
    StationMetadata,
)
from kalshi_weather.weather.stations import Station

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
#: Empirically confirmed (docs/adr/0003-weather-data-source.md): live
#: api.weather.gov /products queries reliably return data at 3 days old and
#: nothing at 7+ days old. Kept conservative rather than exact.
LIVE_RETENTION_DAYS = 5

NWS_API_BASE = "https://api.weather.gov"
IEM_API_BASE = "https://mesonet.agron.iastate.edu/api/1"

#: Called with (source, endpoint, request_key, http_status, payload_json) for
#: every response received, mirroring kalshi.client.RawPayloadSink. Returns
#: the persisted raw_api_payloads row id. Text responses are wrapped as
#: {"text": "..."} so the raw_api_payloads.payload_json column (JSON-typed)
#: can still hold them.
RawPayloadSink = Callable[[str, str, str, int, Any], Awaitable[int]]


class WeatherProviderError(RuntimeError):
    """Raised for non-retryable provider errors."""


class WeatherProvider(Protocol):
    """Interface a weather data source must implement. NwsProvider is the
    only implementation for now; a future provider (e.g. a different
    official source for a different contract type) implements the same
    Protocol without changing any downstream ingestion code."""

    last_raw_payload_id: int | None

    async def __aenter__(self) -> "WeatherProvider": ...

    async def __aexit__(self, *exc_info: object) -> None: ...

    async def get_station_metadata(self, station: Station) -> StationMetadata: ...

    async def get_observations(
        self, station: Station, *, start: date, end: date
    ) -> list[ObservationRecord]: ...

    async def get_forecast(self, station: Station) -> list[ForecastRecord]: ...


class NwsProvider:
    """WeatherProvider backed by NWS's live API (api.weather.gov) for recent
    data, falling back to IEM's text-product archive
    (mesonet.agron.iastate.edu) for historical observations beyond the live
    API's retention window. Forecasts are live-only this phase -- historical
    forecast backfill (NCEI's NDFD archive, GRIB2/NetCDF) is a documented,
    deferred limitation; see docs/adr/0003-weather-data-source.md.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval_seconds: float = 0.1,
        raw_payload_sink: RawPayloadSink | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._max_retries = max_retries
        # Proactive courtesy spacing for NWS/IEM (free public APIs with no
        # published hard limit) -- same pattern as kalshi/client.py.
        self._min_request_interval_seconds = min_request_interval_seconds
        self._last_request_at: float | None = None
        self._raw_payload_sink = raw_payload_sink
        #: Operational counters for this provider instance's lifetime.
        self.requests_attempted: int = 0
        self.retries: int = 0
        #: id of the raw_api_payloads row for the most recently completed
        #: request -- same pattern as kalshi.client.KalshiClient, read
        #: immediately after a call, not safe across concurrent calls.
        self.last_raw_payload_id: int | None = None
        # NWS explicitly asks API consumers to identify themselves; no
        # authentication is required for either host.
        self._http = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> "NwsProvider":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _throttle(self) -> None:
        """Proactively space out requests by at least min_request_interval_seconds."""
        if self._min_request_interval_seconds <= 0:
            return
        if self._last_request_at is not None:
            remaining = self._min_request_interval_seconds - (
                time.monotonic() - self._last_request_at
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_request_at = time.monotonic()

    async def _request(
        self,
        source: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        as_text: bool = False,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            await self._throttle()
            self.requests_attempted += 1
            if attempt > 0:
                self.retries += 1
            try:
                response = await self._http.get(url, params=params)
            except httpx.TransportError as exc:
                last_error = exc
                await asyncio.sleep(DEFAULT_BACKOFF_BASE_SECONDS * (2**attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = WeatherProviderError(f"{url} -> {response.status_code}")
                await asyncio.sleep(DEFAULT_BACKOFF_BASE_SECONDS * (2**attempt))
                continue
            if response.status_code >= 400:
                raise WeatherProviderError(
                    f"{url} -> {response.status_code}: {response.text[:500]}"
                )

            result: Any = response.text if as_text else response.json()
            if self._raw_payload_sink is not None:
                request_key = f"GET:{url}:{params or {}}"
                payload_for_storage = {"text": result} if as_text else result
                self.last_raw_payload_id = await self._raw_payload_sink(
                    source, url, request_key, response.status_code, payload_for_storage
                )
            return result

        assert last_error is not None
        raise last_error

    async def _resolve_point(self, station: Station) -> PointsResponse:
        payload = await self._request(
            "weather", f"{NWS_API_BASE}/points/{station.latitude},{station.longitude}"
        )
        return PointsResponse.model_validate(payload)

    async def get_station_metadata(self, station: Station) -> StationMetadata:
        point = await self._resolve_point(station)
        return StationMetadata(
            station_id=station.station_id,
            provider="nws",
            source_location_code=station.source_location_code,
            office=point.properties.cwa,
            latitude=station.latitude,
            longitude=station.longitude,
            name=station.name,
            timezone=point.properties.timeZone or station.timezone,
        )

    async def get_forecast(self, station: Station) -> list[ForecastRecord]:
        point = await self._resolve_point(station)
        if point.properties.forecast is None:
            raise WeatherProviderError(f"no forecast URL for station {station.station_id!r}")
        payload = await self._request("weather", point.properties.forecast)
        parsed = ForecastResponse.model_validate(payload)
        # captured now, right after the one request that produced every
        # period below -- no further requests happen before this is used.
        raw_payload_id = self.last_raw_payload_id

        records: list[ForecastRecord] = []
        for period in parsed.properties.periods:
            if period.temperature is None:
                continue
            records.append(
                ForecastRecord(
                    station_id=station.station_id,
                    provider="nws",
                    variable="temperature",
                    point_estimate=Decimal(period.temperature),
                    unit=period.temperatureUnit or "F",
                    issue_time=parsed.properties.updateTime,
                    valid_start=period.startTime,
                    valid_end=period.endTime,
                    raw_payload_id=raw_payload_id,
                )
            )
        return records

    async def get_observations(
        self, station: Station, *, start: date, end: date
    ) -> list[ObservationRecord]:
        """Fetch observations for [start, end] (inclusive), routing each
        part of the range to the live API or the historical (IEM) archive
        as appropriate -- transparent to the caller."""
        cutoff = (utc_now() - timedelta(days=LIVE_RETENTION_DAYS)).date()
        records: list[ObservationRecord] = []

        if end >= cutoff:
            live_start = max(start, cutoff)
            records.extend(
                await self._get_recent_observations(station, start=live_start, end=end)
            )
        if start < cutoff:
            historical_end = min(end, cutoff - timedelta(days=1))
            if start <= historical_end:
                records.extend(
                    await self._get_historical_observations(
                        station, start=start, end=historical_end
                    )
                )
        return records

    async def _get_recent_observations(
        self, station: Station, *, start: date, end: date
    ) -> list[ObservationRecord]:
        payload = await self._request(
            "weather",
            f"{NWS_API_BASE}/products",
            params={
                "type": "CLI",
                "location": station.source_location_code,
                "start": f"{start.isoformat()}T00:00:00Z",
                "end": f"{end.isoformat()}T23:59:59Z",
                "limit": 500,
            },
        )
        summaries = ProductListResponse.model_validate(payload).graph

        records: list[ObservationRecord] = []
        for summary in summaries:
            detail_payload = await self._request(
                "weather", f"{NWS_API_BASE}/products/{summary.id}"
            )
            detail = ProductDetail.model_validate(detail_payload)
            records.extend(
                self._parse_product(
                    station,
                    product_id=detail.id,
                    issuance_time=detail.issuanceTime,
                    product_text=detail.productText,
                )
            )
        return records

    async def _get_historical_observations(
        self, station: Station, *, start: date, end: date
    ) -> list[ObservationRecord]:
        pil = f"CLI{station.source_location_code}"
        records: list[ObservationRecord] = []
        day = start
        while day <= end:
            listing = await self._request(
                "weather",
                f"{IEM_API_BASE}/nws/afos/list.json",
                params={"pil": pil, "date": day.isoformat()},
            )
            for row in listing.get("data", []):
                text_link = row.get("text_link")
                product_id = row.get("product_id")
                entered = row.get("entered")
                if not text_link or not product_id or not entered:
                    logger.warning(
                        "weather.historical_row_incomplete", station=station.station_id, row=row
                    )
                    continue
                product_text = await self._request("weather", text_link, as_text=True)
                records.extend(
                    self._parse_product(
                        station,
                        product_id=product_id,
                        issuance_time=parse_iso8601_utc(entered),
                        product_text=product_text,
                    )
                )
            day += timedelta(days=1)
        return records

    def _parse_product(
        self, station: Station, *, product_id: str, issuance_time: Any, product_text: str
    ) -> list[ObservationRecord]:
        try:
            parsed = parse_cli_temperatures(product_text)
        except CliParseError as exc:
            logger.warning(
                "weather.cli_parse_failed",
                station=station.station_id,
                product_id=product_id,
                error=str(exc),
            )
            return []

        common = {
            "station_id": station.station_id,
            "provider": "nws",
            "unit": "F",
            "observation_date": parsed.observation_date,
            "issuance_time": issuance_time,
            # captured now, right after the specific request (detail/text
            # fetch) that produced product_text -- correct even though
            # get_observations() makes many such requests per call.
            "raw_payload_id": self.last_raw_payload_id,
            "source_product_id": product_id,
        }
        return [
            ObservationRecord(variable="tmax_f", value=Decimal(parsed.tmax_f), **common),
            ObservationRecord(variable="tmin_f", value=Decimal(parsed.tmin_f), **common),
        ]
