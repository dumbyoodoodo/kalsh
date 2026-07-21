from datetime import UTC, datetime, timedelta

import pytest

from kalshi_weather.ingestion.validation import (
    MalformedPayloadError,
    validate_price_cents,
    validate_ticker,
    validate_timestamp,
)


def test_validate_timestamp_accepts_valid_aware_datetime() -> None:
    value = datetime(2026, 7, 20, tzinfo=UTC)
    assert validate_timestamp(value, field_name="t") == value


def test_validate_timestamp_normalizes_non_utc_to_utc() -> None:
    from datetime import timezone

    eastern = timezone(timedelta(hours=-5))
    value = datetime(2026, 7, 20, 12, 0, tzinfo=eastern)
    result = validate_timestamp(value, field_name="t")
    assert result.tzinfo == UTC
    assert result == value


def test_validate_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(MalformedPayloadError):
        validate_timestamp(datetime(2026, 7, 20), field_name="t")


def test_validate_timestamp_rejects_too_old() -> None:
    with pytest.raises(MalformedPayloadError):
        validate_timestamp(datetime(2000, 1, 1, tzinfo=UTC), field_name="t")


def test_validate_timestamp_rejects_far_future() -> None:
    far_future = datetime.now(UTC) + timedelta(days=3650)
    with pytest.raises(MalformedPayloadError):
        validate_timestamp(far_future, field_name="t")


def test_validate_timestamp_allows_small_future_skew() -> None:
    slightly_ahead = datetime.now(UTC) + timedelta(seconds=30)
    # should not raise -- within default 5-minute clock-skew tolerance
    validate_timestamp(slightly_ahead, field_name="t")


def test_validate_timestamp_respects_custom_skew_tolerance() -> None:
    ahead = datetime.now(UTC) + timedelta(minutes=10)
    with pytest.raises(MalformedPayloadError):
        validate_timestamp(ahead, field_name="t", max_future_skew=timedelta(minutes=5))
    validate_timestamp(ahead, field_name="t", max_future_skew=timedelta(minutes=15))


@pytest.mark.parametrize("value", [0, 1, 50, 99, 100])
def test_validate_price_cents_accepts_full_market_range(value: int) -> None:
    validate_price_cents(value, field_name="p")  # should not raise


@pytest.mark.parametrize("value", [-1, 101, 1000])
def test_validate_price_cents_rejects_out_of_range(value: int) -> None:
    with pytest.raises(MalformedPayloadError):
        validate_price_cents(value, field_name="p")


def test_validate_price_cents_allows_none() -> None:
    validate_price_cents(None, field_name="p")  # should not raise


def test_validate_price_cents_respects_custom_bounds() -> None:
    with pytest.raises(MalformedPayloadError):
        validate_price_cents(0, field_name="p", min_cents=1, max_cents=99)
    validate_price_cents(1, field_name="p", min_cents=1, max_cents=99)


def test_validate_ticker_accepts_nonempty() -> None:
    validate_ticker("KXHIGHNY", field_name="ticker")  # should not raise


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_validate_ticker_rejects_empty_or_whitespace(value: str) -> None:
    with pytest.raises(MalformedPayloadError):
        validate_ticker(value, field_name="ticker")
