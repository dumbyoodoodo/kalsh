"""Build the versioned forecast-horizon extract for H0003.

One row per (station_id, variable, target_date, issue_time): the daily
high/low forecast derived from that issuance (reusing the platform's
existing `_forecast_issue_high_low` / ADR 0004 max/min-of-periods
convention, verbatim -- not reinvented), the horizon in hours (the
standard NWS-verification lead time: earliest `valid_start` among the
periods contributing to that day's aggregation, minus `issue_time` --
NOT a calendar-midnight reference, which produces negative horizons for
ordinary same-day forecasts; see PREREG Sec 2/12 R2), and the settled
truth (latest-issuance CLI value -- the `_settled_observations`
convention `build_weather_panel` already uses), where it exists yet.

Read-only against the database; writes a versioned parquet + manifest
(content hash, row count, git commit) under
<KALSHI_DATA_DIR>/datasets/<version>/. Prints STRUCTURAL statistics
only (row counts, coverage, horizon distribution) -- no bias/MAE/error
statistic is computed here, per the pre-registration discipline (PREREG
Sec 0/10): those exist only in the pre-registered analysis script.

Usage:
    python scripts/build_h0003_forecast_extract.py --version exp-20260722-h0003-forecast-extract
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kalshi_weather.config import get_settings
from kalshi_weather.dataset.builder import load_source_frames
from kalshi_weather.dataset.manifest import frame_content_hash
from kalshi_weather.storage.database import create_engine, create_session_factory

STATIONS = ["CHI", "DEN", "LAX", "NYC"]
VARIABLES = ["tmax_f", "tmin_f"]


def _forecast_groups(forecasts: pl.DataFrame) -> pl.DataFrame:
    """One row per (station_id, target_date, issue_time): forecast_high_f,
    forecast_low_f (verbatim `_forecast_issue_high_low` convention -- max/min
    of point estimates over periods touching target_date) plus
    earliest_valid_start (new: min(valid_start) over the same group, the
    lead-time reference point)."""
    return (
        forecasts.group_by(["station_id", "target_date", "issue_time"])
        .agg(
            forecast_high_f=pl.col("point_estimate").max(),
            forecast_low_f=pl.col("point_estimate").min(),
            earliest_valid_start=pl.col("valid_start").min(),
        )
        .sort(["station_id", "target_date", "issue_time"])
    )


def _settled_truth(observations: pl.DataFrame) -> pl.DataFrame:
    """One row per (station_id, variable, observation_date): the latest-
    issuance value and its finalizing issuance_time -- the same convention
    `_settled_observations` uses (verbatim, reimplemented here since that
    helper is builder-module-private and pivots differently)."""
    return (
        observations.sort("issuance_time")
        .group_by(["station_id", "variable", "observation_date"])
        .agg(
            settled_value=pl.col("value").last(),
            settled_issuance_time=pl.col("issuance_time").last(),
        )
    )


def build_extract(forecasts: pl.DataFrame, observations: pl.DataFrame) -> pl.DataFrame:
    groups = _forecast_groups(forecasts)
    unpivoted = pl.concat(
        [
            groups.select(
                station_id="station_id",
                target_date="target_date",
                issue_time="issue_time",
                earliest_valid_start="earliest_valid_start",
                variable=pl.lit("tmax_f"),
                forecast_value="forecast_high_f",
            ),
            groups.select(
                station_id="station_id",
                target_date="target_date",
                issue_time="issue_time",
                earliest_valid_start="earliest_valid_start",
                variable=pl.lit("tmin_f"),
                forecast_value="forecast_low_f",
            ),
        ]
    )
    truth = _settled_truth(observations)
    joined = unpivoted.join(
        truth,
        left_on=["station_id", "variable", "target_date"],
        right_on=["station_id", "variable", "observation_date"],
        how="left",
    )
    horizon_hours = (
        pl.col("earliest_valid_start") - pl.col("issue_time")
    ).dt.total_seconds() / 3600.0
    return joined.with_columns(horizon_hours=horizon_hours).sort(
        ["station_id", "variable", "target_date", "issue_time"]
    )


async def _load(start: str | None, end: str | None) -> tuple[pl.DataFrame, pl.DataFrame]:
    from datetime import date as _date

    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        sources = await load_source_frames(
            session,
            start=_date.fromisoformat(start) if start else None,
            end=_date.fromisoformat(end) if end else None,
        )
    await engine.dispose()
    return sources.forecasts, sources.observations


if __name__ == "__main__":
    import asyncio
    import contextlib

    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    forecasts, observations = asyncio.run(_load(args.start, args.end))

    settings = get_settings()
    out_dir = settings.ensure_dataset_root() / args.version
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "forecast_horizon_extract.parquet"
    if out_path.exists():
        print(f"refusing to overwrite existing immutable extract: {out_path}", file=sys.stderr)
        sys.exit(1)

    extract = build_extract(forecasts, observations)
    extract.write_parquet(out_path)
    content_hash = frame_content_hash(pl.read_parquet(out_path))

    git_commit = None
    with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
        git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    manifest = {
        "version": args.version,
        "frame": "forecast_horizon_extract.parquet",
        "content_hashes": {"forecast_horizon_extract": content_hash},
        "row_counts": {"forecast_horizon_extract": extract.height},
        "git_commit": git_commit,
        "built_at": datetime.now(UTC).isoformat(),
        "source": (
            "weather_forecasts + weather_observations via "
            "kalshi_weather.dataset.builder.load_source_frames; "
            "high/low aggregation reuses _forecast_issue_high_low's convention (ADR 0004)"
        ),
        "note": (
            "structural extract for H0003; one row per "
            "(station_id, variable, target_date, issue_time)"
        ),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # STRUCTURAL statistics only (pre-registration discipline; see docstring).
    print(f"wrote {out_path}")
    print(f"content_hash: {content_hash}")
    print(f"rows: {extract.height}")
    for station in STATIONS:
        for variable in VARIABLES:
            sub = extract.filter(
                (pl.col("station_id") == station) & (pl.col("variable") == variable)
            )
            settled = sub.filter(pl.col("settled_value").is_not_null()).height
            hz: dict[str, int] = defaultdict(int)
            for v in sub["horizon_hours"].to_list():
                if v is None:
                    hz["null"] += 1
                elif v < 0:
                    hz["negative"] += 1
                elif v < 12:
                    hz["0-12h"] += 1
                elif v < 24:
                    hz["12-24h"] += 1
                elif v < 48:
                    hz["24-48h"] += 1
                else:
                    hz["48h+"] += 1
            print(
                f"  {station} {variable}: rows={sub.height} with_settled_truth={settled} "
                f"horizon_buckets={dict(hz)}"
            )
