from datetime import date
from pathlib import Path

import pytest

from kalshi_weather.weather.cli_parser import CliParseError, parse_cli_temperatures

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "weather"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_parses_real_today_variant() -> None:
    result = parse_cli_temperatures(_load("cli_nyc_today.txt"))
    assert result.observation_date == date(2026, 7, 20)
    assert result.tmax_f == 81
    assert result.tmin_f == 63


def test_parses_real_yesterday_variant() -> None:
    """Early-morning issuance: section labeled YESTERDAY, not TODAY -- the
    covered date comes from the header line, not the section label."""
    result = parse_cli_temperatures(_load("cli_nyc_yesterday.txt"))
    assert result.observation_date == date(2026, 6, 14)
    assert result.tmax_f == 87
    assert result.tmin_f == 70


def test_missing_temperature_section_raises() -> None:
    text = """
...THE CENTRAL PARK NY CLIMATE SUMMARY FOR JULY 20 2026...

PRECIPITATION (IN)
  TODAY            0.00          1.97 1889   0.16  -0.16      T
"""
    with pytest.raises(CliParseError):
        parse_cli_temperatures(text)


def test_missing_minimum_raises() -> None:
    text = """
...THE CENTRAL PARK NY CLIMATE SUMMARY FOR JULY 20 2026...

TEMPERATURE (F)
 TODAY
  MAXIMUM         81    125 PM 101    1980  85     -4       88
"""
    with pytest.raises(CliParseError):
        parse_cli_temperatures(text)


def test_missing_summary_date_header_raises() -> None:
    text = """
TEMPERATURE (F)
 TODAY
  MAXIMUM         81    125 PM 101    1980  85     -4       88
  MINIMUM         63    541 AM  55    1890  71     -8       75
"""
    with pytest.raises(CliParseError):
        parse_cli_temperatures(text)


def test_completely_malformed_text_raises() -> None:
    with pytest.raises(CliParseError):
        parse_cli_temperatures("not a climate report at all")


def test_negative_temperature_parses_correctly() -> None:
    text = """
...THE FAIRBANKS AK CLIMATE SUMMARY FOR JANUARY 15 2026...

TEMPERATURE (F)
 TODAY
  MAXIMUM        -12    125 PM  15    1980 -20      8       -5
  MINIMUM        -35    541 AM -40    1890 -30     -5      -20
"""
    result = parse_cli_temperatures(text)
    assert result.tmax_f == -12
    assert result.tmin_f == -35


def test_record_flagged_value_parses() -> None:
    # Regression: a record-tying/breaking value carries a trailing "R" flag
    # (real LAX 2026-07-17 product). The value 86 must still be extracted --
    # the whole day was silently dropped before the fix.
    text = """
...THE LOS ANGELES CA CLIMATE SUMMARY FOR JULY 17 2026...

TEMPERATURE (F)
 TODAY
  MAXIMUM         86R 12:53 PM  85    2003  75     11       73
  MINIMUM         69   4:19 AM  56    1947  64      5       63
"""
    result = parse_cli_temperatures(text)
    assert result.tmax_f == 86
    assert result.tmin_f == 69


def test_both_values_flagged_parse() -> None:
    # PHX 2026-07-24: extreme heat, MAXIMUM 117R; an estimated "E" flag also works.
    text = """
...THE PHOENIX AZ CLIMATE SUMMARY FOR JULY 24 2026...

TEMPERATURE (F)
 TODAY
  MAXIMUM        117R   316 PM 116    2014 106     11
  MINIMUM         97E   650 AM  68    1913  85     12
"""
    result = parse_cli_temperatures(text)
    assert result.tmax_f == 117
    assert result.tmin_f == 97


def test_genuinely_missing_value_still_raises() -> None:
    # A value reported as missing ("M", no digits) must still fail closed --
    # the fix must not fabricate a number for a genuinely absent value.
    text = """
...THE CENTRAL PARK NY CLIMATE SUMMARY FOR JULY 20 2026...

TEMPERATURE (F)
 TODAY
  MAXIMUM          M    125 PM 101    1980  85     -4       88
  MINIMUM         63    541 AM  55    1890  71     -8       75
"""
    with pytest.raises(CliParseError):
        parse_cli_temperatures(text)
