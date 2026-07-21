import json
from pathlib import Path

import polars as pl
import pytest

from kalshi_weather.dataset.export import (
    ExportFormat,
    UnsupportedExportFormatError,
    default_version,
    export_dataset,
)
from kalshi_weather.dataset.manifest import build_manifest


def _manifest(frames: dict[str, pl.DataFrame]):  # type: ignore[no-untyped-def]
    return build_manifest(
        dataset_name="research_dataset",
        version="v1",
        frames=frames,
        database_url="sqlite://",
        source_db_revision=None,
        config={},
        git_commit="abc",
    )


def test_default_version_is_sortable_timestamp() -> None:
    v = default_version()
    assert v.endswith("Z") and len(v) == len("YYYYMMDDTHHMMSSZ")


def test_export_writes_parquet_and_sidecars_and_roundtrips(tmp_path: Path) -> None:
    frames = {
        "weather_panel": pl.DataFrame({"station_id": ["NYC"], "residual_high_f": [2.0]}),
        "market_weather": pl.DataFrame({"market_ticker": ["X"], "forecast_high_f": [80.0]}),
    }
    result = export_dataset(
        frames=frames,
        manifest=_manifest(frames),
        validation={"ok": True, "findings": []},
        stats={"counts": {"stations": 1}},
        root=tmp_path,
        version="v1",
    )
    assert result.output_dir == tmp_path / "v1"
    reloaded = pl.read_parquet(result.parquet_paths["weather_panel"])
    assert reloaded.equals(frames["weather_panel"])
    assert json.loads(result.validation_path.read_text())["ok"] is True
    assert json.loads(result.stats_path.read_text())["counts"]["stations"] == 1
    assert json.loads(result.manifest_path.read_text())["version"] == "v1"


def test_export_refuses_to_overwrite_existing_version(tmp_path: Path) -> None:
    frames = {"weather_panel": pl.DataFrame({"x": [1]})}
    export_dataset(
        frames=frames,
        manifest=_manifest(frames),
        validation={},
        stats={},
        root=tmp_path,
        version="v1",
    )
    with pytest.raises(FileExistsError):
        export_dataset(
            frames=frames,
            manifest=_manifest(frames),
            validation={},
            stats={},
            root=tmp_path,
            version="v1",
        )


def test_export_duckdb_is_documented_future_not_implemented(tmp_path: Path) -> None:
    frames = {"weather_panel": pl.DataFrame({"x": [1]})}
    with pytest.raises(UnsupportedExportFormatError):
        export_dataset(
            frames=frames,
            manifest=_manifest(frames),
            validation={},
            stats={},
            root=tmp_path,
            version="v1",
            fmt=ExportFormat.DUCKDB,
        )
