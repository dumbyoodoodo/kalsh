"""Build point-in-time research datasets from the collected source tables.

Two datasets are produced (see docs/adr/0004-research-dataset.md):

- ``weather_panel`` -- one row per (station, target_date): the settled
  observation, a summary of the forecast issuances for that day, and the
  forecast residuals. Built from the weather tables alone; always available.
- ``market_weather`` -- one row per market snapshot, enriched with the
  forecast/observation/order-book/trade facts that were *knowable as of that
  snapshot's timestamp* (as-of joins), plus the post-settlement outcome as a
  clearly-labelled target column. Requires the market->station mapping
  (market_map.py); empty when none is supplied.

The pure ``build_*`` functions take Polars frames and return Polars frames, so
point-in-time correctness is unit-testable without a database. ``load_source_
frames`` is the only part that touches SQLAlchemy; it also computes each
forecast's local ``target_date`` (in the station's timezone) and normalizes
every timestamp to naive-UTC so all downstream comparisons are apples-to-apples.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.dataset.asof import asof_count, asof_latest
from kalshi_weather.dataset.market_map import MarketMapping
from kalshi_weather.dataset.observation_timeline import (
    attach_calendar_lock,
    compute_running_extremes,
)
from kalshi_weather.dataset.pit import local_date
from kalshi_weather.dataset.provenance import (
    EnvironmentPolicy,
    ProvenanceCoverage,
    liquidity_admissible,
    price_admissible,
)
from kalshi_weather.domain.time import to_utc
from kalshi_weather.storage.models import (
    MarketCandlestick,
    MarketSnapshot,
    OrderbookSnapshot,
    TradeRecord,
    WeatherForecast,
    WeatherObservation,
    WeatherStation,
)

# --- Explicit source-frame schemas -------------------------------------------
# Declared so an empty database still yields correctly-typed (empty) frames,
# which keeps every downstream join/select from blowing up on a fresh DB.

_DT = pl.Datetime("us")

#: Bumped whenever build_market_prices's column layout or join logic changes
#: meaning -- recorded in the dataset manifest (dataset/pipeline.py) so a
#: consumer can tell which version of the frame's semantics produced it.
MARKET_PRICES_SCHEMA_VERSION = "1"

#: Bumped whenever build_market_price_weather's column layout or join logic
#: changes meaning (Phase 7B, docs/adr/0008-point-in-time-alignment.md).
MARKET_PRICE_WEATHER_SCHEMA_VERSION = "1"

STATIONS_SCHEMA: dict[str, Any] = {"station_id": pl.Utf8, "timezone": pl.Utf8}
MARKETS_SCHEMA: dict[str, Any] = {
    "market_ticker": pl.Utf8,
    "event_ticker": pl.Utf8,
    "status": pl.Utf8,
    "yes_bid_cents": pl.Int64,
    "yes_ask_cents": pl.Int64,
    "last_price_cents": pl.Int64,
    "volume": pl.Int64,
    "open_interest": pl.Int64,
    "rules_primary": pl.Utf8,
    "observed_at": _DT,
    "raw_payload_id": pl.Int64,
    # Settlement fields (migration 0006, docs/adr/0006-settlement-labels.md)
    # -- collected since Milestone E-A but not previously surfaced to the
    # Polars layer. Only populated on a market's later snapshots once it
    # settles; `build_market_prices` reduces the many snapshot rows per
    # ticker down to one static row before using these.
    "close_time": _DT,
    "result": pl.Utf8,
    "expiration_value": pl.Float64,
    "settlement_ts": _DT,
    "floor_strike": pl.Float64,
    "cap_strike": pl.Float64,
    "strike_type": pl.Utf8,
}
CANDLESTICKS_SCHEMA: dict[str, Any] = {
    "market_ticker": pl.Utf8,
    "period_interval_seconds": pl.Int64,
    "period_start": _DT,
    "period_end": _DT,
    "price_open_cents": pl.Int64,
    "price_high_cents": pl.Int64,
    "price_low_cents": pl.Int64,
    "price_close_cents": pl.Int64,
    "price_mean_cents": pl.Int64,
    "price_close_is_carried_forward": pl.Boolean,
    "yes_bid_open_cents": pl.Int64,
    "yes_bid_high_cents": pl.Int64,
    "yes_bid_low_cents": pl.Int64,
    "yes_bid_close_cents": pl.Int64,
    "yes_ask_open_cents": pl.Int64,
    "yes_ask_high_cents": pl.Int64,
    "yes_ask_low_cents": pl.Int64,
    "yes_ask_close_cents": pl.Int64,
    "volume": pl.Int64,
    "open_interest": pl.Int64,
    "raw_payload_id": pl.Int64,
}
ORDERBOOKS_SCHEMA: dict[str, Any] = {
    "market_ticker": pl.Utf8,
    "captured_at": _DT,
    "best_yes_bid_cents": pl.Int64,
    "best_yes_ask_cents": pl.Int64,
    "spread_cents": pl.Int64,
}
TRADES_SCHEMA: dict[str, Any] = {
    "market_ticker": pl.Utf8,
    "executed_at": _DT,
    "price_cents": pl.Int64,
}
FORECASTS_SCHEMA: dict[str, Any] = {
    "station_id": pl.Utf8,
    "target_date": pl.Date,
    "point_estimate": pl.Float64,
    "issue_time": _DT,
    "valid_start": _DT,
    "valid_end": _DT,
    "raw_payload_id": pl.Int64,
}
OBSERVATIONS_SCHEMA: dict[str, Any] = {
    "station_id": pl.Utf8,
    "variable": pl.Utf8,
    "value": pl.Float64,
    "observation_date": pl.Date,
    "issuance_time": _DT,
    "raw_payload_id": pl.Int64,
}


@dataclass(frozen=True, slots=True)
class SourceFrames:
    stations: pl.DataFrame
    markets: pl.DataFrame
    orderbooks: pl.DataFrame
    trades: pl.DataFrame
    forecasts: pl.DataFrame
    observations: pl.DataFrame
    candlesticks: pl.DataFrame


def _ensure_aware_utc(value: datetime) -> datetime:
    """Interpret a stored timestamp as UTC. Our datetime columns are
    ``TIMESTAMP WITHOUT TIME ZONE`` and every writer stores UTC (CLAUDE.md:
    "use timezone-aware UTC timestamps internally"), so a naive value read back
    is UTC -- attach that zone rather than reject it."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return to_utc(value)


