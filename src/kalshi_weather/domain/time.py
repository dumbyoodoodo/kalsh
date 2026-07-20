"""Timezone-aware UTC timestamp helpers. Never use naive datetimes internally."""

from datetime import UTC, datetime


class NaiveDatetimeError(ValueError):
    """Raised when a naive (timezone-less) datetime is passed where UTC is required."""


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC. Rejects naive datetimes."""
    if value.tzinfo is None:
        raise NaiveDatetimeError(f"naive datetime not allowed: {value!r}")
    return value.astimezone(UTC)


def parse_iso8601_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp string into a timezone-aware UTC datetime."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise NaiveDatetimeError(f"naive timestamp string not allowed: {value!r}")
    return parsed.astimezone(UTC)
