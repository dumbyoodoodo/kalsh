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
    ),
}


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
