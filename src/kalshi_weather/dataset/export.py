"""Export a built dataset to a versioned, self-describing directory.

Layout (immutable once written, per RESEARCH.md dataset versioning):

    <root>/<version>/
        <frame>.parquet          one per built dataset (weather_panel, ...)
        manifest.json            provenance: git commit, source DB rev, hashes
        validation.json          the validation report
        stats.json               summary statistics

Parquet is the storage format now; a DuckDB export is a documented future
option (the enum + dispatch below is the seam for it) but has no consumer yet,
so it is intentionally not implemented rather than shipped untested.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import polars as pl

from kalshi_weather.dataset.manifest import DatasetManifest


class ExportFormat(StrEnum):
    PARQUET = "parquet"
    DUCKDB = "duckdb"


class UnsupportedExportFormatError(NotImplementedError):
    """Raised for a recognized-but-not-yet-implemented export format."""


def default_version(now: datetime | None = None) -> str:
    """A UTC timestamp version like ``20260720T140501Z`` -- sortable and
    collision-free for successive builds. A caller may override with a
    semantic version instead."""
    moment = now or datetime.now(UTC)
    return moment.strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True, slots=True)
class ExportResult:
    version: str
    output_dir: Path
    parquet_paths: dict[str, Path]
    manifest_path: Path
    validation_path: Path
    stats_path: Path


def export_dataset(
    *,
    frames: dict[str, pl.DataFrame],
    manifest: DatasetManifest,
    validation: dict[str, Any],
    stats: dict[str, Any],
    root: Path,
    version: str,
    fmt: ExportFormat = ExportFormat.PARQUET,
) -> ExportResult:
    """Write all frames + sidecar JSON under ``<root>/<version>/``. Creates the
    directory tree if missing (storage config never requires manual setup, per
    CLAUDE.md). Refuses to overwrite an existing version directory -- a
    published version is immutable."""
    if fmt is ExportFormat.DUCKDB:
        raise UnsupportedExportFormatError(
            "DuckDB export is a documented future option (see docs/runbooks/dataset.md); "
            "use parquet for now"
        )

    output_dir = root / version
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(
            f"dataset version directory already exists and is non-empty: {output_dir} "
            "(a published dataset version is immutable; build a new version instead)"
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    parquet_paths: dict[str, Path] = {}
    for name, frame in frames.items():
        path = output_dir / f"{name}.parquet"
        frame.write_parquet(path)
        parquet_paths[name] = path

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    validation_path = output_dir / "validation.json"
    validation_path.write_text(json.dumps(validation, indent=2, default=str), encoding="utf-8")
    stats_path = output_dir / "stats.json"
    stats_path.write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")

    return ExportResult(
        version=version,
        output_dir=output_dir,
        parquet_paths=parquet_paths,
        manifest_path=manifest_path,
        validation_path=validation_path,
        stats_path=stats_path,
    )
