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

from kalshi_weather.dataset.market_map import MarketMapping
from kalshi_weather.dataset.pit import local_date
from kalshi_weather.domain.time import to_utc
from kalshi_weather.storage.models import (
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
) -> SourceFrames:
    """Read the source tables into Polars frames. ``start``/``end`` bound the
    weather target dates and market snapshot dates (inclusive) when given."""
    stations = {
        s.station_id: s.timezone
        for s in (await session.scalars(select(WeatherStation))).all()
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
        market_rows.append(
            {
                "market_ticker": m.market_ticker,
                "event_ticker": m.event_ticker,
                "status": m.status,
                "yes_bid_cents": m.yes_bid_cents,
                "yes_ask_cents": m.yes_ask_cents,
                "last_price_cents": m.last_price_cents,
                "volume": m.volume,
                "open_interest": m.open_interest,
                "rules_primary": m.rules_primary,
                "observed_at": observed,
                "raw_payload_id": m.raw_payload_id,
            }
        )
    markets_frame = pl.DataFrame(market_rows, schema=MARKETS_SCHEMA, orient="row")
    known_tickers = set(markets_frame["market_ticker"].to_list())

    orderbook_rows = [
        {
            "market_ticker": o.market_ticker,
            "captured_at": _naive_utc(o.captured_at),
            "best_yes_bid_cents": o.best_yes_bid_cents,
            "best_yes_ask_cents": o.best_yes_ask_cents,
            "spread_cents": o.spread_cents,
        }
        for o in (await session.scalars(select(OrderbookSnapshot))).all()
        if o.market_ticker in known_tickers
    ]
    orderbooks_frame = pl.DataFrame(orderbook_rows, schema=ORDERBOOKS_SCHEMA, orient="row")

    trade_rows = [
        {
            "market_ticker": t.market_ticker,
            "executed_at": _naive_utc(t.executed_at),
            "price_cents": t.price_cents,
        }
        for t in (await session.scalars(select(TradeRecord))).all()
        if t.market_ticker in known_tickers
    ]
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

    return SourceFrames(
        stations=stations_frame,
        markets=markets_frame,
        orderbooks=orderbooks_frame,
        trades=trades_frame,
        forecasts=forecasts_frame,
        observations=observations_frame,
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
    pivoted = latest.pivot(
        on="variable", index=["station_id", "observation_date"], values="value"
    )
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


# --- As-of join helpers ------------------------------------------------------


def _asof_latest(
    base: pl.DataFrame,
    other: pl.DataFrame,
    *,
    by: list[str],
    left_time: str,
    right_time: str,
    bring: dict[str, str],
) -> pl.DataFrame:
    """Left-join onto ``base`` the most recent ``other`` row per ``by`` group
    whose ``right_time`` is <= ``base[left_time]`` (a backward as-of join --
    the leakage-free 'latest known as of now' operation). ``bring`` maps
    source column -> output column."""
    if base.height == 0 or other.height == 0:
        return base.with_columns(
            [pl.lit(None, dtype=other.schema[src]).alias(out) for src, out in bring.items()]
        )
    # Select the join keys + join-time + sources once (dedup in case a source
    # *is* the join-time column), then materialize the renamed output columns
    # as copies so the original join-time column survives for join_asof.
    sub = other.select(list(dict.fromkeys([*by, right_time, *bring.keys()]))).with_columns(
        [pl.col(src).alias(out) for src, out in bring.items()]
    )
    left = base.sort(left_time)
    right = sub.sort(right_time)
    joined = left.join_asof(
        right,
        left_on=left_time,
        right_on=right_time,
        by=by,
        strategy="backward",
        check_sortedness=False,
    )
    return joined.select([*base.columns, *bring.values()])


def _asof_count(
    base: pl.DataFrame,
    other: pl.DataFrame,
    *,
    by: list[str],
    left_time: str,
    right_time: str,
    out: str,
) -> pl.DataFrame:
    """Attach to ``base`` the number of ``other`` rows per ``by`` group whose
    ``right_time`` <= ``base[left_time]`` -- a running count, computed via
    cumulative count + backward as-of so it stays leakage-free."""
    if base.height == 0 or other.height == 0:
        return base.with_columns(pl.lit(0, dtype=pl.Int64).alias(out))
    counted = (
        other.select([*by, right_time])
        .sort([*by, right_time])
        .with_columns(pl.int_range(1, pl.len() + 1).over(by).alias(out))
    )
    left = base.sort(left_time)
    right = counted.sort(right_time)
    joined = left.join_asof(
        right,
        left_on=left_time,
        right_on=right_time,
        by=by,
        strategy="backward",
        check_sortedness=False,
    )
    return joined.drop(right_time).with_columns(pl.col(out).fill_null(0))


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


def build_market_weather(
    sources: SourceFrames, mappings: list[MarketMapping]
) -> pl.DataFrame:
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
    result = _asof_latest(
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
    result = _asof_count(
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
    result = _asof_latest(
        result,
        obs,
        by=["station_id", "variable", "target_date"],
        left_time="observed_at",
        right_time="issuance_time",
        bring={"value": "obs_value_known", "issuance_time": "obs_issuance_time_known"},
    )

    result = _asof_latest(
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
    result = _asof_latest(
        result,
        sources.trades,
        by=["market_ticker"],
        left_time="observed_at",
        right_time="executed_at",
        bring={"price_cents": "last_trade_price_cents"},
    )
    result = _asof_count(
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
        .with_columns(
            pl.col("settled_variable").str.replace("settled_", "").alias("variable")
        )
        .select(
            ["station_id", "target_date", "variable", "settled_value", "settlement_issuance_time"]
        )
    )
    result = result.join(
        settled_long, on=["station_id", "target_date", "variable"], how="left"
    )
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


def build_datasets(
    sources: SourceFrames,
    mappings: list[MarketMapping],
    *,
    which: str = "all",
) -> BuiltDataset:
    """Build the requested dataset(s). ``which`` is 'all', 'weather_panel', or
    'market_weather'."""
    frames: dict[str, pl.DataFrame] = {}
    if which in ("all", "weather_panel"):
        frames["weather_panel"] = build_weather_panel(sources)
    if which in ("all", "market_weather"):
        frames["market_weather"] = build_market_weather(sources, mappings)
    if not frames:
        raise ValueError(f"unknown dataset selection {which!r}")
    return BuiltDataset(frames=frames)