def _naive_utc(value: datetime | None) -> datetime | None:
    """Normalize an instant to naive-UTC so Polars datetime comparisons across
    Postgres and SQLite sources behave identically."""
    if value is None:
        return None
    return _ensure_aware_utc(value).replace(tzinfo=None)


async def load_source_frames(
    session: AsyncSession,
    *,
    start: date | None = None,
    end: date | None = None,
    env_policy: EnvironmentPolicy | None = None,
    coverage: ProvenanceCoverage | None = None,
) -> SourceFrames:
    """Read the source tables into Polars frames. ``start``/``end`` bound the
    weather target dates and market snapshot dates (inclusive) when given.

    ``env_policy`` (ADR 0014) makes loading provenance-aware. When supplied,
    liquidity is admitted only from the policy's environments (production):
    non-production order books and trades are dropped entirely, and
    volume/open-interest on non-production snapshots and candlesticks are set to
    NULL -- *missing*, never zero. Candlestick *prices* are kept for
    production-and-deterministic-NULL rows (the sole candlestick writer has only
    ever read production; see dataset/provenance.py). Environment-invariant
    fields (identity, status, result, close time, strikes) are kept regardless.
    When ``env_policy`` is None the loader behaves exactly as before (no
    filtering), so the generic builder and its fixtures are unaffected. Pass a
    ``coverage`` accumulator to collect per-table environment counts."""

    def _liq(env: str | None) -> bool:
        # Admit liquidity fields? Always True in the no-policy (legacy) mode.
        return env_policy is None or liquidity_admissible(env, env_policy)

    stations = {
        s.station_id: s.timezone for s in (await session.scalars(select(WeatherStation))).all()
    }
    stations_frame = pl.DataFrame(
        [{"station_id": sid, "timezone": tz} for sid, tz in stations.items()],
        schema=STATIONS_SCHEMA,
        orient="row",
    )

    market_rows = []
    for m in (await session.scalars(select(MarketSnapshot))).all():
        observed = _naive_utc(m.observed_at)
        if start is not None and observed is not None and observed.date() < start:
            continue
        if end is not None and observed is not None and observed.date() > end:
            continue
        if coverage is not None and env_policy is not None:
            coverage.record("market_snapshots", m.environment, env_policy)
        # Liquidity/market-state fields require production; identity, status,
        # result, close time, and strikes are environment-invariant (ADR 0013)
        # and kept. Excluded liquidity becomes NULL (missing), never zero.
        liq_ok = _liq(m.environment)
        market_rows.append(
            {
                "market_ticker": m.market_ticker,
                "event_ticker": m.event_ticker,
                "status": m.status,
                "yes_bid_cents": m.yes_bid_cents if liq_ok else None,
                "yes_ask_cents": m.yes_ask_cents if liq_ok else None,
                "last_price_cents": m.last_price_cents if liq_ok else None,
                "volume": m.volume if liq_ok else None,
                "open_interest": m.open_interest if liq_ok else None,
                "rules_primary": m.rules_primary,
                "observed_at": observed,
                "raw_payload_id": m.raw_payload_id,
                "close_time": _naive_utc(m.close_time),
                "result": m.result,
                "expiration_value": float(m.expiration_value)
                if m.expiration_value is not None
                else None,
                "settlement_ts": _naive_utc(m.settlement_ts),
                "floor_strike": float(m.floor_strike) if m.floor_strike is not None else None,
                "cap_strike": float(m.cap_strike) if m.cap_strike is not None else None,
                "strike_type": m.strike_type,
            }
        )
    markets_frame = pl.DataFrame(market_rows, schema=MARKETS_SCHEMA, orient="row")
    known_tickers = set(markets_frame["market_ticker"].to_list())

    # Order books and trades are pure liquidity: a non-production row is dropped
    # entirely (there is no environment-invariant field to preserve).
    orderbook_rows = []
    for o in (await session.scalars(select(OrderbookSnapshot))).all():
        if coverage is not None and env_policy is not None:
            coverage.record("orderbook_snapshots", o.environment, env_policy)
        if o.market_ticker not in known_tickers or not _liq(o.environment):
            continue
        orderbook_rows.append(
            {
                "market_ticker": o.market_ticker,
                "captured_at": _naive_utc(o.captured_at),
                "best_yes_bid_cents": o.best_yes_bid_cents,
                "best_yes_ask_cents": o.best_yes_ask_cents,
                "spread_cents": o.spread_cents,
            }
        )
    orderbooks_frame = pl.DataFrame(orderbook_rows, schema=ORDERBOOKS_SCHEMA, orient="row")

    trade_rows = []
    for t in (await session.scalars(select(TradeRecord))).all():
        if coverage is not None and env_policy is not None:
            coverage.record("trades", t.environment, env_policy)
        if t.market_ticker not in known_tickers or not _liq(t.environment):
            continue
        trade_rows.append(
            {
                "market_ticker": t.market_ticker,
                "executed_at": _naive_utc(t.executed_at),
                "price_cents": t.price_cents,
            }
        )
    trades_frame = pl.DataFrame(trade_rows, schema=TRADES_SCHEMA, orient="row")

    forecast_rows = []
    for f in (await session.scalars(select(WeatherForecast))).all():
        tz = stations.get(f.station_id)
        if tz is None:
            continue  # forecast for an unknown station; skip (station orphan)
        target = local_date(_ensure_aware_utc(f.valid_start), tz)
        if start is not None and target < start:
            continue
        if end is not None and target > end:
            continue
        forecast_rows.append(
            {
                "station_id": f.station_id,
                "target_date": target,
                "point_estimate": float(f.point_estimate),
                "issue_time": _naive_utc(f.issue_time),
                "valid_start": _naive_utc(f.valid_start),
                "valid_end": _naive_utc(f.valid_end),
                "raw_payload_id": f.raw_payload_id,
            }
        )
    forecasts_frame = pl.DataFrame(forecast_rows, schema=FORECASTS_SCHEMA, orient="row")

    observation_rows = []
    for ob in (await session.scalars(select(WeatherObservation))).all():
        if start is not None and ob.observation_date < start:
            continue
        if end is not None and ob.observation_date > end:
            continue
        observation_rows.append(
            {
                "station_id": ob.station_id,
                "variable": ob.variable,
                "value": float(ob.value),
                "observation_date": ob.observation_date,
                "issuance_time": _naive_utc(ob.issuance_time),
                "raw_payload_id": ob.raw_payload_id,
            }
        )
    observations_frame = pl.DataFrame(observation_rows, schema=OBSERVATIONS_SCHEMA, orient="row")

    # Candlestick prices are admissible for production and deterministic-NULL
    # rows (sole writer is always-production price-sync); a non-price-admissible
    # row is dropped. Volume/OI are liquidity: kept only for production, NULL
    # (missing) otherwise -- a NULL candle's price is trustworthy but its volume
    # is unattributable.
    candlestick_rows = []
    for c in (await session.scalars(select(MarketCandlestick))).all():
        if coverage is not None and env_policy is not None:
            coverage.record("market_candlesticks", c.environment, env_policy)
        if c.market_ticker not in known_tickers:
            continue
        if env_policy is not None and not price_admissible(
            c.environment, env_policy, table="market_candlesticks"
        ):
            continue
        c_liq = _liq(c.environment)
        candlestick_rows.append(
            {
                "market_ticker": c.market_ticker,
                "period_interval_seconds": c.period_interval_seconds,
                "period_start": _naive_utc(c.period_start),
                "period_end": _naive_utc(c.period_end),
                "price_open_cents": c.price_open_cents,
                "price_high_cents": c.price_high_cents,
                "price_low_cents": c.price_low_cents,
                "price_close_cents": c.price_close_cents,
                "price_mean_cents": c.price_mean_cents,
                "price_close_is_carried_forward": c.price_close_is_carried_forward,
                "yes_bid_open_cents": c.yes_bid_open_cents,
                "yes_bid_high_cents": c.yes_bid_high_cents,
                "yes_bid_low_cents": c.yes_bid_low_cents,
                "yes_bid_close_cents": c.yes_bid_close_cents,
                "yes_ask_open_cents": c.yes_ask_open_cents,
                "yes_ask_high_cents": c.yes_ask_high_cents,
                "yes_ask_low_cents": c.yes_ask_low_cents,
                "yes_ask_close_cents": c.yes_ask_close_cents,
                "volume": c.volume if c_liq else None,
                "open_interest": c.open_interest if c_liq else None,
                "raw_payload_id": c.raw_payload_id,
            }
        )
    candlesticks_frame = pl.DataFrame(candlestick_rows, schema=CANDLESTICKS_SCHEMA, orient="row")

    return SourceFrames(
        stations=stations_frame,
        markets=markets_frame,
        orderbooks=orderbooks_frame,
        trades=trades_frame,
        forecasts=forecasts_frame,
        observations=observations_frame,
        candlesticks=candlesticks_frame,
    )


