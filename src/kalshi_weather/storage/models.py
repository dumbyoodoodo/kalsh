"""SQLAlchemy ORM models for the Milestone 1 subset of DATA_MODEL.md.

Settlement, forecast, model-prediction, signal, order/fill/position tables
are deferred to the milestones that first need them (Milestone 2+).
"""

import hashlib
import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, Integer, Numeric, String, Text
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
    # Kalshi's own `updated_time` for this market, distinct from `observed_at`.
    source_updated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    # Hash of the normalized fields above (excluding id/observed_at/
    # raw_payload_id), used to skip inserting a snapshot identical to the
    # immediately-prior one for this ticker. See save_market_snapshot.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_api_payloads.id"))
    observed_at: Mapped[datetime] = mapped_column(nullable=False)


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
