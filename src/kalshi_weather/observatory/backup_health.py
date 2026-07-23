"""Backup health checks for the data quality observatory (Phase 5 of the
backup & recovery system): folds `ops/backup.py`'s status record and the
backup directory's own contents into the same `Finding`/`Severity`
vocabulary every other observatory check uses, so `ops monitor`'s existing
exactly-once alert-transition state machine covers backups for free --
no new alerting code, no new state machine.

Every check here is a pure function over already-loaded inputs (a
`BackupStatus`, a list of `BackupFile`s, a disk-usage tuple, a skip
count); a thin loader (`load_backup_health_inputs`) does the actual
filesystem/`aws` CLI reads. This module computes no research statistic --
detection only.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kalshi_weather.observatory.severity import Finding, Severity
from kalshi_weather.ops.backup import BackupStatus, read_backup_status
from kalshi_weather.ops.backup_retention import BackupFile, list_backup_files

#: A freshness/remote-staleness breach beyond this multiple of the
#: configured threshold escalates from WARNING to CRITICAL -- distinguishes
#: "a bit overdue" from "genuinely not happening."
CRITICAL_STALENESS_MULTIPLIER = 2.0

#: Consecutive lock-contention skips at/above this count escalate severity
#: -- one skip is a slow prior run; several in a row means something is
#: stuck.
LOCK_SKIP_WARNING_COUNT = 2
LOCK_SKIP_CRITICAL_COUNT = 5

SKIP_COUNT_FILENAME = "lock_skip_count"


@dataclass(frozen=True, slots=True)
class DiskUsage:
    free_bytes: int
    total_bytes: int

    @property
    def free_gb(self) -> float:
        return self.free_bytes / (1024**3)


def check_backup_command(status: BackupStatus | None) -> Finding:
    if status is None:
        return Finding(
            domain="backup",
            check="backup_command",
            severity=Severity.INFO,
            count=0,
            message="no backup has run yet",
        )
    if status.local_outcome == "failed":
        return Finding(
            domain="backup",
            check="backup_command",
            severity=Severity.CRITICAL,
            count=1,
            message=(
                f"most recent backup attempt failed: {status.error or 'no error detail recorded'}"
            ),
        )
    return Finding(
        domain="backup",
        check="backup_command",
        severity=Severity.INFO,
        count=0,
        message=(
            f"most recent backup succeeded "
            f"({status.entries} archive entries, {status.size_bytes} bytes)"
        ),
    )


def check_backup_freshness(
    files: list[BackupFile], *, now: datetime, stale_after_hours: float
) -> Finding:
    """Newest local backup file's age vs `stale_after_hours` -- ground
    truth from the directory itself, not the status record (a status file
    could in principle be stale/missing while backups still land fine, or
    vice versa; checking the directory directly is the more trustworthy
    signal for "does a recent backup actually exist")."""
    if not files:
        return Finding(
            domain="backup",
            check="backup_local_freshness",
            severity=Severity.INFO,
            count=0,
            message="no local backups exist yet",
        )
    newest = max(files, key=lambda f: f.timestamp)
    age_hours = (now - newest.timestamp).total_seconds() / 3600.0
    if age_hours > stale_after_hours * CRITICAL_STALENESS_MULTIPLIER:
        severity = Severity.CRITICAL
    elif age_hours > stale_after_hours:
        severity = Severity.WARNING
    else:
        severity = Severity.INFO
    return Finding(
        domain="backup",
        check="backup_local_freshness",
        severity=severity,
        count=1,
        message=(
            f"newest local backup is {age_hours:.1f}h old "
            f"(threshold {stale_after_hours}h): {newest.path.name}"
        ),
    )


def check_remote_copy(status: BackupStatus | None, *, remote_configured: bool) -> Finding:
    if status is None:
        return Finding(
            domain="backup",
            check="backup_remote_copy",
            severity=Severity.INFO,
            count=0,
            message="no backup has run yet",
        )
    if status.remote_outcome in ("not_configured", "skipped"):
        severity = Severity.WARNING if remote_configured else Severity.INFO
    elif status.remote_outcome == "success":
        severity = Severity.INFO
    else:  # "failed" | "unavailable"
        severity = Severity.WARNING
    return Finding(
        domain="backup",
        check="backup_remote_copy",
        severity=severity,
        count=0 if status.remote_outcome == "success" else 1,
        message=f"remote copy: {status.remote_outcome} -- {status.remote_detail}",
    )


def check_remote_staleness(
    newest_remote_timestamp: datetime | None,
    *,
    remote_configured: bool,
    now: datetime,
    stale_after_hours: float,
) -> Finding:
    """Only meaningful once a remote destination is configured -- an
    unconfigured remote is `backup_remote_copy`'s concern (an expected,
    disclosed gap, not a staleness alarm)."""
    if not remote_configured:
        return Finding(
            domain="backup",
            check="backup_remote_freshness",
            severity=Severity.INFO,
            count=0,
            message="no off-machine destination configured",
        )
    if newest_remote_timestamp is None:
        return Finding(
            domain="backup",
            check="backup_remote_freshness",
            severity=Severity.WARNING,
            count=1,
            message="off-machine destination configured, but no backup found there yet",
        )
    age_hours = (now - newest_remote_timestamp).total_seconds() / 3600.0
    if age_hours > stale_after_hours * CRITICAL_STALENESS_MULTIPLIER:
        severity = Severity.CRITICAL
    elif age_hours > stale_after_hours:
        severity = Severity.WARNING
    else:
        severity = Severity.INFO
    return Finding(
        domain="backup",
        check="backup_remote_freshness",
        severity=severity,
        count=1,
        message=(
            f"newest off-machine backup is {age_hours:.1f}h old (threshold {stale_after_hours}h)"
        ),
    )


def check_disk_space(
    usage: DiskUsage, *, warning_free_gb: float, critical_free_gb: float
) -> Finding:
    if usage.free_gb < critical_free_gb:
        severity = Severity.CRITICAL
    elif usage.free_gb < warning_free_gb:
        severity = Severity.WARNING
    else:
        severity = Severity.INFO
    return Finding(
        domain="backup",
        check="backup_disk_space",
        severity=severity,
        count=0,
        message=f"{usage.free_gb:.1f} GB free on the backup destination filesystem",
    )


def check_lock_contention(skip_count: int) -> Finding:
    if skip_count >= LOCK_SKIP_CRITICAL_COUNT:
        severity = Severity.CRITICAL
    elif skip_count >= LOCK_SKIP_WARNING_COUNT:
        severity = Severity.WARNING
    else:
        severity = Severity.INFO
    return Finding(
        domain="backup",
        check="backup_lock_contention",
        severity=severity,
        count=skip_count,
        message=(
            f"{skip_count} consecutive scheduled backup run(s) skipped due to an already-held lock"
        ),
    )


# --- Thin loaders (filesystem / aws CLI reads; no decision logic) -------------


def load_disk_usage(path: Path) -> DiskUsage:
    total, _used, free = shutil.disk_usage(path)
    return DiskUsage(free_bytes=free, total_bytes=total)


def load_skip_count(backup_dir: Path) -> int:
    path = backup_dir / SKIP_COUNT_FILENAME
    if not path.exists():
        return 0
    try:
        return int(path.read_text().strip() or "0")
    except ValueError:
        return 0


def load_newest_remote_timestamp(
    *, remote_type: str, filesystem_path: str | None, s3_bucket: str | None, s3_prefix: str
) -> datetime | None:
    """Ground-truth newest-backup timestamp at the remote destination
    itself (not inferred from the last status record) -- mirrors
    `check_backup_freshness`'s local-directory approach."""
    if remote_type == "filesystem":
        if not filesystem_path:
            return None
        files = list_backup_files(Path(filesystem_path))
        return max((f.timestamp for f in files), default=None)
    if remote_type == "s3":
        if not s3_bucket or shutil.which("aws") is None:
            return None
        from kalshi_weather.ops.backup_retention import parse_backup_filename

        prefix = f"{s3_prefix.rstrip('/')}/" if s3_prefix else ""
        try:
            result = subprocess.run(
                ["aws", "s3", "ls", f"s3://{s3_bucket}/{prefix}"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return None
        if result.returncode != 0:
            return None
        timestamps = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            ts = parse_backup_filename(parts[-1])
            if ts is not None:
                timestamps.append(ts)
        return max(timestamps, default=None)
    return None


@dataclass(frozen=True, slots=True)
class BackupHealthConfig:
    backup_dir: Path
    status_path: Path
    remote_type: str
    remote_filesystem_path: str | None
    remote_s3_bucket: str | None
    remote_s3_prefix: str
    stale_after_hours: float
    remote_stale_after_hours: float
    disk_warning_free_gb: float
    disk_critical_free_gb: float


def build_backup_findings(
    config: BackupHealthConfig, *, now: datetime | None = None
) -> list[Finding]:
    """Orchestrates every check above -- the single entry point
    `observatory/report.py` calls."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    status = read_backup_status(config.status_path)
    files = list_backup_files(config.backup_dir)
    remote_configured = config.remote_type != "none"

    findings = [
        check_backup_command(status),
        check_backup_freshness(files, now=now, stale_after_hours=config.stale_after_hours),
        check_remote_copy(status, remote_configured=remote_configured),
    ]

    newest_remote = load_newest_remote_timestamp(
        remote_type=config.remote_type,
        filesystem_path=config.remote_filesystem_path,
        s3_bucket=config.remote_s3_bucket,
        s3_prefix=config.remote_s3_prefix,
    )
    findings.append(
        check_remote_staleness(
            newest_remote,
            remote_configured=remote_configured,
            now=now,
            stale_after_hours=config.remote_stale_after_hours,
        )
    )

    if config.backup_dir.is_dir():
        findings.append(
            check_disk_space(
                load_disk_usage(config.backup_dir),
                warning_free_gb=config.disk_warning_free_gb,
                critical_free_gb=config.disk_critical_free_gb,
            )
        )
    findings.append(check_lock_contention(load_skip_count(config.backup_dir)))
    return findings