# --- Forecast / observation reductions ---------------------------------------


def _forecast_issue_high_low(forecasts: pl.DataFrame) -> pl.DataFrame:
    """One row per (station, target_date, issue_time): the high/low forecast
    for that day from that issuance. 'High'/'low' are the max/min period point
    estimates touching the target date -- an interpretation-light aggregation
    (a period is a National Weather Service forecast segment; the warmest
    segment of a day is its forecast high). Documented in the ADR."""
    return (
        forecasts.group_by(["station_id", "target_date", "issue_time"])
        .agg(
            forecast_high_f=pl.col("point_estimate").max(),
            forecast_low_f=pl.col("point_estimate").min(),
        )
        .sort(["station_id", "target_date", "issue_time"])
    )


def build_forecast_horizon_frame(forecasts: pl.DataFrame) -> pl.DataFrame:
    """One row per (station_id, variable, target_date, issue_time): the same
    high/low aggregation `_forecast_issue_high_low` computes (kept in sync
    deliberately -- both group by the identical key and take the identical
    max/min of `point_estimate`), unpivoted into `tmax_f`/`tmin_f` rows and
    extended with `valid_start` = the earliest period `valid_start` in the
    group -- the lead-time reference point `kalshi_weather.verification`
    needs and `_forecast_issue_high_low`'s callers (`build_weather_panel`)
    have no reason to carry. Second consumer, alongside
    `scripts/build_h0003_forecast_extract.py` (which independently
    implements the identical grouping over its own pinned, frozen extract --
    that script is a closed experiment's artifact and is not refactored to
    call this; this is the shared, canonical form for new callers)."""
    groups = (
        forecasts.group_by(["station_id", "target_date", "issue_time"])
        .agg(
            forecast_high_f=pl.col("point_estimate").max(),
            forecast_low_f=pl.col("point_estimate").min(),
            valid_start=pl.col("valid_start").min(),
        )
        .sort(["station_id", "target_date", "issue_time"])
    )
    return pl.concat(
        [
            groups.select(
                "station_id",
                "target_date",
                "issue_time",
                "valid_start",
                variable=pl.lit("tmax_f"),
                forecast_value="forecast_high_f",
            ),
            groups.select(
                "station_id",
                "target_date",
                "issue_time",
                "valid_start",
                variable=pl.lit("tmin_f"),
                forecast_value="forecast_low_f",
            ),
        ]
    ).sort(["station_id", "variable", "target_date", "issue_time"])


