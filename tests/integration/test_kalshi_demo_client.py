"""Opt-in integration test against Kalshi's real demo environment.

Skips cleanly unless KALSHI_DEMO_API_KEY_ID and KALSHI_DEMO_PRIVATE_KEY
are both set. Read-only: does not place, cancel, or modify any order.
"""

import os

import pytest

from kalshi_weather.config import Environment, get_settings
from kalshi_weather.kalshi.auth import load_private_key_from_setting
from kalshi_weather.kalshi.client import KalshiClient

pytestmark = pytest.mark.integration

_HAS_DEMO_CREDS = bool(
    os.environ.get("KALSHI_DEMO_API_KEY_ID") and os.environ.get("KALSHI_DEMO_PRIVATE_KEY")
)


@pytest.mark.skipif(not _HAS_DEMO_CREDS, reason="KALSHI_DEMO_API_KEY_ID/PRIVATE_KEY not set")
async def test_list_series_against_live_demo() -> None:
    settings = get_settings()
    private_key = load_private_key_from_setting(
        settings.kalshi_demo_private_key.get_secret_value()  # type: ignore[union-attr]
    )

    async with KalshiClient(
        base_url=settings.kalshi_demo_base_url,
        environment=Environment.DEMO,
        key_id=settings.kalshi_demo_api_key_id.get_secret_value(),  # type: ignore[union-attr]
        private_key=private_key,
    ) as client:
        series = await client.list_series()

    assert isinstance(series, list)
