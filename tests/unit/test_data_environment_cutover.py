"""Production data-collection cutover (ADR 0014).

The research/archival data clients read from ``kalshi_data_env`` (default
production), NOT from ``kalshi_env`` (the demo-defaulted live-trading gate).
These tests pin: every data client resolves to the one configured data
environment through the centralized ``kalshi_data_base_url``; the data client is
unauthenticated (so it can never be mistaken for the future demo execution
client); the two environments are decoupled; and an invalid data env fails
loudly at construction.
"""

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kalshi_weather.cli import _build_client, _build_price_client
from kalshi_weather.config import Environment, Settings

PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"


def _settings(**kw) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def _factory() -> async_sessionmaker:  # type: ignore[type-arg]
    # _build_price_client only touches the factory inside its sink closure,
    # which these tests never fire; a bare in-memory factory suffices.
    return async_sessionmaker(create_async_engine("sqlite+aiosqlite:///:memory:"))


# --- config: the centralized data-env resolution ---------------------------


def test_default_data_env_is_production() -> None:
    s = _settings()
    assert s.kalshi_data_env is Environment.PRODUCTION
    assert s.kalshi_data_base_url == PROD_URL


def test_data_base_url_follows_data_env() -> None:
    assert _settings(KALSHI_DATA_ENV="production").kalshi_data_base_url == PROD_URL
    assert _settings(KALSHI_DATA_ENV="demo").kalshi_data_base_url == DEMO_URL


def test_invalid_data_env_fails_at_startup() -> None:
    for bad in ("development", "backtest", "prod", ""):
        with pytest.raises(ValidationError):
            _settings(KALSHI_DATA_ENV=bad)


# --- endpoint selection: every data client -> production -------------------


def test_data_client_resolves_to_production() -> None:
    s = _settings(KALSHI_DATA_ENV="production", KALSHI_ENV="demo")
    c = _build_client(s)
    assert str(c._http.base_url).rstrip("/") == PROD_URL
    assert c.source_environment == "production"


def test_price_client_resolves_to_production() -> None:
    s = _settings(KALSHI_DATA_ENV="production")
    c = _build_price_client(s, _factory())
    assert str(c._http.base_url).rstrip("/") == PROD_URL
    assert c.source_environment == "production"


def test_no_data_workflow_silently_falls_back_to_demo() -> None:
    # Even with the trading env demo AND no explicit data env, the data client
    # must be production, never a silent demo fallback.
    s = _settings(KALSHI_ENV="demo")  # KALSHI_DATA_ENV unset -> default
    assert _build_client(s).source_environment == "production"
    assert _build_price_client(s, _factory()).source_environment == "production"


def test_demo_data_env_is_only_reached_explicitly() -> None:
    s = _settings(KALSHI_DATA_ENV="demo")
    assert _build_client(s).source_environment == "demo"
    assert _build_price_client(s, _factory()).source_environment == "demo"


# --- safety: data client is unauthenticated, decoupled from the trade gate --


def test_data_client_is_unauthenticated() -> None:
    # No signing credentials are attached, so the data client can never be
    # mistaken for (or substituted with) the future demo execution client.
    s = _settings(KALSHI_DATA_ENV="production")
    c = _build_client(s)
    assert c._key_id is None
    assert c._private_key is None
    c2 = _build_price_client(s, _factory())
    assert c2._key_id is None
    assert c2._private_key is None


def test_data_env_is_decoupled_from_trading_env() -> None:
    # kalshi_env (trading gate) does not move the data client's environment.
    for trading in ("demo", "development", "backtest"):
        s = _settings(KALSHI_ENV=trading, KALSHI_DATA_ENV="production")
        assert s.kalshi_env.value == trading
        assert _build_client(s).source_environment == "production"


def test_trading_gate_still_enforced_and_defaults_demo() -> None:
    # The live-trading safety gate is untouched: it still defaults to demo and
    # still forbids enabling live trading outside production. Setting the DATA
    # env to production must NOT enable trading.
    assert _settings().kalshi_env is Environment.DEMO
    with pytest.raises(ValidationError):
        _settings(KALSHI_DATA_ENV="production", ENABLE_LIVE_TRADING="true")


def test_unknown_host_still_stamps_unknown() -> None:
    # A data env pointing at an unrecognized host (misconfigured base URL) must
    # resolve provenance to unknown, never guess.
    s = _settings(KALSHI_DATA_ENV="production", KALSHI_PROD_BASE_URL="https://example.invalid/x")
    assert _build_client(s).source_environment == "unknown"
