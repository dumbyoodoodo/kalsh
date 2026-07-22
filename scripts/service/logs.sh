#!/usr/bin/env bash
# Tail the collector service logs.
#   scripts/service/logs.sh          follow live output (stdout + stderr)
#   scripts/service/logs.sh -n 200   print the last 200 lines and exit
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

if [[ "${1:-}" == "-n" ]]; then
    tail -n "${2:-100}" "${OUT_LOG}" "${ERR_LOG}"
else
    echo "following ${OUT_LOG} and ${ERR_LOG} (Ctrl-C to stop)"
    tail -f "${OUT_LOG}" "${ERR_LOG}"
fi
