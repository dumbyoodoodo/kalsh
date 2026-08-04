"""SQLAlchemy ORM models for the Milestone 1 subset of DATA_MODEL.md.

Settlement, forecast, model-prediction, signal, order/fill/position tables
are deferred to the milestones that first need them (Milestone 2+).
"""

import hashlib
import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# SQLite only honors autoincrement rowid-aliasing on a column declared exactly
# as INTEGER PRIMARY KEY; BigInteger's "BIGINT" DDL breaks that on SQLite
# (used by the in-memory unit tests), so fall back to Integer there. Postgres
# always gets a real BIGINT.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


def content_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RawApiPayload(Base):
    """Append-only capture of every external API response, verbatim."""

    __tablename__ = "raw_api_payloads"
    __table_args__ = (
        Index(
            "ix_raw_api_payloads_source_request_hash",
            "source",
            "request_key",
            "content_hash",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint_or_channel: Mapped[str] = mapped_column(String(255), nullable=False)
    request_key: Mapped[str] = mapped_column(String(512), nullable=False)
    received_at: Mapped[datetime] = mapped_column(nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    # Kalshi source environment (ADR 0013): demo | production | unknown; NULL
    # for pre-0010 rows and for non-Kalshi payloads (e.g. weather), whose
    # host provenance this field does not describe. `endpoint_or_channel`
    # stores only the path, so it cannot distinguish demo from production.
    environment: Mapped[str | None] = mapped_column(String(16))


class SeriesRecord(Base):
    __tablename__ = "series"

    series_ticker: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(Text)
    frequency: Mapped[str | None] = mapped_column(String(32))
    settlement_source: Mapped[str | None] = mapped_column(Text)
    # Kalshi's own `last_updated_ts` for this row, distinct from `observed_at`
    # (when *we* ingested it) -- confirmed present live, see
    # docs/API_VERIFICATION.md / docs/adr/0002-ingestion-collector.md.
    source_updated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    observed_at: Mapped[datetime] = mapped_column(nullable=False)


class EventRecord(Base):
    __tablename__ = "events"

    event_ticker: Mapped[str] = mapped_column(String(64), primary_key=True)
    series_ticker: Mapped[str | None] = mapped_column(
        ForeignKey("series.series_ticker"), nullable=True
    )
    category: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(Text)
    sub_title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(32))
    # open_time/close_time/settlement_time are part of the original
    # DATA_MODEL.md design but are not present on live /events responses as
    # of this writing (see docs/API_VERIFICATION.md) -- kept nullable for
    # forward compatibility (e.g. once settlement mapping/Milestone 2b needs
    # them) rather than removed and re-added later.
    open_time: Mapped[datetime | None] = mapped_column(nullable=True)
    close_time: Mapped[datetime | None] = mapped_column(nullable=True)
    settlement_time: Mapped[datetime | None] = mapped_column(nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    observed_at: Mapped[datetime] = mapped_column(nullable=False)


class MarketSnapshot(Base):
    """A point-in-time snapshot of a market; never overwritten (append-only)."""

    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    market_ticker: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Not a ForeignKey: markets are commonly fetched/persisted before their
    # parent event has been ingested (confirmed live -- see
    # docs/API_VERIFICATION.md). Enforcing referential integrity here would
    # make `market show` unusable until `events` is separately populated,
    # which no current CLI command does.
    event_ticker: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_type: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(Text)
    subtitle: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(32))
    yes_bid_cents: Mapped[int | None] = mapped_column(Integer)
    yes_ask_cents: Mapped[int | None] = mapped_column(Integer)
    last_price_cents: Mapped[int | None] = mapped_column(Integer)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    open_interest: Mapped[int | None] = mapped_column(BigInteger)
    close_time: Mapped[datetime | None] = mapped_column(nullable=True)
    rules_primary: Mapped[str | None] = mapped_column(Text)
    rules_secondary: Mapped[str | None] = mapped_column(Text)
    # Settlement fields (E-A, docs/adr/0006-settlement-labels.md): populated
    # once a market settles. `expiration_value` is the underlying value Kalshi
    # paid on (e.g. degrees F); `settlement_ts` the exact determination time;
    # floor/cap/strike_type the structured strike definition.
    result: Mapped[str | None] = mapped_column(String(16))
    expiration_value: Mapped[Any] = mapped_column(Numeric(10, 2), nullable=True)
    settlement_ts: Mapped[datetime | None] = mapped_column(nullable=True)
    # The venue's own finality marker (migration 0008, ADR 0011): Kalshi keeps
    # revising `volume`, `open_interest` and occasionally `result` *after*
    # publishing status="finalized" with a settlement_ts, up until this time
    # -- observed ~7 days after close_time. A snapshot taken before it is
    # provisional metadata, not final. NULL on every row written before 0008.
    expiration_time: Mapped[datetime | None] = mapped_column(nullable=True)
    floor_strike: Mapped[Any] = mapped_column(Numeric(10, 2), nullable=True)
    cap_strike: Mapped[Any] = mapped_column(Numeric(10, 2), nullable=True)
    strike_type: Mapped[str | None] = mapped_column(String(16))
    # Kalshi's own `updated_time` for this market, distinct from `observed_at`.
    source_updated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    # Hash of the normalized fields above (excluding id/observed_at/
    # raw_payload_id), used to skip inserting a snapshot identical to the
    # immediately-prior one for this ticker. See save_market_snapshot.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    observed_at: Mapped[datetime] = mapped_column(nullable=False)
    # Kalshi source environment (ADR 0013): demo | production | unknown; NULL on
    # pre-0010 rows. Demo carries ~zero volume/OI; production carries the real
    # values, so this disambiguates the liquidity dimension per ticker.
    environment: Mapped[str | None] = mapped_column(String(16))


class OrderbookSnapshot(Base):
    __tablename__ = "orderbook_snapshots"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    market_ticker: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(nullable=False)
    yes_levels_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    no_levels_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    best_yes_bid_cents: Mapped[int | None] = mapped_column(Integer)
    best_yes_ask_cents: Mapped[int | None] = mapped_column(Integer)
    best_no_bid_cents: Mapped[int | None] = mapped_column(Integer)
    best_no_ask_cents: Mapped[int | None] = mapped_column(Integer)
    spread_cents: Mapped[int | None] = mapped_column(Integer)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    # Hash of yes_levels_json/no_levels_json, used to skip inserting a book
    # identical to the immediately-prior one for this ticker.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    # Kalshi source environment (ADR 0013): demo | production | unknown; NULL on
    # pre-0010 rows. Demo order books are empty; production has real depth.
    environment: Mapped[str | None] = mapped_column(String(16))


class TradeRecord(Base):
    __tablename__ = "trades"
    __table_args__ = (Index("ix_trades_market_ticker", "market_ticker"),)

    trade_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market_ticker: Mapped[str] = mapped_column(String(64), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    taker_side: Mapped[str | None] = mapped_column(String(8))
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    # Kalshi source environment (ADR 0013): demo | production | unknown; NULL on
    # rows written before migration 0010. Stamped from the producing client's
    # base URL, never guessed. Demo trades are near-empty; production carries
    # the real activity.
    environment: Mapped[str | None] = mapped_column(String(16))


class WeatherStation(Base):
    """Static registry entry (weather/stations.py) persisted for FK/audit
    purposes; upserted, not append-only -- station identity doesn't change."""

    __tablename__ = "weather_stations"

    station_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source_location_code: Mapped[str] = mapped_column(String(16), nullable=False)
    office: Mapped[str | None] = mapped_column(String(8))
    latitude: Mapped[Any] = mapped_column(Numeric(9, 6), nullable=False)
    longitude: Mapped[Any] = mapped_column(Numeric(9, 6), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    observed_at: Mapped[datetime] = mapped_column(nullable=False)


class WeatherObservation(Base):
    """A single station/variable value from one issuance of a CLI report.

    Append-only: CLI reports are reissued multiple times per day (a same-day
    preliminary issuance, then a final one after midnight -- see
    weather/cli_parser.py) and every issuance is kept, never overwritten.
    `observation_date` is the calendar day the value describes; `issuance_time`
    is when *this version* of the report was published -- "the latest version
    for a date" is a query (max issuance_time per station/variable/date), not
    a mutation.
    """

    __tablename__ = "weather_observations"
    __table_args__ = (
        Index(
            "ix_weather_observations_dedup",
            "station_id",
            "variable",
            "issuance_time",
            unique=True,
        ),
        Index(
            "ix_weather_observations_lookup",
            "station_id",
            "variable",
            "observation_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(
        ForeignKey("weather_stations.station_id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    variable: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[Any] = mapped_column(Numeric(6, 2), nullable=False)
    unit: Mapped[str] = mapped_column(String(8), nullable=False)
    observation_date: Mapped[date] = mapped_column(nullable=False)
    issuance_time: Mapped[datetime] = mapped_column(nullable=False)
    source_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    observed_at: Mapped[datetime] = mapped_column(nullable=False)


class WeatherForecast(Base):
    """A single station/variable forecast period from one forecast issuance.

    Append-only: never overwrite a forecast with a later one (DATA_MODEL.md);
    `issue_time` is what makes forecast-error research free of look-ahead
    bias later -- a decision at time T may only use forecasts with
    issue_time <= T.
    """

    __tablename__ = "weather_forecasts"
    __table_args__ = (
        Index(
            "ix_weather_forecasts_dedup",
            "station_id",
            "variable",
            "issue_time",
            "valid_start",
            unique=True,
        ),
        Index(
            "ix_weather_forecasts_lookup",
            "station_id",
            "variable",
            "valid_start",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(
        ForeignKey("weather_stations.station_id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    variable: Mapped[str] = mapped_column(String(32), nullable=False)
    point_estimate: Mapped[Any] = mapped_column(Numeric(6, 2), nullable=False)
    unit: Mapped[str] = mapped_column(String(8), nullable=False)
    issue_time: Mapped[datetime] = mapped_column(nullable=False)
    valid_start: Mapped[datetime] = mapped_column(nullable=False)
    valid_end: Mapped[datetime] = mapped_column(nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    observed_at: Mapped[datetime] = mapped_column(nullable=False)


class SettlementSpecRecord(Base):
    """A stored settlement resolution for one market (settlement/spec.py).

    Append-only and versioned: unique on (market_ticker, parser_version,
    rules_hash), so re-running the same parser over unchanged rules is a
    detectable no-op, while a parser upgrade or a rules change produces a new
    row alongside the old one -- the resolution history is never rewritten.
    The latest row per market (max id) is the current resolution.
    """

    __tablename__ = "settlement_specs"
    __table_args__ = (
        Index(
            "ix_settlement_specs_dedup",
            "market_ticker",
            "parser_version",
            "rules_hash",
            unique=True,
        ),
        Index("ix_settlement_specs_market", "market_ticker"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    market_ticker: Mapped[str] = mapped_column(String(64), nullable=False)
    series_ticker: Mapped[str] = mapped_column(String(64), nullable=False)
    event_ticker: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    city: Mapped[str | None] = mapped_column(String(64))
    # Not a ForeignKey: unsupported markets legitimately reference stations
    # absent from weather_stations (that absence is the finding).
    station_id: Mapped[str | None] = mapped_column(String(32))
    variable: Mapped[str | None] = mapped_column(String(32))
    target_date: Mapped[date | None] = mapped_column(nullable=True)
    settlement_source: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    wfo_site: Mapped[str | None] = mapped_column(String(8))
    source_location_code: Mapped[str | None] = mapped_column(String(16))
    unit: Mapped[str | None] = mapped_column(String(8))
    observation_window: Mapped[str | None] = mapped_column(String(32))
    rounding_rule: Mapped[str | None] = mapped_column(String(32))
    market_close_time: Mapped[datetime | None] = mapped_column(nullable=True)
    notes_json: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    parser_version: Mapped[str] = mapped_column(String(16), nullable=False)
    rules_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    observed_at: Mapped[datetime] = mapped_column(nullable=False)


class CollectorRun(Base):
    """One collection cycle's operational record (ops metrics, Phase 6).

    Append-only history of every cycle each collector executes: what it
    saved, how long it took, how many requests/retries it cost, and whether
    it succeeded. `ops health` derives collector liveness/staleness from the
    most recent row per collector; longer windows give success rates and
    throughput trends. A metrics write failing must never break collection --
    writers treat this table as best-effort (see run_collector_loop).
    """

    __tablename__ = "collector_runs"
    __table_args__ = (Index("ix_collector_runs_collector", "collector", "started_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    collector: Mapped[str] = mapped_column(String(32), nullable=False)  # "kalshi" | "weather"
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime] = mapped_column(nullable=False)
    duration_seconds: Mapped[float] = mapped_column(nullable=False)
    success: Mapped[bool] = mapped_column(nullable=False)
    requests_attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stats_json: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")


class MarketCandlestick(Base):
    """One OHLC candle for a market at a given resolution (Phase 7A price
    ingestion, docs/adr/0007-price-ingestion.md).

    Append-only and immutable once the period has elapsed: unlike weather CLI
    reports, Kalshi's candlestick endpoint does not revise past candles (an
    already-elapsed period is a closed historical fact) -- confirmed by the
    absence of any revision/versioning field in the wire format. Natural key
    is (market_ticker, period_interval_seconds, period_end); a re-fetch of an
    already-stored candle is a no-op skip, never an overwrite.

    Prices are stored as integer cents (CLAUDE.md: never binary floats for
    money), converted from Kalshi's decimal-dollar wire strings the same way
    `Market`/`Trade` already do. `price_*` reflects the last TRADED price for
    the period; `yes_bid_*`/`yes_ask_*` reflect quoted OHLC and remain
    populated even when the period had zero trading volume -- `volume == 0`
    is the authoritative "no trade occurred" signal; `price_close_cents`
    alone must never be read as evidence a trade happened (see
    `price_close_is_carried_forward`).
    """

    __tablename__ = "market_candlesticks"
    __table_args__ = (
        Index(
            "ix_market_candlesticks_dedup",
            "market_ticker",
            "period_interval_seconds",
            "period_end",
            unique=True,
        ),
        Index(
            "ix_market_candlesticks_lookup",
            "market_ticker",
            "period_interval_seconds",
            "period_end",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    market_ticker: Mapped[str] = mapped_column(String(64), nullable=False)
    series_ticker: Mapped[str] = mapped_column(String(64), nullable=False)
    period_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[datetime] = mapped_column(nullable=False)
    period_end: Mapped[datetime] = mapped_column(nullable=False)
    price_open_cents: Mapped[int | None] = mapped_column(Integer)
    price_high_cents: Mapped[int | None] = mapped_column(Integer)
    price_low_cents: Mapped[int | None] = mapped_column(Integer)
    price_close_cents: Mapped[int | None] = mapped_column(Integer)
    price_mean_cents: Mapped[int | None] = mapped_column(Integer)
    # True when the API's own price.close_dollars was absent (zero-volume
    # period) and price_close_cents was filled from price.previous_dollars
    # instead -- a backward-looking carry-forward already known at this
    # period's own timestamp, never a future value. See ingestion/price_backfill.py.
    price_close_is_carried_forward: Mapped[bool] = mapped_column(nullable=False, default=False)
    yes_bid_open_cents: Mapped[int | None] = mapped_column(Integer)
    yes_bid_high_cents: Mapped[int | None] = mapped_column(Integer)
    yes_bid_low_cents: Mapped[int | None] = mapped_column(Integer)
    yes_bid_close_cents: Mapped[int | None] = mapped_column(Integer)
    yes_ask_open_cents: Mapped[int | None] = mapped_column(Integer)
    yes_ask_high_cents: Mapped[int | None] = mapped_column(Integer)
    yes_ask_low_cents: Mapped[int | None] = mapped_column(Integer)
    yes_ask_close_cents: Mapped[int | None] = mapped_column(Integer)
    volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_interest: Mapped[int | None] = mapped_column(Integer)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    observed_at: Mapped[datetime] = mapped_column(nullable=False)
    # Kalshi source environment (ADR 0013): demo | production | unknown; NULL on
    # pre-0010 rows. Candlesticks come from the production price client.
    environment: Mapped[str | None] = mapped_column(String(16))


class MarketMetadataVerification(Base):
    """One post-finality verification attempt against a settled market.

    Exists because "did we already re-check this market after the venue's
    finality time?" cannot be answered from `market_snapshots` alone: a
    verification that finds *nothing changed* correctly appends no snapshot
    (content-hash dedup), so an unchanged verification would be
    indistinguishable from never having checked, and the market would be
    re-fetched forever. Recording the attempt separately keeps
    `market_snapshots` meaning exactly one thing -- observed market state --
    instead of overloading it with "we looked".

    Append-only, like every other table here. A market may accumulate several
    rows (a transient failure is simply not recorded, so it is retried; a
    manual re-verification appends another row); the revision pass treats a
    market as done when a row with a terminal outcome exists.
    """

    __tablename__ = "market_metadata_verifications"
    __table_args__ = (
        Index("ix_market_metadata_verifications_ticker", "market_ticker", "verified_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    market_ticker: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: When this verification ran (UTC).
    verified_at: Mapped[datetime] = mapped_column(nullable=False)
    #: The finality time this verification was gated on -- either the market's
    #: own `expiration_time` or the documented close_time fallback. Stored so a
    #: later audit can tell which rule admitted the market.
    finality_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: unchanged | changed | market_removed | retention_expired
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    #: True when this verification appended a new market_snapshots row.
    snapshot_appended: Mapped[bool] = mapped_column(nullable=False, default=False)
    #: Which fields the venue had revised (comma-separated), for observability.
    changed_fields: Mapped[str | None] = mapped_column(String(128))
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")


class SettlementAttempt(Base):
    """One settlement-capture attempt against one market (migration 0009).

    Exists because the settlement queue had no memory. A market checked and
    found *still unsettled* (HTTP 200, empty `result`) stayed eligible at
    identical priority, so with `ORDER BY close_time ASC` the oldest
    permanently-unsettled markets occupied every slot of the bounded batch
    forever. On 2026-07-23 that was 80 pending `KXRAIN` markets holding all 25
    head slots across 20+ consecutive cycles while 1,476 other pending
    markets -- 10 of 10 sampled being `finalized` with real results -- were
    never reached. See ADR 0012.

    Append-only: a market accumulates one row per attempt, never updated. The
    queue reads only the *latest* row per ticker to decide eligibility, so the
    full attempt history stays auditable (why a market was deferred, when, and
    for how long) without any mutable state.

    Retry state deliberately lives here rather than on `market_snapshots`,
    which is append-only observation data and must not carry operational
    bookkeeping.
    """

    __tablename__ = "settlement_attempts"
    __table_args__ = (
        Index("ix_settlement_attempts_latest", "market_ticker", "attempted_at"),
        Index("ix_settlement_attempts_retry", "retryable", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    market_ticker: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempted_at: Mapped[datetime] = mapped_column(nullable=False)
    #: One of ingestion.settlement_sync.SettlementOutcome.
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: False marks a terminal outcome -- the market leaves the queue for good.
    retryable: Mapped[bool] = mapped_column(nullable=False, default=True)
    #: Earliest time this market may be re-attempted. NULL when terminal.
    next_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    #: Bounded, sanitized detail -- never a full payload or credential.
    detail: Mapped[str | None] = mapped_column(String(300))
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("market_snapshots.id"))
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    # Kalshi source environment (ADR 0013): demo | production | unknown; NULL on
    # pre-0010 rows. Stamped from the settlement client (currently demo).
    environment: Mapped[str | None] = mapped_column(String(16))


class MarketPollAttempt(Base):
    """One ticker-endpoint polling attempt or explicit polling decision within a
    collection cycle (migration 0011; ADR 0020).

    Exists because ``collector_runs`` proves only that the collector ran a cycle
    collector-wide; it cannot prove a SPECIFIC ticker was eligible, requested,
    succeeded, returned unchanged content, failed, or was skipped. Order-book and
    market snapshots dedup unchanged content, so a successful poll that returned
    an identical book leaves NO row -- indistinguishable, from the snapshot tables
    alone, from a ticker that was never polled. This ledger records that
    distinction directly (``succeeded_unchanged`` with ``deduplicated=true`` still
    proves the ticker was observed).

    Append-only: one row per logical ticker-endpoint attempt, never updated or
    deleted. HTTP-level retries the client performed internally are summarized in
    ``retry_count`` (one logical row per collector decision, not one per HTTP
    attempt). PROSPECTIVE ONLY -- no historical row is ever backfilled; the
    availability builder treats pre-0011 periods as legacy/inferred evidence.
    """

    __tablename__ = "market_poll_attempts"
    __table_args__ = (
        Index("ix_market_poll_attempts_ticker_time", "ticker", "requested_at"),
        Index("ix_market_poll_attempts_run", "collector_run_id"),
        Index("ix_market_poll_attempts_endpoint_outcome", "endpoint_type", "outcome"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    #: The cycle this attempt belongs to. Written with the run record post-cycle.
    collector_run_id: Mapped[int] = mapped_column(ForeignKey("collector_runs.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(64), nullable=False)
    #: market_snapshot | orderbook | trades | settlement | metadata_revision
    endpoint_type: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Kalshi source environment (ADR 0013): demo | production | unknown.
    environment: Mapped[str | None] = mapped_column(String(16))
    #: eligible | not_eligible -- whether the ticker was in the poll-eligible set.
    eligibility_state: Mapped[str] = mapped_column(String(24), nullable=False)
    #: attempted | skipped | not_attempted -- what the collector did about it.
    attempt_state: Mapped[str] = mapped_column(String(24), nullable=False)
    #: The deterministic outcome taxonomy (see ingestion.poll_ledger.PollOutcome).
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime] = mapped_column(nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    #: Internal client HTTP retries for this one logical request (not a new row).
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rate_limited: Mapped[bool] = mapped_column(nullable=False, default=False)
    #: Rows persisted by this attempt; 0 for unchanged/empty/failed.
    persisted_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: True when a successful response deduped to no new row (still observed).
    deduplicated: Mapped[bool] = mapped_column(nullable=False, default=False)
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    error_class: Mapped[str | None] = mapped_column(String(64))
    #: Bounded, sanitized detail -- never a full payload or credential.
    bounded_error_detail: Mapped[str | None] = mapped_column(String(500))
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class WeatherCollectionAttempt(Base):
    """One logical station/product weather collection attempt (migration 0012).

    Exists because ``collector_runs.stats_json`` proves only that a weather
    CYCLE ran: its ``invalid_items`` and ``errors`` scalars carry no station
    dimension, so a parser rejection or an unavailable source could not be
    attributed to the station that caused it. Successes were already
    attributable (``weather_observations`` rows carry ``station_id``); it is
    FAILURES AND NON-EVENTS that left no trace, which is why the 2026-08-04
    pilot review had to report ``evidence_unavailable`` and could not KEEP any
    station.

    Append-only: one row per logical station/product attempt, never updated or
    deleted. Retries the provider performs internally are summarized in
    ``retry_count`` -- one logical row per collector decision, not one per HTTP
    attempt. PROSPECTIVE ONLY: no historical row is ever backfilled, and any
    window predating this table stays LEGACY_UNKNOWN rather than becoming zero.
    """

    __tablename__ = "weather_collection_attempts"
    __table_args__ = (
        Index("ix_weather_attempts_station_time", "station_code", "requested_at"),
        Index("ix_weather_attempts_run", "collector_run_id"),
        Index("ix_weather_attempts_product_outcome", "product_type", "outcome"),
        Index("ix_weather_attempts_target_date", "target_station_local_date"),
        Index("ix_weather_attempts_env_time", "environment", "requested_at"),
        UniqueConstraint(
            "collector_run_id",
            "environment",
            "station_code",
            "product_type",
            "logical_request_key",
            name="uq_weather_attempt_logical",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    #: Stable id generated BEFORE network I/O, derived from the logical key.
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    collector_run_id: Mapped[int] = mapped_column(ForeignKey("collector_runs.id"), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    station_code: Mapped[str] = mapped_column(String(32), nullable=False)
    wfo: Mapped[str | None] = mapped_column(String(8))
    product_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Deterministic identity of the INTENDED request; excludes retry number so
    #: retries collapse into one terminal record.
    logical_request_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_request_id: Mapped[str | None] = mapped_column(String(255))
    source_product_id: Mapped[str | None] = mapped_column(String(128))

    requested_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime] = mapped_column(nullable=False)
    observed_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    #: Station-LOCAL target date (never a UTC day). Null only where the product
    #: genuinely has no target date (e.g. station metadata).
    target_station_local_date: Mapped[date | None] = mapped_column()

    source_endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    source_availability: Mapped[str] = mapped_column(String(24), nullable=False)

    parser_name: Mapped[str | None] = mapped_column(String(64))
    parser_version: Mapped[str | None] = mapped_column(String(16))
    parser_error_type: Mapped[str | None] = mapped_column(String(64))
    #: Sanitized and bounded -- never raw provider text, which can carry secrets.
    parser_error_message: Mapped[str | None] = mapped_column(String(500))
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))

    parsed_entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persisted_entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persistence_error_type: Mapped[str | None] = mapped_column(String(64))
    persistence_error_message: Mapped[str | None] = mapped_column(String(500))
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
