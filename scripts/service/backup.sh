#!/usr/bin/env bash
# Scheduled entry point for scripts/backup_postgres.sh (see
# docs/runbooks/backup_recovery.md). Invoked once/day by the
# com.kalshi-weather.backup launchd agent (StartCalendarInterval, not
# cron). backup_postgres.sh itself holds a non-blocking lock, so an
# overlapping invocation skips cleanly rather than running two dumps at
# once.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "${REPO_DIR}"

exec "${REPO_DIR}/scripts/backup_postgres.sh"
