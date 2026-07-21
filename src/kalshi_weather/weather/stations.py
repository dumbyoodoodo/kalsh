"""Static registry of weather stations this project collects data for.

Kalshi cites settlement stations by name in market rules prose (e.g.
"Central Park NY" -- see a real example in
docs/adr/0003-weather-data-source.md), not by a resolvable code; there is no
API to auto-derive this mapping. Stations are added here by hand, as data,
when a new city/station is needed -- not by writing new code.

Full Kalshi-market-to-station mapping (deciding *which* Kalshi series maps
to *which* entry here) is Milestone 2b's job (settlement resolution); this
registry only tracks the stations we've already decided to collect weather
data for, starting with the one STRATEGY_SPEC.md's initial scope names.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Station:
    station_id: str  # our internal id, used throughout storage/CLI
    source_location_code: str  # NWS CLI product location code, e.g. "NYC"
    name: str  # human name, matching Kalshi's rules text where possible
    latitude: Decimal
    longitude: Decimal
    timezone: str  # IANA tz name, used to interpret CLI reports' local times
    city: str  # human city name, used by settlement specs
    #: Issuing WFO office for this station's CLI product, verified live (see
    #: docs/adr/0003-weather-data-source.md). Used by the settlement parser to
    #: cross-check Kalshi's settlement-source URL (`site=` query param).
    wfo_site: str


#: Confirmed live (docs/adr/0003-weather-data-source.md): /points/{lat,lon}
#: for this coordinate resolves to WFO office "OKX", matching Kalshi's own
#: rules-text citation for NYC daily-temperature markets.
STATIONS: dict[str, Station] = {
    "NYC": Station(
        station_id="NYC",
        source_location_code="NYC",
        name="Central Park, NY",
        latitude=Decimal("40.7829"),
        longitude=Decimal("-73.9654"),
        timezone="America/New_York",
        city="New York",
        wfo_site="OKX",
    ),
}


def station_for_location_code(code: str) -> Station | None:
    """Registry station whose NWS CLI location code matches ``code``, if any."""
    for station in STATIONS.values():
        if station.source_location_code == code:
            return station
    return None


class UnknownStationError(KeyError):
    """Raised when a station_id isn't in the registry."""


def get_station(station_id: str) -> Station:
    try:
        return STATIONS[station_id]
    except KeyError:
        raise UnknownStationError(
            f"unknown station_id {station_id!r}; add it to weather/stations.py"
        ) from None


def list_stations() -> list[Station]:
    return list(STATIONS.values())
