# Runbook: PostgreSQL backup & recovery

Closes the 2026-07-22 Research Program Audit's one remaining CRITICAL
operational finding: backups were manual, only one had ever been taken,
no schedule existed, no off-machine copy existed, and collected historical
data -- irreplaceable per CLAUDE.md -- risked total loss on a single disk
failure.

**Status at the time this runbook was written: PARTIALLY CLOSED.** Local,
scheduled, verified backups plus a proven restore drill are real and
live. A genuine off-machine copy is implemented and tested but **not yet
configured** on this machine -- see "Off-machine copy" below. Do not treat
this system as full disaster-recovery redundancy until a real off-machine
destination is set.

**Update 2026-07-23: CLOSED.** `BACKUP_REMOTE_TYPE=s3` is now configured
against `s3://<your-backup-bucket>/postgres/` (`BACKUP_S3_PREFIX=postgres`;
the actual bucket name lives only in the gitignored `.env`, never in this
repository).
The newest verified local backup was uploaded, independently round-trip
verified (SHA-256 of the local file matched a separately re-downloaded
copy), and a full restore drill (`scripts/backup_restore_drill.py`) was run
against that S3-downloaded copy specifically -- not the original local
file -- restoring cleanly into a disposable container with `alembic_version`
identifiable, all representative tables present with plausible row counts,
and a sampled timestamp/numeric column round-tripping correctly. The
observatory's `backup_remote_freshness`/`backup_remote_copy` checks (ground
truth against the bucket's own newest object) report healthy. `BACKUP_AUTO_PRUNE`
remains `false` -- unchanged, deliberate. See the S3 handoff verification
record for the exact object path and hashes.

## Architecture

