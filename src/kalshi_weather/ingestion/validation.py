"""Validation for incoming Kalshi market data before it's persisted.

Keeps a single bad item from corrupting storage or crashing a collection
cycle: callers catch MalformedPayloadError per-item (see
ingestion/collector.py), log it, and skip that item -- the cycle continues.
"""

from datetime import UTC, datetime, timedelta

from kalshi_weather.domain.time import NaiveDatetimeError, to_utc, utc_now

#: Market-level prices (Market.yes_bid/yes_ask/...) legitimately sit at the
#: 0/100 boundary in live data (an empty/one-sided market), unlike
#: kalshi.orderbook's stricter 1-99 bound for individual resting bid levels.
MIN_MARKET_PRICE_CENTS = 0
MAX_MARKET_PRICE_CENTS = 100

DEFAULT_MAX_FUTURE_SKEW = timedelta(minutes=5)
#: A source timestamp before this is treated as corrupt rather than real --
#: Kalshi's weather markets didn't exist yet.
SANE_TIMESTAMP_FLOOR = datetime(2018, 1, 1, tzinfo=UTC)


class MalformedPayloadError(ValueError):
    """Raised when incoming data fails validation and must not be persisted."""


def validate_timestamp(
    value: datetime,
    *,
    field_name: str,
    max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW,
    floor: datetime = SANE_TIMESTAMP_FLOOR,
) -> datetime:
    """Validate a source timestamp: timezone-aware, not absurdly old, not
    further in the future than clock-skew tolerance allows. Returns the
    UTC-normalized value; raises MalformedPayloadError otherwise."""
    try:
        utc_value = to_utc(value)
    except NaiveDatetimeError as exc:
        raise MalformedPayloadError(f"{field_name}: {exc}") from exc

    if utc_value < floor:
        raise MalformedPayloadError(
            f"{field_name}={utc_value.isoformat()} is before the sane floor {floor.isoformat()}"
        )

    now = utc_now()
    if utc_value > now + max_future_skew:
        raise MalformedPayloadError(
            f"{field_name}={utc_value.isoformat()} is more than {max_future_skew} "
            f"in the future (now={now.isoformat()})"
        )

    return utc_value


def validate_price_cents(
    value: int | None,
    *,
    field_name: str,
    min_cents: int = MIN_MARKET_PRICE_CENTS,
    max_cents: int = MAX_MARKET_PRICE_CENTS,
) -> None:
    """Validate a market-level price in cents. `None` (unknown/absent) passes."""
    if value is None:
        return
    if not (min_cents <= value <= max_cents):
        raise MalformedPayloadError(f"{field_name}={value} out of range [{min_cents}, {max_cents}]")


def validate_ticker(value: str, *, field_name: str) -> None:
    """Validate a ticker/id string is non-empty after stripping whitespace."""
    if not value or not value.strip():
        raise MalformedPayloadError(f"{field_name} is empty")
