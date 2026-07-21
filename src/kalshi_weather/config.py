"""Typed application settings.

The default environment is always ``demo`` for anything trading-related; a
value must be supplied explicitly to select ``production``. Live order
submission additionally requires ``enable_live_trading`` and
``live_trading_confirm`` to both be true, on top of it not existing as code
yet in this milestone.
"""

from enum import StrEnum

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
