# Shared definitions for the collector launchd service scripts.
# Sourced, not executed.

SERVICE_LABEL="com.kalshi-weather.collector"
ROTATE_LABEL="com.kalshi-weather.logrotate"
MONITOR_LABEL="com.kalshi-weather.monitor"
BACKUP_LABEL="com.kalshi-weather.backup"

# Repo root = two levels up from this file.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

AGENTS_DIR="${HOME}/Library/LaunchAgents"
SERVICE_PLIST="${AGENTS_DIR}/${SERVICE_LABEL}.plist"
ROTATE_PLIST="${AGENTS_DIR}/${ROTATE_LABEL}.plist"
MONITOR_PLIST="${AGENTS_DIR}/${MONITOR_LABEL}.plist"
BACKUP_PLIST="${AGENTS_DIR}/${BACKUP_LABEL}.plist"

# Service logs live on the INTERNAL disk by default (macOS convention),
# deliberately not under KALSHI_DATA_DIR: if the external drive is missing,
# the failure to start must itself be logged somewhere that still exists.
# Override with KALSHI_LOG_DIR (environment wins over .env).
_read_env_var() {
    # _read_env_var NAME -> prints value from environment, else from .env.
    # cut -f2- preserves '=' and spaces in values.
    local name="$1" val=""
    val="${!name:-}"
    if [[ -z "${val}" && -f "${REPO_DIR}/.env" ]]; then
        val="$(grep -E "^${name}=" "${REPO_DIR}/.env" | tail -1 | cut -d= -f2-)"
    fi
    printf '%s' "${val}"
}

LOG_DIR="$(_read_env_var KALSHI_LOG_DIR)"
LOG_DIR="${LOG_DIR:-${HOME}/Library/Logs/kalshi-weather}"
OUT_LOG="${LOG_DIR}/collector.out.log"
ERR_LOG="${LOG_DIR}/collector.err.log"

# Monitor (ops monitor) service: state/history are operational bookkeeping,
# not scientific data, so they live alongside the other operational logs --
# never under KALSHI_DATA_DIR (see docs/runbooks/monitoring_alerting.md).
MONITOR_OUT_LOG="${LOG_DIR}/monitor.out.log"
MONITOR_ERR_LOG="${LOG_DIR}/monitor.err.log"
MONITOR_STATE_PATH="${LOG_DIR}/monitor_state.json"
MONITOR_HISTORY_PATH="${LOG_DIR}/alert_history.jsonl"
# How often launchd fires the monitor, in seconds. Override with
# MONITOR_INTERVAL_SECONDS (environment wins over .env); 900s (15 min) is
# the default the monitoring runbook documents.
MONITOR_INTERVAL_SECONDS="$(_read_env_var MONITOR_INTERVAL_SECONDS)"
MONITOR_INTERVAL_SECONDS="${MONITOR_INTERVAL_SECONDS:-900}"

# Backup (scripts/backup_postgres.sh) service: once-daily via launchd
# StartCalendarInterval, not StartInterval -- see
# docs/runbooks/backup_recovery.md "Schedule" for why a fixed local time
# was chosen over a repeating interval.
BACKUP_OUT_LOG="${LOG_DIR}/backup.out.log"
BACKUP_ERR_LOG="${LOG_DIR}/backup.err.log"
# Default 03:00 local time -- outside typical Kalshi trading-hours activity
# and the two NWS forecast-issuance windows (00-12/12-24 local) each
# station-day gets checked against (docs/runbooks/operations.md "Monitoring
# forecast-collection cadence"), so a backup's brief `docker compose exec`
# load never lands inside either. Override with BACKUP_SCHEDULE_HOUR/
# BACKUP_SCHEDULE_MINUTE.
BACKUP_SCHEDULE_HOUR="$(_read_env_var BACKUP_SCHEDULE_HOUR)"
BACKUP_SCHEDULE_HOUR="${BACKUP_SCHEDULE_HOUR:-3}"
BACKUP_SCHEDULE_MINUTE="$(_read_env_var BACKUP_SCHEDULE_MINUTE)"
BACKUP_SCHEDULE_MINUTE="${BACKUP_SCHEDULE_MINUTE:-0}"

GUI_DOMAIN="gui/$(id -u)"
