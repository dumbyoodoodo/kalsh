"""SEA/PHX/MIA station-pilot tests: registry entries, frozen-experiment
isolation, and station-local timezone/target-date behavior (ADR 0023)."""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kalshi_weather.dataset.pit import local_date
from kalshi_weather.weather.stations import (
    STATIONS,
    station_for_location_code,
    validate_registry,
)

REPO = Path(__file__).resolve().parents[2]


# --- registry entries --------------------------------------------------------


def test_pilot_stations_present_with_authoritative_mappings() -> None:
    expect = {
        "SEA": ("SEA", "SEW", "America/Los_Angeles", "Seattle"),
        "PHX": ("PHX", "PSR", "America/Phoenix", "Phoenix"),
        "MIA": ("MIA", "MFL", "America/New_York", "Miami"),
    }
    for sid, (code, wfo, tz, city) in expect.items():
        st = STATIONS[sid]
        assert st.source_location_code == code
        assert st.wfo_site == wfo
        assert st.timezone == tz
        assert st.city == city
        assert station_for_location_code(code) is st


def test_registry_grew_by_exactly_three_and_validates() -> None:
    assert set(STATIONS) == {"NYC", "CHI", "DEN", "LAX", "SEA", "PHX", "MIA"}
    validate_registry()  # unique codes, resolvable timezones, no empty fields


def test_existing_stations_unchanged() -> None:
    assert STATIONS["NYC"].source_location_code == "NYC"
    assert STATIONS["CHI"].source_location_code == "MDW"  # Midway, per Kalshi
    assert STATIONS["DEN"].wfo_site == "BOU"
    assert STATIONS["LAX"].wfo_site == "LOX"


# --- frozen-experiment isolation ---------------------------------------------


def test_frozen_experiment_station_sets_do_not_include_pilot_cities() -> None:
    from kalshi_weather.experiments.decision_grain import _STATIONS as h0018_h0019
    from kalshi_weather.experiments.h0020_readiness import STATIONS as h0020

    assert tuple(sorted(h0018_h0019)) == ("CHI", "DEN", "LAX", "NYC")
    assert tuple(sorted(h0020)) == ("CHI", "DEN", "LAX", "NYC")


def test_experiment_modules_do_not_iterate_the_live_registry() -> None:
    # frozen scope is a literal tuple in each module, never derived from the
    # registry -- so registry growth cannot leak into experiment datasets
    for rel in (
        "src/kalshi_weather/experiments/decision_grain.py",
        "src/kalshi_weather/experiments/readiness.py",
        "src/kalshi_weather/experiments/h0020.py",
        "src/kalshi_weather/experiments/h0020_readiness.py",
        "src/kalshi_weather/experiments/runner.py",
    ):
        src = (REPO / rel).read_text()
        pattern = r"list_stations|STATIONS\s*=\s*STATIONS|weather\.stations import"
        assert not re.search(pattern, src), rel


def test_h0020_windows_and_frozen_hashes_unchanged() -> None:
    from kalshi_weather.experiments.h0020 import (
        EXCLUDED_GAP_END,
        EXCLUDED_GAP_START,
        TEST_END_INITIAL,
        TEST_START,
    )

    assert str(EXCLUDED_GAP_START) == "2026-08-12" and str(EXCLUDED_GAP_END) == "2026-08-25"
    assert str(TEST_START) == "2026-09-09" and str(TEST_END_INITIAL) == "2026-09-22"
    frozen = {
        "docs/research/experiments/EXP-FUTURE-H0019/config.json": "5d1f763dcf6b",
        "docs/research/experiments/EXP-FUTURE-H0020/config.json": "5d70303816f5",
        "docs/research/experiments/EXP-20260726-H0018/results.json": "b691849e2ce1",
    }
    for rel, prefix in frozen.items():
        digest = hashlib.sha256((REPO / rel).read_bytes()).hexdigest()
        assert digest.startswith(prefix), rel


# --- timezone / target-date behavior -----------------------------------------


def test_sea_dst_target_date_boundaries() -> None:
    tz = STATIONS["SEA"].timezone
    # July (PDT, UTC-7): 06:59Z is still the previous local day
    assert local_date(datetime(2026, 7, 27, 6, 59, tzinfo=UTC), tz).isoformat() == "2026-07-26"
    assert local_date(datetime(2026, 7, 27, 7, 0, tzinfo=UTC), tz).isoformat() == "2026-07-27"
    # January (PST, UTC-8): boundary shifts to 08:00Z
    assert local_date(datetime(2026, 1, 15, 7, 59, tzinfo=UTC), tz).isoformat() == "2026-01-14"
    assert local_date(datetime(2026, 1, 15, 8, 0, tzinfo=UTC), tz).isoformat() == "2026-01-15"


def test_phx_has_no_daylight_saving_and_is_not_denver() -> None:
    phx = ZoneInfo(STATIONS["PHX"].timezone)
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=phx).utcoffset()
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=phx).utcoffset()
    assert summer == winter  # America/Phoenix never shifts
    den = ZoneInfo(STATIONS["DEN"].timezone)
    den_summer = datetime(2026, 7, 15, 12, 0, tzinfo=den).utcoffset()
    assert summer != den_summer  # Denver observes DST in July; Phoenix must not


def test_phx_target_date_midnight_boundary_year_round() -> None:
    tz = STATIONS["PHX"].timezone
    # MST (UTC-7) year-round: boundary at 07:00Z in BOTH July and January
    for month in (7, 1):
        assert local_date(datetime(2026, month, 20, 6, 59, tzinfo=UTC), tz).day == 19
        assert local_date(datetime(2026, month, 20, 7, 0, tzinfo=UTC), tz).day == 20


def test_mia_dst_target_date_boundaries() -> None:
    tz = STATIONS["MIA"].timezone
    # July (EDT, UTC-4)
    assert local_date(datetime(2026, 7, 27, 3, 59, tzinfo=UTC), tz).isoformat() == "2026-07-26"
    assert local_date(datetime(2026, 7, 27, 4, 0, tzinfo=UTC), tz).isoformat() == "2026-07-27"
    # January (EST, UTC-5)
    assert local_date(datetime(2026, 1, 15, 4, 59, tzinfo=UTC), tz).isoformat() == "2026-01-14"
    assert local_date(datetime(2026, 1, 15, 5, 0, tzinfo=UTC), tz).isoformat() == "2026-01-15"
