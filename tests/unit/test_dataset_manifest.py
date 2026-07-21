import json

import polars as pl

from kalshi_weather.dataset.manifest import (
    build_manifest,
    frame_content_hash,
    sanitize_database_url,
)


def test_sanitize_database_url_strips_credentials() -> None:
    url = "postgresql+psycopg://kalshi:secret@localhost:5432/kalshi_weather"
    sanitized = sanitize_database_url(url)
    assert "secret" not in sanitized
    assert "kalshi:" not in sanitized
    assert "localhost:5432/kalshi_weather" in sanitized


def test_content_hash_is_order_independent() -> None:
    a = pl.DataFrame({"x": [1, 2, 3], "y": ["a", "b", "c"]})
    b = a.sort("x", descending=True)  # same rows, different order
    assert frame_content_hash(a) == frame_content_hash(b)


def test_content_hash_changes_with_data() -> None:
    a = pl.DataFrame({"x": [1, 2, 3]})
    c = pl.DataFrame({"x": [1, 2, 4]})
    assert frame_content_hash(a) != frame_content_hash(c)


def test_content_hash_empty_is_stable_and_schema_sensitive() -> None:
    e1 = pl.DataFrame({"x": [], "y": []})
    e2 = pl.DataFrame({"x": [], "y": []})
    e3 = pl.DataFrame({"x": [], "z": []})
    assert frame_content_hash(e1) == frame_content_hash(e2)
    assert frame_content_hash(e1) != frame_content_hash(e3)


def test_build_manifest_records_provenance_and_is_json_serializable() -> None:
    frames = {"weather_panel": pl.DataFrame({"x": [1, 2]})}
    manifest = build_manifest(
        dataset_name="research_dataset",
        version="v1",
        frames=frames,
        database_url="postgresql+psycopg://u:p@host:5432/db",
        source_db_revision="0003",
        config={"which": "all"},
        git_commit="deadbeef",
    )
    assert manifest.git_commit == "deadbeef"
    assert manifest.source_db_revision == "0003"
    assert manifest.row_counts == {"weather_panel": 2}
    assert "weather_panel" in manifest.content_hashes
    assert "p@host" not in manifest.database_url  # credentials sanitized
    parsed = json.loads(manifest.to_json())
    assert parsed["version"] == "v1"
    assert parsed["dataset_schema_version"] == "1"
