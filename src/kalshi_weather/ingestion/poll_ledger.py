"""Per-ticker polling evidence: deterministic taxonomy + buffered record.

The collector buffers one ``PollAttempt`` per logical ticker-endpoint decision
during a cycle, then writes them append-only WITH the cycle's ``collector_runs``
record (the run id only exists after the cycle). See ADR 0020. Nothing here reads
or mutates existing tables; historical rows are never backfilled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

#: Max stored error detail -- bounded and sanitized, never a full payload.
_MAX_DETAIL = 480


class EndpointType(StrEnum):
    MARKET_SNAPSHOT = "market_snapshot"
    ORDERBOOK = "orderbook"
    TRADES = "trades"
    SETTLEMENT = "settlement"
    METADATA_REVISION = "metadata_revision"


class EligibilityState(StrEnum):
    ELIGIBLE = "eligible"
    NOT_ELIGIBLE = "not_eligible"


class AttemptState(StrEnum):
    ATTEMPTED = "attempted"
    SKIPPED = "skipped"
    NOT_ATTEMPTED = "not_attempted"


class PollOutcome(StrEnum):
    SUCCEEDED_NEW_DATA = "succeeded_new_data"
    SUCCEEDED_UNCHANGED = "succeeded_unchanged"
    SUCCEEDED_EMPTY = "succeeded_empty"
    SKIPPED_NOT_ELIGIBLE = "skipped_not_eligible"
    SKIPPED_POLICY = "skipped_policy"
    RATE_LIMITED = "rate_limited"
    API_FAILURE = "api_failure"
    MALFORMED_PAYLOAD = "malformed_payload"
    PERSISTENCE_FAILURE = "persistence_failure"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"
    UNKNOWN_FAILURE = "unknown_failure"


#: Outcomes that PROVE the ticker was observed (even if it produced no new row).
OBSERVED_OUTCOMES = frozenset(
    {
        PollOutcome.SUCCEEDED_NEW_DATA,
        PollOutcome.SUCCEEDED_UNCHANGED,
        PollOutcome.SUCCEEDED_EMPTY,
    }
)
#: Outcomes that interrupt passive-fill continuity (attempted but not observed).
CONTINUITY_BREAKING_OUTCOMES = frozenset(
    {
        PollOutcome.RATE_LIMITED,
        PollOutcome.API_FAILURE,
        PollOutcome.MALFORMED_PAYLOAD,
        PollOutcome.PERSISTENCE_FAILURE,
        PollOutcome.UNKNOWN_FAILURE,
    }
)


def bounded_detail(text: str | None) -> str | None:
    """Bound and single-line an error string. Callers pass already-sanitized text
    (KalshiAPIError bodies are API error JSON, not credentials); this only guards
    length and newlines so a stray large body can never bloat the ledger."""
    if not text:
        return None
    flat = " ".join(text.split())
    return flat[:_MAX_DETAIL]


def classify_exception(exc: BaseException) -> tuple[PollOutcome, int | None, bool]:
    """Map a collector exception to (outcome, http_status, rate_limited)."""
    from sqlalchemy.exc import SQLAlchemyError

    from kalshi_weather.ingestion.validation import MalformedPayloadError
    from kalshi_weather.kalshi.client import KalshiAPIError
    from kalshi_weather.kalshi.orderbook import InvalidPriceLevelError

    if isinstance(exc, KalshiAPIError):
        status = getattr(exc, "status_code", None)
        if status == 429:
            return PollOutcome.RATE_LIMITED, status, True
        return PollOutcome.API_FAILURE, status, False
    # An out-of-range price level / negative quantity in an order-book payload is
    # malformed data, not an unclassified failure (both already break passive
    # continuity; this only sharpens the recorded label).
    if isinstance(exc, MalformedPayloadError | InvalidPriceLevelError):
        return PollOutcome.MALFORMED_PAYLOAD, None, False
    if isinstance(exc, SQLAlchemyError):
        return PollOutcome.PERSISTENCE_FAILURE, None, False
    return PollOutcome.UNKNOWN_FAILURE, None, False


@dataclass(frozen=True)
class PollAttempt:
    """A buffered polling-evidence record (no id / collector_run_id yet)."""

    ticker: str
    endpoint_type: EndpointType
    environment: str | None
    eligibility_state: EligibilityState
    attempt_state: AttemptState
    outcome: PollOutcome
    requested_at: datetime
    completed_at: datetime
    http_status: int | None = None
    retry_count: int = 0
    rate_limited: bool = False
    persisted_row_count: int = 0
    deduplicated: bool = False
    raw_payload_id: int | None = None
    error_class: str | None = None
    bounded_error_detail: str | None = None

    @classmethod
    def attempted(
        cls,
        *,
        ticker: str,
        endpoint_type: EndpointType,
        environment: str | None,
        outcome: PollOutcome,
        requested_at: datetime,
        completed_at: datetime,
        http_status: int | None = None,
        retry_count: int = 0,
        rate_limited: bool = False,
        persisted_row_count: int = 0,
        deduplicated: bool = False,
        raw_payload_id: int | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
    ) -> PollAttempt:
        return cls(
            ticker=ticker,
            endpoint_type=endpoint_type,
            environment=environment,
            eligibility_state=EligibilityState.ELIGIBLE,
            attempt_state=AttemptState.ATTEMPTED,
            outcome=outcome,
            requested_at=requested_at,
            completed_at=completed_at,
            http_status=http_status,
            retry_count=retry_count,
            rate_limited=rate_limited,
            persisted_row_count=persisted_row_count,
            deduplicated=deduplicated,
            raw_payload_id=raw_payload_id,
            error_class=error_class,
            bounded_error_detail=bounded_detail(error_detail),
        )
