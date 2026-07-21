"""Dataset manifest: the provenance record that makes a build reproducible.

A dataset is only trustworthy if it can be regenerated from what produced it
(RESEARCH.md "Reproducibility"): configuration, source data version, and code
version. The manifest captures all three plus per-table content hashes, so a
later rebuild can be proven byte-for-byte equivalent (or its divergence
localized to a specific table).

Everything here is deliberately non-secret. `database_url` is sanitized to
scheme+host+db before storage -- credentials must never land in a manifest
(CLAUDE.md: never expose credentials in output, logs, or reports).
"""

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import polars as pl

from kalshi_weather.domain.time import utc_now

#: Bumped when the *column layout* of any produced dataset changes, so an old
#: manifest is never mistaken for describing the current schema. Independent
#: of the storage/ORM schema_version (that tracks the source DB).
DATASET_SCHEMA_VERSION = "1"


def sanitize_database_url(url: str) -> str:
    """Drop any username/password from a database URL, keeping scheme, host,
    port, and database name -- enough to identify *which* database produced a
    dataset without ever recording credentials."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<unparseable>"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{parsed.path}"


def current_git_commit(repo_dir: Path | None = None) -> str | None:
    """Full git commit SHA of the working tree, or None if not a git repo /
    git is unavailable. Records the exact code version a dataset was built at."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    commit = result.stdout.strip()
    return commit or None


def frame_content_hash(frame: pl.DataFrame) -> str:
    """Deterministic content hash of a dataframe's *data* (not its file
    encoding). Rows are hashed order-independently so two builds that produce
    the same rows in a different order hash identically; this is what lets a
    rebuild be verified against a prior manifest."""
    columns = sorted(frame.columns)
    if frame.height == 0:
        # Hash the (sorted) column names so an empty frame still has a stable,
        # schema-sensitive fingerprint.
        return hashlib.sha256(",".join(columns).encode("utf-8")).hexdigest()
    # Select columns in a canonical (sorted) order *before* hashing rows:
    # hash_rows combines columns positionally, so an unstable column order
    # (e.g. from a pivot over DB rows with no fixed ordering) would otherwise
    # make identical data hash differently across rebuilds.
    row_hashes = frame.select(columns).hash_rows(seed=0).sort().to_list()
    hasher = hashlib.sha256()
    hasher.update(",".join(columns).encode("utf-8"))
    for value in row_hashes:
        hasher.update(value.to_bytes(8, "little", signed=False))
    return hasher.hexdigest()


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    dataset_name: str
    version: str
    created_at: str  # ISO-8601 UTC; excluded from content hashes by design
    git_commit: str | None
    dataset_schema_version: str
    source_db_revision: str | None  # alembic revision of the source DB, if known
    database_url: str  # sanitized (no credentials)
    row_counts: dict[str, int]
    content_hashes: dict[str, str]
    config: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str)


def build_manifest(
    *,
    dataset_name: str,
    version: str,
    frames: dict[str, pl.DataFrame],
    database_url: str,
    source_db_revision: str | None,
    config: dict[str, Any],
    git_commit: str | None = None,
    repo_dir: Path | None = None,
) -> DatasetManifest:
    """Assemble a manifest from the built frames and the build environment."""
    return DatasetManifest(
        dataset_name=dataset_name,
        version=version,
        created_at=utc_now().isoformat(),
        git_commit=git_commit if git_commit is not None else current_git_commit(repo_dir),
        dataset_schema_version=DATASET_SCHEMA_VERSION,
        source_db_revision=source_db_revision,
        database_url=sanitize_database_url(database_url),
        row_counts={name: frame.height for name, frame in frames.items()},
        content_hashes={name: frame_content_hash(frame) for name, frame in frames.items()},
        config=config,
    )
