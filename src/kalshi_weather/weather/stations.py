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


#: Every entry's (source_location_code, wfo_site) pair is taken verbatim from
#: Kalshi's own settlement-source URL for that city's series
#: (`product.php?site=<WFO>&product=CLI&issuedby=<code>`) -- the same
#: authority the settlement parser resolves against -- and cross-verified
#: live against NWS `/points/{lat,lon}` (the coordinate resolves to the same
#: WFO) and against IEM CLI-product availability at both the 2023 backfill
#: start and the present day. NYC: docs/adr/0003-weather-data-source.md.
#: CHI/DEN/LAX: station-expansion validation, 2026-07-21 (Task 3B) -- note
#: Chicago settles on Midway (MDW), not O'Hare, per Kalshi's own URL.
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
    "CHI": Station(
        station_id="CHI",
        source_location_code="MDW",
        name="Chicago Midway, IL",
        latitude=Decimal("41.7842"),
        longitude=Decimal("-87.7553"),
        timezone="America/Chicago",
        city="Chicago",
        wfo_site="LOT",
    ),
    "DEN": Station(
        station_id="DEN",
        source_location_code="DEN",
        name="Denver International, CO",
        latitude=Decimal("39.8466"),
        longitude=Decimal("-104.6562"),
        timezone="America/Denver",
        city="Denver",
        wfo_site="BOU",
    ),
    "LAX": Station(
        station_id="LAX",
        source_location_code="LAX",
        name="Los Angeles International, CA",
        latitude=Decimal("33.9382"),
        longitude=Decimal("-118.3866"),
        timezone="America/Los_Angeles",
        city="Los Angeles",
        wfo_site="LOX",
    ),
    # --- 2026-07-27 three-city pilot (ADR 0023): SEA/PHX/MIA. Each
    # (source_location_code, wfo_site) pair is verbatim from Kalshi's own
    # settlement-source URLs in collected production data; coordinates and
    # timezones from NWS station metadata (api.weather.gov/stations/K<code>),
    # cross-verified via /points (same WFO, same tz) and IEM CLI availability
    # (recent + 2023 history) on 2026-07-27. PHX deliberately uses
    # America/Phoenix (NO daylight saving) -- never America/Denver.
    "SEA": Station(
        station_id="SEA",
        source_location_code="SEA",
        name="Seattle-Tacoma International, WA",
        latitude=Decimal("47.4447"),
        longitude=Decimal("-122.3136"),
        timezone="America/Los_Angeles",
        city="Seattle",
        wfo_site="SEW",
    ),
    "PHX": Station(
        station_id="PHX",
        source_location_code="PHX",
        name="Phoenix Sky Harbor International, AZ",
        latitude=Decimal("33.4278"),
        longitude=Decimal("-112.0035"),
        timezone="America/Phoenix",
        city="Phoenix",
        wfo_site="PSR",
    ),
    "MIA": Station(
        station_id="MIA",
        source_location_code="MIA",
        name="Miami International, FL",
        latitude=Decimal("25.7906"),
        longitude=Decimal("-80.3164"),
        timezone="America/New_York",
        city="Miami",
        wfo_site="MFL",
    ),
}


class RegistryValidationError(ValueError):
    """A registry entry is malformed or conflicts with another entry."""


def validate_registry(stations: dict[str, Station] | None = None) -> None:
    """Structural validation of the station registry: key/id agreement,
    unique CLI location codes (the settlement parser's lookup key), unique
    (wfo, location) pairs, resolvable IANA timezones, and no empty fields.
    Raises RegistryValidationError on the first violation; called at import
    so a bad entry fails fast rather than mis-mapping data silently."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    entries = STATIONS if stations is None else stations
    seen_codes: dict[str, str] = {}
    for key, station in entries.items():
        if key != station.station_id:
            raise RegistryValidationError(
                f"registry key {key!r} != station_id {station.station_id!r}"
            )
        for field_name in ("source_location_code", "name", "timezone", "city", "wfo_site"):
            if not getattr(station, field_name):
                raise RegistryValidationError(f"station {key!r}: empty {field_name}")
        if station.source_location_code in seen_codes:
            raise RegistryValidationError(
                f"duplicate source_location_code {station.source_location_code!r} "
                f"({seen_codes[station.source_location_code]!r} and {key!r}): the "
                "settlement parser's registry lookup would be ambiguous"
            )
        seen_codes[station.source_location_code] = key
        try:
            ZoneInfo(station.timezone)
        except ZoneInfoNotFoundError as exc:
            raise RegistryValidationError(
                f"station {key!r}: unresolvable IANA timezone {station.timezone!r}"
            ) from exc


validate_registry()


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
    """All registered stations, in deterministic station_id order."""
    return [STATIONS[key] for key in sorted(STATIONS)]
