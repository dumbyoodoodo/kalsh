#!/usr/bin/env bash
# Collector service status: launchd state, process check, single-instance
# check, recent log lines, and (if the DB is reachable) collector freshness
# via `ops health`.
set -euo pipefail

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
