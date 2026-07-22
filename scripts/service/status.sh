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
