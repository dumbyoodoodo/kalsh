#!/usr/bin/env bash
# Does the running collector need a restart to pick up the working tree?
#
#   scripts/service/check_restart.sh            # report only (never restarts)
#   scripts/service/check_restart.sh --restart  # and act on the verdict:
#                                               #   restart  -> restart.sh (graceful)
#                                               #   reinstall-> install.sh (idempotent)
#                                               #   unknown  -> restart.sh (writes first manifest)
#
# The classification rules live in src/kalshi_weather/ops/restart_policy.py
# (the single source of truth); this script just runs `ops restart-check`
# against the manifest the service wrote at its last start.
#
# Exit codes (without --restart): 0 = no restart needed, 10 = restart
# required, 11 = reinstall required, 12 = unknown (no manifest), 1 = error.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "${REPO_DIR}"

do_restart=false
if [[ "${1:-}" == "--restart" ]]; then
    do_restart=true
elif [[ -n "${1:-}" ]]; then
    echo "usage: $0 [--restart]" >&2
    exit 1
fi

rc=0
PYTHONPATH=src .venv/bin/python -m kalshi_weather.cli ops restart-check \
    --manifest "${LOG_DIR}/collector.manifest.json" --repo-root "${REPO_DIR}" || rc=$?

if ! ${do_restart}; then
    exit "${rc}"
fi

case "${rc}" in
    0)  echo "nothing to do" ;;
    10) echo "-> restarting (graceful)"; exec "${REPO_DIR}/scripts/service/restart.sh" ;;
    11) echo "-> reinstalling"; exec "${REPO_DIR}/scripts/service/install.sh" ;;
    12) echo "-> restarting to establish a manifest baseline"
        exec "${REPO_DIR}/scripts/service/restart.sh" ;;
    *)  echo "restart-check failed (exit ${rc}); not restarting" >&2; exit "${rc}" ;;
esac
