"""Candlestick client + model tests (Phase 7A).

Uses a real-shaped fixture (tests/fixtures/kalshi/candlesticks.json) captured
from the live investigation, including a zero-volume period whose `price`
block carries only `previous_dollars` -- the exact case that broke the
investigation's throwaway probe script (INV-20260721)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from kalshi_weather.config import Environment
from kalshi_weather.kalshi.client import (
    MAX_CANDLES_PER_REQUEST,
    KalshiAPIError,
    KalshiClient,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kalshi"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _client(handler: httpx.MockTransport, **kwargs: Any) -> KalshiClient:
    return KalshiClient(
        base_url="https://example.invalid/trade-api/v2",
        environment=Environment.DEVELOPMENT,
        transport=handler,
        min_request_interval_seconds=0,
        **kwargs,
    )


async def test_list_candlesticks_parses_normal_and_zero_volume_periods() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            "/series/KXHIGHNY/markets/KXHIGHNY-26MAY15-B65.5/candlesticks"
        )
        return httpx.Response(200, json=_load("candlesticks.json"))

    async with _client(httpx.MockTransport(handler)) as client:
        candles = await client.list_candlesticks(
            series_ticker="KXHIGHNY",
            ticker="KXHIGHNY-26MAY15-B65.5",
            start_ts=1778800000,
            end_ts=1778810000,
            period_interval=60,
        )

    assert len(candles) == 2
    traded, quiet = candles

    # normal period: full OHLC on price/yes_bid/yes_ask, dollars -> cents
    assert traded.end_period_ts == 1778803200
    assert traded.volume == 274
    assert traded.open_interest == 2214
    assert traded.price.open_cents == 28
    assert traded.price.close_cents == 30
    assert traded.price.mean_cents == 27
    assert traded.yes_bid.close_cents == 27
    assert traded.yes_ask.close_cents == 30

    # zero-volume period: price.close_cents absent, previous_cents present;
    # yes_bid/yes_ask still fully populated (quotes persist with no trades)
    assert quiet.volume == 0
    assert quiet.price.close_cents is None
    assert quiet.price.previous_cents == 30
    assert quiet.yes_bid.close_cents == 27
    assert quiet.yes_ask.close_cents == 30


async def test_list_candlesticks_chunks_ranges_exceeding_the_candle_cap() -> None:
    """At period_interval=1, a window wider than MAX_CANDLES_PER_REQUEST
    minutes must be split into consecutive, non-overlapping sub-requests."""
    seen_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, json={"candlesticks": [], "ticker": "T"})

    start_ts = 0
    end_ts = (MAX_CANDLES_PER_REQUEST * 60) + 120  # a bit over one chunk's span

    async with _client(httpx.MockTransport(handler)) as client:
        await client.list_candlesticks(
            series_ticker="KXHIGHNY",
            ticker="T",
            start_ts=start_ts,
            end_ts=end_ts,
            period_interval=1,
        )

    assert len(seen_params) == 2
    first, second = seen_params
    assert int(first["start_ts"]) == start_ts
    first_end = int(first["end_ts"])
    assert int(second["start_ts"]) == first_end + 1  # no overlap, no gap
    assert int(second["end_ts"]) == end_ts
    # each chunk must respect the observed 5,000-candle cap
    assert (first_end - start_ts) <= MAX_CANDLES_PER_REQUEST * 60


async def test_list_candlesticks_single_request_when_within_cap() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"candlesticks": [], "ticker": "T"})

    async with _client(httpx.MockTransport(handler)) as client:
        await client.list_candlesticks(
            series_ticker="KXHIGHNY", ticker="T", start_ts=0, end_ts=3600, period_interval=60
        )

    assert calls == 1


async def test_list_candlesticks_rejects_inverted_range() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        with pytest.raises(ValueError, match="after end_ts"):
            await client.list_candlesticks(
                series_ticker="KXHIGHNY", ticker="T", start_ts=100, end_ts=1, period_interval=60
            )


async def test_list_candlesticks_expired_market_raises_typed_404() -> None:
    """A market that has aged out of the retention window (INV-20260721)
    returns 404 -- must surface as the existing typed KalshiAPIError with an
    inspectable status_code, not a generic exception, so callers can
    classify it as EXPIRED without string-matching."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    async with _client(httpx.MockTransport(handler), max_retries=1) as client:
        with pytest.raises(KalshiAPIError) as exc_info:
            await client.list_candlesticks(
                series_ticker="KXHIGHNY", ticker="OLD-TICKER", start_ts=0, end_ts=60,
                period_interval=60,
            )
    assert exc_info.value.status_code == 404


async def test_list_candlesticks_calls_raw_payload_sink() -> None:
    captured: list[tuple[str, str]] = []

    async def sink(
        source: str, endpoint: str, request_key: str, status: int, payload: Any
    ) -> int:
        captured.append((source, endpoint))
        return 42

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("candlesticks.json"))

    async with _client(httpx.MockTransport(handler), raw_payload_sink=sink) as client:
        await client.list_candlesticks(
            series_ticker="KXHIGHNY", ticker="T", start_ts=0, end_ts=60, period_interval=60
        )
        assert client.last_raw_payload_id == 42

    assert captured == [("kalshi_rest", "/series/KXHIGHNY/markets/T/candlesticks")]