def _settled_observations(observations: pl.DataFrame) -> pl.DataFrame:
    """One row per (station, observation_date): the settled tmax/tmin (the
    value from the *latest* issuance, since later CLI reports supersede
    earlier provisional ones), the settlement issuance time, and how many
    issuances (revisions) were seen."""
    latest = (
        observations.sort("issuance_time")
        .group_by(["station_id", "observation_date", "variable"])
        .agg(
            value=pl.col("value").last(),
            issuance_time=pl.col("issuance_time").last(),
            n_issuances=pl.len(),
        )
    )
    pivoted = latest.pivot(on="variable", index=["station_id", "observation_date"], values="value")
    for col in ("tmax_f", "tmin_f"):
        if col not in pivoted.columns:
            pivoted = pivoted.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))
    settle_meta = latest.group_by(["station_id", "observation_date"]).agg(
        settlement_issuance_time=pl.col("issuance_time").max(),
        n_observation_issuances=pl.col("n_issuances").sum(),
    )
    return (
        pivoted.rename({"tmax_f": "settled_tmax_f", "tmin_f": "settled_tmin_f"})
        .join(settle_meta, on=["station_id", "observation_date"], how="left")
        .rename({"observation_date": "target_date"})
    )


def build_weather_panel(sources: SourceFrames) -> pl.DataFrame:
    """Per-(station, target_date) settled outcome + forecast summary +
    residuals. A full outer join so a day with only forecasts or only
    observations still appears (its missing side is null -- surfaced by the
    validation layer, not silently dropped)."""
    fc_issue = _forecast_issue_high_low(sources.forecasts)
    fc_summary = fc_issue.group_by(["station_id", "target_date"]).agg(
        n_forecast_issuances=pl.col("issue_time").n_unique(),
        first_forecast_issue_time=pl.col("issue_time").min(),
        last_forecast_issue_time=pl.col("issue_time").max(),
        final_forecast_high_f=pl.col("forecast_high_f").sort_by("issue_time").last(),
        final_forecast_low_f=pl.col("forecast_low_f").sort_by("issue_time").last(),
    )
    settled = _settled_observations(sources.observations)

    panel = fc_summary.join(settled, on=["station_id", "target_date"], how="full", coalesce=True)
    panel = panel.with_columns(
        residual_high_f=pl.col("final_forecast_high_f") - pl.col("settled_tmax_f"),
        residual_low_f=pl.col("final_forecast_low_f") - pl.col("settled_tmin_f"),
    )
    return panel.sort(["station_id", "target_date"])


