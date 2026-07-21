"""Typed application settings.

The default environment is always ``demo`` for anything trading-related; a
value must be supplied explicitly to select ``production``. Live order
submission additionally requires ``enable_live_trading`` and
``live_trading_confirm`` to both be true, on top of it not existing as code
yet in this milestone.
"""

from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    DEMO = "demo"
    BACKTEST = "backtest"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    kalshi_env: Environment = Field(default=Environment.DEMO, alias="KALSHI_ENV")

    database_url: str = Field(
        default="postgresql+psycopg://kalshi:kalshi@localhost:5432/kalshi_weather",
        alias="DATABASE_URL",
    )

    kalshi_prod_base_url: str = Field(
        default="https://api.elections.kalshi.com/trade-api/v2",
        alias="KALSHI_PROD_BASE_URL",
    )
    kalshi_demo_base_url: str = Field(
        default="https://demo-api.kalshi.co/trade-api/v2",
        alias="KALSHI_DEMO_BASE_URL",
    )

    kalshi_demo_api_key_id: str | None = Field(default=None, alias="KALSHI_DEMO_API_KEY_ID")
    # Either raw PEM text pasted directly into .env, or a path to a PEM file
    # on disk -- see kalshi.auth.load_private_key_from_setting.
    kalshi_demo_private_key: str | None = Field(default=None, alias="KALSHI_DEMO_PRIVATE_KEY")

    # Non-negotiable per CLAUDE.md: both flags default false, and a second
    # explicit runtime confirmation is required beyond this settings object.
    enable_live_trading: bool = Field(default=False, alias="ENABLE_LIVE_TRADING")
    live_trading_confirm: bool = Field(default=False, alias="LIVE_TRADING_CONFIRM")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Collector defaults (see ingestion/collector.py). "Climate and Weather"
    # is Kalshi's real live category value, confirmed in docs/API_VERIFICATION.md
    # -- not "Weather", which was an earlier unverified guess.
    collector_category: str = Field(
        default="Climate and Weather", alias="COLLECTOR_CATEGORY"
    )
    collector_market_status: str = Field(default="open", alias="COLLECTOR_MARKET_STATUS")
    collector_interval_seconds: float = Field(
        default=300.0, alias="COLLECTOR_INTERVAL_SECONDS"
    )
    # Proactive client-side request spacing (seconds). Kalshi's default matches
    # its reference client (see kalshi/client.py); the weather value paces
    # NWS/IEM requests as a courtesy to free public APIs.
    kalshi_min_request_interval_seconds: float = Field(
        default=0.1, alias="KALSHI_MIN_REQUEST_INTERVAL_SECONDS"
    )
    weather_min_request_interval_seconds: float = Field(
        default=0.1, alias="WEATHER_MIN_REQUEST_INTERVAL_SECONDS"
    )
    # A collector whose most recent recorded run is older than this many
    # multiples of its interval is reported stale by `ops health`.
    ops_stale_after_intervals: float = Field(default=3.0, alias="OPS_STALE_AFTER_INTERVALS")

    # Weather collector defaults (see ingestion/weather_collector.py,
    # weather/provider.py). NWS asks API consumers to identify themselves in
    # the User-Agent; there's no authentication to configure.
    weather_user_agent: str = Field(
        default="kalshi-weather-research (github.com/kalshi-weather-research)",
        alias="WEATHER_USER_AGENT",
    )
    weather_backfill_days: int = Field(default=30, alias="WEATHER_BACKFILL_DAYS")
    weather_interval_seconds: float = Field(
        default=1800.0, alias="WEATHER_INTERVAL_SECONDS"
    )

    # Research dataset export (see dataset/, docs/runbooks/dataset.md). Storage
    # location is configurable and never hardcoded; the directory is created on
    # demand. The market map is the documented stand-in for settlement_specs
    # until Milestone 2b (dataset/market_map.py).
    dataset_root: Path = Field(default=Path("data/datasets"), alias="DATASET_ROOT")
    dataset_market_map_path: Path | None = Field(
        default=None, alias="DATASET_MARKET_MAP_PATH"
    )

    @model_validator(mode="after")
    def _forbid_live_trading_outside_production(self) -> "Settings":
        if self.kalshi_env != Environment.PRODUCTION and (
            self.enable_live_trading or self.live_trading_confirm
        ):
            raise ValueError(
                "enable_live_trading/live_trading_confirm may only be set "
                "when kalshi_env=production"
            )
        return self

    def base_url_for(self, env: Environment) -> str:
        if env == Environment.PRODUCTION:
            return self.kalshi_prod_base_url
        if env == Environment.DEMO:
            return self.kalshi_demo_base_url
        raise ValueError(f"no Kalshi base URL for environment {env!r}")


def get_settings() -> Settings:
    return Settings()
