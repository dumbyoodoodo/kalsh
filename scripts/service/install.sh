#!/usr/bin/env bash
# Install (or reinstall) the collector, log-rotation, monitor, and backup
# launchd agents. Generates the plists at install time so absolute paths
# come from THIS checkout on THIS machine -- nothing machine-specific is
# committed. Idempotent: safe to re-run after moving the repo or editing
# these scripts.
#
# Reload is PER-AGENT, not all-or-nothing: each agent's freshly-generated
# plist is compared against what's already installed, and bootout+bootstrap
# only runs for an agent whose plist actually changed (or that isn't loaded
# yet). Running this to add a brand-new agent -- e.g. after this backup
# system's first install -- therefore does NOT restart the live collector
# if the collector's own plist is unchanged. Before this script existed in
# its current form, EVERY run reloaded ALL agents unconditionally; that
# unconditional reload is what caused the collector's brief restart the
# first time the monitor agent was added (documented in
# docs/runbooks/monitoring_alerting.md's install notes) -- this is the fix.
#
# Usage: scripts/service/install.sh
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ERROR: this installer targets macOS launchd only (detected $(uname -s))." >&2
    echo "On Linux, write a systemd unit instead -- see docs/runbooks/collector_service.md." >&2
    exit 1
fi

mkdir -p "${AGENTS_DIR}" "${LOG_DIR}"

# Fail the INSTALL loudly on a broken environment rather than installing a
# service that will only crash-loop.
echo "running preflight..."
PYTHONPATH="${REPO_DIR}/src" "${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/scripts/service/preflight.py"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

cat > "${TMP_DIR}/collector.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${SERVICE_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${REPO_DIR}/scripts/service/run.sh</string>
    </array>
    <key>WorkingDirectory</key><string>${REPO_DIR}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict>
        <!-- restart on unexpected (nonzero) exit; a graceful SIGTERM stop
             (exit 0) or a duplicate-instance guard exit stays stopped -->
        <key>SuccessfulExit</key><false/>
    </dict>
    <key>ThrottleInterval</key><integer>60</integer>
    <key>ExitTimeOut</key><integer>30</integer>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>${OUT_LOG}</string>
    <key>StandardErrorPath</key><string>${ERR_LOG}</string>
</dict>
</plist>
PLIST

cat > "${TMP_DIR}/rotate.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${ROTATE_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${REPO_DIR}/scripts/service/rotate_logs.sh</string>
    </array>
    <key>WorkingDirectory</key><string>${REPO_DIR}</string>
    <key>StartInterval</key><integer>21600</integer>
    <key>RunAtLoad</key><false/>
    <key>StandardErrorPath</key><string>${LOG_DIR}/logrotate.err.log</string>
</dict>
</plist>
PLIST

cat > "${TMP_DIR}/monitor.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${MONITOR_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${REPO_DIR}/scripts/service/monitor.sh</string>
    </array>
    <key>WorkingDirectory</key><string>${REPO_DIR}</string>
    <key>StartInterval</key><integer>${MONITOR_INTERVAL_SECONDS}</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>${MONITOR_OUT_LOG}</string>
    <key>StandardErrorPath</key><string>${MONITOR_ERR_LOG}</string>
</dict>
</plist>
PLIST

cat > "${TMP_DIR}/backup.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${BACKUP_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${REPO_DIR}/scripts/service/backup.sh</string>
    </array>
    <key>WorkingDirectory</key><string>${REPO_DIR}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>${BACKUP_SCHEDULE_HOUR}</integer>
        <key>Minute</key><integer>${BACKUP_SCHEDULE_MINUTE}</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>${BACKUP_OUT_LOG}</string>
    <key>StandardErrorPath</key><string>${BACKUP_ERR_LOG}</string>
</dict>
</plist>
PLIST

plutil -lint "${TMP_DIR}/collector.plist" "${TMP_DIR}/rotate.plist" \
    "${TMP_DIR}/monitor.plist" "${TMP_DIR}/backup.plist"

# Per-agent conditional reload: only bootout+bootstrap a label whose plist
# content actually changed, or that isn't currently loaded. See this
# script's header comment for why.
_install_agent() {
    local label="$1" new_plist="$2" installed_plist="$3"
    if [[ -f "${installed_plist}" ]] && cmp -s "${new_plist}" "${installed_plist}" \
        && launchctl print "${GUI_DOMAIN}/${label}" > /dev/null 2>&1; then
        echo "  ${label}: unchanged, already loaded -- not reloaded"
        return 0
    fi
    cp "${new_plist}" "${installed_plist}"
    launchctl bootout "${GUI_DOMAIN}/${label}" 2>/dev/null || true
    launchctl bootstrap "${GUI_DOMAIN}" "${installed_plist}"
    echo "  ${label}: (re)loaded"
}

echo "reloading agents (only those that changed):"
_install_agent "${SERVICE_LABEL}" "${TMP_DIR}/collector.plist" "${SERVICE_PLIST}"
_install_agent "${ROTATE_LABEL}" "${TMP_DIR}/rotate.plist" "${ROTATE_PLIST}"
_install_agent "${MONITOR_LABEL}" "${TMP_DIR}/monitor.plist" "${MONITOR_PLIST}"
_install_agent "${BACKUP_LABEL}" "${TMP_DIR}/backup.plist" "${BACKUP_PLIST}"

echo "installed:"
echo "  ${SERVICE_PLIST}"
echo "  ${ROTATE_PLIST}"
echo "  ${MONITOR_PLIST}  (every ${MONITOR_INTERVAL_SECONDS}s)"
echo "  ${BACKUP_PLIST}  (daily at ${BACKUP_SCHEDULE_HOUR}:$(printf '%02d' "${BACKUP_SCHEDULE_MINUTE}"))"
echo "logs: ${OUT_LOG} / ${ERR_LOG}"
echo "monitor logs: ${MONITOR_OUT_LOG} / ${MONITOR_ERR_LOG}"
echo "backup logs: ${BACKUP_OUT_LOG} / ${BACKUP_ERR_LOG}"
echo "check: scripts/service/status.sh"
