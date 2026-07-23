#!/usr/bin/env bash
# Dump the live Postgres database (docker compose service `postgres`) to a
# timestamped pg_dump custom-format archive under the configured local
# backup directory.
#
# Local destination (mirrors cli.py `_backup_dir` exactly):
#   BACKUP_LOCAL_DIR (env wins; else .env) when set -- an internal-disk
#   directory that the launchd execution context can write. macOS TCC
#   denies launchd agents access to removable volumes, which silently
#   failed every scheduled run against the external SSD (2026-07-23
#   production-gap investigation). Otherwise the original
#   <KALSHI_DATA_DIR>/backups/postgres derivation is used. The S3
#   off-machine copy (ops backup finalize) is redundancy on top of -- never
#   a substitute for -- a successfully created local backup.
#
# Usage: scripts/backup_postgres.sh
#   Backups are verified with pg_restore --list before the script reports
#   success. Never deletes or rotates old backups itself -- pruning is a
#   deliberate, separate, explicitly-invoked act (`ops backup prune`; see
#   docs/runbooks/backup_recovery.md "Retention policy").
#
# Concurrency: a non-blocking directory lock (`mkdir` is atomic on POSIX
# filesystems) means an overlapping scheduled run skips cleanly rather than
# running two dumps at once.
#
# Atomicity: pg_dump writes to a `.partial` temp file, never the final
# name -- a crash or kill mid-dump leaves an unambiguous `.partial` behind
# (cleaned up by `ops backup prune`'s stale-partial rule), never a corrupt
# file sitting at the name a future run's overwrite-protection would trust.
#
# Failure visibility: once the backup directory is resolved, an ERR trap
# records a failed-outcome status for ANY unexpected abort (not just the
# explicitly-handled pg_dump/verify failures), so a run that dies early can
# never again be invisible to observatory/backup_health.py.
set -euo pipefail

cd "$(dirname "$0")/.."

# launchd agents get a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) that
# omits the Docker CLI's install locations -- discovered live 2026-07-23
# ("docker: command not found" from the scheduled context; masked until
# then by the TCC failure that killed the script even earlier). Append the
# standard Homebrew/Docker-Desktop locations ONLY when docker is not
# already resolvable, so an environment that deliberately fronts a
# different docker (e.g. tests stubbing it) is never overridden.
if ! command -v docker > /dev/null 2>&1; then
    export PATH="${PATH}:/usr/local/bin:/opt/homebrew/bin"
fi

_read_env_var() {
    local name="$1" val=""
    val="${!name:-}"
    if [[ -z "${val}" && -f .env ]]; then
        # cut -f2- preserves values containing '=' and spaces (no quoting games).
        val="$(grep -E "^${name}=" .env | tail -1 | cut -d= -f2-)"
    fi
    printf '%s' "${val}"
}

# --- Resolve the local backup destination -----------------------------------
backup_local_dir="$(_read_env_var BACKUP_LOCAL_DIR)"
if [[ -n "${backup_local_dir}" ]]; then
    backup_dir="${backup_local_dir}"
else
    KALSHI_DATA_DIR="$(_read_env_var KALSHI_DATA_DIR)"
    if [[ -z "${KALSHI_DATA_DIR}" ]]; then
        echo "ERROR: neither BACKUP_LOCAL_DIR nor KALSHI_DATA_DIR is set (env or .env)" >&2
        exit 1
    fi
    if [[ ! -d "${KALSHI_DATA_DIR}" ]]; then
        echo "ERROR: data root '${KALSHI_DATA_DIR}' does not exist -- is the drive mounted?" >&2
        exit 1
    fi
    backup_dir="${KALSHI_DATA_DIR}/backups/postgres"
fi

