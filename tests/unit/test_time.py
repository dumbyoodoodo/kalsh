from datetime import UTC, datetime

import pytest

from kalshi_weather.domain.time import NaiveDatetimeError, parse_iso8601_utc, to_utc, utc_now


def test_utc_now_is_timezone_aware() -> None:
    assert utc_now().tzinfo is not None


def test_to_utc_rejects_naive_datetime() -> None:
    with pytest.raises(NaiveDatetimeError):
        to_utc(datetime(2026, 1, 1))


def test_to_utc_converts_aware_datetime() -> None:
    eastern_offset = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert to_utc(eastern_offset) == eastern_offset


def test_parse_iso8601_utc_with_z_suffix() -> None:
    parsed = parse_iso8601_utc("2026-07-20T12:00:00Z")
    assert parsed == datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def test_parse_iso8601_utc_rejects_naive_string() -> None:
    with pytest.raises(NaiveDatetimeError):
        parse_iso8601_utc("2026-07-20T12:00:00")
