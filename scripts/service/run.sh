#!/usr/bin/env bash
# launchd entry point for the collector service. Invoked by launchd (see
# install.sh); not normally run by hand. Order of operations:
#   1. single-instance guard   2. log rotation   3. preflight   4. exec collector
#
# Exit semantics matter: launchd is configured with KeepAlive.SuccessfulExit=
# false, so exit 0 means "stay stopped" and nonzero means "retry after the
# throttle interval". A duplicate instance exits 0 (starting a second copy
# would be worse than staying down); a failed preflight exits 1 (retry until
# the drive is mounted / Postgres is up).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "${REPO_DIR}"

# launchd gives agents a minimal environment; make PATH sane.
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="${REPO_DIR}/src"
export PYTHONUNBUFFERED=1

# --- 1. Rotate logs if oversized (copytruncate-style; see rotate_logs.sh).
mkdir -p "${LOG_DIR}"
"${REPO_DIR}/scripts/service/rotate_logs.sh" || true

# --- 2. Preflight: data root mounted+writable, secrets loadable, DB up.
"${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/scripts/service/preflight.py"

# --- 3. Become the collector, via the single-instance launcher (launch.py):
# it flocks ${LOG_DIR}/collector.lock inside the final collector process, so
# the lock lives exactly as long as the collector does and a second start
# (racing respawn, or overlap with a draining predecessor) waits, then defers.
exec "${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/scripts/service/launch.py" \
    "${LOG_DIR}/collector.lock" "${LOG_DIR}/collector.manifest.json"
