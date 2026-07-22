#!/usr/bin/env bash
# Size-based rotation for the collector service logs. Runs at service start
# and periodically via the com.kalshi-weather.logrotate launchd agent (native
# launchd StartInterval -- not cron).
#
# Copytruncate style (copy the file, then truncate in place) because the
# collector keeps an O_APPEND fd open on the live file -- renaming it out from
# under the process would send all future output to a rotated file forever.
# A few lines written during the copy window can be lost; acceptable for
# operational logs (the scientific record lives in Postgres, not here).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

MAX_BYTES=$((20 * 1024 * 1024))   # rotate above 20 MB
KEEP=5                            # keep .1 (newest) .. .5 (oldest)

rotate() {
    local log="$1"
    [[ -f "${log}" ]] || return 0
    local size
    size=$(stat -f%z "${log}")
    (( size > MAX_BYTES )) || return 0
    local i
    for (( i = KEEP - 1; i >= 1; i-- )); do
        [[ -f "${log}.${i}" ]] && mv "${log}.${i}" "${log}.$((i + 1))"
    done
    cp "${log}" "${log}.1"
    : > "${log}"
    echo "rotated ${log} (${size} bytes)"
}

mkdir -p "${LOG_DIR}"
rotate "${OUT_LOG}"
rotate "${ERR_LOG}"
