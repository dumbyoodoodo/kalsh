"""PostgreSQL backup retention policy (grandfather-father-son): keep every
backup within a recent daily window, then thin to one representative per
week, then one per month, then delete anything older. Detection/decision
only -- deletion is a separate, explicit step (`apply_retention_plan`) that
a caller opts into, and dry-run mode never touches the filesystem.

Deliberately **not wired into the automatic backup schedule by default**.
`scripts/backup_postgres.sh` has documented, since it was first written,
that "pruning is a deliberate manual act" -- a prior, explicit policy
decision. This module makes that policy fully implementable (tested,
dry-run-capable, safe), but `ops backup prune` must still be invoked
explicitly (by hand, by cron, or via `BACKUP_AUTO_PRUNE=true` calling it
from the backup script) rather than assumed. See
`docs/runbooks/backup_recovery.md` "Retention policy" for the decision
record.

This module computes no research statistic. It only classifies backup
files as keep/delete and, when asked, deletes the "delete" ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

#: `kalshi_weather-YYYYMMDDTHHMMSSZ.dump` -- scripts/backup_postgres.sh's
#: existing naming convention, unchanged.
_FILENAME_PATTERN = re.compile(r"^kalshi_weather-(\d{8}T\d{6}Z)\.dump$")

#: `.partial` files older than this are abandoned (a crashed or killed
#: backup run) rather than in-progress -- a normal dump completes in
#: minutes, not hours, at this database's current size.
DEFAULT_PARTIAL_GRACE_HOURS = 2.0


@dataclass(frozen=True, slots=True)
class BackupFile:
    path: Path
    timestamp: datetime  # naive UTC, parsed from the filename
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    file: BackupFile
    action: str  # "keep" | "delete"
    reason: str


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    decisions: tuple[RetentionDecision, ...]
    aborted: bool = False
    abort_reason: str | None = None

    @property
    def to_delete(self) -> tuple[RetentionDecision, ...]:
        return tuple(d for d in self.decisions if d.action == "delete")

    @property
    def to_keep(self) -> tuple[RetentionDecision, ...]:
        return tuple(d for d in self.decisions if d.action == "keep")


def parse_backup_filename(name: str) -> datetime | None:
    """`None` if `name` doesn't match the naming convention at all -- the
    caller decides whether that's benign (an unrelated file) or a reason
    to distrust the directory's identity. Returns **naive UTC** (the
    timestamp is already UTC per the filename's own `Z` suffix; naive
    matches this codebase's established convention -- see
    `domain.time.to_naive_utc` -- for arithmetic against `now` values,
    which are themselves naive throughout `observatory/`)."""
    match = _FILENAME_PATTERN.match(name)
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")


def list_backup_files(directory: Path) -> list[BackupFile]:
    """Every file in `directory` matching the naming convention -- `.partial`
    and unrelated files are silently excluded (see `find_stale_partial_files`
    for the former)."""
    if not directory.is_dir():
        return []
    files = []
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        ts = parse_backup_filename(entry.name)
        if ts is None:
            continue
        files.append(BackupFile(path=entry, timestamp=ts, size_bytes=entry.stat().st_size))
    return sorted(files, key=lambda f: f.timestamp)


def find_stale_partial_files(
    directory: Path, *, now: datetime, grace_hours: float = DEFAULT_PARTIAL_GRACE_HOURS
) -> list[Path]:
    """`.partial` temp files (see `scripts/backup_postgres.sh`'s atomic
    write) older than `grace_hours` -- an abandoned dump from a crashed or
    killed run, not one still in progress."""
    if not directory.is_dir():
        return []
    stale = []
    for entry in directory.glob("*.dump.partial"):
        if not entry.is_file():
            continue
        # naive UTC, matching `now` (see parse_backup_filename's docstring
        # for why this codebase keeps these comparisons naive-vs-naive).
        mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC).replace(tzinfo=None)
        age_hours = (now - mtime).total_seconds() / 3600.0
        if age_hours > grace_hours:
            stale.append(entry)
    return stale


def _week_key(dt: datetime) -> tuple[int, int]:
    iso = dt.isocalendar()
    return (iso.year, iso.week)


def _month_key(dt: datetime) -> tuple[int, int]:
    return (dt.year, dt.month)


def compute_retention_plan(
    files: Sequence[BackupFile],
    *,
    now: datetime,
    daily_days: int = 14,
    weekly_weeks: int = 8,
    monthly_months: int = 12,
) -> RetentionPlan:
    """Pure, deterministic classification of every file in `files` into
    keep/delete, given only `files` and `now` -- no filesystem access, no
    wall-clock reads beyond the `now` passed in. Rules, applied in order:

    1. The single newest file is always kept, regardless of age -- a
       retention policy must never be able to delete the only backup that
       exists, or the most recent one, even if the tiering math below
       would otherwise say to.
    2. Anything within `daily_days` of `now` is kept (the dense recent
       window).
    3. Beyond that, out to `daily_days + weekly_weeks` weeks, exactly one
       file per ISO (year, week) is kept -- deterministically, the
       *earliest* file in that week (ties broken by filename, which sorts
       chronologically).
    4. Beyond that, out to `daily_days + weekly_weeks*7 + monthly_months`
       months, exactly one file per (year, month) is kept, by the same
       earliest-wins rule.
    5. Anything older still is deleted.

    Returns `aborted=True` (no deletions should ever be applied) if `files`
    is empty -- there is nothing to retain a policy over, and an empty
    list is far more likely to mean "wrong directory" than "no backups
    exist yet" for a caller that reached this function at all.
    """
    if not files:
        return RetentionPlan(decisions=(), aborted=True, abort_reason="no backup files given")

    ordered = sorted(files, key=lambda f: f.timestamp)
    newest = ordered[-1]

    daily_cutoff = now - timedelta(days=daily_days)
    weekly_cutoff = now - timedelta(days=daily_days + weekly_weeks * 7)
    monthly_cutoff = now - timedelta(days=daily_days + weekly_weeks * 7 + monthly_months * 30)

    # Earliest-file-per-bucket representatives, computed over the whole set
    # first so the "is this the kept one" check below is a simple lookup.
    weekly_representative: dict[tuple[int, int], BackupFile] = {}
    monthly_representative: dict[tuple[int, int], BackupFile] = {}
    for f in ordered:
        if weekly_cutoff < f.timestamp <= daily_cutoff:
            key = _week_key(f.timestamp)
            if key not in weekly_representative:
                weekly_representative[key] = f
        elif monthly_cutoff < f.timestamp <= weekly_cutoff:
            key = _month_key(f.timestamp)
            if key not in monthly_representative:
                monthly_representative[key] = f

    decisions: list[RetentionDecision] = []
    for f in ordered:
        if f is newest:
            decisions.append(
                RetentionDecision(file=f, action="keep", reason="newest backup, never pruned")
            )
        elif f.timestamp > daily_cutoff:
            decisions.append(
                RetentionDecision(
                    file=f, action="keep", reason=f"within {daily_days}d daily retention window"
                )
            )
        elif f.timestamp > weekly_cutoff:
            key = _week_key(f.timestamp)
            if weekly_representative.get(key) is f:
                decisions.append(
                    RetentionDecision(
                        file=f,
                        action="keep",
                        reason=f"weekly representative for ISO week {key[0]}-W{key[1]:02d}",
                    )
                )
            else:
                week_label = f"{key[0]}-W{key[1]:02d}"
                decisions.append(
                    RetentionDecision(
                        file=f,
                        action="delete",
                        reason=f"superseded by the weekly representative for {week_label}",
                    )
                )
        elif f.timestamp > monthly_cutoff:
            key = _month_key(f.timestamp)
            if monthly_representative.get(key) is f:
                decisions.append(
                    RetentionDecision(
                        file=f,
                        action="keep",
                        reason=f"monthly representative for {key[0]}-{key[1]:02d}",
                    )
                )
            else:
                month_label = f"{key[0]}-{key[1]:02d}"
                decisions.append(
                    RetentionDecision(
                        file=f,
                        action="delete",
                        reason=f"superseded by the monthly representative for {month_label}",
                    )
                )
        else:
            decisions.append(
                RetentionDecision(
                    file=f,
                    action="delete",
                    reason=f"older than the {monthly_months}mo monthly retention window",
                )
            )
    return RetentionPlan(decisions=tuple(decisions))


@dataclass(frozen=True, slots=True)
class RetentionLogEntry:
    path: str
    action: str
    reason: str
    dry_run: bool
    deleted: bool


def apply_retention_plan(plan: RetentionPlan, *, dry_run: bool) -> list[RetentionLogEntry]:
    """Execute (or, if `dry_run`, merely log) `plan`'s decisions. Every
    decision -- keep and delete alike -- produces a log entry, so a dry
    run's output is a complete preview of what a real run would do."""
    if plan.aborted:
        return []
    log: list[RetentionLogEntry] = []
    for decision in plan.decisions:
        deleted = False
        if decision.action == "delete" and not dry_run:
            decision.file.path.unlink(missing_ok=True)
            deleted = True
        log.append(
            RetentionLogEntry(
                path=str(decision.file.path),
                action=decision.action,
                reason=decision.reason,
                dry_run=dry_run,
                deleted=deleted,
            )
        )
    return log


def apply_partial_cleanup(paths: Sequence[Path], *, dry_run: bool) -> list[RetentionLogEntry]:
    """Same log-every-decision convention as `apply_retention_plan`, for
    abandoned `.partial` files (`find_stale_partial_files`)."""
    log = []
    for path in paths:
        deleted = False
        if not dry_run:
            path.unlink(missing_ok=True)
            deleted = True
        log.append(
            RetentionLogEntry(
                path=str(path),
                action="delete",
                reason="abandoned .partial file beyond the grace period",
                dry_run=dry_run,
                deleted=deleted,
            )
        )
    return log
