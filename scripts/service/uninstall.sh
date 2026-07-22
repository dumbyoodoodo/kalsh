#!/usr/bin/env bash
# Remove the collector service registration. Stops the running collector
# gracefully (SIGTERM -> ops run's signal handler). Leaves all logs and, of
# course, all collected data untouched.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

for label in "${SERVICE_LABEL}" "${ROTATE_LABEL}"; do
    launchctl bootout "${GUI_DOMAIN}/${label}" 2>/dev/null \
        && echo "stopped + unregistered ${label}" \
        || echo "${label} was not loaded"
done
rm -f "${SERVICE_PLIST}" "${ROTATE_PLIST}"
echo "removed plists; logs kept in ${LOG_DIR}"