- `scripts/backup_postgres.sh` (existing, minimally modified) -- the
  proven `pg_dump`/`pg_restore --list` pipeline, unchanged in its core
  behavior (custom-format dump, database identity, timestamp naming,
  overwrite protection, verification). Two things were added: an atomic
  temp-file write (dump to `.dump.partial`, verify, then rename -- a
  crash mid-dump can no longer leave a corrupt file at the name a future
  run's overwrite-protection would trust) and a non-blocking `mkdir`-based
  lock (overlapping scheduled runs skip cleanly instead of running two
  dumps at once).
- `kalshi_weather.ops.backup` (new) -- status recording
  (`last_backup_status.json`, atomic-rename write) and off-machine copy
  dispatch with independent round-trip integrity verification. Called by
  the bash script via `ops backup finalize` after its own dump+verify work
  -- never before.
- `kalshi_weather.ops.backup_retention` (new) -- the grandfather-father-son
  retention policy: pure, deterministic, dry-run-capable, fully tested.
  See "Retention policy" below for why it is **not** wired into the
  automatic schedule by default.
- `kalshi_weather.observatory.backup_health` (new) -- folds backup status
  into the same `Finding`/`Severity` vocabulary every other observatory
  check uses, so `ops monitor`'s existing exactly-once alert-transition
  state machine covers backups for free -- no new alerting code.
- `scripts/service/backup.sh` + the `com.kalshi-weather.backup` launchd
  agent -- runs the backup once/day.
- `scripts/backup_restore_drill.py` -- proves a backup is actually
  restorable (not just `pg_restore --list`-readable) against a disposable,
  isolated container.

## Installation

```bash
scripts/service/install.sh
```

Same installer that manages the collector/log-rotation/monitor agents.
**Per-agent conditional reload**: each agent's freshly-generated plist is
compared against what's already installed; only an agent whose plist
actually changed (or that isn't loaded yet) is bootout+bootstrapped. Adding
the backup agent for the first time therefore did **not** restart the
already-running collector -- verified live (collector PID unchanged across
the install that added this agent). Before this per-agent comparison
existed, every `install.sh` run reloaded every agent unconditionally,
which is what caused the collector's brief restart the first time the
*monitor* agent was added in the prior task -- this fixes that class of
problem going forward for every agent, not just backup.

`scripts/service/uninstall.sh` removes all four agents (collector,
log-rotation, monitor, backup) and leaves logs, alert history, and
**every backup file** untouched.

## Configuration

All in `.env` (see `.env.example`'s "PostgreSQL backup & recovery"
section):

| Variable | Default | Meaning |
|---|---|---|
| `BACKUP_DB_USER` / `BACKUP_DB_NAME` | `kalshi` / `kalshi_weather` | Match `docker-compose.yml`'s hardcoded values exactly -- unset changes nothing. |
| `BACKUP_SCHEDULE_HOUR` / `BACKUP_SCHEDULE_MINUTE` | `3` / `0` | Once-daily local time launchd runs the backup. Read at **install time** -- changing requires `scripts/service/install.sh`, not `restart.sh`. |
| `BACKUP_REMOTE_TYPE` | `none` | `filesystem` \| `s3` \| `none`. |
| `BACKUP_REMOTE_PATH` | unset | A filesystem destination -- **must be a different physical disk** than `KALSHI_DATA_DIR`. |
| `BACKUP_S3_BUCKET` / `BACKUP_S3_PREFIX` | unset / `postgres` | S3 destination; credentials come from the `aws` CLI's own chain, never `.env`. |
| `BACKUP_STALE_AFTER_HOURS` / `BACKUP_REMOTE_STALE_AFTER_HOURS` | `30` / `48` | Freshness thresholds for the observatory. |
| `BACKUP_DISK_WARNING_FREE_GB` / `BACKUP_DISK_CRITICAL_FREE_GB` | `20` / `5` | Free-space thresholds on the local backup filesystem. |
| `BACKUP_AUTO_PRUNE` | `false` | See "Retention policy" -- opt-in only. |
| `BACKUP_RETENTION_DAILY_DAYS` / `_WEEKLY_WEEKS` / `_MONTHLY_MONTHS` | `14` / `8` / `12` | Retention tiers. |

## Schedule

Once/day via launchd `StartCalendarInterval` (a fixed local time, not a
repeating interval -- unlike the monitor's `StartInterval`) at 03:00 local
by default: outside typical Kalshi trading-hours activity and both NWS
forecast-issuance windows (00-12/12-24 local per station), so the backup's
brief `docker compose exec` load never coincides with either.

## Local storage

Unchanged from the original script: `<KALSHI_DATA_DIR>/backups/postgres/`,
custom-format (`pg_dump -Fc`), named `kalshi_weather-<UTC timestamp>.dump`.
Never overwritten (refuses if the target name already exists). A dump
in progress is named `<name>.dump.partial` and is renamed to the final
name only after `pg_restore --list` verifies it -- so a crash or kill
mid-dump leaves an unambiguous `.partial`, never a corrupt file at a name
future runs would trust.

## Off-machine copy

**Configured on this machine as of 2026-07-23**: `BACKUP_REMOTE_TYPE=s3`,
`BACKUP_S3_BUCKET=<your-backup-bucket>`, `BACKUP_S3_PREFIX=postgres` (the
real bucket name is set only in the gitignored `.env`). Two transports are
implemented and tested; `s3` is the one currently active:

- **`filesystem`**: set `BACKUP_REMOTE_TYPE=filesystem` and
  `BACKUP_REMOTE_PATH=/Volumes/SomeOtherDrive/kalshi-weather-backups` --
  a mounted volume that is **not** the same physical disk as
  `KALSHI_DATA_DIR`. After copying, the destination file is read back and
  its SHA-256 compared against the source -- a genuine round-trip
  integrity check, not just "the copy command reported success."
- **`s3`**: install the `aws` CLI (not installed on this machine as of
  writing) and configure its own credentials (`aws configure`, or an
  environment/instance-role chain) -- entirely outside this project's
  `.env`, so no AWS secret is ever stored here. Set
  `BACKUP_REMOTE_TYPE=s3`, `BACKUP_S3_BUCKET=...`, `BACKUP_S3_PREFIX=...`.
  After upload, the object is streamed back down and hashed (not trusted
  via S3's ETag, which is not simply an MD5 for multipart uploads) and
  compared against the source.

Local backup success is **never** contingent on the remote copy: a remote
failure is recorded as a degraded outcome in the status record, never
raised as an error that could block or corrupt the local backup.

**Never point `BACKUP_REMOTE_PATH` at the same drive as `KALSHI_DATA_DIR`.**
A second folder on the SSD already holding the primary data is not
redundancy -- it shares every failure mode (drive failure, theft, fire)
the off-machine copy exists to protect against.

## Retention policy

**A deliberate policy decision, not an oversight**: `scripts/backup_postgres.sh`
has documented, since it was first written, that "pruning is a deliberate
manual act." That is a prior, explicit decision this project made. This
system makes retention **fully implementable** (`kalshi_weather.ops.backup_retention`,
grandfather-father-son: every backup within 14 days kept, one
representative per week for the next 8 weeks, one per month for the next
12 months, older pruned) -- tested, deterministic, dry-run-capable,
logs every keep/delete decision -- but does **not** wire it into the
automatic schedule unless `BACKUP_AUTO_PRUNE=true` is explicitly set. The
single newest backup is never deleted, regardless of age or configuration.
Pruning only ever runs after a new backup and its verification succeed
(never before or independently of one). An empty or unrecognized target
directory aborts with no deletions -- "wrong directory" is treated as more
likely than "no backups yet."

```bash
uv run kalshi-weather ops backup prune                    # dry run (default) -- logs what WOULD happen
uv run kalshi-weather ops backup prune --no-dry-run        # actually deletes
uv run kalshi-weather ops backup prune --target remote     # same policy against BACKUP_REMOTE_PATH (filesystem only)
```

Abandoned `.partial` files (a crashed dump) older than 2 hours are cleaned
up by the same command -- a normal dump completes in seconds to low
minutes at this database's current size, so anything older is not "still
in progress."

## Monitoring semantics

Backup health is one more domain (`"backup"`) in the existing Data Quality
Observatory (`docs/runbooks/data_quality_observatory.md`) -- `ops observatory`
and the scheduled `ops monitor` both cover it automatically, with zero new
alerting code:

| Check | Severity policy |
|---|---|
| `backup_command` | CRITICAL if the most recent attempt failed (dump or verification); INFO otherwise. |
| `backup_local_freshness` | WARNING beyond `BACKUP_STALE_AFTER_HOURS`, CRITICAL beyond 2x that. |
| `backup_remote_copy` | WARNING on `failed`/`unavailable`; WARNING if configured but the last run reports `not_configured` (a drift signal); INFO otherwise. |
| `backup_remote_freshness` | Only evaluated once a remote destination is configured; same WARNING/CRITICAL escalation as local freshness, checked against the destination's own newest file (ground truth, not inferred from the last status record). |
| `backup_disk_space` | WARNING/CRITICAL on free space below the configured thresholds. |
| `backup_lock_contention` | WARNING at 2+ consecutive skipped runs, CRITICAL at 5+ -- a stuck lock, not a single slow prior run. |

These ride `ops monitor`'s existing exactly-once state machine: a fresh
backup failure alerts once, repeats while unchanged are suppressed, and
recovery to healthy sends one recovery notification -- exactly like every
other observatory domain (`docs/runbooks/monitoring_alerting.md`).

## Telegram alerts

No new alert configuration -- backup findings use the exact same
`ALERT_TRANSPORT`/`ALERT_TELEGRAM_BOT_TOKEN`/`ALERT_TELEGRAM_CHAT_ID`
already documented in `docs/runbooks/monitoring_alerting.md`. If that's
already configured for collector/data-quality alerts, backup alerts are
already live too.

## Manual backup command

```bash
scripts/backup_postgres.sh
```

Safe to run any time, including while the scheduled agent is also
installed (the lock means only one actually runs).

## Backup verification

Every backup is verified two ways, at two different levels of rigor:

1. **At creation** (`scripts/backup_postgres.sh`): `pg_restore --list`
   confirms the archive is readable and has a plausible entry count
   (&gt;= 10) before the `.partial` file is ever renamed to its final
   name.
2. **After any off-machine copy**: an independent SHA-256 round-trip
   check (read the destination back, hash it, compare) -- not merely
   "the copy/upload command exited 0."

Neither of these proves the backup actually **restores** -- that is what
the restore drill is for.

## Complete restore procedure

```bash
uv run python scripts/backup_restore_drill.py
```

This is the **real** disaster-recovery proof, not a stand-in for it:

1. Starts a disposable, isolated `postgres:16-alpine` container -- no
   named volume, no published port, no relation to the running
   `kalsh-postgres-1` production container.
2. Copies the newest local `.dump` file in and runs `pg_restore` against
   it.
3. Validates: `alembic_version` is present and identifiable, the schema
   restored (`information_schema.tables` nonzero), a representative set
   of tables across every collection stream plus the platform-level
   `raw_api_payloads` audit trail have plausible (nonzero) row counts, and
   a sampled timestamp/numeric column round-trips correctly.
4. **Always** removes the disposable container in a `finally` block,
   success or failure. Never touches, queries, or modifies the production
   database.
5. Writes a machine-readable report to
   `<backup_dir>/restore_drill_report.json`.

Restoring into the *real* production database (an actual disaster
recovery, not a drill) is a separate, manual, high-stakes act: stop the
collector (`scripts/service/uninstall.sh` or `launchctl bootout`), then
`docker compose exec -T postgres pg_restore -U kalshi -d kalshi_weather --clean --if-exists < <backup file>`,
then re-verify with `ops health`/`ops backup status` before restarting
collection. This is intentionally **not** automated -- restoring over a
live, possibly-still-partially-intact database is exactly the kind of
action CLAUDE.md requires a human to explicitly decide on, not a script.

## Disaster-recovery procedure

1. Confirm the production database is actually the thing that's broken
   (not, e.g., a collector bug or a network issue) -- `ops health`,
   `docker compose logs postgres`.
2. Identify the newest verified backup: `ops backup status`, or read
   `<backup_dir>/last_backup_status.json` directly.
3. If local backups are also lost (the disk itself failed), the
   off-machine copy is the only remaining source -- this is exactly why
   Phase 3 ("Off-machine copy") matters and why this system is currently
   only PARTIALLY CLOSED without one configured.
4. Stop the collector service (`scripts/service/uninstall.sh`).
5. Restore per "Complete restore procedure" above, into the *real*
   database this time (after standing up a fresh, empty `postgres`
   service if the disk/volume itself is gone).
6. Re-run `scripts/backup_restore_drill.py`'s validation queries by hand
   against the restored production database (schema revision, table
   counts, a sampled row) before resuming collection.
7. Reinstall and restart the collector (`scripts/service/install.sh`).
8. Document what happened and why in `docs/runbooks/operations.md`'s
   "Recovering from failures" table if it's a new failure mode.

## Credential rotation

- **Database**: `docker-compose.yml`'s `POSTGRES_PASSWORD` -- rotating it
  requires updating `docker-compose.yml` and `DATABASE_URL` together, then
  `docker compose up -d postgres` to apply. `scripts/backup_postgres.sh`
  itself never handles a password (it runs `pg_dump` *inside* the
  container via `docker compose exec`, authenticating over the local
  socket, not over the network).
- **S3**: rotate via the `aws` CLI's own credential source (e.g. IAM
  console, `aws configure`) -- nothing in this project stores or reads an
  AWS secret, so there is nothing here to rotate.
- **Telegram**: see `docs/runbooks/monitoring_alerting.md`'s credential
  guidance -- shared with backup alerts.

## Disk-full recovery

- **Local backup destination full**: `ops backup status`/`ops observatory`
  surface `backup_disk_space` before this happens (WARNING at 20GB free,
  CRITICAL at 5GB, both configurable). If it does happen anyway: `pg_dump`
  fails partway, the `.partial` file is left (not a corrupt "successful"
  backup), and `backup_command` goes CRITICAL. Free space (see "Retention
  policy" for pruning old backups) and re-run
  `scripts/backup_postgres.sh` manually; the next scheduled run will also
  succeed once space exists.
- **Root/internal disk full** (logs, not backups): see
  `docs/runbooks/collector_service.md`'s log rotation -- unrelated to this
  system, but the same underlying disk-space observatory check
  (`ops health`'s `database size` line) is the first place to look.

## How to disable the service safely

Disabling only the backup agent, leaving the collector/monitor untouched:

```bash
launchctl bootout gui/$(id -u)/com.kalshi-weather.backup
rm ~/Library/LaunchAgents/com.kalshi-weather.backup.plist
```

This stops future scheduled runs; it does **not** delete any existing
backup, the status file, or the retention capability -- `ops backup prune`
and `scripts/backup_postgres.sh` (run manually) both continue to work.
`scripts/service/uninstall.sh` removes all four agents at once if a full
teardown is intended instead.

## How to confirm the last successful local and remote backup

```bash
uv run kalshi-weather ops backup status          # human-readable
uv run kalshi-weather ops backup status --json   # machine-readable
```

Shows the last recorded outcome (local and remote, each with a
timestamp/detail) plus the current local backup inventory (count and
newest file). `scripts/service/status.sh` includes the same information
alongside collector/monitor status in one combined view.

## Testing

- `tests/unit/test_ops_backup_retention.py` -- the retention policy (every
  tier, never-delete-newest, empty-directory abort, dry-run vs. real
  deletion, stale-`.partial` cleanup, determinism).
- `tests/unit/test_ops_backup.py` -- status persistence (atomic write/read
  roundtrip), filesystem and S3 remote copy (success, destination
  unavailable, checksum mismatch -- all via `httpx`-free mocked
  `subprocess`, no real network calls), dispatch, `finalize_backup`
  orchestration, and an explicit assertion that neither `RemoteConfig` nor
  `BackupStatus` has any field that could hold a credential.
- `tests/unit/test_observatory_backup_health.py` -- every check's severity
  policy, the loaders, and a regression test (a real `.dump` file on disk,
  not an empty directory) for the exact naive/aware datetime bug this task
  caught live (see "Errors found during this task" below).
- `tests/unit/test_backup_script_lock.py` -- the `mkdir`-lock concurrency
  primitive the bash script relies on, plus its skip-counter logic,
  reproduced and proven directly (the full script needs a live
  docker-compose Postgres, so isn't itself pytest-driven).
- `tests/unit/test_ops_monitor_scenarios.py::test_scenario_backup_failure`
  -- a failed backup rides the same exactly-once alert-transition state
  machine as every other observatory domain.

```bash
uv run pytest tests/unit/test_ops_backup_retention.py tests/unit/test_ops_backup.py \
  tests/unit/test_observatory_backup_health.py tests/unit/test_backup_script_lock.py -v
```

## Errors found during this task

A live smoke test (running `ops observatory` for real, against the real
backup directory) surfaced a genuine bug before it shipped: `parse_backup_filename`
originally attached `tzinfo=UTC` to the timestamp it parsed from a backup's
filename, while every `now` value flowing through the observatory
(`report.py`, `ops monitor`) is naive-UTC -- the exact same class of
`TypeError: can't subtract offset-naive and offset-aware datetimes` bug
fixed earlier in `observatory/continuity.py` for the same reason. Fixed by
returning naive UTC from `parse_backup_filename` (matching
`domain.time.to_naive_utc`'s established convention throughout this
codebase) and adding a regression test that exercises a real on-disk
`.dump` file through the full `build_backup_findings` orchestration --
every prior test in this area used an empty directory, which is exactly
why the bug wasn't caught until live validation.
