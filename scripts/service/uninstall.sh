#!/usr/bin/env bash
# Remove the collector service registration. Stops the running collector
# gracefully (SIGTERM -> ops run's signal handler). Leaves all logs and, of
# course, all collected data AND backups untouched -- including
# monitor_state.json, alert_history.jsonl, and every file under
# <KALSHI_DATA_DIR>/backups/postgres/, which remain useful records even
# with the service uninstalled. See docs/runbooks/backup_recovery.md "How
# to disable the service safely" for uninstalling just the backup agent
# without touching the collector/monitor.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

for label in "${SERVICE_LABEL}" "${ROTATE_LABEL}" "${MONITOR_LABEL}" "${BACKUP_LABEL}"; do
    launchctl bootout "${GUI_DOMAIN}/${label}" 2>/dev/null \
        && echo "stopped + unregistered ${label}" \
        || echo "${label} was not loaded"
done
rm -f "${SERVICE_PLIST}" "${ROTATE_PLIST}" "${MONITOR_PLIST}" "${BACKUP_PLIST}"
echo "removed plists; logs and alert history kept in ${LOG_DIR}"
echo "backups kept in <KALSHI_DATA_DIR>/backups/postgres/"
