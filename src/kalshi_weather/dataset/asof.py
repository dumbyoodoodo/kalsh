"""Reusable point-in-time ("as-of") join engine.

The single operation every leakage-free dataset in this project needs is:
*attach to each row of a base frame the most recent fact from another frame
that was already knowable at the base row's own timestamp*. `market_weather`
(Milestone 4) and `market_prices`/`market_price_weather` (Phase 7A/7B) all
reduce to this primitive, so it lives here once rather than being
reimplemented per dataset.

Built on Polars' native `join_asof` with a `"backward"` strategy, which by
construction can only match a right-side row whose timestamp is `<=` the
left-side timestamp -- a later (future) row can never be selected. This
makes the engine leakage-free by construction, not by convention: there is
no code path that could accidentally pick a future row.

Deterministic, vectorized (no per-row Python loop), and resolution-agnostic
-- the join key is a timestamp column of any granularity (1-minute candles,
5-minute snapshots, daily observations all work identically), and `by`
supports any number of grouping columns, so multiple cities/stations join
correctly in one call rather than needing a per-station loop.
"""

import polars as pl


def asof_latest(
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
    the leakage-free "latest known as of now" operation). ``bring`` maps
    source column -> output column. ``by`` may be any number of columns
    (e.g. ``["station_id"]`` for one city, ``["station_id", "variable"]`` for
    multiple cities x multiple variables at once)."""
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


def asof_count(
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
