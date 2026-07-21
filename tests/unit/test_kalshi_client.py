import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from kalshi_weather.config import Environment
from kalshi_weather.kalshi.client import KalshiAPIError, KalshiAuthError, KalshiClient

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kalshi"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


async def _no_sleep(*_args: Any, **_kwargs: Any) -> None:
    return None


def _client(handler: httpx.MockTransport, **kwargs: Any) -> KalshiClient:
    return KalshiClient(
        base_url="https://example.invalid/trade-api/v2",
        environment=Environment.DEVELOPMENT,
        transport=handler,
        **kwargs,
    )


async def test_list_series_parses_response_and_calls_raw_sink() -> None:
    captured: list[tuple[str, str, str, int, Any]] = []

    async def sink(
        source: str, endpoint: str, request_key: str, status: int, payload: Any
    ) -> None:
        captured.append((source, endpoint, request_key, status, payload))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/series")
        return httpx.Response(200, json=_load("series_list.json"))

    async with _client(
        httpx.MockTransport(handler), raw_payload_sink=sink
    ) as client:
        series = await client.list_series()

    assert [s.ticker for s in series] == ["KXHIGHNY", "KXFED"]
    assert len(captured) == 1
    assert captured[0][0] == "kalshi_rest"


async def test_get_orderbook_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/orderbook")
        return httpx.Response(200, json=_load("orderbook_full.json"))

    async with _client(httpx.MockTransport(handler)) as client:
        book = await client.get_orderbook("SOME-TICKER")

    assert book.orderbook.yes[0] == [42, 100]


async def test_authenticated_request_without_key_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not reach network for unauthenticated failure")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(KalshiAuthError):
            await client._request("GET", "/portfolio/balance", require_auth=True)


async def test_request_is_signed_automatically_when_credentials_present() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    from kalshi_weather.kalshi.auth import ACCESS_KEY_HEADER, ACCESS_SIGNATURE_HEADER

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json=_load("series_list.json"))

    async with _client(
        httpx.MockTransport(handler), key_id="test-key", private_key=private_key
    ) as client:
        await client.list_series()

    assert seen_headers.get(ACCESS_KEY_HEADER.lower()) == "test-key"
    assert ACCESS_SIGNATURE_HEADER.lower() in seen_headers


async def test_4xx_raises_immediately_without_retry() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(404, json={"error": "not found"})

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(KalshiAPIError) as exc_info:
            await client.get_market("MISSING-TICKER")

    assert exc_info.value.status_code == 404
    assert call_count == 1


async def test_5xx_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kalshi_weather.kalshi.client.asyncio.sleep", _no_sleep)
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=_load("market.json"))

    async with _client(httpx.MockTransport(handler), max_retries=5) as client:
        market = await client.get_market("KXHIGHNY-26JUL21-T85")

    assert call_count == 3
    assert market.ticker == "KXHIGHNY-26JUL21-T85"


async def test_5xx_exhausts_retries_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kalshi_weather.kalshi.client.asyncio.sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    async with _client(httpx.MockTransport(handler), max_retries=2) as client:
        with pytest.raises(KalshiAPIError):
            await client.get_market("ANY-TICKER")


async def test_timeout_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kalshi_weather.kalshi.client.asyncio.sleep", _no_sleep)
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json=_load("market.json"))

    async with _client(httpx.MockTransport(handler), max_retries=5) as client:
        market = await client.get_market("KXHIGHNY-26JUL21-T85")

    assert call_count == 2
    assert market.ticker == "KXHIGHNY-26JUL21-T85"


async def test_connection_error_exhausts_retries_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kalshi_weather.kalshi.client.asyncio.sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(httpx.MockTransport(handler), max_retries=2) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get_market("ANY-TICKER")


async def test_malformed_json_response_raises_clearly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not json {{{", headers={"content-type": "application/json"}
        )

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(json.JSONDecodeError):
            await client.get_market("ANY-TICKER")


async def test_unexpected_content_type_html_error_page_raises_clearly() -> None:
    """A proxy/gateway failure returning an HTML error page instead of JSON
    must not be silently misparsed -- it should fail loudly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html><body>502 Bad Gateway</body></html>")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(json.JSONDecodeError):
            await client.get_market("ANY-TICKER")


async def test_requests_are_throttled_to_min_interval() -> None:
    """Proactive spacing between requests -- confirmed live that firing
    requests back-to-back draws 429s well before any documented per-tier
    rate limit is exhausted (see docs/adr/0002-ingestion-collector.md)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("series_list.json"))

    interval = 0.05
    async with _client(
        httpx.MockTransport(handler), min_request_interval_seconds=interval
    ) as client:
        started = time.monotonic()
        await client.list_series()
        await client.list_series()
        await client.list_series()
        elapsed = time.monotonic() - started

    # 3 requests -> 2 gaps of at least `interval` each
    assert elapsed >= interval * 2


async def test_zero_min_interval_disables_throttling() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("series_list.json"))

    async with _client(
        httpx.MockTransport(handler), min_request_interval_seconds=0
    ) as client:
        started = time.monotonic()
        for _ in range(5):
            await client.list_series()
        elapsed = time.monotonic() - started

    assert elapsed < 0.1  # no artificial spacing introduced
