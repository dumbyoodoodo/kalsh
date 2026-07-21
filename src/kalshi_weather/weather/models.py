"""Typed schemas for the NWS (api.weather.gov) weather data source.

# CONFIRMED (docs/adr/0003-weather-data-source.md): field names below were
# verified against live api.weather.gov responses during Phase 3 research,
# not guessed. `extra="allow"` is used the same way as `kalshi/models.py` --
# an unexpected/renamed field surfaces as an extra attribute rather than a
# hard parse failure, since raw payloads are captured unconditionally before
# schema validation regardless of whether the typed model matches.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NwsModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class PointRelativeLocation(NwsModel):
    city: str | None = None
    state: str | None = None


class PointProperties(NwsModel):
    cwa: str | None = None  # WFO office code, e.g. "OKX"
    gridId: str | None = None  # same as cwa in practice, confirmed live
    gridX: int | None = None
    gridY: int | None = None
    forecast: str | None = None
    forecastHourly: str | None = None
    forecastGridData: str | None = None
    observationStations: str | None = None
    timeZone: str | None = None
    relativeLocation: dict[str, Any] | None = None


class PointsResponse(NwsModel):
    properties: PointProperties


class ProductSummary(NwsModel):
    """One entry from /products or /products/types/{type}/locations/{loc}."""

    id: str
    wmoCollectiveId: str | None = None
    issuingOffice: str | None = None
    issuanceTime: datetime
    productCode: str | None = None
    productName: str | None = None


class ProductListResponse(NwsModel):
    graph: list[ProductSummary] = Field(default_factory=list, alias="@graph")


class ProductDetail(NwsModel):
    """A single product from /products/{id}, including its raw text body."""

    id: str
    issuingOffice: str | None = None
    issuanceTime: datetime
    productCode: str | None = None
    productText: str


class ForecastPeriod(NwsModel):
    number: int | None = None
    name: str | None = None
    startTime: datetime
    endTime: datetime
    isDaytime: bool | None = None
    temperature: int | None = None
    temperatureUnit: str | None = None
    shortForecast: str | None = None
    detailedForecast: str | None = None


class ForecastProperties(NwsModel):
    updateTime: datetime
    periods: list[ForecastPeriod] = []


class ForecastResponse(NwsModel):
    properties: ForecastProperties


# --- Normalized internal types, provider-agnostic (not NWS-specific) ---


class StationMetadata(BaseModel):
    station_id: str  # our internal id, e.g. "NYC" -- see weather/stations.py
    provider: str
    source_location_code: str  # e.g. NWS CLI product location code "NYC"
    office: str | None = None  # e.g. WFO "OKX"
    latitude: Decimal
    longitude: Decimal
    name: str
    timezone: str


class ObservationRecord(BaseModel):
    station_id: str
    provider: str
    variable: str  # "tmax_f" / "tmin_f" to start
    value: Decimal
    unit: str
    # The calendar day this value describes -- read directly from the CLI
    # report's own "...CLIMATE SUMMARY FOR <date>..." header, which can
    # differ from issuance_time's calendar date (an early-morning issuance
    # reports on the *previous* day; see weather/cli_parser.py).
    observation_date: date
    # When *this version* of the report was issued -- CLI reports are
    # reissued multiple times per day, so this is what "latest version"
    # dedup/recency is keyed on, not observation_date.
    issuance_time: datetime
    source_product_id: str
    # Set by the provider immediately after the specific request that
    # produced this record -- get_observations() makes many internal
    # requests per call, so a single "last request" id read after the fact
    # would misattribute every record but the very last one.
    raw_payload_id: int | None = None


class ForecastRecord(BaseModel):
    station_id: str
    provider: str
    variable: str
    point_estimate: Decimal
    unit: str
    issue_time: datetime
    valid_start: datetime
    valid_end: datetime
    raw_payload_id: int | None = None
