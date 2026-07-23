"""Typed application settings.

The default environment is always ``demo`` for anything trading-related; a
value must be supplied explicitly to select ``production``. Live order
submission additionally requires ``enable_live_trading`` and
``live_trading_confirm`` to both be true, on top of it not existing as code
yet in this milestone.
"""

from enum import StrEnum
from pathlib import Path

from pydantic import Field, PrivateAttr, model_validator
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
        # An empty env value means "unset, use the default" -- without this,
        # `KALSHI_DATA_DIR=` (the .env.example placeholder style) would parse
        # to Path(".") and count as explicitly configured.
        env_ignore_empty=True,
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
    collector_category: str = Field(default="Climate and Weather", alias="COLLECTOR_CATEGORY")
    collector_market_status: str = Field(default="open", alias="COLLECTOR_MARKET_STATUS")
    collector_interval_seconds: float = Field(default=300.0, alias="COLLECTOR_INTERVAL_SECONDS")
    # Bounded bootstrap window for a market with no stored trade checkpoint
    # yet (see docs/adr/0002-ingestion-collector.md decision 9). Without this,
    # a market with a very large trade history (observed: >100,000 trades on
    # some Climate & Weather markets) would have its first fetch abort at
    # `paginate()`'s max_pages guard before any trades are persisted, so no
    # checkpoint is ever established and every subsequent cycle repeats the
    # same full-history fetch forever. 30 days mirrors the existing
    # WEATHER_BACKFILL_DAYS default.
    initial_trade_bootstrap_lookback_days: int = Field(
        default=30, alias="INITIAL_TRADE_BOOTSTRAP_LOOKBACK_DAYS"
    )
    # Settled-transition capture (ingestion/settlement_sync.py). The open-only
    # discovery filter means a market leaves the discovery list the moment it
    # closes -- before a final result-bearing snapshot exists -- which starves
    # price sync, retention monitoring, and settlement labels (2026-07-23
    # production-gap investigation). Each cycle re-checks a bounded number of
    # recently-tracked tickers that vanished from the open list; the queue is
    # data-derived (latest snapshot has no result yet) and self-draining.
    collector_settle_check_limit: int = Field(default=25, alias="COLLECTOR_SETTLE_CHECK_LIMIT")
    collector_settle_check_days: int = Field(default=7, alias="COLLECTOR_SETTLE_CHECK_DAYS")
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

    # --- Monitoring & alerting (docs/runbooks/monitoring_alerting.md) ---
    # Transport `ops monitor` uses to deliver an alert on a WARNING/CRITICAL
    # observatory status. "none" (default) disables delivery entirely -- the
    # monitor still runs, decides, and logs history; it just never calls out.
    alert_transport: str = Field(default="none", alias="ALERT_TRANSPORT")
    alert_telegram_bot_token: str | None = Field(default=None, alias="ALERT_TELEGRAM_BOT_TOKEN")
    alert_telegram_chat_id: str | None = Field(default=None, alias="ALERT_TELEGRAM_CHAT_ID")

    # --- PostgreSQL backup & recovery (docs/runbooks/backup_recovery.md) ---
    # Identity of the database scripts/backup_postgres.sh dumps -- defaults
    # match docker-compose.yml's hardcoded POSTGRES_USER/POSTGRES_DB exactly,
    # so leaving these unset changes no existing behavior.
    backup_db_user: str = Field(default="kalshi", alias="BACKUP_DB_USER")
    backup_db_name: str = Field(default="kalshi_weather", alias="BACKUP_DB_NAME")
    # Local backup destination override. Unset (default): derive
    # `<KALSHI_DATA_DIR>/backups/postgres` as before. Set explicitly (an
    # absolute path) when the derived location is not writable from the
    # launchd execution context -- macOS TCC denies launchd agents access to
    # removable volumes, which silently broke every scheduled backup run
    # (2026-07-23 production-gap investigation). An internal-disk local
    # backup plus the S3 off-machine copy is the supported layout for that
    # case; S3 is never a substitute for a successfully created local backup.
    backup_local_dir: Path | None = Field(default=None, alias="BACKUP_LOCAL_DIR")
    # Off-machine copy destination. "none" (default): local backups only --
    # the audit finding stays PARTIALLY CLOSED, not CLOSED, until one of
    # these is configured. filesystem | s3 | none.
    backup_remote_type: str = Field(default="none", alias="BACKUP_REMOTE_TYPE")
    # A DIFFERENT mounted volume/NAS path than KALSHI_DATA_DIR -- never the
    # same physical disk (see docs/runbooks/backup_recovery.md "Off-machine
    # copy" for why that wouldn't be genuine redundancy).
    backup_remote_path: str | None = Field(default=None, alias="BACKUP_REMOTE_PATH")
    # S3-compatible destination. Credentials are never stored here -- the
    # `aws` CLI's own credential chain (env vars, ~/.aws/credentials, an
    # instance role) is used, exactly so no AWS secret ever needs to enter
    # this project's .env.
    backup_s3_bucket: str | None = Field(default=None, alias="BACKUP_S3_BUCKET")
    backup_s3_prefix: str = Field(default="postgres", alias="BACKUP_S3_PREFIX")
    # A local backup older than this is a WARNING/CRITICAL finding (the
    # default schedule is once/day; 30h mirrors the forecast stream's own
    # staleness convention -- a day plus slack, not a razor-thin margin).
    backup_stale_after_hours: float = Field(default=30.0, alias="BACKUP_STALE_AFTER_HOURS")
    # The off-machine copy is allowed a more lenient window -- it depends on
    # a second system/network being reachable, which fails independently of
    # local backup health.
    backup_remote_stale_after_hours: float = Field(
        default=48.0, alias="BACKUP_REMOTE_STALE_AFTER_HOURS"
    )
    # Free-space thresholds on the local backup destination's filesystem.
    backup_disk_warning_free_gb: float = Field(default=20.0, alias="BACKUP_DISK_WARNING_FREE_GB")
    backup_disk_critical_free_gb: float = Field(default=5.0, alias="BACKUP_DISK_CRITICAL_FREE_GB")
    # Retention policy (ops/backup_retention.py, `ops backup prune`). Exists
    # as a fully tested, dry-run-capable capability, but auto-pruning is OFF
    # by default -- scripts/backup_postgres.sh's own long-standing comment
    # ("pruning is a deliberate manual act") is a prior, explicit policy
    # decision this project made; overriding it silently would violate that
    # decision, so BACKUP_AUTO_PRUNE must be explicitly set to enable it.
    backup_auto_prune: bool = Field(default=False, alias="BACKUP_AUTO_PRUNE")
    backup_retention_daily_days: int = Field(default=14, alias="BACKUP_RETENTION_DAILY_DAYS")
    backup_retention_weekly_weeks: int = Field(default=8, alias="BACKUP_RETENTION_WEEKLY_WEEKS")
    backup_retention_monthly_months: int = Field(
        default=12, alias="BACKUP_RETENTION_MONTHLY_MONTHS"
    )

    # Weather collector defaults (see ingestion/weather_collector.py,
    # weather/provider.py). NWS asks API consumers to identify themselves in
    # the User-Agent; there's no authentication to configure.
    weather_user_agent: str = Field(
        default="kalshi-weather-research (github.com/kalshi-weather-research)",
        alias="WEATHER_USER_AGENT",
    )
    weather_backfill_days: int = Field(default=30, alias="WEATHER_BACKFILL_DAYS")
    weather_interval_seconds: float = Field(default=1800.0, alias="WEATHER_INTERVAL_SECONDS")

    # Price ingestion defaults (see ingestion/price_backfill.py,
    # docs/runbooks/price_ingestion.md). The observed retention window is a
    # measured finding (~67 days as of 2026-07-21, docs/research/
    # investigations/INV-20260721-price-history-recovery.md), not a documented
    # API guarantee -- kept configurable and re-verifiable, not hardcoded logic.
    price_candle_resolution_minutes: int = Field(default=1, alias="PRICE_CANDLE_RESOLUTION_MINUTES")
    price_sync_limit_per_cycle: int = Field(default=50, alias="PRICE_SYNC_LIMIT_PER_CYCLE")
    price_sync_interval_seconds: float = Field(default=1800.0, alias="PRICE_SYNC_INTERVAL_SECONDS")
    price_observed_retention_days: int = Field(default=67, alias="PRICE_OBSERVED_RETENTION_DAYS")
    price_retention_warning_buffer_days: int = Field(
        default=10, alias="PRICE_RETENTION_WARNING_BUFFER_DAYS"
    )

    # --- Storage roots (CLAUDE.md "Storage philosophy": code and data have
    # separate lifecycles; data locations are config, never source). ---
    #
    # KALSHI_DATA_DIR is the single root for all *file-based* persistent data
    # (dataset exports today; any future file artifact). Point it at an
    # external drive to keep bulk data off the repo disk. The PostgreSQL
    # store is separate infrastructure (docker-compose.yml; see
    # KALSHI_PG_DATA_DIR there) because a live database has stricter
    # filesystem requirements than plain files. Git-tracked research records
    # (docs/research/**) are deliberately NOT under this root: they are part
    # of the reproducibility record, versioned with the code.
    data_dir: Path = Field(default=Path("data"), alias="KALSHI_DATA_DIR")
    # Dataset exports live beneath the data root by default; DATASET_ROOT
    # remains an explicit override for split layouts. An empty value means
    # "derive from KALSHI_DATA_DIR" (resolved by the validator below; note
    # pathlib normalizes "" to ".", so DATASET_ROOT=. also derives).
    dataset_root: Path = Field(default=Path(""), alias="DATASET_ROOT")
    dataset_market_map_path: Path | None = Field(default=None, alias="DATASET_MARKET_MAP_PATH")

    # Captured by _derive_dataset_root BEFORE it mutates dataset_root:
    # pydantic v2 adds a field to model_fields_set on assignment, so the
    # "was this explicitly configured?" question must be answered first.
    _storage_explicitly_configured: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _derive_dataset_root(self) -> "Settings":
        self._storage_explicitly_configured = bool(
            {"data_dir", "dataset_root"} & self.model_fields_set
        )
        if self.dataset_root == Path(""):
            self.dataset_root = self.data_dir / "datasets"
        return self

    def ensure_dataset_root(self) -> Path:
        """Fail-fast guard for file-data writes, resolving the dataset root.

        If the data root was *explicitly configured* (KALSHI_DATA_DIR or
        DATASET_ROOT set in the environment/.env), its base directory must
        already exist: for an external drive, a missing root almost always
        means the drive isn't mounted, and silently creating the path would
        write data to the wrong disk. The repo-local *default* is created on
        demand, as before. Writability is always probed, so a read-only
        mount fails here -- loudly, before any collector or export runs --
        rather than partway through a write.
        """
        explicitly_configured = self._storage_explicitly_configured
        root = self.dataset_root
        base = root if root.is_absolute() else Path.cwd() / root
        if explicitly_configured:
            # The configured *parent* volume/root must exist; the datasets
            # subdirectory itself may still be created on first use.
            probe_base = base if base.exists() else base.parent
            if not probe_base.exists():
                raise RuntimeError(
                    f"configured data root {base} does not exist -- if it lives on "
                    "an external drive, is the drive mounted? Refusing to create "
                    "it implicitly (that would silently write to the wrong disk)."
                )
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".write-probe"
        try:
            probe.write_text("")
            probe.unlink()
        except OSError as exc:
            raise RuntimeError(f"configured data root {base} is not writable: {exc}") from exc
        return base

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
