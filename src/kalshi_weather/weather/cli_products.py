"""Parsing of NWS CLI (climate report) product text.

Extracts the fields the structured observation tables deliberately do not
carry: the local time-of-occurrence of the daily maximum/minimum, the
preliminary product's "AS OF" cutoff hour, and the WMO correction marker.
First demonstrated exploratorily in E0001
(`docs/research/investigations/2026-07-22-e0001-chi-2026-mechanism.md`,
>=96% coverage over CHI/NYC 2023-2026); productionized here as the
prerequisite H0015's entry names before any pre-registration may build on
occurrence times.

Format variants handled (observed across the four archived stations):

- occurrence time with a colon (CHI/LAX): ``MAXIMUM   75   2:59 PM``
- compact occurrence time (NYC/DEN):      ``MAXIMUM   74    357 PM`` /
  ``MAXIMUM   67   1225 AM``
- record-tie suffix on the value:         ``MAXIMUM   100R  1:15 PM``
- missing data renders as ``MM`` (no time) -> fields are None
- correction suffix on the WMO header:    ``CDUS43 KLOT 152149 CCA``
- preliminary cutoff line:                ``AS OF 0400 PM LOCAL TIME.``

Values are parsed as integers (CLI temperature lines carry whole degrees);
occurrence hours are local wall-clock fractional hours in [0, 24) in the
product's own local time zone (no conversion is performed here).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MAX_RE = re.compile(r"MAXIMUM\s+(-?\d+)R?\s+(\d{1,2}):?(\d{2})\s*([AP]M)")
_MIN_RE = re.compile(r"MINIMUM\s+(-?\d+)R?\s+(\d{1,2}):?(\d{2})\s*([AP]M)")
_MAX_VALUE_ONLY_RE = re.compile(r"MAXIMUM\s+(-?\d+)R?\b")
_MIN_VALUE_ONLY_RE = re.compile(r"MINIMUM\s+(-?\d+)R?\b")
_HEADER_RE = re.compile(r"^C[A-Z]US\d\d\s+K[A-Z]{3}\s+\d{6}\s*([A-Z]{3})?\s*$", re.M)
_ASOF_RE = re.compile(r"AS OF (\d{1,2})(\d{2}) ([AP]M)")


def _to_hour(hour_12: int, minute: int, ampm: str) -> float:
    return hour_12 % 12 + (12 if ampm == "PM" else 0) + minute / 60


@dataclass(frozen=True, slots=True)
class CliProductFields:
    """Fields extracted from one CLI product text.

    ``None`` means the field was absent or unparseable in this product —
    callers must treat that as missing data, never as a value.
    """

    max_value: int | None
    max_occurrence_hour: float | None
    min_value: int | None
    min_occurrence_hour: float | None
    asof_hour: int | None
    corrected: bool


def parse_cli_product(text: str) -> CliProductFields:
    """Parse one CLI product's text. Never raises on malformed text —
    unparseable fields come back as None so completeness is measurable
    (and gateable) by the caller."""
    max_value: int | None = None
    max_hour: float | None = None
    min_value: int | None = None
    min_hour: float | None = None

    m = _MAX_RE.search(text)
    if m:
        max_value = int(m.group(1))
        max_hour = _to_hour(int(m.group(2)), int(m.group(3)), m.group(4))
    else:
        mv = _MAX_VALUE_ONLY_RE.search(text)
        if mv:
            max_value = int(mv.group(1))

    m = _MIN_RE.search(text)
    if m:
        min_value = int(m.group(1))
        min_hour = _to_hour(int(m.group(2)), int(m.group(3)), m.group(4))
    else:
        mv = _MIN_VALUE_ONLY_RE.search(text)
        if mv:
            min_value = int(mv.group(1))

    asof_hour: int | None = None
    m = _ASOF_RE.search(text)
    if m:
        asof_hour = int(m.group(1)) % 12 + (12 if m.group(3) == "PM" else 0)

    corrected = False
    m = _HEADER_RE.search(text)
    if m and m.group(1) is not None and m.group(1).startswith("CC"):
        corrected = True

    return CliProductFields(
        max_value=max_value,
        max_occurrence_hour=max_hour,
        min_value=min_value,
        min_occurrence_hour=min_hour,
        asof_hour=asof_hour,
        corrected=corrected,
    )
