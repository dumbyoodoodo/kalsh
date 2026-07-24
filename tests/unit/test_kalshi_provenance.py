"""Kalshi source-environment provenance (ADR 0013).

The resolver maps a client's effective base URL to the canonical stored value
(demo/production/unknown). Host matching is structural (parsed hostname), so it
cannot be spoofed by a substring elsewhere in the URL -- the tests below pin
that explicitly, because a naive `"demo-api.kalshi.co" in url` check would
mislabel `https://evil.com/?x=demo-api.kalshi.co` as demo.
"""

import pytest

from kalshi_weather.config import Environment
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.kalshi.provenance import SourceEnvironment, resolve_kalshi_environment

DEMO = "https://demo-api.kalshi.co/trade-api/v2"
PROD = "https://api.elections.kalshi.com/trade-api/v2"


@pytest.mark.parametrize(
    "url, expected",
    [
        (DEMO, SourceEnvironment.DEMO),
        (PROD, SourceEnvironment.PRODUCTION),
        # trailing slash
        (DEMO + "/", SourceEnvironment.DEMO),
        (PROD + "/", SourceEnvironment.PRODUCTION),
        # path/query appended to the real host still resolves by host
        (PROD + "/markets?ticker=X", SourceEnvironment.PRODUCTION),
        ("https://demo-api.kalshi.co/a/b/c?q=1", SourceEnvironment.DEMO),
        # case-insensitive host
        ("https://DEMO-API.KALSHI.CO/x", SourceEnvironment.DEMO),
        # --- spoofing attempts, all must be unknown ---
        ("https://evil.com/path/demo-api.kalshi.co", SourceEnvironment.UNKNOWN),
        ("https://evil.com/?x=api.elections.kalshi.com", SourceEnvironment.UNKNOWN),
        ("https://demo-api.kalshi.co.attacker.com/", SourceEnvironment.UNKNOWN),
        ("https://api.elections.kalshi.com.evil.net/x", SourceEnvironment.UNKNOWN),
        ("https://demo-api.kalshi.co@evil.com/x", SourceEnvironment.UNKNOWN),
        # missing / malformed
        (None, SourceEnvironment.UNKNOWN),
        ("", SourceEnvironment.UNKNOWN),
        ("not a url", SourceEnvironment.UNKNOWN),
        ("https://sandbox.kalshi.com/x", SourceEnvironment.UNKNOWN),
    ],
)
def test_resolver(url: str | None, expected: SourceEnvironment) -> None:
    assert resolve_kalshi_environment(url) is expected


def test_resolver_returns_only_canonical_values() -> None:
    for url in (DEMO, PROD, "https://x.example/y", None):
        assert resolve_kalshi_environment(url).value in ("demo", "production", "unknown")


def _client(base_url: str) -> KalshiClient:
    return KalshiClient(base_url=base_url, environment=Environment.DEVELOPMENT)


async def test_client_source_environment_demo() -> None:
    async with _client(DEMO) as c:
        assert c.source_environment == "demo"


async def test_client_source_environment_production() -> None:
    async with _client(PROD) as c:
        assert c.source_environment == "production"


async def test_client_source_environment_unknown_host() -> None:
    async with _client("https://example.invalid/trade-api/v2") as c:
        assert c.source_environment == "unknown"