# --- As-of joins --------------------------------------------------------
# The join engine itself lives in dataset/asof.py (reusable, independently
# tested); this module only supplies the dataset-specific join plans.


def _market_map_frame(mappings: list[MarketMapping]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "market_ticker": m.market_ticker,
                "station_id": m.station_id,
                "variable": m.variable,
                "target_date": m.target_date,
            }
            for m in mappings
        ],
        schema={
            "market_ticker": pl.Utf8,
            "station_id": pl.Utf8,
            "variable": pl.Utf8,
            "target_date": pl.Date,
        },
        orient="row",
    )


def build_market_weather(sources: SourceFrames, mappings: list[MarketMapping]) -> pl.DataFrame:
    """Per market snapshot, the weather/order-book/trade facts knowable as of
    that snapshot's timestamp, plus the settled outcome as a labelled target.

    Only markets present in ``mappings`` appear -- an unmapped market has no
    known settlement target, so it is an 'orphan' (reported by validation),
    not a null-weather row here."""
    market_map = _market_map_frame(mappings)
    markets = sources.markets.join(market_map, on="market_ticker", how="inner")

    fc_issue = _forecast_issue_high_low(sources.forecasts).with_columns(
        pl.col("issue_time").alias("forecast_issue_time")
    )
    result = asof_latest(
        markets,
        fc_issue,
        by=["station_id", "target_date"],
        left_time="observed_at",
        right_time="issue_time",
        bring={
            "forecast_high_f": "forecast_high_f",
            "forecast_low_f": "forecast_low_f",
            "forecast_issue_time": "forecast_issue_time",
        },
    )
    result = asof_count(
        result,
        fc_issue,
        by=["station_id", "target_date"],
        left_time="observed_at",
        right_time="issue_time",
        out="n_forecast_issuances_known",
    )

    # As-of known observation for the market's own settled variable. Rename the
    # weather observation's date column to match the market's target_date key.
    obs = sources.observations.rename({"observation_date": "target_date"})
    result = asof_latest(
        result,
        obs,
        by=["station_id", "variable", "target_date"],
        left_time="observed_at",
        right_time="issuance_time",
        bring={"value": "obs_value_known", "issuance_time": "obs_issuance_time_known"},
    )

    result = asof_latest(
        result,
        sources.orderbooks,
        by=["market_ticker"],
        left_time="observed_at",
        right_time="captured_at",
        bring={
            "best_yes_bid_cents": "best_yes_bid_cents",
            "best_yes_ask_cents": "best_yes_ask_cents",
            "spread_cents": "spread_cents",
        },
    )
    result = asof_latest(
        result,
        sources.trades,
        by=["market_ticker"],
        left_time="observed_at",
        right_time="executed_at",
        bring={"price_cents": "last_trade_price_cents"},
    )
    result = asof_count(
        result,
        sources.trades,
        by=["market_ticker"],
        left_time="observed_at",
        right_time="executed_at",
        out="n_trades_known",
    )

    # Settled outcome (post-settlement label): the market's own variable's
    # final value for its target date.
    settled = _settled_observations(sources.observations)
    settled_long = (
        settled.unpivot(
            on=["settled_tmax_f", "settled_tmin_f"],
            index=["station_id", "target_date", "settlement_issuance_time"],
            variable_name="settled_variable",
            value_name="settled_value",
        )
        .with_columns(pl.col("settled_variable").str.replace("settled_", "").alias("variable"))
        .select(
            ["station_id", "target_date", "variable", "settled_value", "settlement_issuance_time"]
        )
    )
    result = result.join(settled_long, on=["station_id", "target_date", "variable"], how="left")
    result = result.with_columns(
        forecast_age_seconds=(
            pl.col("observed_at") - pl.col("forecast_issue_time")
        ).dt.total_seconds()
    )
    return result.sort(["market_ticker", "observed_at"])


