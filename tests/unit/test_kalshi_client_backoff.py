"""Reactive-backoff correctness for KalshiClient (rate-limit audit 2026-07-26):
Retry-After handling, bounded jittered exponential fallback, retry bounds, and
that a 429 resumes correctly. Uses a fake sleep so no test actually waits.
"""

from datetime import timedelta

import httpx
import pytest

from kalshi_weather.config import Environment
from kalshi_weather.domain.time import utc_now
from kalshi_weather.kalshi import client as client_mod
from kalshi_weather.kalshi.client import KalshiAPIError, KalshiClient, _parse_retry_after

PROD = "https://api.elections.kalshi.com/trade-api/v2"


@pytest.fixture
def fake_sleep(monkeypatch):  # type: ignore[no-untyped-def]
    """Record sleep durations instead of waiting."""
    waits: list[float] = []

    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(client_mod.asyncio, "sleep", _sleep)
    return waits


def _client(handler, **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("backoff_jitter", 0.0)  # deterministic unless a test overrides
    return KalshiClient(
        base_url=PROD,
        environment=Environment.PRODUCTION,
        transport=httpx.MockTransport(handler),
        min_request_interval_seconds=0,
        **kw,
    )


# --- Retry-After parsing ----------------------------------------------------


def test_parse_retry_after_seconds() -> None:
    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("nonsense") is None


def test_parse_retry_after_http_date() -> None:
    future = utc_now() + timedelta(seconds=30)
    header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    secs = _parse_retry_after(header, now=utc_now())
    assert secs is not None and 25 <= secs <= 31
    # a past date yields 0, never negative
    past = (utc_now() - timedelta(seconds=60)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert _parse_retry_after(past, now=utc_now()) == 0.0


# --- backoff behavior -------------------------------------------------------


async def test_retry_after_seconds_is_respected(fake_sleep) -> None:  # type: ignore[no-untyped-def]
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, text="slow down")
        return httpx.Response(200, json={"markets": [], "cursor": ""})

    async with _client(handler) as c:
        await c.list_markets()
    # waited exactly the server-directed 3s (jitter 0), not the 0.5 exponential
    assert fake_sleep == [3.0]
    assert c.rate_limit_hits == 1
    assert c.total_backoff_seconds == 3.0


async def test_missing_retry_after_uses_bounded_exponential(fake_sleep) -> None:  # type: ignore[no-untyped-def]
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="no header")  # always 429, no Retry-After

    async with _client(
        handler, max_retries=4, backoff_base_seconds=0.5, backoff_max_seconds=8.0
    ) as c:
        with pytest.raises(KalshiAPIError):
            await c.list_markets()
    # attempts 0..3 -> 0.5, 1, 2, ... but the LAST attempt does not sleep-then-retry
    # (loop exhausts). Waits are for attempts that continue: 0.5, 1.0, 2.0
    assert fake_sleep == [0.5, 1.0, 2.0]
    assert c.rate_limit_hits == 4


async def test_exponential_is_capped(fake_sleep) -> None:  # type: ignore[no-untyped-def]
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    async with _client(
        handler, max_retries=6, backoff_base_seconds=1.0, backoff_max_seconds=4.0
    ) as c:
        with pytest.raises(KalshiAPIError):
            await c.list_markets()
    # 1, 2, 4, 4, 4 -- capped at 4.0
    assert fake_sleep == [1.0, 2.0, 4.0, 4.0, 4.0]


async def test_retry_after_capped_at_max(fake_sleep) -> None:  # type: ignore[no-untyped-def]
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "100"}, text="wait long")

    async with _client(handler, max_retries=2, backoff_max_seconds=8.0) as c:
        with pytest.raises(KalshiAPIError):
            await c.list_markets()
    assert fake_sleep == [8.0]  # 100s hint capped at the max


async def test_repeated_429_stops_at_retry_limit(fake_sleep) -> None:  # type: ignore[no-untyped-def]
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="always")

    async with _client(handler, max_retries=3) as c:
        with pytest.raises(KalshiAPIError):
            await c.list_markets()
    assert calls["n"] == 3  # exactly max_retries attempts, then raise


async def test_non_retryable_4xx_is_not_retried(fake_sleep) -> None:  # type: ignore[no-untyped-def]
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    async with _client(handler) as c:
        with pytest.raises(KalshiAPIError):
            await c.get_market("NOPE")
    assert calls["n"] == 1  # 404 raised immediately, no retry
    assert fake_sleep == []  # never backed off
    assert c.rate_limit_hits == 0


async def test_429_then_success_returns_data(fake_sleep) -> None:  # type: ignore[no-untyped-def]
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"market": {"ticker": "T", "status": "open"}})

    async with _client(handler) as c:
        m = await c.get_market("T")
    assert m.ticker == "T"  # resumes and returns the payload after the 429


async def test_jitter_is_bounded_and_one_sided_for_hints(fake_sleep) -> None:  # type: ignore[no-untyped-def]
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "2"})

    async with _client(handler, max_retries=2, backoff_jitter=0.25) as c:
        with pytest.raises(KalshiAPIError):
            await c.list_markets()
    # server hint 2s + one-sided jitter [0, 0.5]: never less than directed
    assert len(fake_sleep) == 1  # 2 attempts -> one backoff before the final give-up
    assert 2.0 <= fake_sleep[0] <= 2.5


# --- config validation ------------------------------------------------------


def test_invalid_backoff_config_fails_at_startup() -> None:
    from pydantic import ValidationError

    from kalshi_weather.config import Settings

    for bad in (
        {"KALSHI_BACKOFF_BASE_SECONDS": "0"},
        {"KALSHI_BACKOFF_MAX_SECONDS": "0.1", "KALSHI_BACKOFF_BASE_SECONDS": "0.5"},
        {"KALSHI_BACKOFF_JITTER": "1.5"},
        {"KALSHI_BACKOFF_JITTER": "-0.1"},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **bad)  # type: ignore[arg-type]
    # a valid config constructs fine
    ok = Settings(
        _env_file=None,
        KALSHI_BACKOFF_BASE_SECONDS="0.5",
        KALSHI_BACKOFF_MAX_SECONDS="8",
        KALSHI_BACKOFF_JITTER="0.25",
    )  # type: ignore[arg-type]
    assert ok.kalshi_backoff_max_seconds == 8.0
