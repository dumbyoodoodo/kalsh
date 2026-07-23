"""PostgreSQL backup finalization: status recording + off-machine copy.

`scripts/backup_postgres.sh` owns the actual `pg_dump`/`pg_restore --list`
work (unchanged, proven behavior -- see its own comments); after it has a
verified local `.dump` file (or has failed), it calls this module's CLI
entry point (`ops backup finalize`) to do the two things that benefit from
real, tested code rather than more bash: write a machine-readable status
record for `observatory/backup_health.py` to read, and -- if configured --
copy the verified file to an off-machine destination with independent
integrity verification at the far end.

Local backup success is never contingent on the remote copy succeeding
(CLAUDE.md-adjacent to this project's existing "local backup succeeds
independently of remote availability" requirement) -- `finalize_backup`
always writes a status record; a remote failure is recorded as a degraded
outcome, never raised as an exception that could be mistaken for a local
failure.

Secrets: this module never stores or logs AWS credentials -- S3 copies
shell out to the `aws` CLI, which manages its own credential chain
(environment, `~/.aws/credentials`, an instance role) entirely outside
this project's `.env`. Filesystem-destination paths are read from
`Settings`/`.env`, same as everywhere else in this codebase; nothing about
this module writes a new credential anywhere.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

STATUS_SCHEMA = 1
HASH_CHUNK_BYTES = 1024 * 1024
S3_TIMEOUT_SECONDS = 300
#: Filename for the status record inside the backup directory --
#: referenced by both `ops backup finalize`'s writer and
#: `observatory/backup_health.py`'s reader.
STATUS_FILENAME = "last_backup_status.json"


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    remote_type: str  # "filesystem" | "s3" | "none"
    filesystem_path: str | None = None
    s3_bucket: str | None = None
    s3_prefix: str = "postgres"


@dataclass(frozen=True, slots=True)
class RemoteResult:
    outcome: str  # "success" | "failed" | "not_configured" | "unavailable"
    detail: str


@dataclass(frozen=True, slots=True)
class BackupStatus:
    schema: int
    timestamp: str
    local_outcome: str  # "success" | "failed"
    local_path: str | None
    size_bytes: int | None
    entries: int | None
    duration_seconds: float | None
    error: str | None
    remote_outcome: str  # "success" | "failed" | "not_configured" | "unavailable" | "skipped"
    remote_detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "timestamp": self.timestamp,
            "local_outcome": self.local_outcome,
            "local_path": self.local_path,
            "size_bytes": self.size_bytes,
            "entries": self.entries,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "remote_outcome": self.remote_outcome,
            "remote_detail": self.remote_detail,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(proc: subprocess.Popen[bytes]) -> str:
    digest = hashlib.sha256()
    assert proc.stdout is not None
    for chunk in iter(lambda: proc.stdout.read(HASH_CHUNK_BYTES), b""):  # type: ignore[union-attr]
        digest.update(chunk)
    return digest.hexdigest()


def copy_to_filesystem(local_path: Path, dest_dir_str: str) -> RemoteResult:
    """Copy `local_path` into `dest_dir_str`, then read the copy back and
    compare its SHA-256 against the source -- a genuine round-trip
    integrity check, not just "the copy command reported success"."""
    dest_dir = Path(dest_dir_str)
    if not dest_dir.is_dir():
        return RemoteResult(
            outcome="unavailable",
            detail=f"destination directory does not exist -- is it mounted? ({dest_dir})",
        )
    dest_path = dest_dir / local_path.name
    try:
        shutil.copy2(local_path, dest_path)
    except OSError as exc:
        return RemoteResult(outcome="failed", detail=f"copy failed: {type(exc).__name__}: {exc}")

    source_hash = sha256_file(local_path)
    try:
        dest_hash = sha256_file(dest_path)
    except OSError as exc:
        return RemoteResult(
            outcome="failed", detail=f"could not re-read destination for verification: {exc}"
        )
    if source_hash != dest_hash:
        return RemoteResult(
            outcome="failed",
            detail="checksum mismatch after copy -- destination file does not match the source",
        )
    return RemoteResult(outcome="success", detail=f"filesystem: verified copy at {dest_path}")


def copy_to_s3(local_path: Path, *, bucket: str, prefix: str) -> RemoteResult:
    """`aws s3 cp` the file up, then stream it back down (`aws s3 cp ... -`)
    and compare a SHA-256 hash against the source -- deliberately a
    download-and-hash round trip rather than trusting S3's ETag (which is
    not simply an MD5 for multipart uploads), so this verification is
    correct regardless of upload chunking."""
    if shutil.which("aws") is None:
        return RemoteResult(
            outcome="unavailable", detail="the `aws` CLI is not installed on this machine"
        )
    key = f"{prefix.rstrip('/')}/{local_path.name}" if prefix else local_path.name
    uri = f"s3://{bucket}/{key}"
    try:
        upload = subprocess.run(
            ["aws", "s3", "cp", str(local_path), uri],
            capture_output=True,
            text=True,
            timeout=S3_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return RemoteResult(outcome="failed", detail="aws s3 cp (upload) timed out")
    if upload.returncode != 0:
        return RemoteResult(
            outcome="failed", detail=f"aws s3 cp (upload) failed: {upload.stderr.strip()[:300]}"
        )

    source_hash = sha256_file(local_path)
    try:
        with subprocess.Popen(
            ["aws", "s3", "cp", uri, "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as proc:
            dest_hash = _sha256_stream(proc)
            _, stderr = proc.communicate(timeout=S3_TIMEOUT_SECONDS)
            if proc.returncode != 0:
                stderr_text = stderr.decode(errors="replace")[:300]
                return RemoteResult(
                    outcome="failed",
                    detail=f"aws s3 cp (verify download) failed: {stderr_text}",
                )
    except subprocess.TimeoutExpired:
        return RemoteResult(outcome="failed", detail="aws s3 cp (verify download) timed out")

    if source_hash != dest_hash:
        return RemoteResult(
            outcome="failed",
            detail="checksum mismatch after upload -- object in S3 does not match the source",
        )
    return RemoteResult(outcome="success", detail=f"s3: verified upload to {uri}")


def copy_to_remote(local_path: Path, config: RemoteConfig) -> RemoteResult:
    """Dispatch on `config.remote_type`. `"none"` is a deliberate no-op --
    the audit finding this system closes stays PARTIALLY CLOSED, not
    silently reported as fully redundant, until a real destination is
    configured (see docs/runbooks/backup_recovery.md)."""
    if config.remote_type == "filesystem":
        if not config.filesystem_path:
            return RemoteResult(outcome="unavailable", detail="BACKUP_REMOTE_PATH is not set")
        return copy_to_filesystem(local_path, config.filesystem_path)
    if config.remote_type == "s3":
        if not config.s3_bucket:
            return RemoteResult(outcome="unavailable", detail="BACKUP_S3_BUCKET is not set")
        return copy_to_s3(local_path, bucket=config.s3_bucket, prefix=config.s3_prefix)
    if config.remote_type == "none":
        return RemoteResult(
            outcome="not_configured", detail="no off-machine destination configured"
        )
    return RemoteResult(
        outcome="failed", detail=f"unknown BACKUP_REMOTE_TYPE {config.remote_type!r}"
    )


def write_backup_status(path: Path, status: BackupStatus) -> None:
    """Atomic-rename write (same convention as `ops/restart_policy.py` and
    `ops/monitor.py`) -- a crash mid-write must never leave a corrupt
    status file that `observatory/backup_health.py` would misread."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status.to_dict(), indent=2) + "\n")
    tmp.replace(path)