# Database identity -- defaults match docker-compose.yml's hardcoded
# POSTGRES_USER/POSTGRES_DB exactly, so leaving these unset changes nothing.
db_user="$(_read_env_var BACKUP_DB_USER)"
db_user="${db_user:-kalshi}"
db_name="$(_read_env_var BACKUP_DB_NAME)"
db_name="${db_name:-kalshi_weather}"
auto_prune="$(_read_env_var BACKUP_AUTO_PRUNE)"
auto_prune="${auto_prune:-false}"

mkdir -p "${backup_dir}"

finalize() {
    # Always tells ops/backup.py what happened -- success or failure -- so
    # the status record (and therefore the observatory) never goes stale
    # just because a run failed. This call's own exit code is advisory
    # only: it is never allowed to flip this script's exit code, per "local
    # backup succeeds independently of remote-copy availability."
    PYTHONPATH="$(pwd)/src" .venv/bin/python -m kalshi_weather.cli ops backup finalize \
        --backup-dir "${backup_dir}" "$@" || true
}

# Any unexpected abort from here on records a failed status (best-effort) --
# a pre-finalize death must be visible to the observatory, never silent.
on_unexpected_error() {
    local line="$1"
    echo "ERROR: backup script aborted unexpectedly at line ${line}" >&2
    finalize --outcome failed --error "backup script aborted unexpectedly at line ${line}"
}
trap 'on_unexpected_error ${LINENO}' ERR

# --- Non-blocking lock: skip cleanly if another run is still in progress ---
lock_dir="${backup_dir}/.backup.lock"
if ! mkdir "${lock_dir}" 2>/dev/null; then
    skip_count_file="${backup_dir}/lock_skip_count"
    prev="$(cat "${skip_count_file}" 2>/dev/null || echo 0)"
    echo $((prev + 1)) > "${skip_count_file}"
    echo "another backup run holds ${lock_dir}; skipping this cycle" >&2
    exit 0
fi
trap 'rmdir "${lock_dir}" 2>/dev/null || true' EXIT
# A run that gets far enough to acquire the lock resets the skip counter --
# observatory/backup_health.py's lock-contention check cares about
# *consecutive* skips, not a lifetime total.
echo 0 > "${backup_dir}/lock_skip_count"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="${backup_dir}/kalshi_weather-${stamp}.dump"
partial="${out}.partial"

if [[ -e "${out}" ]]; then
    echo "ERROR: ${out} already exists; refusing to overwrite" >&2
    finalize --outcome failed --error "target ${out} already exists; refused to overwrite"
    exit 1
fi

start_ts=$(date +%s)
echo "dumping ${db_name} -> ${partial}"
if ! docker compose exec -T postgres pg_dump -U "${db_user}" -d "${db_name}" -Fc > "${partial}"; then
    echo "ERROR: pg_dump failed" >&2
    finalize --outcome failed --error "pg_dump failed"
    exit 1
fi

# Verify the archive is readable and non-trivial before declaring success,
# and before it ever occupies the final filename.
entries="$(docker compose exec -T postgres pg_restore --list < "${partial}" | wc -l | tr -d ' ')"
if [[ "${entries}" -lt 10 ]]; then
    echo "ERROR: archive lists only ${entries} entries -- dump looks broken" >&2
    finalize --outcome failed --error "pg_restore --list found only ${entries} entries (partial file left for forensics)"
    exit 1
fi

mv "${partial}" "${out}"
size_bytes="$(stat -f%z "${out}" 2>/dev/null || stat -c%s "${out}")"
size_human="$(du -h "${out}" | cut -f1)"
duration=$(( $(date +%s) - start_ts ))
echo "OK: ${out} (${size_human}, ${entries} archive entries, pg_restore --list verified, ${duration}s)"

finalize --outcome success \
    --local-path "${out}" --entries "${entries}" \
    --size-bytes "${size_bytes}" --duration-seconds "${duration}"

if [[ "${auto_prune}" == "true" ]]; then
    PYTHONPATH="$(pwd)/src" .venv/bin/python -m kalshi_weather.cli ops backup prune --no-dry-run || true
fi
