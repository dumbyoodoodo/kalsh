#!/usr/bin/env bash
# Scheduled entry point for `ops heartbeat` (see
# docs/runbooks/monitoring_alerting.md). Invoked periodically by the
# com.kalshi-weather.heartbeat launchd agent (StartInterval) -- each run pings
# an EXTERNAL dead-man's-switch URL iff the collector is healthy, then exits.
# When the machine is powered off nothing runs, so the pings stop and the
# external monitor raises the outage alert (2026-07-26 power-loss incident).
# Always exits 0: the signal is the ping's presence/absence, not this exit
# code, and a non-zero exit would only add launchd noise.
set -uo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "${REPO_DIR}"

env PYTHONPATH="${REPO_DIR}/src" "${REPO_DIR}/.venv/bin/python" -m kalshi_weather.cli \
    ops heartbeat || true
