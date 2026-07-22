#!/usr/bin/env bash
# Restart the collector service (kill current instance, launchd starts a
# fresh one immediately). Use after upgrading code or changing .env.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

launchctl kickstart -k "${GUI_DOMAIN}/${SERVICE_LABEL}"
echo "restart signalled; watch scripts/service/logs.sh"
