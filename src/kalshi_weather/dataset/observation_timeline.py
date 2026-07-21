"""Point-in-time reconstruction of the intraday weather-observation timeline
(Phase 7B, docs/adr/0008-point-in-time-alignment.md).

Two independent, deterministic, model-free computations -- no forecasting,
no statistics, no fitted parameters:

- `compute_running_extremes`: the running daily high/low *as reported by
  successive same-day CLI issuances*, taken in issuance-chronological order.
  Each new issuance can only add information already published by NWS, so
  the running value is leak-free by construction (see RESEARCH.md's
  look-ahead rule) -- it is exactly "the hard bound known so far" that
  H0007's rationale describes, not a prediction of the eventual settled
  value.
- `attach_calendar_lock`: whether a target date's observation window (a
  station-local *calendar day*, per every resolved `settlement_specs.
  observation_window` in this project) has fully elapsed as of a given
  instant. Pure calendar arithmetic -- no observation data, no model. Loops
  over the distinct station_ids present (a small, bounded set even with
  many cities), fully vectorized within each.
"""

import polars as pl


def compute_running_extremes(observations: pl.DataFrame) -> pl.DataFrame:
    """Add ``running_extreme_f``: the cumulative max (``tmax_f``) or min
    (``tmin_f``) of ``value`` within each ``(station_id, variable,
    observation_date)`` group, taken in ``issuance_time`` order -- a
    monotonic, hard bound on the eventual settled value that only ever
    tightens as later same-day issuances arrive, never loosens from a future
    one. tmax_f's running value is non-decreasing (a lower bound on the
    eventual high); tmin_f's is non-increasing (an upper bound on the
    eventual low)."""
    if observations.height == 0:
        return observations.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("running_extreme_f")
        )
    sorted_obs = observations.sort(["station_id", "variable", "observation_date", "issuance_time"])
    group = ["station_id", "variable", "observation_date"]
    is_tmax = pl.col("variable") == "tmax_f"
    running = (
        pl.when(is_tmax)
        .then(pl.col("value").cum_max().over(group))
        .otherwise(pl.col("value").cum_min().over(group))
        .alias("running_extreme_f")
    )
    return sorted_obs.with_columns(running)


def attach_calendar_lock(
    frame: pl.DataFrame,
    stations: pl.DataFrame,
    *,
    timestamp_col: str,
    target_date_col: str,
    out_col: str = "observation_window_elapsed",
) -> pl.DataFrame:
    """Attach a boolean: has the station-local calendar day of
    ``target_date_col`` fully elapsed as of ``timestamp_col`` (naive-UTC)?

    Requires ``frame`` to carry a ``station_id`` column; timezone comes from
    ``stations`` (``station_id`` -> IANA timezone, `dataset/builder.py`'s
    ``STATIONS_SCHEMA``). A station absent from the registry yields a null
    (unknown), never a guessed timezone.
    """
    if frame.height == 0:
        return frame.with_columns(pl.lit(None, dtype=pl.Boolean).alias(out_col))
    tz_lookup = dict(
        zip(stations["station_id"].to_list(), stations["timezone"].to_list(), strict=True)
    )
    parts = []
    for station_id in frame["station_id"].unique(maintain_order=True).to_list():
        sub = frame.filter(pl.col("station_id") == station_id)
        tz = tz_lookup.get(station_id)
        if tz is None:
            parts.append(sub.with_columns(pl.lit(None, dtype=pl.Boolean).alias(out_col)))
            continue
        local_date = (
            sub[timestamp_col].dt.replace_time_zone("UTC").dt.convert_time_zone(tz).dt.date()
        )
        parts.append(sub.with_columns((local_date > sub[target_date_col]).alias(out_col)))
    return pl.concat(parts)