@dataclass(frozen=True, slots=True)
class BuiltDataset:
    frames: dict[str, pl.DataFrame]


def orphan_market_tickers(sources: SourceFrames, mappings: list[MarketMapping]) -> list[str]:
    """Market tickers present in the data but absent from the mapping -- they
    have no settlement target and so cannot be joined to weather."""
    mapped = {m.market_ticker for m in mappings}
    present = set(sources.markets["market_ticker"].to_list())
    return sorted(present - mapped)


def orphan_station_ids(sources: SourceFrames) -> list[str]:
    """Stations referenced by weather rows but not in the station registry
    (so their timezone/metadata is unknown)."""
    registry = set(sources.stations["station_id"].to_list())
    referenced = set(sources.observations["station_id"].to_list())
    return sorted(referenced - registry)


def build_observation_issuances(sources: SourceFrames) -> pl.DataFrame:
    """Issuance-level observation extract: one row per stored CLI issuance,
    verbatim from the source table (no derivation). Exists so issuance-level
    research (H0002's pre-registered "versioned weather_panel-adjacent extract
    of first/last issuance per station/variable/day") runs against a
    versioned, content-hashed export rather than an unpinned ad hoc query --
    per RESEARCH.md's standard experiment protocol, step 2."""
    return sources.observations.sort(
        ["station_id", "variable", "observation_date", "issuance_time"]
    )


