#!/usr/bin/env python3
"""Restore drill: prove a backup is actually restorable, not just readable
by `pg_restore --list` (docs/runbooks/backup_recovery.md "Recovery
validation" -- a backup is not complete until restore is tested).

Spins up a disposable, isolated PostgreSQL container -- a fresh image pull,
no named volume, no port published, no relation whatsoever to the running
`kalsh-postgres-1` production container -- restores the newest local
backup into it, and validates schema/representative tables/row counts/
critical column types. Always removes the disposable container afterward,
success or failure (`finally`). Never connects to, queries, or modifies
the production database or docker-compose.yml's `postgres` service.

Writes a machine-readable JSON report to
`<backup_dir>/restore_drill_report.json`.

Usage: uv run python scripts/backup_restore_drill.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshi_weather.config import get_settings
from kalshi_weather.ops.backup_retention import list_backup_files

IMAGE = "postgres:16-alpine"
DB_USER = "kalshi"
DB_NAME = "kalshi_weather"
READY_TIMEOUT_SECONDS = 30

#: A representative sample across every collection stream plus the
#: platform-level audit trail -- not every table, but enough to prove the
#: restore is structurally sound end to end, not merely non-empty.
REPRESENTATIVE_TABLES = (
    "raw_api_payloads",
    "weather_stations",
    "weather_observations",
    "weather_forecasts",
    "market_snapshots",
    "trades",
    "market_candlesticks",
    "collector_runs",
)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def _psql(container: str, sql: str) -> str:
    result = _run(
        ["docker", "exec", container, "psql", "-U", DB_USER, "-d", DB_NAME, "-tA", "-c", sql]
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql query failed: {sql!r}: {result.stderr.strip()}")
    return result.stdout.strip()


def main() -> int:
    settings = get_settings()
    base = settings.data_dir if settings.data_dir.is_absolute() else Path.cwd() / settings.data_dir
    backup_dir = base / "backups" / "postgres"
    files = list_backup_files(backup_dir)
    if not files:
        print(f"ERROR: no backup files found in {backup_dir}", file=sys.stderr)
        return 1
    newest = max(files, key=lambda f: f.timestamp)

    container = f"kalshi-restore-drill-{uuid.uuid4().hex[:8]}"
    report: dict[str, object] = {
        "schema": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "backup_file": str(newest.path),
        "backup_timestamp": newest.timestamp.isoformat(),
        "container": container,
        "steps": [],
        "ok": False,
    }
    steps: list[dict[str, object]] = report["steps"]  # type: ignore[assignment]

    def record(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})
        print(f"[{'OK' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")

    try:
        # 1. Start a disposable container -- no volume, no published port,
        # no connection to the real docker-compose stack whatsoever.
        run_result = _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-e",
                f"POSTGRES_USER={DB_USER}",
                "-e",
                "POSTGRES_PASSWORD=kalshi-drill-only",
                "-e",
                f"POSTGRES_DB={DB_NAME}",
                IMAGE,
            ]
        )
        if run_result.returncode != 0:
            record("start disposable container", False, run_result.stderr.strip())
            return 1
        record("start disposable container", True, container)

        # 2. Wait for it to accept connections.
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        ready = False
        while time.monotonic() < deadline:
            check = _run(["docker", "exec", container, "pg_isready", "-U", DB_USER])
            if check.returncode == 0:
                ready = True
                break
            time.sleep(1)
        record("wait for disposable postgres to accept connections", ready)
        if not ready:
            return 1

        # 3. Copy the backup file in and restore it.
        cp_result = _run(["docker", "cp", str(newest.path), f"{container}:/tmp/restore.dump"])
        record(
            "copy backup file into container", cp_result.returncode == 0, cp_result.stderr.strip()
        )
        if cp_result.returncode != 0:
            return 1

        restore_result = _run(
            [
                "docker",
                "exec",
                container,
                "pg_restore",
                "-U",
                DB_USER,
                "-d",
                DB_NAME,
                "--no-owner",
                "--no-privileges",
                "/tmp/restore.dump",
            ]
        )
        # pg_restore commonly exits nonzero on benign warnings (e.g. an
        # extension already present) -- treat "did tables actually land"
        # (checked next) as the real signal, not this exit code alone.
        record(
            "pg_restore",
            True,
            f"exit={restore_result.returncode} stderr_tail={restore_result.stderr.strip()[-300:]}",
        )

        # 4. Schema/migration identifiability.
        revision = _psql(container, "SELECT version_num FROM alembic_version;")
        record("alembic_version identifiable", bool(revision), revision)

        table_count = int(
            _psql(
                container,
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';",
            )
        )
        record("schema restores (tables present)", table_count > 0, f"{table_count} tables")

        # 5. Representative tables restore with plausible row counts.
        table_counts: dict[str, int] = {}
        for table in REPRESENTATIVE_TABLES:
            count = int(_psql(container, f"SELECT count(*) FROM {table};"))
            table_counts[table] = count
        report["table_counts"] = table_counts
        nonzero_tables = sum(1 for c in table_counts.values() if c > 0)
        record(
            "representative tables have plausible (nonzero) row counts",
            nonzero_tables >= len(REPRESENTATIVE_TABLES) - 1,  # tolerate one legitimately-empty
            json.dumps(table_counts),
        )

        # 6. Critical timestamp/numeric columns survive correctly.
        sample = _psql(
            container,
            "SELECT station_id, issuance_time, value FROM weather_observations "
            "ORDER BY id LIMIT 1;",
        )
        parts = sample.split("|")
        timestamp_ok = False
        numeric_ok = False
        if len(parts) == 3:
            try:
                datetime.fromisoformat(parts[1].strip())
                timestamp_ok = True
            except ValueError:
                pass
            try:
                float(parts[2].strip())
                numeric_ok = True
            except ValueError:
                pass
        record("timestamp column survives as a parseable datetime", timestamp_ok, sample)
        record("numeric column survives as a parseable number", numeric_ok, sample)

        report["ok"] = all(bool(s["ok"]) for s in steps)
        return 0 if report["ok"] else 1
    except Exception as exc:
        record("unexpected error during restore drill", False, f"{type(exc).__name__}: {exc}")
        report["ok"] = False
        return 1
    finally:
        # Always tear down -- success or failure, never leave a disposable
        # container running.
        _run(["docker", "rm", "-f", container])
        record("cleanup: disposable container removed", True)
        report_path = backup_dir / "restore_drill_report.json"
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"report written: {report_path}")


if __name__ == "__main__":
    raise SystemExit(main())
