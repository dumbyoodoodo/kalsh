"""Tests for weather/cli_products.py -- the CLI product-text parser.

Format fixtures mirror the real archived variants of all four stations
(CHI colon form, NYC/DEN compact form, LAX colon form, DEN early-morning
AS OF, record suffix, MM missing data, WMO correction headers)."""

from __future__ import annotations

from kalshi_weather.weather.cli_products import CliProductFields, parse_cli_product

CHI_STYLE = """773
CDUS43 KLOT 152149
CLIMDW

CLIMATE REPORT
NATIONAL WEATHER SERVICE CHICAGO IL
449 PM CDT MON JUN 15 2026

...THE CHICAGO-MIDWAY CLIMATE SUMMARY FOR JUNE 15 2026...
VALID TODAY AS OF 0400 PM LOCAL TIME.

TEMPERATURE (F)
 TODAY
  MAXIMUM         75   2:59 PM 100    2022  81     -6       81
  MINIMUM         52   4:42 AM  45    1933  62    -10       60
"""

NYC_STYLE = """004
CDUS41 KOKX 160629
CLINYC

...THE CENTRAL PARK NY CLIMATE SUMMARY FOR JUNE 15 2026...

TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         74    357 PM  96    1891  80     -6       64
  MINIMUM         58   1201 AM  44    1978  64     -6       57
"""

DEN_EARLY = """000
CDUS45 KBOU 151200
CLIDEN

...THE DENVER CO CLIMATE SUMMARY FOR JUNE 15 2025...
VALID TODAY AS OF 0600 AM LOCAL TIME.

TEMPERATURE (F)
 TODAY
  MAXIMUM         67   1225 AM 101    2021  83    -16       93
  MINIMUM         55    512 AM  30    1951  52      3       58
"""

CORRECTED = """000
CDUS43 KLOT 152149 CCA
CLIMDW

  MAXIMUM         75   2:59 PM 100    2022  81     -6       81
"""

RECORD_SUFFIX = """000
CDUS45 KBOU 151200
  MAXIMUM        101R   3:15 PM 101    2021  83     18       93
"""

MISSING_DATA = """000
CDUS43 KLOT 152149
  MAXIMUM         MM
  MINIMUM         MM
"""

VALUE_NO_TIME = """000
CDUS43 KLOT 152149
  MAXIMUM         75
"""


def test_chi_colon_format() -> None:
    p = parse_cli_product(CHI_STYLE)
    assert p.max_value == 75
    assert p.max_occurrence_hour == 14 + 59 / 60
    assert p.min_value == 52
    assert p.min_occurrence_hour == 4 + 42 / 60
    assert p.asof_hour == 16
    assert p.corrected is False


def test_nyc_compact_format() -> None:
    p = parse_cli_product(NYC_STYLE)
    assert p.max_value == 74
    assert p.max_occurrence_hour == 15 + 57 / 60
    assert p.min_occurrence_hour == 0 + 1 / 60  # 1201 AM -> 00:01
    assert p.asof_hour is None  # final product has no AS OF line


def test_den_early_morning_asof() -> None:
    p = parse_cli_product(DEN_EARLY)
    assert p.asof_hour == 6
    assert p.max_value == 67
    assert p.max_occurrence_hour == 0 + 25 / 60  # 1225 AM -> 00:25


def test_midnight_and_noon_edges() -> None:
    p = parse_cli_product("  MAXIMUM   70   12:00 AM x\n  MINIMUM   50   12:00 PM x\n")
    assert p.max_occurrence_hour == 0.0  # 12:00 AM is midnight
    assert p.min_occurrence_hour == 12.0  # 12:00 PM is noon


def test_correction_header_detected() -> None:
    assert parse_cli_product(CORRECTED).corrected is True
    assert parse_cli_product(CHI_STYLE).corrected is False


def test_record_suffix_value_parses() -> None:
    p = parse_cli_product(RECORD_SUFFIX)
    assert p.max_value == 101
    assert p.max_occurrence_hour == 15.25


def test_missing_data_yields_none_never_raises() -> None:
    p = parse_cli_product(MISSING_DATA)
    assert p == CliProductFields(
        max_value=None,
        max_occurrence_hour=None,
        min_value=None,
        min_occurrence_hour=None,
        asof_hour=None,
        corrected=False,
    )


def test_value_without_time_keeps_value_drops_hour() -> None:
    p = parse_cli_product(VALUE_NO_TIME)
    assert p.max_value == 75
    assert p.max_occurrence_hour is None


def test_empty_and_garbage_text() -> None:
    assert parse_cli_product("").max_value is None
    assert parse_cli_product("no climate data here").max_value is None


def test_negative_value_winter() -> None:
    p = parse_cli_product("  MAXIMUM         -4   3:10 PM ...\n")
    assert p.max_value == -4
    assert p.max_occurrence_hour == 15 + 10 / 60
