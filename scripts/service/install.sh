#!/usr/bin/env bash
# Install (or reinstall) the collector as a user launchd agent.
# Generates the plists at install time so absolute paths come from THIS
# checkout on THIS machine -- nothing machine-specific is committed.
# Idempotent: safe to re-run after moving the repo or editing these scripts.
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

cat > "${SERVICE_PLIST}" <<PLIST
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

cat > "${ROTATE_PLIST}" <<PLIST
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

plutil -lint "${SERVICE_PLIST}" "${ROTATE_PLIST}"

# Reload cleanly if already installed (bootout is a service-registration
# operation only; it never touches data or logs).
for label in "${SERVICE_LABEL}" "${ROTATE_LABEL}"; do
    launchctl bootout "${GUI_DOMAIN}/${label}" 2>/dev/null || true
done
launchctl bootstrap "${GUI_DOMAIN}" "${SERVICE_PLIST}"
launchctl bootstrap "${GUI_DOMAIN}" "${ROTATE_PLIST}"

echo "installed:"
echo "  ${SERVICE_PLIST}"
echo "  ${ROTATE_PLIST}"
echo "logs: ${OUT_LOG} / ${ERR_LOG}"
echo "check: scripts/service/status.sh"
