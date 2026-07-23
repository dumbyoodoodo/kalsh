"""End-to-end tests for scripts/backup_postgres.sh's status recording and
BACKUP_LOCAL_DIR resolution, run against the real script with a stubbed
`docker` on PATH (no live Postgres needed) and a disposable backup
directory. `BACKUP_REMOTE_TYPE=none` is forced in the environment so no
off-machine copy (S3 or filesystem) is ever attempted from a test.

These prove the two guarantees the 2026-07-23 launchd/TCC incident showed
were missing:
1. a failed run always leaves a machine-readable failed-outcome status
   record (it used to die before any status write), and
2. the local destination honors BACKUP_LOCAL_DIR so the scheduled (launchd)
   context can write an internal-disk directory.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "backup_postgres.sh"

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / ".venv" / "bin" / "python").exists(),
    reason="repo virtualenv required (script invokes .venv/bin/python for finalize)",
)


def _write_fake_docker(bin_dir: Path, *, dump_ok: bool, entries: int = 20) -> None:
    """A `docker` stub: `... pg_dump ...` emits bytes (or fails);
    `... pg_restore --list` emits `entries` lines."""
    lines = "\n".join(f"; entry {i}" for i in range(entries))
    body = f"""#!/usr/bin/env bash
if [[ "$*" == *pg_dump* ]]; then
    {'printf "fake-dump-bytes-%s" "$(date +%s)"' if dump_ok else "exit 1"}
elif [[ "$*" == *pg_restore* ]]; then
    cat > /dev/null
    printf '%s\\n' "{lines}"
fi
"""
    fake = bin_dir / "docker"
    fake.write_text(body)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


def _run_script(backup_dir: Path, fake_bin: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["BACKUP_LOCAL_DIR"] = str(backup_dir)
    env["BACKUP_REMOTE_TYPE"] = "none"  # never touch S3/filesystem remotes from tests
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, cwd=REPO_ROOT
    )


def test_failed_dump_records_failed_status(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, dump_ok=False)
    backup_dir = tmp_path / "backups"

    result = _run_script(backup_dir, fake_bin)

    assert result.returncode == 1
    status = json.loads((backup_dir / "last_backup_status.json").read_text())
    assert status["local_outcome"] == "failed"
    assert "pg_dump failed" in (status["error"] or "")
    assert status["remote_outcome"] == "skipped"
    # no dump file was promoted to a trusted name
    assert not list(backup_dir.glob("*.dump"))


def test_successful_run_writes_dump_and_success_status(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, dump_ok=True)
    backup_dir = tmp_path / "backups"

    result = _run_script(backup_dir, fake_bin)

    assert result.returncode == 0, result.stderr
    dumps = list(backup_dir.glob("kalshi_weather-*.dump"))
    assert len(dumps) == 1
    assert not list(backup_dir.glob("*.partial"))  # atomic rename happened
    status = json.loads((backup_dir / "last_backup_status.json").read_text())
    assert status["local_outcome"] == "success"
    assert status["local_path"] == str(dumps[0])
    assert status["entries"] >= 10
    # remote explicitly not configured in the test environment
    assert status["remote_outcome"] == "not_configured"
    # lock released; skip counter reset by the successful acquisition
    assert not (backup_dir / ".backup.lock").exists()
    assert (backup_dir / "lock_skip_count").read_text().strip() == "0"


def test_broken_archive_records_failed_status_and_keeps_partial(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, dump_ok=True, entries=3)  # < 10 -> looks broken
    backup_dir = tmp_path / "backups"

    result = _run_script(backup_dir, fake_bin)

    assert result.returncode == 1
    status = json.loads((backup_dir / "last_backup_status.json").read_text())
    assert status["local_outcome"] == "failed"
    assert "only 3 entries" in (status["error"] or "")
    # forensic .partial left behind; nothing at a trusted final name
    assert list(backup_dir.glob("*.partial"))
    assert not list(backup_dir.glob("*.dump"))
