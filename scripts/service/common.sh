# Shared definitions for the collector launchd service scripts.
# Sourced, not executed.

SERVICE_LABEL="com.kalshi-weather.collector"
ROTATE_LABEL="com.kalshi-weather.logrotate"

# Repo root = two levels up from this file.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

AGENTS_DIR="${HOME}/Library/LaunchAgents"
SERVICE_PLIST="${AGENTS_DIR}/${SERVICE_LABEL}.plist"
ROTATE_PLIST="${AGENTS_DIR}/${ROTATE_LABEL}.plist"

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

GUI_DOMAIN="gui/$(id -u)"
