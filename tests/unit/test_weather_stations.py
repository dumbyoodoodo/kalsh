"""Station-registry tests (Task 3B station expansion): uniqueness,
deterministic ordering, timezone correctness, source-ID mapping,
conflicting-entry detection, and NYC regression protection. All
deterministic; no network, no randomness."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from kalshi_weather.weather.stations import (
    STATIONS,
    RegistryValidationError,
    Station,
    get_station,
    list_stations,
    station_for_location_code,
    validate_registry,
)

EXPECTED_STATIONS = {
    # station_id: (source_location_code, wfo_site, timezone, city)
    "NYC": ("NYC", "OKX", "America/New_York", "New York"),
    "CHI": ("MDW", "LOT", "America/Chicago", "Chicago"),
    "DEN": ("DEN", "BOU", "America/Denver", "Denver"),
    "LAX": ("LAX", "LOX", "America/Los_Angeles", "Los Angeles"),
}


def test_registry_contains_exactly_the_expected_stations() -> None:
    assert set(STATIONS) == set(EXPECTED_STATIONS)


def test_registry_mappings_match_kalshi_settlement_urls() -> None:
    """Every (location code, WFO) pair comes verbatim from Kalshi's own
    settlement-source URL (site=<WFO>&issuedby=<code>), verified live
    2026-07-21 -- including Chicago settling on Midway, not O'Hare."""
    for station_id, (code, wfo, tz, city) in EXPECTED_STATIONS.items():
        station = get_station(station_id)
        assert station.source_location_code == code
        assert station.wfo_site == wfo
        assert station.timezone == tz
        assert station.city == city


def test_list_stations_is_deterministically_ordered() -> None:
    ids = [s.station_id for s in list_stations()]
    assert ids == sorted(ids)
    assert ids == ["CHI", "DEN", "LAX", "NYC"]


def test_station_ids_and_location_codes_are_unique() -> None:
    stations = list_stations()
    ids = [s.station_id for s in stations]
    codes = [s.source_location_code for s in stations]
    assert len(set(ids)) == len(ids)
    assert len(set(codes)) == len(codes)


def test_all_timezones_resolve_via_zoneinfo() -> None:
    for station in list_stations():
        assert ZoneInfo(station.timezone) is not None


def test_station_for_location_code_maps_mdw_to_chi() -> None:
    """The settlement parser's lookup key: MDW (Kalshi's issuedby code)
    must map to our CHI station even though the ids differ."""
    station = station_for_location_code("MDW")
    assert station is not None
    assert station.station_id == "CHI"


def test_station_for_location_code_unknown_returns_none() -> None:
    assert station_for_location_code("ZZZ") is None


def test_nyc_regression_protection() -> None:
    """The pre-expansion NYC entry is byte-for-byte unchanged."""
    nyc = get_station("NYC")
    assert nyc == Station(
        station_id="NYC",
        source_location_code="NYC",
        name="Central Park, NY",
        latitude=Decimal("40.7829"),
        longitude=Decimal("-73.9654"),
        timezone="America/New_York",
        city="New York",
        wfo_site="OKX",
    )


# --- validate_registry conflict detection ------------------------------------


def _synthetic(station_id: str, code: str, tz: str = "America/New_York") -> Station:
    return Station(
        station_id=station_id,
        source_location_code=code,
        name=f"Test {station_id}",
        latitude=Decimal("1.0"),
        longitude=Decimal("2.0"),
        timezone=tz,
        city=station_id,
        wfo_site="TST",
    )


def test_validate_registry_passes_on_the_real_registry() -> None:
    validate_registry()  # must not raise


def test_validate_registry_rejects_key_id_mismatch() -> None:
    bad = {"AAA": _synthetic("BBB", "AAA")}
    with pytest.raises(RegistryValidationError, match="key"):
        validate_registry(bad)


def test_validate_registry_rejects_duplicate_location_codes() -> None:
    bad = {
        "AAA": _synthetic("AAA", "SAME"),
        "BBB": _synthetic("BBB", "SAME"),
    }
    with pytest.raises(RegistryValidationError, match="duplicate source_location_code"):
        validate_registry(bad)


def test_validate_registry_rejects_bad_timezone() -> None:
    bad = {"AAA": _synthetic("AAA", "AAA", tz="Mars/Olympus_Mons")}
    with pytest.raises(RegistryValidationError, match="timezone"):
        validate_registry(bad)


def test_validate_registry_rejects_empty_field() -> None:
    bad = {"AAA": replace(_synthetic("AAA", "AAA"), wfo_site="")}
    with pytest.raises(RegistryValidationError, match="empty wfo_site"):
        validate_registry(bad)


# --- DST behavior for a non-NYC timezone -------------------------------------


def test_chicago_dst_transitions_resolve_correct_offsets() -> None:
    """America/Chicago: spring-forward 2026-03-08 (CST->CDT) and fall-back
    2025-11-02 (CDT->CST) -- offsets differ from NYC's by one hour."""
    chi = ZoneInfo(get_station("CHI").timezone)
    before_spring = datetime(2026, 3, 8, 7, 30, tzinfo=UTC).astimezone(chi)  # 01:30 CST
    assert before_spring.utcoffset() == timedelta(hours=-6)
    after_spring = datetime(2026, 3, 8, 8, 30, tzinfo=UTC).astimezone(chi)  # 03:30 CDT
    assert after_spring.utcoffset() == timedelta(hours=-5)
    before_fall = datetime(2025, 11, 2, 6, 30, tzinfo=UTC).astimezone(chi)  # 01:30 CDT
    assert before_fall.utcoffset() == timedelta(hours=-5)
    after_fall = datetime(2025, 11, 2, 7, 30, tzinfo=UTC).astimezone(chi)  # 01:30 CST
    assert after_fall.utcoffset() == timedelta(hours=-6)


def test_same_utc_instant_lands_on_different_local_dates_across_stations() -> None:
    """Day-boundary independence: 05:30 UTC is already the next local day
    in NYC (01:30) but still the prior local day in LAX (22:30) -- the
    per-station local calendar day is what CLI settlement uses, so
    cross-station date handling must never share a single local date."""
    instant = datetime(2026, 6, 2, 5, 30, tzinfo=UTC)
    ny_date = instant.astimezone(ZoneInfo(get_station("NYC").timezone)).date()
    lax_date = instant.astimezone(ZoneInfo(get_station("LAX").timezone)).date()
    assert ny_date.isoformat() == "2026-06-02"
    assert lax_date.isoformat() == "2026-06-01"
