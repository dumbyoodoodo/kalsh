"""Build the versioned CLI occurrence-time extract for H0015.

One row per (station_id, observation_date): fields parsed from the FIRST
product of the day (the preliminary -- AS-OF cutoff hour, correction flag)
and the FINAL product (occurrence times and values for max/min, correction
flag), plus provenance (raw_payload_ids). Parser: the productionized
`kalshi_weather.weather.cli_products` module. Read-only against the
database; writes a versioned parquet + manifest (content hash, row count,
git commit) under <KALSHI_DATA_DIR>/datasets/<version>/.

Pre-registration discipline: this builder prints STRUCTURAL statistics
only (row counts, parse coverage, AS-OF distributions) -- it computes no
revision statistic, no late-max fraction, and no join to revision
outcomes. Those exist only in the (pre-registered) analysis script.

Usage:
    python scripts/build_h0015_occurrence_extract.py --version exp-20260722-h0015-occurrence
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kalshi_weather.config import get_settings
from kalshi_weather.dataset.manifest import frame_content_hash
from kalshi_weather.weather.cli_products import parse_cli_product

STATIONS = ["CHI", "DEN", "LAX", "NYC"]


def fetch_products() -> dict[tuple[str, date], list[tuple[datetime, int, str]]]:
    """(station, obs_date) -> [(issuance_time_utc, raw_payload_id, text)],
    ascending by issuance time. tmax_f rows enumerate every CLI product."""
    q = (
        "COPY (SELECT wo.station_id, wo.observation_date, "
        "wo.issuance_time AT TIME ZONE 'UTC', wo.raw_payload_id, "
        "replace(replace(rp.payload_json->>'text', E'\\n', '<NL>'), '|', '<PIPE>') "
        "FROM weather_observations wo JOIN raw_api_payloads rp ON rp.id = wo.raw_payload_id "
        "WHERE wo.variable = 'tmax_f') "
        "TO STDOUT WITH (FORMAT csv, DELIMITER '|', QUOTE E'\\x01')"
    )
    out = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "kalshi",
            "-d",
            "kalshi_weather",
            "-c",
            q,
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    ).stdout
    rows: dict[tuple[str, date], list[tuple[datetime, int, str]]] = defaultdict(list)
    for line in out.splitlines():
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        station, obs_date, iss, payload_id, text = parts
        rows[(station, date.fromisoformat(obs_date))].append(
            (
                datetime.fromisoformat(iss),
                int(payload_id),
                text.replace("<NL>", "\n").replace("<PIPE>", "|"),
            )
        )
    for products in rows.values():
        products.sort()
    return rows


def build_frame(products: dict[tuple[str, date], list[tuple[datetime, int, str]]]) -> pl.DataFrame:
    records: list[dict[str, object]] = []
    for station, obs_date in sorted(products):
        entries = products[(station, obs_date)]
        first = parse_cli_product(entries[0][2])
        final = parse_cli_product(entries[-1][2])
        records.append(
            {
                "station_id": station,
                "observation_date": obs_date,
                "n_products": len(entries),
                "first_issuance_time": entries[0][0],
                "final_issuance_time": entries[-1][0],
                "first_asof_hour": first.asof_hour,
                "first_corrected": first.corrected,
                "final_max_value": final.max_value,
                "final_max_occurrence_hour": final.max_occurrence_hour,
                "final_min_value": final.min_value,
                "final_min_occurrence_hour": final.min_occurrence_hour,
                "final_corrected": final.corrected,
                "first_raw_payload_id": entries[0][1],
                "final_raw_payload_id": entries[-1][1],
            }
        )
    return pl.DataFrame(records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    settings = get_settings()
    out_dir = settings.ensure_dataset_root() / args.version
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "cli_occurrences.parquet"
    if out_path.exists():
        print(f"refusing to overwrite existing immutable extract: {out_path}", file=sys.stderr)
        sys.exit(1)

    frame = build_frame(fetch_products())
    frame.write_parquet(out_path)
    content_hash = frame_content_hash(pl.read_parquet(out_path))

    git_commit = None
    with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
        git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    manifest = {
        "version": args.version,
        "frame": "cli_occurrences.parquet",
        "content_hashes": {"cli_occurrences": content_hash},
        "row_counts": {"cli_occurrences": frame.height},
        "git_commit": git_commit,
        "built_at": datetime.now(UTC).isoformat(),
        "source": (
            "weather_observations (tmax_f rows) JOIN raw_api_payloads; "
            "parser kalshi_weather.weather.cli_products"
        ),
        "note": "structural extract for H0015; one row per (station_id, observation_date)",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # STRUCTURAL statistics only (pre-registration discipline; see docstring).
    print(f"wrote {out_path}")
    print(f"content_hash: {content_hash}")
    print(f"rows: {frame.height}")
    for station in STATIONS:
        sub = frame.filter(pl.col("station_id") == station)
        occ_ok = sub.filter(pl.col("final_max_occurrence_hour").is_not_null()).height
        asof = (
            sub.filter(pl.col("first_asof_hour").is_not_null())
            .group_by("first_asof_hour")
            .len()
            .sort("first_asof_hour")
        )
        asof_summary = {int(r["first_asof_hour"]): r["len"] for r in asof.iter_rows(named=True)}
        print(
            f"  {station}: days={sub.height} occurrence_parse_ok={occ_ok} "
            f"({100 * occ_ok / max(sub.height, 1):.1f}%) first_asof_hours={asof_summary}"
        )
