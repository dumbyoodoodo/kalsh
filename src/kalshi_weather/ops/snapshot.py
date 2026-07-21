"""Daily research snapshots (Phase 6).

A research snapshot is the Milestone 4 versioned dataset export (Parquet +
manifest + validation + stats -- already carrying git commit, source DB
revision, and content hashes) **plus** the operational context of the moment
it was cut: the data-quality report, recent collector metrics, and the
settlement parser's confidence distribution. Together these let any future
experiment reproduce the exact dataset used *and* audit the health of the
platform that produced it.

Written into the same immutable `<DATASET_ROOT>/<version>/` directory as two
extra sidecars: `quality.json` and `ops.json`.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.dataset import pipeline as dataset_pipeline
from kalshi_weather.dataset.export import ExportFormat
from kalshi_weather.domain.time import utc_now
from kalshi_weather.ops.health import build_health_report
from kalshi_weather.ops.quality import run_quality_checks
from kalshi_weather.settlement.resolver import CompositeSettlementResolver
from kalshi_weather.settlement.spec import PARSER_VERSION
from kalshi_weather.storage.repositories import get_recent_collector_runs


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    version: str
    output_dir: Path
    dataset_ok: bool
    quality_ok: bool


def default_snapshot_version() -> str:
    """Date-keyed version (one snapshot per day by convention); a second
    snapshot the same day needs an explicit version, since versions are
    immutable."""
    return utc_now().strftime("snapshot-%Y%m%d")


async def create_snapshot(
    session: AsyncSession,
    *,
    database_url: str,
    overrides_path: Path | None,
    root: Path,
    version: str,
    kalshi_interval_seconds: float,
    weather_interval_seconds: float,
    stale_after_intervals: float,
) -> SnapshotResult:
    """Build + export the versioned dataset, then attach quality and ops
    sidecars. Fails loudly if the version directory already exists."""
    resolver = CompositeSettlementResolver(overrides_path)
    mappings, resolution = await resolver.resolve_report(session)
    output = await dataset_pipeline.build(
        session,
        database_url=database_url,
        mappings=mappings,
        which="all",
        version=version,
        resolver_meta={
            "resolver": "parser+overrides",
            "parser_version": PARSER_VERSION,
            "resolution_counts": resolution.counts(),
            "overridden_markets": sorted(resolution.overridden),
        },
    )
    export = dataset_pipeline.export(
        output, root=root, version=version, fmt=ExportFormat.PARQUET
    )

    quality = await run_quality_checks(session)
    (export.output_dir / "quality.json").write_text(
        json.dumps(quality.to_dict(), indent=2, default=str), encoding="utf-8"
    )

    health = await build_health_report(
        session,
        kalshi_interval_seconds=kalshi_interval_seconds,
        weather_interval_seconds=weather_interval_seconds,
        stale_after_intervals=stale_after_intervals,
        quality=quality,
    )
    runs = await get_recent_collector_runs(session, limit=50)
    ops_payload: dict[str, Any] = {
        "generated_at": utc_now().isoformat(),
        "health": health.to_dict(),
        "settlement_resolution": resolution.to_dict(),
        "recent_collector_runs": [
            {
                "collector": r.collector,
                "started_at": r.started_at.isoformat(),
                "duration_seconds": r.duration_seconds,
                "success": r.success,
                "requests_attempted": r.requests_attempted,
                "retries": r.retries,
                "stats": r.stats_json,
                "error": r.error,
            }
            for r in runs
        ],
    }
    (export.output_dir / "ops.json").write_text(
        json.dumps(ops_payload, indent=2, default=str), encoding="utf-8"
    )

    return SnapshotResult(
        version=version,
        output_dir=export.output_dir,
        dataset_ok=output.validation.ok,
        quality_ok=quality.ok,
    )
