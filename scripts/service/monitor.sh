#!/usr/bin/env bash
# Scheduled entry point for `ops monitor` (see
# docs/runbooks/monitoring_alerting.md). Invoked periodically by the
# com.kalshi-weather.monitor launchd agent (StartInterval, not cron) --
# never run as a long-lived daemon: each invocation runs the observatory
# once, decides whether to alert, and exits. `ops monitor` itself holds a
# non-blocking lock so an overlapping invocation (a slow run outliving the
# interval) skips cleanly rather than running concurrently.
#
# The Kalshi recovery watch (`ops recovery-watch`) rides the SAME cycle --
# no second launchd agent, no second timer. It holds its own non-blocking
# lock (<state-path>.lock, same mechanism as `ops monitor`), deduplicates
# notifications per state transition, and NEVER runs the paper validation.
# Its failure is logged but never masks the monitor's own exit status.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "${REPO_DIR}"

monitor_rc=0
env PYTHONPATH="${REPO_DIR}/src" "${REPO_DIR}/.venv/bin/python" -m kalshi_weather.cli \
    ops monitor \
    --state-path "${MONITOR_STATE_PATH}" \
    --history-path "${MONITOR_HISTORY_PATH}" || monitor_rc=$?

env PYTHONPATH="${REPO_DIR}/src" "${REPO_DIR}/.venv/bin/python" -m kalshi_weather.cli \
    ops recovery-watch \
    --state-path "${RECOVERY_WATCH_STATE_PATH}" \
    --history-path "${RECOVERY_WATCH_HISTORY_PATH}" \
    || echo "recovery-watch failed (non-fatal; monitor status preserved)" >&2

exit "${monitor_rc}"