def read_backup_status(path: Path) -> BackupStatus | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return BackupStatus(
        schema=raw["schema"],
        timestamp=raw["timestamp"],
        local_outcome=raw["local_outcome"],
        local_path=raw.get("local_path"),
        size_bytes=raw.get("size_bytes"),
        entries=raw.get("entries"),
        duration_seconds=raw.get("duration_seconds"),
        error=raw.get("error"),
        remote_outcome=raw["remote_outcome"],
        remote_detail=raw["remote_detail"],
    )


def finalize_backup(
    *,
    now: datetime,
    local_outcome: str,
    local_path: Path | None,
    size_bytes: int | None,
    entries: int | None,
    duration_seconds: float | None,
    error: str | None,
    remote_config: RemoteConfig,
) -> BackupStatus:
    """Orchestrate the finalize step: attempt a remote copy only when the
    local backup itself succeeded (nothing to copy otherwise), and always
    produce a `BackupStatus` -- this function never raises for a remote
    failure; that is exactly the "degraded, not fatal" outcome
    docs/runbooks/backup_recovery.md documents."""
    if local_outcome != "success":
        remote_result = RemoteResult(
            outcome="skipped", detail="local backup failed; nothing to copy"
        )
    elif local_path is None:
        remote_result = RemoteResult(
            outcome="failed", detail="local backup reported success but no local_path was given"
        )
    else:
        remote_result = copy_to_remote(local_path, remote_config)

    return BackupStatus(
        schema=STATUS_SCHEMA,
        timestamp=now.isoformat(),
        local_outcome=local_outcome,
        local_path=str(local_path) if local_path else None,
        size_bytes=size_bytes,
        entries=entries,
        duration_seconds=duration_seconds,
        error=error,
        remote_outcome=remote_result.outcome,
        remote_detail=remote_result.detail,
    )
