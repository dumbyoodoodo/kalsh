"""Deterministic decision-grain builder for H0018.

One row per eligible settled weather market at a fixed horizon (default 24h)
before close. Every feature is selected strictly as-of the decision timestamp
(no information created/published after it), so the grain is leakage-safe by
construction. Markets missing any required point-in-time input are excluded with
a recorded reason -- nothing is imputed and no liquidity feature is used.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import polars as pl

from kalshi_weather.dataset.builder import SourceFrames
from kalshi_weather.settlement.labels import SettlementLabel

_STATIONS = ("CHI", "DEN", "LAX", "NYC")


@dataclass(frozen=True)
class DecisionGrain:
    frame: pl.DataFrame  # one row per eligible market
    exclusions: dict[str, int]  # reason -> count
    horizon_hours: int


def _market_strikes(sources: SourceFrames) -> pl.DataFrame:
    """One row per ticker: the (environment-invariant) strike/direction, taken
    from any snapshot (identity fields don't vary by environment; ADR 0013)."""
    m = sources.markets
    if m.height == 0:
        return pl.DataFrame(
            schema={
                "market_ticker": pl.Utf8,
                "floor_strike": pl.Float64,
                "cap_strike": pl.Float64,
                "strike_type": pl.Utf8,
            }
        )
    return m.group_by("market_ticker").agg(
        pl.col("floor_strike").drop_nulls().first().alias("floor_strike"),
        pl.col("cap_strike").drop_nulls().first().alias("cap_strike"),
        pl.col("strike_type").drop_nulls().first().alias("strike_type"),
    )


def build_decision_grain(
    sources: SourceFrames,
    labels: list[SettlementLabel],
    *,
    horizon_hours: int = 24,
) -> DecisionGrain:
    exclusions: Counter[str] = Counter()

    # eligible labels: weather station, binary result, real close time
    rows = []
    for lb in labels:
        if lb.station_id not in _STATIONS:
            exclusions["not_weather_station"] += 1
            continue
        if lb.kalshi_result not in ("yes", "no"):
            exclusions["no_binary_result"] += 1
            continue
        if lb.close_time is None:
            exclusions["no_close_time"] += 1
            continue
        rows.append(
            {
                "market_ticker": lb.market_ticker,
                "station": lb.station_id,
                "variable": lb.variable,
                "target_date": lb.target_date,
                "close_time": lb.close_time,
                "y": 1 if lb.kalshi_result == "yes" else 0,
            }
        )
    if not rows:
        return DecisionGrain(pl.DataFrame(), dict(exclusions), horizon_hours)

    grain = pl.DataFrame(rows).with_columns(
        (pl.col("close_time") - pl.duration(hours=horizon_hours)).alias("decision_time")
    )
    grain = grain.join(_market_strikes(sources), on="market_ticker", how="left").with_columns(
        pl.coalesce(["floor_strike", "cap_strike"]).alias("threshold"),
        pl.when(pl.col("strike_type") == "greater").then(1).otherwise(-1).alias("direction"),
    )

    # --- as-of market price: latest candle close with period_end <= decision ---
    cd = sources.candlesticks.filter(pl.col("price_close_cents").is_not_null())
    grain = _asof_latest(
        grain,
        cd,
        on="market_ticker",
        time_left="decision_time",
        time_right="period_end",
        take={"price_close_cents": "mkt_close_cents", "period_end": "price_asof"},
    )

    # --- as-of forecast: latest issuance for (station,target_date) <= decision ---
    fc = sources.forecasts
    grain = _asof_latest(
        grain,
        fc,
        on=["station", "target_date"],
        time_left="decision_time",
        time_right="issue_time",
        take={"point_estimate": "fc_point", "issue_time": "fc_issue"},
        left_on_map={"station": "station_id"} if "station_id" in fc.columns else None,
    )

    # --- as-of observation: latest observation for station <= decision ---
    ob = sources.observations
    grain = _asof_latest(
        grain,
        ob,
        on=["station"],
        time_left="decision_time",
        time_right="issuance_time",
        take={"value": "obs_value", "issuance_time": "obs_time"},
        left_on_map={"station": "station_id"} if "station_id" in ob.columns else None,
    )

    # --- eligibility filtering on required point-in-time inputs ---
    def _drop(df: pl.DataFrame, cond: pl.Expr, reason: str) -> pl.DataFrame:
        n_before = df.height
        kept = df.filter(~cond)
        exclusions[reason] += n_before - kept.height
        return kept

    grain = _drop(grain, pl.col("threshold").is_null(), "no_parsed_threshold")
    grain = _drop(grain, pl.col("mkt_close_cents").is_null(), "no_market_price_at_horizon")
    grain = _drop(grain, pl.col("fc_point").is_null(), "no_forecast_at_horizon")
    grain = _drop(grain, pl.col("obs_value").is_null(), "no_observation_at_horizon")

    if grain.height == 0:
        return DecisionGrain(grain, dict(exclusions), horizon_hours)

    # --- derived features (all as-of safe) ---
    grain = grain.with_columns(
        (pl.col("mkt_close_cents") / 100.0).alias("mkt_prob"),
        (pl.col("fc_point") - pl.col("threshold")).alias("fc_gap"),
        (pl.col("obs_value") - pl.col("threshold")).alias("obs_gap"),
        ((pl.col("decision_time") - pl.col("fc_issue")).dt.total_minutes() / 60.0).alias(
            "fc_age_h"
        ),
        ((pl.col("decision_time") - pl.col("obs_time")).dt.total_minutes() / 60.0).alias(
            "obs_age_h"
        ),
        # direction-signed gaps: positive => forecast/obs favors YES
        (pl.col("direction") * (pl.col("fc_point") - pl.col("threshold"))).alias("fc_gap_dir"),
        (pl.col("direction") * (pl.col("obs_value") - pl.col("threshold"))).alias("obs_gap_dir"),
        # Independent inference unit (H0019 registration): the underlying
        # weather outcome = (station, variable, target_date). Threshold markets
        # on the same outcome share a group and are never treated as independent.
        (
            pl.col("station")
            + "|"
            + pl.col("variable").fill_null("_")
            + "|"
            + pl.col("target_date").cast(pl.Utf8)
        ).alias("event_group"),
    ).sort(["target_date", "station", "market_ticker"])
    return DecisionGrain(grain, dict(exclusions), horizon_hours)


def _asof_latest(
    left: pl.DataFrame,
    right: pl.DataFrame,
    *,
    on: str | list[str],
    time_left: str,
    time_right: str,
    take: dict[str, str],
    left_on_map: dict[str, str] | None = None,
) -> pl.DataFrame:
    """For each left row, pick the right row with the greatest ``time_right``
    that is <= the left row's ``time_left``, matched on ``on``. Pure as-of."""
    keys = [on] if isinstance(on, str) else list(on)
    right_keys = keys
    if left_on_map:
        right = right.rename({v: k for k, v in left_on_map.items()})
    take_cols = list(take.values())
    if right.height == 0:
        return left.with_columns([pl.lit(None).alias(v) for v in take_cols])
    # Copy the time column into _rt first, so it can also appear (renamed) in
    # `take` (e.g. period_end -> price_asof) without a duplicate projection.
    need = list(dict.fromkeys([*right_keys, time_right, *take.keys()]))
    r = right.select(need).with_columns(pl.col(time_right).alias("_rt")).rename(take)
    # Keep ONLY past matches (<= decision time); pick the latest per market.
    valid = (
        left.join(r, on=right_keys, how="left")
        .filter(pl.col("_rt").is_not_null() & (pl.col("_rt") <= pl.col(time_left)))
        .sort("_rt")
        .group_by("market_ticker", maintain_order=True)
        .agg(*[pl.col(c).last() for c in take_cols])
    )
    # Left-join the picked feature back onto EVERY original market, so a market
    # whose only matches are in the future (or that has no match) survives with
    # NULL features -- it is never silently dropped (it is excluded downstream
    # with a recorded reason instead).
    return left.join(valid, on="market_ticker", how="left")
