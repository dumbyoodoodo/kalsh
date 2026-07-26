#!/usr/bin/env bash
# Collector service status: launchd state, process check, single-instance
# check, recent log lines, collector freshness via `ops health`, monitor/backup
# state, and external-heartbeat status. Strictly READ-ONLY by default -- it
# sends no network request. Pass --ping-heartbeat to additionally fire one real
# heartbeat (a network ping) as a manual end-to-end test.
set -euo pipefail

PING_HEARTBEAT=0
for arg in "$@"; do
    case "${arg}" in
        --ping-heartbeat) PING_HEARTBEAT=1 ;;
        -h|--help)
            echo "usage: status.sh [--ping-heartbeat]"
            echo "  --ping-heartbeat  fire one real heartbeat ping (network) as an end-to-end test"
            exit 0
            ;;
        *) echo "unknown argument: ${arg} (try --help)" >&2; exit 2 ;;
    esac
done

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

echo "== launchd =="
if launchctl print "${GUI_DOMAIN}/${SERVICE_LABEL}" > /dev/null 2>&1; then
    launchctl print "${GUI_DOMAIN}/${SERVICE_LABEL}" \
        | grep -E "state = |pid = |last exit code|runs = " | sed 's/^[[:space:]]*/  /'
else
    echo "  NOT LOADED (run scripts/service/install.sh)"
fi

echo "== processes =="
# service instances run via launch.py; a manual `ops run` matches the 2nd pattern
pids="$(pgrep -f 'service/launch.py|kalshi_weather.cli ops run' || true)"
count="$(printf '%s' "${pids}" | grep -c . || true)"
echo "  collector processes running: ${count:-0}${pids:+ (pid ${pids//$'\n'/ })}"
if [[ "${count:-0}" -gt 1 ]]; then
    echo "  WARNING: more than one collector instance!"
fi

echo "== recent log =="
tail -n 3 "${OUT_LOG}" 2>/dev/null | cut -c1-160 | sed 's/^/  /' || echo "  (no log yet)"

echo "== collector freshness (ops health) =="
cd "${REPO_DIR}"
PYTHONPATH=src .venv/bin/python -m kalshi_weather.cli ops health 2>/dev/null \
    || echo "  (database unreachable -- start it: docker compose up -d postgres)"

echo "== monitor (ops monitor / alerting) =="
if launchctl print "${GUI_DOMAIN}/${MONITOR_LABEL}" > /dev/null 2>&1; then
    launchctl print "${GUI_DOMAIN}/${MONITOR_LABEL}" \
        | grep -E "state = |last exit code" | sed 's/^[[:space:]]*/  /'
else
    echo "  NOT LOADED (run scripts/service/install.sh)"
fi
if [[ -f "${MONITOR_STATE_PATH}" ]]; then
    echo "  current alert state: $(tr -s ' \n' ' ' < "${MONITOR_STATE_PATH}")"
else
    echo "  no state file yet (monitor hasn't run) -- ${MONITOR_STATE_PATH}"
fi
echo "  last 3 alert-history entries:"
tail -n 3 "${MONITOR_HISTORY_PATH}" 2>/dev/null | sed 's/^/    /' || echo "    (no history yet)"

echo "== heartbeat (external uptime dead-man's-switch) =="
# Config presence only -- NEVER print HEARTBEAT_URL or its token.
if [[ -n "$(_read_env_var HEARTBEAT_URL)" ]]; then
    hb_url_flag="--url-configured"
    echo "  HEARTBEAT_URL: configured (value hidden)"
else
    hb_url_flag="--no-url-configured"
    echo "  HEARTBEAT_URL: not configured"
fi
# Launchd facts. For a StartInterval agent, 'loaded' (scheduled) is the
# meaningful liveness; 'running' is only true during its brief periodic run.
hb_installed=no; hb_loaded=no; hb_running=no; hb_agent_flag="--no-agent-running"
[[ -f "${HEARTBEAT_PLIST}" ]] && hb_installed=yes
if launchctl print "${GUI_DOMAIN}/${HEARTBEAT_LABEL}" > /dev/null 2>&1; then
    hb_loaded=yes
    hb_agent_flag="--agent-running"
    if launchctl print "${GUI_DOMAIN}/${HEARTBEAT_LABEL}" 2>/dev/null | grep -qE "state = running"; then
        hb_running=yes
    fi
fi
echo "  agent: installed=${hb_installed} loaded=${hb_loaded} running-now=${hb_running}"
if [[ "${hb_installed}" == "no" ]]; then
    echo "  (agent not installed -- run scripts/service/install.sh)"
fi
if [[ "${PING_HEARTBEAT}" == "1" ]]; then
    echo "  --ping-heartbeat: firing one real heartbeat (network) ..."
    cd "${REPO_DIR}"
    PYTHONPATH=src .venv/bin/python -m kalshi_weather.cli \
        ops heartbeat --state-path "${HEARTBEAT_STATE_PATH}" 2>/dev/null \
        | sed 's/^/    /' || echo "    (ops heartbeat failed)"
fi
cd "${REPO_DIR}"
PYTHONPATH=src .venv/bin/python -m kalshi_weather.cli ops heartbeat-status \
    --state-path "${HEARTBEAT_STATE_PATH}" \
    --period-seconds "${HEARTBEAT_INTERVAL_SECONDS}" \
    --grace-seconds "${HEARTBEAT_INTERVAL_SECONDS}" \
    "${hb_url_flag}" "${hb_agent_flag}" 2>/dev/null \
    || echo "  (ops heartbeat-status failed)"

echo "== backup (scripts/backup_postgres.sh) =="
if launchctl print "${GUI_DOMAIN}/${BACKUP_LABEL}" > /dev/null 2>&1; then
    launchctl print "${GUI_DOMAIN}/${BACKUP_LABEL}" \
        | grep -E "state = |last exit code" | sed 's/^[[:space:]]*/  /'
else
    echo "  NOT LOADED (run scripts/service/install.sh)"
fi
cd "${REPO_DIR}"
PYTHONPATH=src .venv/bin/python -m kalshi_weather.cli ops backup status 2>/dev/null \
    | sed 's/^/  /' \
    || echo "  (ops backup status failed -- check .env / KALSHI_DATA_DIR)"
