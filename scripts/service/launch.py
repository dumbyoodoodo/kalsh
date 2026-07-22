"""Single-instance launcher: acquire an exclusive lock, then become the
collector (`ops run`) in-process.

The lock fd stays open in THIS process for the collector's whole lifetime, so
the kernel releases it at the exact instant the collector exits, however it
exits (SIGKILL included). During a service restart the predecessor may drain
gracefully for up to launchd's ExitTimeOut (30s), so we retry for 90s before
concluding the holder is a genuinely separate instance -- then exit 1, which
launchd's KeepAlive treats as "retry later": if the foreign instance ever
stops, the service converges back to running on its own.
"""

import fcntl
import runpy
import sys
import time
from pathlib import Path

LOCK_WAIT_SECONDS = 90
RETRY_INTERVAL_SECONDS = 2
REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_manifest(manifest_path: str) -> None:
    """Record what code this collector instance runs (for `ops restart-check`
    / check_restart.sh). Strictly best-effort: a manifest failure must never
    cost collector uptime, so any exception is logged and swallowed."""
    try:
        from kalshi_weather.ops.restart_policy import write_manifest

        write_manifest(REPO_ROOT, Path(manifest_path))
    except Exception as exc:  # uptime beats bookkeeping here
        print(f"manifest write failed (non-fatal): {exc!r}", file=sys.stderr)


def main() -> None:
    lock_path = sys.argv[1]
    lock_file = open(lock_path, "w")  # noqa: SIM115 -- held for process lifetime
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                print(
                    f"another collector instance held {lock_path} for "
                    f"{LOCK_WAIT_SECONDS}s; exiting (launchd will retry)",
                    file=sys.stderr,
                )
                sys.exit(1)
            time.sleep(RETRY_INTERVAL_SECONDS)

    if len(sys.argv) > 2:  # optional: run.sh passes <log dir>/collector.manifest.json
        _write_manifest(sys.argv[2])

    sys.argv = ["kalshi-weather", "ops", "run"]
    runpy.run_module("kalshi_weather.cli", run_name="__main__")


if __name__ == "__main__":
    main()
