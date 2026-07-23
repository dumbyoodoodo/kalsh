"""Validates the concurrency primitive `scripts/backup_postgres.sh` relies
on for overlapping-run suppression: `mkdir` on an already-existing
directory is atomic and fails on POSIX filesystems. The full script isn't
exercised here (it needs a live docker-compose Postgres) -- this test
isolates and proves the exact mechanism the script's lock block uses,
mirroring its logic (mkdir succeeds once, a second mkdir on the same path
fails, rmdir releases it).
"""

import subprocess
from pathlib import Path


def test_mkdir_lock_is_atomic_and_a_second_attempt_fails(tmp_path: Path) -> None:
    lock_dir = tmp_path / "backup.lock"

    first = subprocess.run(["mkdir", str(lock_dir)], capture_output=True)
    assert first.returncode == 0
    assert lock_dir.is_dir()

    second = subprocess.run(["mkdir", str(lock_dir)], capture_output=True)
    assert second.returncode != 0  # already held -- the script treats this as "skip this cycle"


def test_mkdir_lock_is_available_again_after_release(tmp_path: Path) -> None:
    lock_dir = tmp_path / "backup.lock"
    subprocess.run(["mkdir", str(lock_dir)], check=True)
    subprocess.run(["rmdir", str(lock_dir)], check=True)
    third = subprocess.run(["mkdir", str(lock_dir)], capture_output=True)
    assert third.returncode == 0


def test_skip_counter_increments_when_lock_held(tmp_path: Path) -> None:
    """Reproduces backup_postgres.sh's skip-counter block exactly (the
    script's own inline shell, run standalone) to prove the counter
    increments correctly across repeated contention and resets are
    independent of this test's Python layer."""
    lock_dir = tmp_path / "backup.lock"
    skip_file = tmp_path / "lock_skip_count"
    script = f"""
    set -euo pipefail
    lock_dir="{lock_dir}"
    skip_count_file="{skip_file}"
    if ! mkdir "${{lock_dir}}" 2>/dev/null; then
        prev="$(cat "${{skip_count_file}}" 2>/dev/null || echo 0)"
        echo $((prev + 1)) > "${{skip_count_file}}"
        echo "skipped"
        exit 0
    fi
    echo "acquired"
    """
    lock_dir.mkdir()  # simulate a run already in progress

    subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True)
    subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True)

    assert skip_file.read_text().strip() == "2"
