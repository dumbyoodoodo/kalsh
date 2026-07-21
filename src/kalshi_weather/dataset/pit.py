"""Point-in-time helpers for building leakage-free research datasets.

The whole point of this module is to make "what was known at time t" an
explicit, testable computation rather than an accident of how a join was
written. Two ideas recur:

- **Target date derivation**: a forecast/observation describes a *local*
  calendar day at a station, so the calendar day it belongs to must be
  computed in the station's own timezone, never UTC (an 8pm EDT reading is
  still "today" locally but "tomorrow" in UTC).
- **As-of correctness**: a row attached to a decision at time ``t`` may only
  use source rows whose *knowledge timestamp* (a forecast's ``issue_time``, an
  observation's ``issuance_time``) is ``<= t``. A later revision is invisible
  at ``t`` -- see RESEARCH.md "Avoiding look-ahead bias".
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from kalshi_weather.domain.time import to_utc


def local_date(instant: datetime, timezone: str) -> date:
    """Calendar date of ``instant`` in the given IANA timezone.

    Raises NaiveDatetimeError (via to_utc) if ``instant`` is naive -- a naive
    timestamp has no defined instant and must never enter a point-in-time
    dataset.
    """
    return to_utc(instant).astimezone(ZoneInfo(timezone)).date()


def lead_seconds(*, issue_time: datetime, target: datetime) -> float:
    """Seconds between a forecast's issue time and the instant it targets.

    Positive means the forecast was issued *before* the target (the normal
    case: a real forecast). Negative would mean it was issued after the thing
    it forecasts -- a leakage red flag the validation layer checks for.
    """
    return (to_utc(target) - to_utc(issue_time)).total_seconds()
