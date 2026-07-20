"""Read-only Kalshi market-data client.

Public production market-data endpoints work without credentials. Demo
(and any authenticated) requests are signed via `kalshi.auth`. No
order-submission method exists anywhere in this client -- that is out of
scope until a later milestone and explicit human approval per CLAUDE.md.
"""

import asyncio
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_weather.config import Environment
from kalshi_weather.kalshi import auth
from kalshi_weather.kalshi.models import (
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

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5

#: Called with (source, endpoint, request_key, http_status, payload_json) for
#: every response received, before schema validation. Used to persist raw
#: payloads immutably per the append-only raw_api_payloads table.
RawPayloadSink = Callable[[str, str, str, int, Any], Awaitable[None]]


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
        raw_payload_sink: RawPayloadSink | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._environment = environment
        self._key_id = key_id
        self._private_key = private_key
        self._max_retries = max_retries
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
                await self._raw_payload_sink(
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

    async def list_events(self, *, series_ticker: str | None = None) -> list[Event]:
        async def fetch_page(cursor: str | None) -> tuple[list[Event], str | None]:
            params: dict[str, Any] = {}
            if series_ticker is not None:
                params["series_ticker"] = series_ticker
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
            return parsed.markets, parsed.cursor

        return [item async for item in paginate(fetch_page)]

    async def get_market(self, ticker: str) -> Market:
        payload = await self._request("GET", f"/markets/{ticker}")
        return MarketResponse.model_validate(payload).market

    async def get_orderbook(self, ticker: str, *, depth: int | None = None) -> OrderbookResponse:
        params = {"depth": depth} if depth is not None else None
        payload = await self._request("GET", f"/markets/{ticker}/orderbook", params=params)
        return OrderbookResponse.model_validate(payload)

    async def list_trades(self, *, ticker: str | None = None) -> list[Trade]:
        async def fetch_page(cursor: str | None) -> tuple[list[Trade], str | None]:
            params: dict[str, Any] = {}
            if ticker is not None:
                params["ticker"] = ticker
            if cursor is not None:
                params["cursor"] = cursor
            payload = await self._request("GET", "/markets/trades", params=params)
            parsed = TradeListResponse.model_validate(payload)
            return parsed.trades, parsed.cursor

        return [item async for item in paginate(fetch_page)]
