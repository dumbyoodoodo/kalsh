from datetime import UTC, datetime

import pytest

from kalshi_weather.ingestion.validation import (
    WEATHER_TIMESTAMP_FLOOR,
    MalformedPayloadError,
    validate_temperature_f,
    validate_timestamp,
)


@pytest.mark.parametrize("value", [-60, -12, 0, 32, 81, 130])
def test_validate_temperature_f_accepts_plausible_range(value: int) -> None:
    validate_temperature_f(value, field_name="tmax_f")  # should not raise


@pytest.mark.parametrize("value", [-61, 131, 500, -200])
def test_validate_temperature_f_rejects_implausible_values(value: int) -> None:
    with pytest.raises(MalformedPayloadError):
        validate_temperature_f(value, field_name="tmax_f")


def test_validate_temperature_f_respects_custom_bounds() -> None:
    with pytest.raises(MalformedPayloadError):
        validate_temperature_f(105, field_name="tmax_f", min_f=-20, max_f=100)
    validate_temperature_f(99, field_name="tmax_f", min_f=-20, max_f=100)


def test_weather_timestamp_floor_allows_older_dates_than_kalshi_floor() -> None:
    # NWS text products realistically go back to the early 1980s -- older
    # than Kalshi's own SANE_TIMESTAMP_FLOOR (2018), which is specific to
    # when Kalshi weather markets existed.
    value = datetime(1995, 6, 1, tzinfo=UTC)
    result = validate_timestamp(value, field_name="issuance_time", floor=WEATHER_TIMESTAMP_FLOOR)
    assert result == value


def test_weather_timestamp_floor_still_rejects_absurdly_old_dates() -> None:
    with pytest.raises(MalformedPayloadError):
        validate_timestamp(
            datetime(1900, 1, 1, tzinfo=UTC),
            field_name="issuance_time",
            floor=WEATHER_TIMESTAMP_FLOOR,
        )
