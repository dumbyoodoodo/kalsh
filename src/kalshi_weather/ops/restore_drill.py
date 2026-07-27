"""Automated disaster-recovery drill from the newest VERIFIED S3 backup.

Extends the proven local restore drill (scripts/backup_restore_drill.py)
to the off-machine copy: select the newest S3 object that the backup
status record marks verified-uploaded, download it to a temporary path,
verify size + SHA-256 (round-tripped against the local counterpart when it
still exists), restore into a uniquely named DISPOSABLE PostgreSQL
container, validate schema and representative data, then destroy the
container and the download — success or failure.

Hard safety guards, not operator caution:

- every ``docker exec``/``docker rm`` goes through :func:`assert_disposable`,
  which refuses any name that does not carry the drill prefix or that
  matches the production container -- the drill is mechanically incapable
  of addressing ``kalsh-postgres-1``;
- the production connection string is never read by this module -- there
  is no code path that could connect to production;
- commands are logged sanitized (:func:`redact`) -- credential-shaped
  substrings never reach the history file;
- an unverified S3 object is never silently used: selection fails closed
  with an explicit reason.

Results are appended to ``restore_drill_history.jsonl`` (append-only) with
the latest state in ``restore_drill_state.json`` (atomic rename), both in
the backup directory alongside the existing status/report files. Nothing
here touches authoritative research tables, experiments, or paper state.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_weather.ops.backup import BackupStatus, read_backup_status, sha256_file

DRILL_PREFIX = "kalshi-restore-drill-"
PRODUCTION_CONTAINER = "kalsh-postgres-1"
IMAGE = "postgres:16-alpine"
EXPECTED_PG_MAJOR = 16
DB_USER = "kalshi"
DB_NAME = "kalshi_weather"  # inside the isolated container only
HISTORY_FILENAME = "restore_drill_history.jsonl"
STATE_FILENAME = "restore_drill_state.json"

#: Representative tables the restored dump must contain and (mostly) populate.
REPRESENTATIVE_TABLES = (
    "raw_api_payloads",
    "weather_stations",
    "weather_observations",
    "weather_forecasts",
    "market_snapshots",
    "orderbook_snapshots",
    "market_poll_attempts",
    "trades",
    "market_candlesticks",
    "collector_runs",
)

_CRED_RE = re.compile(
    r"postgres(?:ql)?(?:\+\w+)?://[^ @/]+@|AWS_[A-Z_]*KEY[A-Z_]*=\S+|password=\S+",
    re.IGNORECASE,
)


class DrillSafetyError(RuntimeError):
    """A drill operation attempted to address a non-disposable target."""


def redact(text: str) -> str:
    """Strip credential-shaped substrings before anything is logged."""
    return _CRED_RE.sub("[REDACTED]", text)


def disposable_container_name() -> str:
    return f"{DRILL_PREFIX}{uuid.uuid4().hex[:10]}"


def assert_disposable(name: str) -> str:
    """Hard guard: refuse anything that isn't an obviously-disposable drill
    target. Called before EVERY docker command that takes a container name."""
    if not name.startswith(DRILL_PREFIX):
        raise DrillSafetyError(f"container {name!r} lacks the drill prefix; refusing")
    if name == PRODUCTION_CONTAINER or PRODUCTION_CONTAINER in name:
        raise DrillSafetyError(f"container {name!r} matches production; refusing")
    return name


# --- verified-backup selection (pure) ----------------------------------------


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int
    last_modified: str


@dataclass(frozen=True)
class BackupChoice:
    key: str
    size: int
    filename: str
    status_size: int | None
    reason: str | None  # None when usable


def select_verified_backup(
    status: BackupStatus | None,
    objects: list[S3Object],
    *,
    backup_key: str | None = None,
) -> BackupChoice:
    """Choose the newest S3 object covered by the verified-upload status.

    Fails closed: if the newest object is not the one the status record
    verified, or the status is missing/failed, no silent fallback occurs.
    An explicit ``backup_key`` must still exist in the listing with a
    nonzero size (structural verification then happens during the drill).
    """
    dumps = [o for o in objects if o.key.endswith(".dump")]
    if not dumps:
        return BackupChoice("", 0, "", None, "no .dump objects in the S3 listing")
    if backup_key is not None:
        match = next((o for o in dumps if o.key == backup_key or o.key.endswith(backup_key)), None)
        if match is None:
            return BackupChoice(backup_key, 0, "", None, "explicit backup key not in S3 listing")
        if match.size <= 0:
            return BackupChoice(match.key, 0, "", None, "explicit backup object is zero bytes")
        return BackupChoice(match.key, match.size, Path(match.key).name, None, None)

    newest = max(dumps, key=lambda o: Path(o.key).name)  # timestamped names sort correctly
    if newest.size <= 0:
        return BackupChoice(newest.key, 0, "", None, "newest S3 object is zero bytes")
    if status is None:
        return BackupChoice(newest.key, newest.size, "", None, "no backup status record")
    if status.remote_outcome != "success":
        return BackupChoice(
            newest.key, newest.size, "", None, f"last remote outcome is {status.remote_outcome!r}"
        )
    status_name = Path(status.local_path).name if status.local_path else ""
    if Path(newest.key).name != status_name:
        return BackupChoice(
            newest.key,
            newest.size,
            "",
            None,
            f"newest S3 object {Path(newest.key).name!r} is not the verified upload "
            f"{status_name!r}",
        )
    if status.size_bytes is not None and status.size_bytes != newest.size:
        return BackupChoice(
            newest.key,
            newest.size,
            "",
            status.size_bytes,
            f"S3 size {newest.size} != recorded backup size {status.size_bytes}",
        )
    return BackupChoice(newest.key, newest.size, status_name, status.size_bytes, None)


# --- drill orchestration ------------------------------------------------------

Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def default_runner(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


@dataclass(frozen=True)
class DrillConfig:
    bucket: str
    prefix: str
    backup_dir: Path
    expected_revision: str = "0011"
    timeout_seconds: float = 900.0
    keep_on_failure: bool = False
    backup_key: str | None = None
    dry_run: bool = False
    image: str = IMAGE
    ready_timeout_seconds: float = 45.0
    code_commit: str = "unknown"  # recorded in the drill history (CLI supplies)


@dataclass
class DrillResult:
    drill_id: str
    status: str = "failed"  # success | failed | blocked
    failure_reason: str | None = None
    backup_key: str | None = None
    backup_size: int | None = None
    backup_sha256: str | None = None
    container: str | None = None
    pg_version: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    cleanup_ok: bool | None = None

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append({"step": name, "ok": ok, "detail": redact(detail)[:400]})
        return ok


def _append_history(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:  # append-only; never truncated
        fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")


def _write_state(path: Path, entry: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(entry, indent=2, sort_keys=True, default=str))
    tmp.replace(path)


def _list_s3(config: DrillConfig, runner: Runner) -> list[S3Object]:
    proc = runner(
        [
            "aws",
            "s3api",
            "list-objects-v2",
            "--bucket",
            config.bucket,
            "--prefix",
            f"{config.prefix.rstrip('/')}/",
            "--output",
            "json",
        ],
        60.0,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"S3 listing failed: {redact(proc.stderr.strip())[:200]}")
    data = json.loads(proc.stdout or "{}")
    return [
        S3Object(key=c["Key"], size=int(c["Size"]), last_modified=str(c.get("LastModified", "")))
        for c in data.get("Contents", [])
    ]


def run_drill(
    config: DrillConfig,
    *,
    runner: Runner = default_runner,
    now: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> DrillResult:
    """Execute one drill end to end. Never touches production: container
    names are guard-checked before every docker command, and no production
    connection string exists anywhere in this module."""
    now = now or datetime.now(UTC)
    result = DrillResult(drill_id=f"drill-{uuid.uuid4().hex[:12]}", started_at=now.isoformat())
    status = read_backup_status(config.backup_dir / "last_backup_status.json")
    tmp_dir: Path | None = None
    container: str | None = None
    failed_kept = False

    def fail(reason: str, *, blocked: bool = False) -> DrillResult:
        result.status = "blocked" if blocked else "failed"
        result.failure_reason = redact(reason)[:300]
        return result

    try:
        objects = _list_s3(config, runner)
        choice = select_verified_backup(status, objects, backup_key=config.backup_key)
        if choice.reason is not None:
            result.record("select verified S3 backup", False, choice.reason)
            return fail(choice.reason, blocked=True)
        result.backup_key, result.backup_size = choice.key, choice.size
        result.record("select verified S3 backup", True, f"{choice.key} ({choice.size} bytes)")
        if config.dry_run:
            result.status = "success"
            result.record("dry run", True, "selection verified; nothing downloaded")
            return result

        # -- download + integrity ------------------------------------------
        tmp_dir = Path(tempfile.mkdtemp(prefix="kalshi-restore-drill-"))
        dump_path = tmp_dir / Path(choice.key).name
        dl = runner(
            ["aws", "s3", "cp", f"s3://{config.bucket}/{choice.key}", str(dump_path)],
            config.timeout_seconds,
        )
        if not result.record("download from S3", dl.returncode == 0, dl.stderr.strip()[:200]):
            return fail("download failed")
        actual_size = dump_path.stat().st_size if dump_path.exists() else 0
        if not result.record(
            "size matches S3 listing", actual_size == choice.size, f"{actual_size} bytes"
        ):
            return fail("downloaded size mismatch")
        if actual_size == 0:
            result.record("nonzero file", False, "zero bytes")
            return fail("zero-byte download")
        digest = sha256_file(dump_path)
        result.backup_sha256 = digest
        local_twin = config.backup_dir / Path(choice.key).name
        if local_twin.exists():
            twin_digest = sha256_file(local_twin)
            if not result.record(
                "SHA-256 round-trip vs local backup",
                twin_digest == digest,
                f"s3={digest[:16]} local={twin_digest[:16]}",
            ):
                return fail("checksum mismatch against local backup")
        else:
            result.record("SHA-256 recorded (local twin pruned)", True, digest[:16])

        # -- disposable container ------------------------------------------
        container = assert_disposable(disposable_container_name())
        result.container = container
        up = runner(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-e",
                f"POSTGRES_USER={DB_USER}",
                "-e",
                "POSTGRES_PASSWORD=drill-only-disposable",
                "-e",
                f"POSTGRES_DB={DB_NAME}",
                config.image,
            ],
            120.0,
        )
        if not result.record(
            "start disposable container", up.returncode == 0, up.stderr.strip()[:200]
        ):
            return fail("disposable container failed to start")

        deadline = clock() + config.ready_timeout_seconds
        ready = False
        while clock() < deadline:
            chk = runner(
                ["docker", "exec", assert_disposable(container), "pg_isready", "-U", DB_USER], 15.0
            )
            if chk.returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not result.record("disposable postgres accepts connections", ready):
            return fail("disposable postgres never became ready")

        ver = runner(
            ["docker", "exec", assert_disposable(container), "pg_restore", "--version"], 15.0
        )
        result.pg_version = ver.stdout.strip()
        major_ok = f" {EXPECTED_PG_MAJOR}." in ver.stdout
        if not result.record("PostgreSQL major version compatible", major_ok, ver.stdout.strip()):
            return fail("postgres major-version mismatch")

        cp = runner(
            ["docker", "cp", str(dump_path), f"{assert_disposable(container)}:/tmp/drill.dump"],
            config.timeout_seconds,
        )
        if not result.record("copy dump into container", cp.returncode == 0, cp.stderr[:200]):
            return fail("docker cp failed")

        listing = runner(
            [
                "docker",
                "exec",
                assert_disposable(container),
                "pg_restore",
                "--list",
                "/tmp/drill.dump",
            ],
            config.timeout_seconds,
        )
        list_ok = listing.returncode == 0 and "alembic_version" in listing.stdout
        tables_in_dump = [t for t in REPRESENTATIVE_TABLES if t in listing.stdout]
        if not result.record(
            "dump structure valid (pg_restore --list + alembic_version + core tables)",
            list_ok and len(tables_in_dump) >= len(REPRESENTATIVE_TABLES) - 1,
            f"tables_present={len(tables_in_dump)}/{len(REPRESENTATIVE_TABLES)}",
        ):
            return fail("dump structure invalid")

        restore = runner(
            [
                "docker",
                "exec",
                assert_disposable(container),
                "pg_restore",
                "-U",
                DB_USER,
                "-d",
                DB_NAME,
                "--no-owner",
                "--no-privileges",
                "/tmp/drill.dump",
            ],
            config.timeout_seconds,
        )
        # benign warnings exit nonzero; the validations below are the signal
        result.record(
            "pg_restore executed", True, f"exit={restore.returncode} {restore.stderr[-200:]}"
        )

        # -- validation ----------------------------------------------------
        def psql(sql: str) -> str:
            proc = runner(
                [
                    "docker",
                    "exec",
                    assert_disposable(container),  # guard on every query
                    "psql",
                    "-U",
                    DB_USER,
                    "-d",
                    DB_NAME,
                    "-tA",
                    "-c",
                    sql,
                ],
                60.0,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"psql failed: {redact(proc.stderr.strip())[:200]}")
            return proc.stdout.strip()

        revision = psql("SELECT version_num FROM alembic_version;")
        if not result.record(
            f"alembic_version == {config.expected_revision}",
            revision == config.expected_revision,
            revision,
        ):
            return fail(f"schema revision {revision!r} != {config.expected_revision!r}")

        counts: dict[str, int] = {}
        for table in REPRESENTATIVE_TABLES:
            counts[table] = int(psql(f"SELECT count(*) FROM {table};"))
        nonzero = sum(1 for c in counts.values() if c > 0)
        if not result.record(
            "representative tables populated",
            nonzero >= len(REPRESENTATIVE_TABLES) - 1,
            json.dumps(counts),
        ):
            return fail("representative tables missing or empty")

        semantic = [
            (
                "market snapshot sample (ticker+status)",
                "SELECT market_ticker || '|' || coalesce(status,'?') FROM market_snapshots "
                "WHERE market_ticker <> '' LIMIT 1;",
            ),
            (
                "order-book sample with provenance",
                "SELECT market_ticker || '|' || coalesce(raw_payload_id::text,'null') "
                "FROM orderbook_snapshots LIMIT 1;",
            ),
            (
                "polling attempt linked to its collector run",
                "SELECT p.ticker || '|' || r.id::text FROM market_poll_attempts p "
                "JOIN collector_runs r ON r.id = p.collector_run_id LIMIT 1;",
            ),
            (
                "weather forecast with observed_at",
                "SELECT station_id || '|' || observed_at::text FROM weather_forecasts "
                "WHERE observed_at IS NOT NULL LIMIT 1;",
            ),
            (
                "observation with station and date",
                "SELECT station_id || '|' || observation_date::text FROM weather_observations "
                "LIMIT 1;",
            ),
            (
                "settled snapshot sample (result recorded)",
                "SELECT coalesce((SELECT market_ticker || '|' || result FROM market_snapshots "
                "WHERE result IN ('yes','no') LIMIT 1), 'none-settled-at-backup-time');",
            ),
        ]
        for name, sql in semantic:
            value = psql(sql)
            if not result.record(name, bool(value), value[:120]):
                return fail(f"semantic validation failed: {name}")

        latest_run = psql(
            "SELECT coalesce(max(finished_at)::text,'') FROM collector_runs "
            "WHERE finished_at IS NOT NULL;"
        )
        result.record("latest collector-run timestamp present", bool(latest_run), latest_run)

        result.status = "success"
        return result
    except subprocess.TimeoutExpired as exc:
        result.record("timeout", False, f"command timed out: {redact(str(exc.cmd))[:120]}")
        return fail("timeout")
    except (RuntimeError, OSError, json.JSONDecodeError, DrillSafetyError) as exc:
        result.record("unexpected failure", False, f"{type(exc).__name__}: {exc}")
        return fail(f"{type(exc).__name__}: {exc}")
    finally:
        cleanup_ok = True
        if container is not None:
            keep = config.keep_on_failure and result.status != "success"
            if keep:
                failed_kept = True
                result.record("cleanup skipped (--keep-on-failure)", True, container)
            else:
                rm = runner(["docker", "rm", "-f", assert_disposable(container)], 120.0)
                cleanup_ok = cleanup_ok and rm.returncode == 0
                result.record("disposable container removed", rm.returncode == 0, container)
        if tmp_dir is not None and not failed_kept:
            try:
                for child in tmp_dir.iterdir():
                    child.unlink()
                tmp_dir.rmdir()
                result.record("temporary download deleted", True, str(tmp_dir))
            except OSError as exc:
                cleanup_ok = False
                result.record("temporary download deleted", False, str(exc))
        result.cleanup_ok = cleanup_ok
        if not cleanup_ok and result.status == "success":
            result.status = "failed"
            result.failure_reason = "cleanup_failure"
        result.completed_at = datetime.now(UTC).isoformat()
        backup_ts = None
        if result.backup_key:
            m = re.search(r"(\d{8}T\d{6}Z)", result.backup_key)
            backup_ts = m.group(1) if m else None
        entry = {
            "drill_id": result.drill_id,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "git_commit": config.code_commit,
            "backup_timestamp": backup_ts,
            "status": result.status,
            "failure_reason": result.failure_reason,
            "backup_key": result.backup_key,
            "backup_size": result.backup_size,
            "backup_sha256": result.backup_sha256,
            "container": result.container,
            "pg_version": result.pg_version,
            "cleanup_ok": result.cleanup_ok,
            "steps": result.steps,
        }
        if not config.dry_run:
            _append_history(config.backup_dir / HISTORY_FILENAME, entry)
            _write_state(config.backup_dir / STATE_FILENAME, entry)


def read_drill_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())  # type: ignore[no-any-return]