def _market_metadata(sources: SourceFrames) -> pl.DataFrame:
    """One row per market ticker: the static context (event, close time,
    strike structure) reduced from the many append-only snapshot rows per
    ticker. Settlement fields only appear on a market's *later* snapshots
    (once it settles) and are null before that, so `drop_nulls().last()`
    (chronological) picks the most recent known value per field rather than
    assuming the very last snapshot row has everything populated."""
    if sources.markets.height == 0:
        return sources.markets.select(
            "market_ticker",
            "event_ticker",
            "close_time",
            "floor_strike",
            "cap_strike",
            "strike_type",
        )
    return (
        sources.markets.sort("observed_at")
        .group_by("market_ticker")
        .agg(
            event_ticker=pl.col("event_ticker").drop_nulls().last(),
            close_time=pl.col("close_time").drop_nulls().last(),
            floor_strike=pl.col("floor_strike").drop_nulls().last(),
            cap_strike=pl.col("cap_strike").drop_nulls().last(),
            strike_type=pl.col("strike_type").drop_nulls().last(),
        )
    )


def build_market_prices(sources: SourceFrames) -> pl.DataFrame:
    """Per candle: OHLC/volume/open-interest plus static per-market context
    (event, strike structure). One row per stored candle -- no as-of join is
    needed here (unlike market_weather) since each candle already carries
    its own timestamp; point-in-time filtering (e.g. "candles at or before
    decision time T") is the consumer's job, not this frame's. Settlement
    labels (station_id/variable/target_date/value_at_close/
    value_at_settlement/...) are joined on separately in pipeline.py, reusing
    the same labels frame market_weather already builds -- see
    docs/adr/0007-price-ingestion.md."""
    if sources.candlesticks.height == 0:
        return sources.candlesticks.join(_market_metadata(sources), on="market_ticker", how="left")
    result = sources.candlesticks.join(_market_metadata(sources), on="market_ticker", how="left")
    return result.with_columns(
        data_quality_status=pl.when(pl.col("volume") == 0)
        .then(pl.lit("zero_volume"))
        .otherwise(pl.lit("ok"))
    ).sort(["market_ticker", "period_end"])


#: Columns build_market_price_weather adds beyond build_market_prices + the
#: market map -- declared once so an empty build still yields a correctly
#: typed (empty) frame with every expected column (Phase 7B).
MARKET_PRICE_WEATHER_EXTRA_SCHEMA: dict[str, Any] = {
    "obs_value_known": pl.Float64,
    "obs_issuance_time_known": _DT,
    "obs_age_seconds": pl.Float64,
    "observation_quality_status": pl.Utf8,
    "running_tmax_f_known": pl.Float64,
    "running_tmax_issuance_time_known": _DT,
    "tmax_locked": pl.Boolean,
    "running_tmin_f_known": pl.Float64,
    "running_tmin_issuance_time_known": _DT,
    "tmin_locked": pl.Boolean,
    "theoretical_remaining_range_high_f": pl.Float64,
    "theoretical_remaining_range_low_f": pl.Float64,
}


