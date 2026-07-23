#!/usr/bin/env bash
# Scheduled entry point for `ops monitor` (see
# docs/runbooks/monitoring_alerting.md). Invoked periodically by the
# com.kalshi-weather.monitor launchd agent (StartInterval, not cron) --
# never run as a long-lived daemon: each invocation runs the observatory
# once, decides whether to alert, and exits. `ops monitor` itself holds a
# non-blocking lock so an overlapping invocation (a slow run outliving the
# interval) skips cleanly rather than running concurrently.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "${REPO_DIR}"

exec env PYTHONPATH="${REPO_DIR}/src" "${REPO_DIR}/.venv/bin/python" -m kalshi_weather.cli \
    ops monitor \
    --state-path "${MONITOR_STATE_PATH}" \
    --history-path "${MONITOR_HISTORY_PATH}"
