"""Read-only Kalshi market-data client.

Public production market-data endpoints work without credentials. Demo
(and any authenticated) requests are signed via `kalshi.auth`. No
order-submission method exists anywhere in this client -- that is out of
scope until a later milestone and explicit human approval per CLAUDE.md.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_weather.config import Environment
from kalshi_weather.kalshi import auth
from kalshi_weather.kalshi.models import (
    Candlestick,
    CandlestickListResponse,
    Event,
    EventListResponse,
    Market,
    MarketListResponse,
    MarketResponse,
    OrderbookResponse,
    Series,
    SeriesListResponse,
    Trade,
    TradeListResponse,
)
from kalshi_weather.kalshi.pagination import paginate
from kalshi_weather.kalshi.provenance import resolve_kalshi_environment
from kalshi_weather.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
#: Kalshi's candlestick endpoint rejects a request spanning more than 5,000
#: candles at the requested resolution (observed live: a 5,760-minute window
#: at period_interval=1 returned "max candlesticks: 5000" --
#: docs/research/investigations/INV-20260721-price-history-recovery.md).
#: Kept below the observed cap for headroom; only bites at period_interval=1
#: over windows longer than ~3.4 days.
MAX_CANDLES_PER_REQUEST = 4900
#: Minimum spacing between consecutive requests on one client, matching the
#: 100ms threshold in Kalshi's own kalshi-starter-code-python reference
#: client. Confirmed live that firing requests back-to-back with no spacing
#: draws 429s well before any documented per-tier limit is exhausted (see
#: docs/adr/0002-ingestion-collector.md) -- this is a proactive throttle, not
#: just reactive retry-on-429 backoff.
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 0.1

#: Called with (source, endpoint, request_key, http_status, payload_json) for
#: every response received, before schema validation. Used to persist raw
#: payloads immutably per the append-only raw_api_payloads table. Returns the
#: persisted row's id so callers can link normalized records back to it.
RawPayloadSink = Callable[[str, str, str, int, Any], Awaitable[int]]


class KalshiAuthError(RuntimeError):
    """Raised when an authenticated endpoint is called without signing credentials."""


class KalshiAPIError(RuntimeError):
    """Raised when the Kalshi API returns a non-retryable error response."""

    def __init__(self, status_code: int, body: Any) -> None:
        super().__init__(f"Kalshi API error {status_code}: {body!r}")
        self.status_code = status_code
        self.body = body


class KalshiClient:
    """Async client for read-only Kalshi market-data endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        environment: Environment,
        key_id: str | None = None,
        private_key: rsa.RSAPrivateKey | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_request_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
        raw_payload_sink: RawPayloadSink | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._environment = environment
        self._key_id = key_id
        self._private_key = private_key
        self._max_retries = max_retries
        self._min_request_interval_seconds = min_request_interval_seconds
        self._last_request_at: float | None = None
        self._raw_payload_sink = raw_payload_sink
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
        )
        # Kalshi signs the FULL request path including the "/trade-api/v2"
        # prefix, not just the path relative to base_url (confirmed against
        # the official Kalshi/kalshi-starter-code-python reference client;
        # see docs/API_VERIFICATION.md).
        self._signing_path_prefix = self._http.base_url.path.rstrip("/")
        #: id of the raw_api_payloads row for the most recently completed
        #: request, if a raw_payload_sink is configured. Read this
        #: immediately after an `await client.xxx(...)` call to link a
        #: normalized record back to its raw payload -- it is overwritten by
        #: the next request on this client, so it is not safe to read after
        #: later concurrent/overlapping calls.
        self.last_raw_payload_id: int | None = None
        #: Operational counters for this client instance's lifetime --
        #: read by the collector loop when recording per-cycle metrics.
        self.requests_attempted: int = 0
        self.retries: int = 0

    @property
    def source_environment(self) -> str:
        """Canonical provenance (`demo`/`production`/`unknown`) for data this
        client produces, resolved from its effective base URL (ADR 0013).
        Write paths stamp rows with this so demo- and production-sourced data
        stay distinguishable."""
        return resolve_kalshi_environment(str(self._http.base_url)).value

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers_for(self, method: str, path: str, *, require_auth: bool) -> dict[str, str]:
        """Sign the request whenever credentials are configured (needed for demo);
        otherwise send anonymous headers (works for public production market data).
        `require_auth` only controls whether missing credentials are an error.
        """
        if self._key_id is not None and self._private_key is not None:
            signed = auth.sign_request(
                key_id=self._key_id,
                private_key=self._private_key,
                method=method,
                path=f"{self._signing_path_prefix}{path}",
            )
            return signed.as_headers()
        if require_auth:
            raise KalshiAuthError(
                "authenticated request attempted without key_id/private_key configured"
            )
        return {}

    async def _throttle(self) -> None:
        """Proactively space out requests by at least min_request_interval_seconds."""
        if self._min_request_interval_seconds <= 0:
            return
        now = time.monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            remaining = self._min_request_interval_seconds - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_request_at = time.monotonic()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        require_auth: bool = False,
    ) -> Any:
        headers = self._headers_for(method, path, require_auth=require_auth)

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            await self._throttle()
            self.requests_attempted += 1
            if attempt > 0:
                self.retries += 1
            try:
                response = await self._http.request(method, path, params=params, headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
                await asyncio.sleep(DEFAULT_BACKOFF_BASE_SECONDS * (2**attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = KalshiAPIError(response.status_code, response.text)
                await asyncio.sleep(DEFAULT_BACKOFF_BASE_SECONDS * (2**attempt))
                continue

            if response.status_code >= 400:
                raise KalshiAPIError(response.status_code, response.text)

            payload = response.json()
            if self._raw_payload_sink is not None:
                request_key = f"{method}:{path}:{params or {}}"
                self.last_raw_payload_id = await self._raw_payload_sink(
                    "kalshi_rest", path, request_key, response.status_code, payload
                )
            return payload

        assert last_error is not None
        raise last_error

    async def list_series(self, *, category: str | None = None) -> list[Series]:
        async def fetch_page(cursor: str | None) -> tuple[list[Series], str | None]:
            params: dict[str, Any] = {}
            if category is not None:
                params["category"] = category
            if cursor is not None:
                params["cursor"] = cursor
            payload = await self._request("GET", "/series", params=params)
            parsed = SeriesListResponse.model_validate(payload)
            return parsed.series, parsed.cursor

        return [item async for item in paginate(fetch_page)]

    async def list_events(
        self, *, series_ticker: str | None = None, status: str | None = None
    ) -> list[Event]:
        """List events, optionally filtered to a series and/or status.

        `status` matters in practice: an unfiltered call returns every event
        a recurring series has ever had (confirmed live -- one weather series
        alone had 185 events with no filter vs. 2 with `status="open"`; see
        docs/API_VERIFICATION.md addendum / docs/adr/0002-ingestion-collector.md).
        """

        async def fetch_page(cursor: str | None) -> tuple[list[Event], str | None]:
            params: dict[str, Any] = {}
            if series_ticker is not None:
                params["series_ticker"] = series_ticker
            if status is not None:
                params["status"] = status
            if cursor is not None:
                params["cursor"] = cursor
            payload = await self._request("GET", "/events", params=params)
            parsed = EventListResponse.model_validate(payload)
            return parsed.events, parsed.cursor

        return [item async for item in paginate(fetch_page)]

    async def list_markets(
        self, *, event_ticker: str | None = None, status: str | None = None
    ) -> list[Market]:
        async def fetch_page(cursor: str | None) -> tuple[list[Market], str | None]:
            params: dict[str, Any] = {}
            if event_ticker is not None:
                params["event_ticker"] = event_ticker
            if status is not None:
                params["status"] = status
            if cursor is not None:
                params["cursor"] = cursor
            payload = await self._request("GET", "/markets", params=params)
            parsed = MarketListResponse.model_validate(payload)
            for rejected in parsed.rejected_items:
                # Structured parsing-failure record; the raw payload was
                # already persisted verbatim by the sink before validation.
                logger.warning(
                    "kalshi.market_item_rejected",
                    endpoint="/markets",
                    index=rejected.get("index"),
                    ticker=(rejected.get("item") or {}).get("ticker"),
                    error=str(rejected.get("error"))[:300],
                    raw_payload_id=self.last_raw_payload_id,
                )
            return parsed.markets, parsed.cursor

        return [item async for item in paginate(fetch_page)]

    async def get_market(self, ticker: str) -> Market:
        payload = await self._request("GET", f"/markets/{ticker}")
        return MarketResponse.model_validate(payload).market

    async def get_orderbook(self, ticker: str, *, depth: int | None = None) -> OrderbookResponse:
        params = {"depth": depth} if depth is not None else None
        payload = await self._request("GET", f"/markets/{ticker}/orderbook", params=params)
        return OrderbookResponse.model_validate(payload)

    async def list_trades(
        self, *, ticker: str | None = None, min_ts: int | None = None
    ) -> list[Trade]:
        """List trades, optionally filtered to a market and/or a minimum Unix
        timestamp (seconds) -- `min_ts` lets incremental collection avoid
        re-fetching a market's entire trade history on every poll."""

        async def fetch_page(cursor: str | None) -> tuple[list[Trade], str | None]:
            params: dict[str, Any] = {}
            if ticker is not None:
                params["ticker"] = ticker
            if min_ts is not None:
                params["min_ts"] = min_ts
            if cursor is not None:
                params["cursor"] = cursor
            payload = await self._request("GET", "/markets/trades", params=params)
            parsed = TradeListResponse.model_validate(payload)
            for rejected in parsed.rejected_items:
                logger.warning(
                    "kalshi.trade_item_rejected",
                    endpoint="/markets/trades",
                    index=rejected.get("index"),
                    trade_id=(rejected.get("item") or {}).get("trade_id"),
                    error=str(rejected.get("error"))[:300],
                    raw_payload_id=self.last_raw_payload_id,
                )
            return parsed.trades, parsed.cursor

        return [item async for item in paginate(fetch_page)]

    async def list_candlesticks(
        self,
        *,
        series_ticker: str,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int,
    ) -> list[Candlestick]:
        """Fetch OHLC candlestick history for one market across
        [start_ts, end_ts] (Unix seconds, inclusive), at `period_interval`
        minutes (1, 60, and 1440 confirmed live -- see the investigation
        report). The endpoint has no cursor of its own, so a range wider than
        `MAX_CANDLES_PER_REQUEST` candles is split into consecutive,
        non-overlapping sub-requests here -- this is range-chunking, not the
        cursor-based `paginate()` helper used elsewhere, because Kalshi
        provides no continuation token for this endpoint."""
        if start_ts > end_ts:
            raise ValueError(f"start_ts {start_ts} is after end_ts {end_ts}")
        max_span_seconds = MAX_CANDLES_PER_REQUEST * period_interval * 60

        candles: list[Candlestick] = []
        chunk_start = start_ts
        while chunk_start <= end_ts:
            chunk_end = min(chunk_start + max_span_seconds, end_ts)
            payload = await self._request(
                "GET",
                f"/series/{series_ticker}/markets/{ticker}/candlesticks",
                params={
                    "start_ts": chunk_start,
                    "end_ts": chunk_end,
                    "period_interval": period_interval,
                },
            )
            parsed = CandlestickListResponse.model_validate(payload)
            candles.extend(parsed.candlesticks)
            chunk_start = chunk_end + 1
        return candles
