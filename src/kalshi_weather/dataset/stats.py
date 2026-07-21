"""Summary statistics for a built research dataset.

Descriptive only -- counts, coverage, missing/duplicate rates. Nothing here
models or predicts anything (that's Phase 4+); this is the "is the data
plausible and how much of it is there" snapshot a researcher checks before
trusting a build.
"""

from typing import Any

import polars as pl

from kalshi_weather.dataset.builder import SourceFrames


def _null_fraction(frame: pl.DataFrame, columns: list[str]) -> dict[str, float]:
    if frame.height == 0:
        return {c: 0.0 for c in columns if c in frame.columns}
    return {
        c: frame.select(pl.col(c).is_null().mean()).item()
        for c in columns
        if c in frame.columns
    }


def _duplicate_rate(frame: pl.DataFrame, keys: list[str]) -> float:
    if frame.height == 0:
        return 0.0
    unique = frame.select(keys).unique().height
    return (frame.height - unique) / frame.height


def compute_stats(
    sources: SourceFrames, frames: dict[str, pl.DataFrame]
) -> dict[str, Any]:
    panel = frames.get("weather_panel", pl.DataFrame())
    market_weather = frames.get("market_weather", pl.DataFrame())

    n_stations = sources.stations.height
    n_markets = sources.markets.select("market_ticker").n_unique() if sources.markets.height else 0
    n_forecast_issuances = (
        sources.forecasts.select(["station_id", "target_date", "issue_time"]).unique().height
        if sources.forecasts.height
        else 0
    )

    # Coverage: fraction of panel days that have *both* a forecast and a
    # settled observation (the joinable, research-usable days).
    coverage_both = 0.0
    if panel.height and "settled_tmax_f" in panel.columns:
        both = panel.filter(
            pl.col("n_forecast_issuances").is_not_null()
            & pl.col("settled_tmax_f").is_not_null()
        ).height
        coverage_both = both / panel.height

    avg_forecasts_per_market = 0.0
    if market_weather.height and "n_forecast_issuances_known" in market_weather.columns:
        avg_forecasts_per_market = market_weather.select(
            pl.col("n_forecast_issuances_known").mean()
        ).item()

    avg_observations_per_station = (
        sources.observations.height / n_stations if n_stations else 0.0
    )

    return {
        "counts": {
            "stations": n_stations,
            "markets": n_markets,
            "market_snapshots": sources.markets.height,
            "orderbook_snapshots": sources.orderbooks.height,
            "trades": sources.trades.height,
            "forecast_rows": sources.forecasts.height,
            "forecast_issuances": n_forecast_issuances,
            "observations": sources.observations.height,
            "weather_panel_rows": panel.height,
            "market_weather_rows": market_weather.height,
        },
        "coverage": {
            "panel_days_with_forecast_and_observation": coverage_both,
        },
        "averages": {
            "forecasts_known_per_market_snapshot": avg_forecasts_per_market,
            "observations_per_station": avg_observations_per_station,
        },
        "missing_value_fraction": {
            "weather_panel": _null_fraction(
                panel, ["settled_tmax_f", "settled_tmin_f", "final_forecast_high_f"]
            ),
            "market_weather": _null_fraction(
                market_weather, ["forecast_high_f", "obs_value_known", "settled_value"]
            ),
        },
        "duplicate_rate": {
            "weather_panel": _duplicate_rate(panel, ["station_id", "target_date"]),
            "market_weather": _duplicate_rate(market_weather, ["market_ticker", "observed_at"]),
        },
    }
