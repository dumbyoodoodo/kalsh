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
from kalshi_weather.settlement.labels import RECONSTRUCTION_VERSION, SettlementLabel, build_labels


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
    if "market_weather" in built.frames:
        labels = await build_labels(session)
        labels_frame = _labels_frame(labels)
        built.frames["settlement_labels"] = labels_frame
        # Explicit stage-labelled columns joined per market. `settled_value`
        # (latest-final semantics) is intentionally NOT redefined -- new
        # columns carry the stage distinction (docs/adr/0006-settlement-labels.md).
        built.frames["market_weather"] = built.frames["market_weather"].join(
            labels_frame.select(
                "market_ticker",
                "value_at_close",
                "value_at_settlement",
                "latest_final_value",
                "settlement_label_status",
                "settlement_label_version",
            ),
            on="market_ticker",
            how="left",
        )
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
            "settlement_label_reconstruction_version": RECONSTRUCTION_VERSION,
            **({"resolver": resolver_meta} if resolver_meta else {}),
        },
        repo_dir=repo_dir,
    )
    return BuildOutput(
        frames=built.frames, manifest=manifest, validation=report, stats=stats
    )


def _labels_frame(labels: list[SettlementLabel]) -> pl.DataFrame:
    """One row per market: the canonical settlement-time label."""
    return pl.DataFrame(
        [
            {
                "market_ticker": lb.market_ticker,
                "station_id": lb.station_id,
                "variable": lb.variable,
                "target_date": lb.target_date,
                "close_time": lb.close_time,
                "settlement_time": lb.settlement_time,
                "settlement_time_is_exact": lb.settlement_time_is_exact,
                "value_at_close": (
                    float(lb.value_at_close) if lb.value_at_close is not None else None
                ),
                "value_at_settlement": (
                    float(lb.value_at_settlement) if lb.value_at_settlement is not None else None
                ),
                "latest_final_value": (
                    float(lb.latest_final_value) if lb.latest_final_value is not None else None
                ),
                "kalshi_result": lb.kalshi_result,
                "kalshi_expiration_value": (
                    float(lb.kalshi_expiration_value)
                    if lb.kalshi_expiration_value is not None
                    else None
                ),
                "implied_result_at_settlement": lb.implied_result_at_settlement,
                "payout_value_agrees": lb.payout_value_agrees,
                "payout_result_agrees": lb.payout_result_agrees,
                "settlement_label_status": lb.status.value,
                "settlement_label_version": lb.reconstruction_version,
                "notes": "; ".join(lb.notes) if lb.notes else None,
            }
            for lb in labels
        ],
        schema={
            "market_ticker": pl.Utf8,
            "station_id": pl.Utf8,
            "variable": pl.Utf8,
            "target_date": pl.Date,
            "close_time": pl.Datetime("us"),
            "settlement_time": pl.Datetime("us"),
            "settlement_time_is_exact": pl.Boolean,
            "value_at_close": pl.Float64,
            "value_at_settlement": pl.Float64,
            "latest_final_value": pl.Float64,
            "kalshi_result": pl.Utf8,
            "kalshi_expiration_value": pl.Float64,
            "implied_result_at_settlement": pl.Utf8,
            "payout_value_agrees": pl.Boolean,
            "payout_result_agrees": pl.Boolean,
            "settlement_label_status": pl.Utf8,
            "settlement_label_version": pl.Utf8,
            "notes": pl.Utf8,
        },
        orient="row",
    ).sort("market_ticker")


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
