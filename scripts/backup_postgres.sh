#!/usr/bin/env bash
# Dump the live Postgres database (docker compose service `postgres`) to a
# timestamped pg_dump custom-format archive under the configured data root.
#
# The live database stays on the internal disk (a live Postgres data dir needs
# a POSIX filesystem -- see docker-compose.yml); this backup gives the
# irreplaceable collected history an off-machine copy on the external drive.
# A dump archive is a plain file, so ExFAT is fine here.
#
# Usage: scripts/backup_postgres.sh
#   KALSHI_DATA_DIR from the environment wins; otherwise read from .env.
#   Backups land in <KALSHI_DATA_DIR>/backups/postgres/ and are verified with
#   pg_restore --list before the script reports success. Never deletes or
#   rotates old backups -- pruning is a deliberate manual act.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -z "${KALSHI_DATA_DIR:-}" && -f .env ]]; then
    # cut -f2- preserves values containing '=' and spaces (no quoting games).
    KALSHI_DATA_DIR="$(grep -E '^KALSHI_DATA_DIR=' .env | tail -1 | cut -d= -f2-)"
fi
if [[ -z "${KALSHI_DATA_DIR:-}" ]]; then
    echo "ERROR: KALSHI_DATA_DIR not set (env or .env)" >&2
    exit 1
fi
if [[ ! -d "${KALSHI_DATA_DIR}" ]]; then
    echo "ERROR: data root '${KALSHI_DATA_DIR}' does not exist -- is the drive mounted?" >&2
    exit 1
fi

backup_dir="${KALSHI_DATA_DIR}/backups/postgres"
mkdir -p "${backup_dir}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="${backup_dir}/kalshi_weather-${stamp}.dump"

if [[ -e "${out}" ]]; then
    echo "ERROR: ${out} already exists; refusing to overwrite" >&2
    exit 1
fi

echo "dumping kalshi_weather -> ${out}"
docker compose exec -T postgres pg_dump -U kalshi -d kalshi_weather -Fc > "${out}"

# Verify the archive is readable and non-trivial before declaring success.
entries="$(docker compose exec -T postgres pg_restore --list < "${out}" | wc -l | tr -d ' ')"
size="$(du -h "${out}" | cut -f1)"
if [[ "${entries}" -lt 10 ]]; then
    echo "ERROR: archive lists only ${entries} entries -- dump looks broken" >&2
    exit 1
fi
echo "OK: ${out} (${size}, ${entries} archive entries, pg_restore --list verified)"
