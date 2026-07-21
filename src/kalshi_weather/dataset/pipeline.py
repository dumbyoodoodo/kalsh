"""End-to-end dataset build orchestration.

Ties the pieces together: read source frames from the DB, build the requested
datasets, validate them, compute statistics, and assemble a manifest -- then
(optionally) export to a versioned directory. Kept out of ``builder.py`` so the
pure build logic stays free of IO and settings.
"""

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.dataset.builder import build_datasets, load_source_frames
from kalshi_weather.dataset.export import ExportFormat, ExportResult, export_dataset
from kalshi_weather.dataset.manifest import DatasetManifest, build_manifest
from kalshi_weather.dataset.market_map import MarketMapping
from kalshi_weather.dataset.stats import compute_stats
from kalshi_weather.dataset.validation import ValidationReport, validate


@dataclass(frozen=True, slots=True)
class BuildOutput:
    frames: dict[str, pl.DataFrame]
    manifest: DatasetManifest
    validation: ValidationReport
    stats: dict[str, Any]


async def _alembic_revision(session: AsyncSession) -> str | None:
    """Current Alembic revision of the source DB, if the table exists -- the
    'source database version' a manifest records for reproducibility."""
    try:
        result = await session.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:
        return None
    return str(result) if result is not None else None


def _mapping_hash(mappings: list[MarketMapping]) -> str:
    """Stable hash of the market->settlement mapping, recorded in the manifest
    config so a dataset build is reproducible only against the same mapping."""
    canonical = ";".join(
        f"{m.market_ticker}|{m.station_id}|{m.variable}|{m.target_date.isoformat()}"
        for m in sorted(mappings, key=lambda m: m.market_ticker)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def build(
    session: AsyncSession,
    *,
    database_url: str,
    mappings: list[MarketMapping],
    which: str = "all",
    start: date | None = None,
    end: date | None = None,
    version: str = "unversioned",
    repo_dir: Path | None = None,
    resolver_meta: dict[str, Any] | None = None,
) -> BuildOutput:
    """Load, build, validate, and describe the dataset(s) -- everything except
    writing to disk. ``resolver_meta`` (optional) records how the mappings were
    produced (e.g. settlement parser version) in the manifest config."""
    sources = await load_source_frames(session, start=start, end=end)
    built = build_datasets(sources, mappings, which=which)
    report = validate(sources, built.frames, mappings)
    stats = compute_stats(sources, built.frames)
    manifest = build_manifest(
        dataset_name="research_dataset",
        version=version,
        frames=built.frames,
        database_url=database_url,
        source_db_revision=await _alembic_revision(session),
        config={
            "which": which,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "market_map_hash": _mapping_hash(mappings),
            "market_map_size": len(mappings),
            **({"resolver": resolver_meta} if resolver_meta else {}),
        },
        repo_dir=repo_dir,
    )
    return BuildOutput(
        frames=built.frames, manifest=manifest, validation=report, stats=stats
    )


def export(output: BuildOutput, *, root: Path, version: str, fmt: ExportFormat) -> ExportResult:
    """Write a completed build to ``<root>/<version>/``."""
    return export_dataset(
        frames=output.frames,
        manifest=output.manifest,
        validation=output.validation.to_dict(),
        stats=output.stats,
        root=root,
        version=version,
        fmt=fmt,
    )