def build_market_price_weather(
    sources: SourceFrames, mappings: list[MarketMapping]
) -> pl.DataFrame:
    """Per market candle, the weather-observation progression knowable as of
    that candle's own timestamp (``period_end``) -- the point-in-time join
    H0007's readiness assessment identified as missing (Phase 7B,
    docs/adr/0008-point-in-time-alignment.md). Same grain as
    ``market_prices`` (one row per stored candle), restricted to markets
    present in ``mappings`` -- a candle for an unmapped market has no known
    station/variable/target_date to align against, same convention as
    ``market_weather`` (an orphan reported by validation, not a null-weather
    row here).

    Weather progression is attached entirely via `dataset/asof.py`'s
    reusable engine, never inferred or modeled:

    - ``obs_value_known``/``obs_issuance_time_known``: the latest known
      value for the market's OWN settlement variable, exactly as
      ``market_weather`` already defines it.
    - ``running_tmax_f_known``/``running_tmin_f_known``: the running
      cumulative high/low for the market's target date
      (`observation_timeline.compute_running_extremes`), attached
      regardless of which single variable the market itself settles on --
      both are informative, and a market's own variable is a subset of this.
    - ``tmax_locked``/``tmin_locked``: whether the observation window has
      fully elapsed as of this candle (`observation_timeline.
      attach_calendar_lock` -- pure calendar arithmetic), independent of
      whether an issuance has actually arrived yet. Two columns, not one
      shared flag: every resolved settlement spec today uses
      ``observation_window="local_calendar_day"`` for both variables, so
      they currently always agree, but a future spec could diverge.
    - ``theoretical_remaining_range_{high,low}_f``: 0.0 once the
      corresponding variable is locked (no further data can move the
      running value -- H0007's own rationale's "hard bound"), else null
      ("unbounded" -- deliberately not a modeled or forecast estimate).
    - ``obs_age_seconds``: seconds between this candle and the market's own
      variable's ``obs_issuance_time_known`` (null if nothing is known yet).
    - ``observation_quality_status``: ``"known"`` if the market's own
      variable has an as-of observation by this candle, else ``"unknown"``.
    """
    market_map = _market_map_frame(mappings)
    prices = build_market_prices(sources).join(market_map, on="market_ticker", how="inner")
    if prices.height == 0:
        return prices.with_columns(
            [
                pl.lit(None, dtype=dt).alias(name)
                for name, dt in MARKET_PRICE_WEATHER_EXTRA_SCHEMA.items()
            ]
        )

    obs = sources.observations.rename({"observation_date": "target_date"})
    result = asof_latest(
        prices,
        obs,
        by=["station_id", "variable", "target_date"],
        left_time="period_end",
        right_time="issuance_time",
        bring={"value": "obs_value_known", "issuance_time": "obs_issuance_time_known"},
    )

    running = compute_running_extremes(sources.observations).rename(
        {"observation_date": "target_date"}
    )
    result = asof_latest(
        result,
        running.filter(pl.col("variable") == "tmax_f"),
        by=["station_id", "target_date"],
        left_time="period_end",
        right_time="issuance_time",
        bring={
            "running_extreme_f": "running_tmax_f_known",
            "issuance_time": "running_tmax_issuance_time_known",
        },
    )
    result = asof_latest(
        result,
        running.filter(pl.col("variable") == "tmin_f"),
        by=["station_id", "target_date"],
        left_time="period_end",
        right_time="issuance_time",
        bring={
            "running_extreme_f": "running_tmin_f_known",
            "issuance_time": "running_tmin_issuance_time_known",
        },
    )

    result = attach_calendar_lock(
        result,
        sources.stations,
        timestamp_col="period_end",
        target_date_col="target_date",
        out_col="_observation_window_elapsed",
    )
    result = result.with_columns(
        tmax_locked=pl.col("_observation_window_elapsed"),
        tmin_locked=pl.col("_observation_window_elapsed"),
    ).drop("_observation_window_elapsed")

    result = result.with_columns(
        theoretical_remaining_range_high_f=pl.when(pl.col("tmax_locked"))
        .then(pl.lit(0.0))
        .otherwise(pl.lit(None, dtype=pl.Float64)),
        theoretical_remaining_range_low_f=pl.when(pl.col("tmin_locked"))
        .then(pl.lit(0.0))
        .otherwise(pl.lit(None, dtype=pl.Float64)),
        obs_age_seconds=(
            pl.col("period_end") - pl.col("obs_issuance_time_known")
        ).dt.total_seconds(),
        observation_quality_status=pl.when(pl.col("obs_value_known").is_not_null())
        .then(pl.lit("known"))
        .otherwise(pl.lit("unknown")),
    )
    return result.sort(["market_ticker", "period_end"])


def build_datasets(
    sources: SourceFrames,
    mappings: list[MarketMapping],
    *,
    which: str = "all",
) -> BuiltDataset:
    """Build the requested dataset(s). ``which`` is 'all', 'weather_panel',
    'market_weather', 'observation_issuances', 'market_prices', or
    'market_price_weather'."""
    frames: dict[str, pl.DataFrame] = {}
    if which in ("all", "weather_panel"):
        frames["weather_panel"] = build_weather_panel(sources)
    if which in ("all", "market_weather"):
        frames["market_weather"] = build_market_weather(sources, mappings)
    if which in ("all", "observation_issuances"):
        frames["observation_issuances"] = build_observation_issuances(sources)
    if which in ("all", "market_prices"):
        frames["market_prices"] = build_market_prices(sources)
    if which in ("all", "market_price_weather"):
        frames["market_price_weather"] = build_market_price_weather(sources, mappings)
    if not frames:
        raise ValueError(f"unknown dataset selection {which!r}")
    return BuiltDataset(frames=frames)
