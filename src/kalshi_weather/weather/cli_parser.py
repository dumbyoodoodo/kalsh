"""Parser for NWS "Climatological Report (Daily)" (CLI) product text.

# CONFIRMED (docs/adr/0003-weather-data-source.md): format below matches two
# real CLI products fetched live during Phase 3 research (CLINYC), not a
# guessed format:
#
#   - A same-day, mid-afternoon issuance labels the section "TODAY" and is
#     preliminary (the day isn't over yet):
#       TEMPERATURE (F)
#        TODAY
#         MAXIMUM         81    125 PM 101    1980  85     -4       88
#         MINIMUM         63    541 AM  55    1890  71     -8       75
#   - An early-morning issuance (after midnight) labels it "YESTERDAY" and
#     is the final, complete report for the prior calendar day:
#       TEMPERATURE (F)
#        YESTERDAY
#         MAXIMUM         87    212 PM  99    1956  79      8       68
#         MINIMUM         70   1159 PM  49    1875  64      6       59
#
# Rather than infer the covered calendar date from the TODAY/YESTERDAY label
# plus issuance date, this parser reads it directly from the report's own
# "...CLIMATE SUMMARY FOR <month> <day> <year>..." header line, which is
# correct in both cases and avoids that inference entirely.
#
# This is a fixed-width text product, not JSON -- both the live
# api.weather.gov backend and the historical IEM backend return the exact
# same raw text format (see weather/provider.py), so one parser serves both.
"""

import re
from dataclasses import dataclass
from datetime import date

_TEMPERATURE_SECTION_RE = re.compile(r"TEMPERATURE\s*\(F\)", re.IGNORECASE)
#: The observed value may carry a trailing record flag letter (e.g. "86R" on a
#: day the maximum tied/broke a record, "E" for estimated). A trailing ``\b``
#: after the digits would *reject* such values (there is no word boundary
#: between a digit and a following letter), silently dropping the whole day --
#: the cause of the LAX 07-17 / MIA 07-16 / NYC 07-02 / PHX 07-24 record-heat
#: gaps. Capture the leading integer and ignore any trailing flag; a genuinely
#: missing value ("M", no digits) still fails to match, as intended.
_VALUE_LINE_RE = re.compile(r"^\s*(MAXIMUM|MINIMUM)\s+(-?\d+)(?=\D|$)", re.IGNORECASE)
_SUMMARY_DATE_RE = re.compile(
    r"CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})", re.IGNORECASE
)
_MONTH_NAMES = {
    "JANUARY": 1,
    "FEBRUARY": 2,
    "MARCH": 3,
    "APRIL": 4,
    "MAY": 5,
    "JUNE": 6,
    "JULY": 7,
    "AUGUST": 8,
    "SEPTEMBER": 9,
    "OCTOBER": 10,
    "NOVEMBER": 11,
    "DECEMBER": 12,
}


class CliParseError(ValueError):
    """Raised when a CLI product's text can't be parsed for the values needed.

    The raw product text is preserved regardless (see ingestion/weather_collector.py)
    -- this only means the normalized values couldn't be extracted.
    """


@dataclass(frozen=True, slots=True)
class ParsedCliTemperatures:
    observation_date: date  # the calendar day these values describe
    tmax_f: int
    tmin_f: int


def _parse_summary_date(product_text: str) -> date:
    match = _SUMMARY_DATE_RE.search(product_text)
    if match is None:
        raise CliParseError("could not find a 'CLIMATE SUMMARY FOR <date>' header line")
    month_name, day_str, year_str = match.groups()
    month = _MONTH_NAMES.get(month_name.upper())
    if month is None:
        raise CliParseError(f"unrecognized month name {month_name!r} in summary date")
    return date(int(year_str), month, int(day_str))


def parse_cli_temperatures(product_text: str) -> ParsedCliTemperatures:
    """Extract the covered date and MAXIMUM/MINIMUM Fahrenheit temperature
    from a CLI product's raw text. Raises CliParseError if the report is
    missing the summary-date header or a well-formed TEMPERATURE (F) section
    with both values -- known, documented limitation: does not yet
    distinguish a target MAXIMUM/MINIMUM from any other MAXIMUM/MINIMUM-
    labeled row further down the report (e.g. a monthly-record row), since
    no such collision has been observed in either real product sampled;
    see docs/adr/0003-weather-data-source.md.
    """
    observation_date = _parse_summary_date(product_text)

    lines = product_text.splitlines()
    seen_temperature_header = False
    tmax_f: int | None = None
    tmin_f: int | None = None

    for line in lines:
        if _TEMPERATURE_SECTION_RE.search(line):
            seen_temperature_header = True
            continue
        if not seen_temperature_header:
            continue

        match = _VALUE_LINE_RE.match(line)
        if match is None:
            continue
        label, raw_value = match.group(1).upper(), match.group(2)
        if label == "MAXIMUM" and tmax_f is None:
            tmax_f = int(raw_value)
        elif label == "MINIMUM" and tmin_f is None:
            tmin_f = int(raw_value)

        if tmax_f is not None and tmin_f is not None:
            break

    if tmax_f is None or tmin_f is None:
        raise CliParseError(
            "could not find both MAXIMUM and MINIMUM temperature values "
            f"(tmax_f={tmax_f!r}, tmin_f={tmin_f!r})"
        )
    return ParsedCliTemperatures(
        observation_date=observation_date, tmax_f=tmax_f, tmin_f=tmin_f
    )
