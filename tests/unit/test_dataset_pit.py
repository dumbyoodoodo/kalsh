from datetime import UTC, date, datetime

import pytest

from kalshi_weather.dataset.pit import lead_seconds, local_date
from kalshi_weather.domain.time import NaiveDatetimeError

NY = "America/New_York"


def test_local_date_uses_station_timezone_not_utc() -> None:
    # 2026-07-20 23:30 EDT is 2026-07-21 03:30 UTC -- still "the 20th" locally.
    instant = datetime(2026, 7, 21, 3, 30, tzinfo=UTC)
    assert local_date(instant, NY) == date(2026, 7, 20)


def test_local_date_after_local_midnight_rolls_over() -> None:
    # 2026-07-21 00:30 EDT is 2026-07-21 04:30 UTC -> the 21st locally.
    instant = datetime(2026, 7, 21, 4, 30, tzinfo=UTC)
    assert local_date(instant, NY) == date(2026, 7, 21)


def test_local_date_rejects_naive() -> None:
    with pytest.raises(NaiveDatetimeError):
        local_date(datetime(2026, 7, 20, 12, 0), NY)


def test_lead_seconds_positive_when_issued_before_target() -> None:
    issue = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    target = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    assert lead_seconds(issue_time=issue, target=target) == 2 * 86400


def test_lead_seconds_negative_when_issued_after_target() -> None:
    issue = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    target = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    assert lead_seconds(issue_time=issue, target=target) < 0
